"""A sharer's coin-set read for an arcade that borrows the chain (arcade/coinscan.py, 2026-10-28): coins paid below a
borrower's floor, which it had no way at all to find (101 DGB below DigiByte's floor; every arcade but the sharer
borrows DigiByte)."""
import asyncio
import contextlib
import json
import pathlib
import sys
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_coreshare import _linked                               # noqa: E402
from test_mesh import fast, mk, run, started, until               # noqa: F401,E402 -- fast is the shrunk-timing fixture
from test_web import app_state, client                            # noqa: F401,E402

from arcade import coinscan, utxos                                # noqa: E402
from arcade.config import NETWORKS                                # noqa: E402
from arcade.ledger import LedgerIndex                             # noqa: E402
from arcade.mesh.coreshare import CoreShare                       # noqa: E402
from arcade.rpc import RpcError                                   # noqa: E402
from arcade.script import b58check_decode, b58check_encode        # noqa: E402
from arcade.txbuild import p2pkh_script                           # noqa: E402

DGB = NETWORKS["dgb"]
A = b58check_encode(DGB.pubkeyhash_version, bytes(range(20)))
B = b58check_encode(DGB.pubkeyhash_version, bytes(range(1, 21)))
FLOOR = DGB.activation_height


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _Core:
    """The sharer's own Core: its coin set, as `scantxoutset` reads it."""

    def __init__(self, coins, scans=True):
        self.coins, self.scans, self.asked = coins, scans, []
        self._timeout = 120.0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call(self, method, *args):
        if method != "scantxoutset" or not self.scans:
            raise RpcError(-32601, "Method not found", method)
        assert self._timeout >= coinscan.SCAN_SECONDS
        self.asked.append(sorted(args[1]))
        wanted = set(args[1])
        return {"success": True, "height": FLOOR + 900,
                "unspents": [{"txid": t, "vout": v, "scriptPubKey": p2pkh_script(a).hex(), "amount": x, "height": h}
                             for a, t, v, x, h in self.coins if f"addr({a})" in wanted]}


def _lender(core, clock):
    return coinscan.Lender(DGB, lambda: core, clock=clock, start=lambda job: job())


def test_the_sharer_answers_at_once_and_reads_its_coin_set_at_most_once_every_ten_minutes():
    clock = _Clock()
    core = _Core([(A, "aa" * 32, 0, 100.0, FLOOR - 1397), (B, "bb" * 32, 1, 1.0, FLOOR - 1441)])
    lender = _lender(core, clock)
    first = lender.ask({"addresses": [A, A, "not an address"]})
    assert core.asked == [[f"addr({A})"]], "one scan, for the address asked; nothing that is not one"
    assert first["waiting"] == [A] and not first["coins"], "answered before the scan ran"
    got = lender.ask({"addresses": [A]})
    assert got["coins"] == {A: [["aa" * 32, 0, 100 * 100_000_000, FLOOR - 1397]]} and got["waiting"] == []
    assert got["height"] == {A: FLOOR + 900}
    more = lender.ask({"addresses": [B]})
    assert more["waiting"] == [B] and len(core.asked) == 1, "a second scan only after SCAN_GAP"
    assert 0 < more["next"] <= coinscan.SCAN_GAP
    clock.now += coinscan.SCAN_GAP
    lender.ask({"addresses": [B]})
    assert core.asked[-1] == [f"addr({B})"], "only what waits: A's answer is kept"
    clock.now += coinscan.KEEP_SECONDS + 1
    assert lender.ask({"addresses": [A]})["waiting"] == [A], "an answer is forgotten after an hour"


def test_the_queue_and_each_ask_are_bounded():
    clock = _Clock()
    core = _Core([])
    lender = _lender(core, clock)
    many = [b58check_encode(DGB.pubkeyhash_version, i.to_bytes(20, "big")) for i in range(coinscan.QUEUE_MAX + 50)]
    lender.running = True                                         # a scan under way: nothing leaves the queue
    for i in range(0, len(many), coinscan.SCAN_ADDRESSES):
        said = lender.ask({"addresses": many[i:i + coinscan.SCAN_ADDRESSES]})
    assert len(lender.waiting) == coinscan.QUEUE_MAX, "the rest are told to wait, not queued"
    assert len(said["waiting"]) == 50
    lender2 = _lender(core, clock)
    lender2.ask({"addresses": many[:300]})
    assert len(core.asked[-1]) == coinscan.SCAN_ADDRESSES, "an ask is cut at SCAN_ADDRESSES"


def test_a_core_with_no_coin_set_says_so_to_every_later_ask():
    lender = _lender(_Core([], scans=False), _Clock())
    lender.ask({"addresses": [A]})
    with pytest.raises(coinscan.NotLent):
        lender.ask({"addresses": [A]})


def test_a_coin_set_too_slow_to_read_is_not_lent_again():
    """2026-10-28: Bellscoin's coin set took longer than SCAN_SECONDS to read on a hard disk; asked again every gap,
    the Core would read it without end."""
    from arcade.rpc import RpcTransportError

    class Slow(_Core):
        def call(self, method, *args):
            self.asked.append(args)
            raise RpcTransportError("127.0.0.1:19918 did not respond in 1200s -- the node may still be starting")
    clock, core = _Clock(), Slow([])
    lender = _lender(core, clock)
    lender.ask({"addresses": [A]})
    clock.now += coinscan.SCAN_GAP
    with pytest.raises(coinscan.NotLent):
        lender.ask({"addresses": [A]})
    assert len(core.asked) == 1


class _Borrowed:
    """A borrowed chain's client: the sharer's coin-set answer, its blocks and its unspent set."""

    def __init__(self, answer, blocks, spent=(), refuse=None):
        self.answer, self.blocks, self.spent, self.refuse = answer, blocks, set(spent), refuse
        self.batches = []

    def batch(self, calls):
        self.batches.append([m for m, _ in calls])
        out = []
        for method, params in calls:
            if method == coinscan.SCAN_METHOD:
                if self.refuse:
                    raise RpcError(-32601, self.refuse, method)
                wanted = set(params[0]["addresses"])
                out.append({"coins": {a: c for a, c in self.answer.items() if a in wanted},
                            "waiting": [a for a in wanted if a not in self.answer], "next": 0})
            elif method == "gettxout":
                assert params[2] is False, "the mempool left out"
                out.append(None if (params[0], params[1]) in self.spent else {"value": 1})
        return out

    def get_block_hash(self, height):
        return f"{height:064x}"

    def get_block(self, block_hash, verbosity):
        return self.blocks[int(block_hash, 16)]


def _block(*outs):
    """{"tx": [...]} with each (txid, [(address, coins), ...])."""
    return {"tx": [{"txid": t, "vout": [{"value": x, "scriptPubKey": {"hex": p2pkh_script(a).hex()}} for a, x in o]}
                   for t, o in outs]}


class _State:
    def __init__(self):
        self.values = {}

    def setting(self, name, default=None):
        return self.values.get(name, default)

    def set_setting(self, name, value):
        self.values[name] = value


def _chain():
    return type("Chain", (), {"network": "dgb", "params": DGB})()


def test_the_borrower_files_only_coins_its_blocks_bear_out_and_still_unspent(tmp_path):
    state, index = _State(), LedgerIndex(tmp_path / "dgb-ledger.sqlite", DGB, rpc_factory=lambda: None)
    with contextlib.closing(index.open()) as db:
        utxos.install(db)
    coinscan.owe(state, "dgb", [A, B])
    h1, h2 = FLOOR - 1441, FLOOR - 1397
    rpc = _Borrowed(
        {A: [["aa" * 32, 0, 1 * 10**8, h1],                       # the case
             ["bb" * 32, 0, 100 * 10**8, h2],                     # the case
             ["cc" * 32, 0, 5 * 10**8, h2],                       # made up: not in its block
             ["dd" * 32, 1, 7 * 10**8, h2],                       # in its block, paid to somebody else
             ["ee" * 32, 0, 3 * 10**8, h2],                       # spent since
             ["ff" * 32, 0, 9 * 10**8, FLOOR + 5000]]},           # above the index: it files it itself
        {h1: _block(("aa" * 32, [(A, 1.0)])),
         h2: _block(("bb" * 32, [(A, 100.0)]), ("dd" * 32, [(A, 1.0), (B, 7.0)]), ("ee" * 32, [(A, 3.0)]))},
        spent={("ee" * 32, 0)})
    assert coinscan.collect(state, _chain(), index, rpc, upto=FLOOR + 100, asked_at={}) == 2
    with contextlib.closing(index.open()) as db:
        assert utxos.balance(db, A) == 101 * 10**8
    assert json.loads(state.values[coinscan.owed_key("dgb")]) == [B], "A answered; B still waits"


def test_an_older_sharer_is_asked_again_later_not_every_pass(tmp_path):
    state, index = _State(), LedgerIndex(tmp_path / "dgb-ledger.sqlite", DGB, rpc_factory=lambda: None)
    coinscan.owe(state, "dgb", [A])
    clock, asked_at = _Clock(), {}
    rpc = _Borrowed({}, {}, refuse="a shared Core does not answer arcade_coinscan")
    assert coinscan.collect(state, _chain(), index, rpc, upto=FLOOR, clock=clock, asked_at=asked_at) == 0
    assert coinscan.collect(state, _chain(), index, rpc, upto=FLOOR, clock=clock, asked_at=asked_at) == 0
    assert len(rpc.batches) == 1
    assert asked_at["dgb"] == clock.now + coinscan.NONE_AGAIN
    rpc.refuse = "arcade_coinscan asked too often; once every 60 s"
    clock.now = asked_at["dgb"]
    coinscan.collect(state, _chain(), index, rpc, upto=FLOOR, clock=clock, asked_at=asked_at)
    assert asked_at["dgb"] == clock.now + coinscan.ASK_AGAIN, "too often is not 'never'"
    assert json.loads(state.values[coinscan.owed_key("dgb")]) == [A], "still owed"


def test_a_sharer_answers_one_coin_scan_ask_a_minute_per_borrower():
    async def go():
        a, b = mk(), mk()
        tips = {"dgb": {"h": FLOOR, "b": "00" * 32}}
        sa = CoreShare(lambda net, calls: [{"r": {"waiting": []}} for _ in calls], lambda: tips)
        sb = CoreShare(None, lambda: {}, borrowing={"dgb"})
        a.add_extension(sa); b.add_extension(sb)
        await started(a, b)
        try:
            await _linked((b, a))
            await until(lambda: sb.sharers("dgb"), what="offer")
            ask = (coinscan.SCAN_METHOD, [{"addresses": [A]}])
            got = await asyncio.to_thread(sb.call, "dgb", [ask] * 6)
            assert [("r" in g) for g in got] == [True] * 5 + [False], "at most five parts in one ask"
            again = await asyncio.to_thread(sb.call, "dgb", [ask])
            assert "too often" in again[0]["e"][1]
        finally:
            await a.stop(); await b.stop()
    run(go())


def _borrowed_dgb(state, monkeypatch, height):
    from arcade.web.state import ChainContext
    chain = ChainContext(network="dgb", role="ledger", label="DigiByte", conf=pathlib.Path("/nonexistent/d.conf"),
                         borrowed=True)
    monkeypatch.setattr(state, "more", {"dgb": chain}, raising=False)
    monkeypatch.setattr(type(state), "chain_ready", lambda self, c: True)
    monkeypatch.setattr(LedgerIndex, "indexed_height", lambda self: height)
    monkeypatch.setattr(threading, "Thread", lambda target, **kw: type("T", (), {"start": lambda self: target()})())
    return chain


def test_a_borrowing_arcade_owes_every_address_once_and_collects_them(client, monkeypatch):
    from arcade.web.watcher import BlockWatcher
    _app, state = client
    state.set_setting("address:main:" + "ab" * 32, b58check_encode(56, b58check_decode(A)[1]))
    chain = _borrowed_dgb(state, monkeypatch, None)
    with contextlib.closing(state.token_index(chain).open()) as db:
        utxos.watch(db, B, 0, "watched before")
        db.conn.commit()
    monkeypatch.setattr(utxos, "backfill_many", lambda *a, **k: 0)
    rpc = _Borrowed({A: [["aa" * 32, 0, 101 * 10**8, FLOOR - 1397]], B: []},
                    {FLOOR - 1397: _block(("aa" * 32, [(A, 101.0)]))})
    monkeypatch.setattr(type(chain), "rpc", lambda self: contextlib.nullcontext(rpc))
    watcher = BlockWatcher(state)
    watcher._watch_doge_accounts()
    assert sorted(json.loads(state.setting(coinscan.owed_key("dgb")))) == sorted([A, B])
    assert not rpc.batches, "the index has no block yet: nothing asked"
    monkeypatch.setattr(LedgerIndex, "indexed_height", lambda self: FLOOR + 10)
    watcher._doge_watch_at = 0
    watcher._watch_doge_accounts()
    with contextlib.closing(state.token_index(chain).open()) as db:
        assert utxos.balance(db, A) == 101 * 10**8
    assert json.loads(state.setting(coinscan.owed_key("dgb"))) == []
    watcher._doge_watch_at = 0
    watcher._watch_doge_accounts()
    assert len([b for b in rpc.batches if coinscan.SCAN_METHOD in b]) == 1, "once for each address"


def test_an_operator_can_say_no_on_either_side(client, monkeypatch):
    from arcade.web.watcher import BlockWatcher
    _app, state = client
    state.set_setting("ask_coinscan", False)
    state.set_setting("address:main:" + "ab" * 32, b58check_encode(56, b58check_decode(A)[1]))
    _borrowed_dgb(state, monkeypatch, FLOOR + 10)
    monkeypatch.setattr(utxos, "backfill_many", lambda *a, **k: 0)
    BlockWatcher(state)._watch_doge_accounts()
    assert state.setting(coinscan.owed_key("dgb")) is None
