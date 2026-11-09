"""A borrowed DigiByte reads an account's earlier DigiDollars through the arcade it borrows from (2026-11-09, security
review: the history was read whole only from this arcade's own Core, so on a borrowing arcade it said `whole: false`
for good). The sharer's coin-set read (coinscan.Lender, `arcade_coinscan`) takes a DigiDollar address as its Taproot
script; the borrower checks every coin it names against its block's "DD" record (digidollar.seed_from_lender)."""
import contextlib
import pathlib
import sys
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_account_claim import _sign_in                          # noqa: E402
from test_coinscan import _Clock                                  # noqa: E402
import signup_proof as SP                                         # noqa: E402

from arcade import coinscan, digidollar as dd, taproot            # noqa: E402
from arcade.config import NETWORKS                                # noqa: E402
from arcade.ledger import LedgerIndex                             # noqa: E402
from arcade.rpc import RpcError                                   # noqa: E402
from arcade.script import b58check_encode, hash160                # noqa: E402

NET = "dgbtest"
DGBT = NETWORKS[NET]
SINCE = 500


def _key(n: int) -> bytes:
    x, y = taproot.mul(n)
    return bytes([2 + (y & 1)]) + x.to_bytes(32, "big")


ME, THEM = _key(7), _key(11)
MINE = dd.address(NET, ME)


def _script(pub: bytes) -> str:
    return taproot.p2tr(dd.output_key(pub)).hex()


def _paid(txid: str, *outs):
    """A DigiDollar send: each (key, cents) a zero-value Taproot output, then its "DD" record."""
    record = bytes([dd.OP_RETURN]) + dd.push(b"DD") + dd.push(dd.num(2)) + b"".join(dd.push(dd.num(c)) for _k, c in outs)
    vout = [{"n": n, "value": 0, "scriptPubKey": {"hex": _script(k)}} for n, (k, _c) in enumerate(outs)]
    vout.append({"n": len(outs), "value": 0, "scriptPubKey": {"hex": record.hex()}})
    return {"txid": txid, "version": dd.SEND_VERSION, "vin": [], "vout": vout}


# --- the sharer -----------------------------------------------------------------------------------------------------

class _Core:
    """The sharer's own DigiByte Core: its coin set, by script."""

    def __init__(self, coins):
        self.coins, self.asked = coins, []
        self._timeout = 120.0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call(self, method, *args):
        assert method == "scantxoutset"
        self.asked.append(sorted(args[1]))
        return {"height": SINCE + 90, "unspents": [
            {"txid": t, "vout": v, "scriptPubKey": s, "amount": 0, "height": h}
            for s, t, v, h in self.coins if f"raw({s})" in args[1]]}


def test_a_digibyte_sharer_reads_a_digidollar_address_as_its_taproot_script():
    core = _Core([(_script(ME), "aa" * 32, 0, 120), (_script(THEM), "bb" * 32, 0, 130)])
    lender = coinscan.Lender(DGBT, lambda: core, clock=_Clock(), start=lambda job: job())
    assert lender.ask({"addresses": [MINE]})["waiting"] == [MINE]
    assert core.asked == [[f"raw({_script(ME)})"]], "its Taproot script, not addr(): DigiByte Core has no DD addresses"
    got = lender.ask({"addresses": [MINE]})
    assert got["coins"] == {MINE: [["aa" * 32, 0, 0, 120]]} and got["height"] == {MINE: SINCE + 90}


def test_a_sharer_of_another_chain_skips_a_digidollar_address():
    for net, address in (("ltc", MINE), ("dgb", MINE)):          # Litecoin; DigiByte mainnet is not the test chain's
        core = _Core([])
        lender = coinscan.Lender(NETWORKS[net], lambda: core, clock=_Clock(), start=lambda job: job())
        assert lender.ask({"addresses": [address]}) == {"coins": {}, "height": {}, "waiting": [], "next": 0}
        assert core.asked == []


# --- the borrower ---------------------------------------------------------------------------------------------------

class _Lent:
    """A borrowed DigiByte's client: the sharer's coin-set answer, its blocks and its unspent set."""

    def __init__(self, answer, blocks=None, spent=(), refuse=None, height=SINCE + 90):
        self.answer, self.blocks, self.spent, self.refuse, self.height = answer, blocks or {}, set(spent), refuse, height
        self.batches = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def batch(self, calls):
        self.batches.append([m for m, _ in calls])
        out = []
        for method, params in calls:
            if method == coinscan.SCAN_METHOD:
                if self.refuse:
                    raise RpcError(-32601, self.refuse, method)
                wanted = params[0]["addresses"]
                out.append({"coins": {a: self.answer[a] for a in wanted if a in self.answer and self.answer[a] != "wait"},
                            "height": {a: self.height for a in wanted if a in self.answer},
                            "waiting": [a for a in wanted if self.answer.get(a) == "wait"], "next": 0})
            elif method == "gettxout":
                assert params[2] is False, "the mempool left out"
                out.append(None if (params[0], params[1]) in self.spent else {"value": 0})
        return out

    def get_block_hash(self, height):
        return f"{height:064x}"

    def get_block(self, block_hash, verbosity):
        assert verbosity == 2
        return self.blocks[int(block_hash, 16)]


def _index(tmp_path):
    index = LedgerIndex(tmp_path / "dgbtest-ledger.sqlite", DGBT, rpc_factory=lambda: None)
    with contextlib.closing(index.open()) as db:
        dd.watch(db, MINE, SINCE)
    return index


def _row_and_coins(index):
    with contextlib.closing(index.open()) as db:
        return dd.watched_row(db, MINE), dd.coins(db, MINE)


def test_the_borrower_files_only_dollars_their_blocks_bear_out_and_still_unspent(tmp_path):
    index = _index(tmp_path)
    rpc = _Lent({MINE: [["a1" * 32, 0, 0, 120],               # $60 to this address
                        ["b2" * 32, 1, 0, 130],               # $25, the second output of a send to two
                        ["c3" * 32, 0, 0, 130],               # made up: not in its block
                        ["d4" * 32, 0, 0, 130],               # in its block, dollars of somebody else's
                        ["e5" * 32, 0, 0, 140],               # spent since
                        ["f6" * 32, 0, 0, SINCE + 5]]},       # after it was watched: on_block files it
                {120: {"tx": [_paid("a1" * 32, (ME, 6000))]},
                 130: {"tx": [_paid("b2" * 32, (THEM, 500), (ME, 2500)), _paid("d4" * 32, (THEM, 9900))]},
                 140: {"tx": [_paid("e5" * 32, (ME, 100))]}},
                spent={("e5" * 32, 0)})
    assert dd.seed_from_lender(index, rpc, NET, MINE, SINCE) == 2
    row, coins = _row_and_coins(index)
    assert row["seeded"] and dd.from_block(row) == 0, "read whole"
    assert [(c["txid"], c["cents"], c["height"]) for c in coins] == [("a1" * 32, 6000, 120), ("b2" * 32, 2500, 130)]


def test_the_borrower_waits_while_the_sharer_reads_and_hears_when_it_reads_none(tmp_path):
    index = _index(tmp_path)
    assert dd.seed_from_lender(index, _Lent({MINE: "wait"}), NET, MINE, SINCE) is None
    assert dd.seed_from_lender(index, _Lent({MINE: []}, height=SINCE - 1), NET, MINE, SINCE) is None, \
        "a read from before the address was watched is no proof of what came after"
    assert dd.seed_from_lender(index, _Lent({}, refuse="arcade_coinscan asked too often; once every 60 s"),
                               NET, MINE, SINCE) is None, "too often is not 'never'"
    assert not _row_and_coins(index)[0]["seeded"]
    with pytest.raises(coinscan.NotLent):
        dd.seed_from_lender(index, _Lent({}), NET, MINE, SINCE)          # a sharer from before: neither found nor waiting
    with pytest.raises(coinscan.NotLent):
        dd.seed_from_lender(index, _Lent({}, refuse="a shared Core does not answer arcade_coinscan"), NET, MINE, SINCE)
    assert dd.seed_from_lender(index, _Lent({MINE: []}), NET, MINE, SINCE) == 0
    assert _row_and_coins(index)[0]["seeded"], "nothing before it, and that is the whole of it"


# --- the Wallet -----------------------------------------------------------------------------------------------------

def _borrowed(state, monkeypatch, rpc):
    from arcade.web.state import ChainContext
    chain = ChainContext(network=NET, role="ledger", label="DigiByte testnet",
                         conf=pathlib.Path("/nonexistent/digibyte.conf"), borrowed=True)
    monkeypatch.setattr(state, "more", {NET: chain}, raising=False)
    said = {"ready": True, "progress": 1.0, "online": True, "blocks": SINCE, "headers": SINCE}
    monkeypatch.setattr(type(state), "chain_sync", lambda self, c: said)
    monkeypatch.setattr(LedgerIndex, "indexed_height", lambda self: SINCE)
    object.__setattr__(chain, "rpc", lambda: rpc)
    real = threading.Thread
    monkeypatch.setattr(threading, "Thread", lambda target=None, **kw: type("T", (), {"start": lambda self: target()})()
                        if kw.get("name") == "digidollar seed" else real(target=target, **kw))


def _wallet(app, state):
    pubkey = _sign_in(app)
    _priv, pub = SP.coin_key()
    state.set_setting(f"address:{NET}:{pubkey}", b58check_encode(DGBT.pubkeyhash_version, hash160(pub)))
    return dd.address(NET, pub), pub, lambda: app.get(f"/account/digidollar?chain={NET}&pubkey={pub.hex()}").json()


def test_a_borrowing_arcade_reads_the_history_whole_through_its_lender(client, monkeypatch):
    app, state = client
    rpc = _Lent({})
    _borrowed(state, monkeypatch, rpc)
    address, pub, ask = _wallet(app, state)
    rpc.answer = {address: [["a1" * 32, 0, 0, 120]]}
    rpc.blocks = {120: {"tx": [_paid("a1" * 32, (pub, 6000))]}}
    first = ask()
    assert first["earlier"] == "lender" and first["from_block"] == SINCE + 1, first
    again = ask()
    assert again["whole"] is True and again["from_block"] == 0 and again["cents"] == 6000, again
    assert sum(coinscan.SCAN_METHOD in b for b in rpc.batches) == 1, "asked once"


def test_a_lender_that_reads_none_is_said_and_not_asked_on_every_visit(client, monkeypatch):
    app, state = client
    rpc = _Lent({}, refuse="a shared Core does not answer arcade_coinscan")
    _borrowed(state, monkeypatch, rpc)
    _address, _pub, ask = _wallet(app, state)
    ask()
    later = ask()
    assert later["whole"] is False and later["earlier"] == "" and later["from_block"] == SINCE + 1, later
    assert len(rpc.batches) == 1, "asked again after coinscan.NONE_AGAIN, not on every visit"


def test_an_operator_who_said_no_to_asking_the_sharer_is_not_overruled(client, monkeypatch):
    app, state = client
    state.set_setting("ask_coinscan", False)
    rpc = _Lent({})
    _borrowed(state, monkeypatch, rpc)
    _address, _pub, ask = _wallet(app, state)
    assert ask()["earlier"] == "" and rpc.batches == []
