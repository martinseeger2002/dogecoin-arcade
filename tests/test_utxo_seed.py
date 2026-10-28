"""Coins an address got below its chain's floor (2026-10-18): 0.1 LTC sent to an account in block 3,197,387
against an L main floor of 3,198,000 was never filed, because an index reads only from its floor up. A newly
watched address is now read back from the Core's own coin set (`scantxoutset`), at any height."""
import contextlib
import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import utxos                                          # noqa: E402
from arcade.config import NETWORKS                               # noqa: E402
from arcade.ledger import LedgerIndex                            # noqa: E402
from arcade.rpc import RpcError                                  # noqa: E402
from arcade.script import b58check_decode, b58check_encode       # noqa: E402
from arcade.txbuild import p2pkh_script                          # noqa: E402

LTC = NETWORKS["ltc"]
A = b58check_encode(48, bytes(range(20)))
B = b58check_encode(48, bytes(range(1, 21)))
STRANGER = b58check_encode(48, bytes(range(2, 22)))


def _coin(address, txid, vout, amount, height):
    return {"txid": txid, "vout": vout, "scriptPubKey": p2pkh_script(address).hex(),
            "desc": f"addr({address})", "amount": amount, "height": height}


class _Core:
    """A Litecoin Core's coin set, as `scantxoutset` reads it."""

    def __init__(self, coins, scans=True):
        self.coins, self.scans, self.asked = coins, scans, []
        self._timeout = 120.0

    def call(self, method, *args):
        if method != "scantxoutset" or not self.scans:
            raise RpcError(-32601, "Method not found", method)
        assert self._timeout >= utxos.SCAN_SECONDS, "a whole-coin-set scan takes minutes"
        wanted = set(args[1])
        self.asked.append(wanted)
        return {"success": True, "unspents": [c for c in self.coins if c["desc"] in wanted]}


def _index(tmp_path):
    return LedgerIndex(tmp_path / "ltc-ledger.sqlite", LTC, rpc_factory=lambda: None)


def test_a_coin_below_the_floor_is_filed_from_the_core_s_coin_set(tmp_path):
    index = _index(tmp_path)
    floor = LTC.activation_height
    core = _Core([_coin(A, "aa" * 32, 0, 0.1, floor - 100),            # below the floor: the case
                  _coin(A, "bb" * 32, 1, 0.5, floor + 50),             # read already: the index has it
                  _coin(A, "cc" * 32, 0, 0.2, floor + 500),            # above the index: it files it itself
                  _coin(STRANGER, "dd" * 32, 0, 9.0, floor - 5)])      # not asked about
    with contextlib.closing(index.open()) as db:
        utxos.install(db)
        db.conn.execute("INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                        ("bb" * 32, 1, A, 50_000_000, floor + 50))
    assert utxos.seed_from_core(index, core, LTC, {A, B}, floor + 100) == 1
    assert core._timeout == 120.0, "the client's own timeout is put back"
    with contextlib.closing(index.open()) as db:
        assert {(r["txid"], r["value"]) for r in utxos.unspent(db, A)} == {("aa" * 32, 10_000_000),
                                                                          ("bb" * 32, 50_000_000)}
        assert utxos.balance(db, STRANGER) == 0


def test_a_core_without_scantxoutset_says_so(tmp_path):
    assert utxos.seed_from_core(_index(tmp_path), _Core([], scans=False), LTC, {A}, 10) is None
    assert utxos.seed_from_core(_index(tmp_path), _Core([]), LTC, set(), 10) == 0


def _litecoin(state, monkeypatch, core):
    from arcade.web.state import ChainContext
    chain = ChainContext(network="ltc", role="ledger", label="Litecoin",
                         conf=pathlib.Path("/nonexistent/litecoin.conf"))
    monkeypatch.setattr(state, "ltc", chain, raising=False)
    monkeypatch.setattr(type(state), "chain_ready", lambda self, c: True)
    monkeypatch.setattr(chain, "rpc", lambda: contextlib.nullcontext(core), raising=False)
    monkeypatch.setattr(LedgerIndex, "indexed_height", lambda self: LTC.activation_height + 100)
    monkeypatch.setattr(threading, "Thread", lambda target, **kw: type("T", (), {"start": lambda self: target()})())
    return chain


def test_the_watcher_reads_every_account_s_litecoin_coins_from_the_core(client, monkeypatch):
    from arcade.web.watcher import BlockWatcher
    _app, state = client
    floor = LTC.activation_height
    pep_main = b58check_encode(56, b58check_decode(A)[1])              # the account's Pepecoin mainnet key
    state.set_setting("address:main:" + "ab" * 32, pep_main)
    core = _Core([_coin(A, "aa" * 32, 0, 0.1, floor - 613), _coin(B, "ee" * 32, 0, 0.3, floor - 1)])
    chain = _litecoin(state, monkeypatch, core)
    with contextlib.closing(state.token_index(chain).open()) as db:
        utxos.watch(db, B, 0, "watched before the Core was asked")
        db.conn.commit()
    read = []
    monkeypatch.setattr(utxos, "backfill_many", lambda *a, **k: read.append(a) or 0)
    watcher = BlockWatcher(state)
    watcher._watch_doge_accounts()
    with contextlib.closing(state.token_index(chain).open()) as db:
        assert utxos.balance(db, A) == 10_000_000, "a new account address: its coin below the floor"
        assert utxos.balance(db, B) == 30_000_000, "an address watched before: read once more"
    assert core.asked == [{f"addr({A})", f"addr({B})"}], "one scan for both"
    assert not read, "no block read-back when the Core answered"
    assert state.setting("utxo_seeded:ltc") == "done"
    watcher._doge_watch_at = 0
    watcher._watch_doge_accounts()
    assert len(core.asked) == 1, "once"


def test_a_core_without_it_reads_the_blocks_as_before(client, monkeypatch):
    from arcade.web.watcher import BlockWatcher
    _app, state = client
    state.set_setting("address:main:" + "ab" * 32, b58check_encode(56, b58check_decode(A)[1]))
    chain = _litecoin(state, monkeypatch, _Core([], scans=False))
    read = []
    monkeypatch.setattr(utxos, "backfill_many", lambda index, rpc, params, addresses, start, end:
                        read.append(set(addresses)) or 0)
    BlockWatcher(state)._watch_doge_accounts()
    assert read == [{A}]
    assert state.setting("utxo_seeded:ltc") is None, "nothing watched before, so no once-per-chain pass"
    with contextlib.closing(state.token_index(chain).open()) as db:
        assert A in utxos.watching(db)


def test_an_address_watched_before_the_index_has_a_block_is_read_back_once_it_has_one(client, monkeypatch):
    """2026-10-27: 101 DGB paid to an account below DigiByte's floor, before any arcade had read a DigiByte block.
    Its read-back asked the coin set for coins up to block 0, filed none, and was marked done for good."""
    from arcade.web.watcher import BlockWatcher
    _app, state = client
    floor = LTC.activation_height
    state.set_setting("address:main:" + "ab" * 32, b58check_encode(56, b58check_decode(A)[1]))
    core = _Core([_coin(A, "aa" * 32, 0, 1.0, floor - 1441), _coin(A, "bb" * 32, 1, 100.0, floor - 1397)])
    chain = _litecoin(state, monkeypatch, core)
    monkeypatch.setattr(LedgerIndex, "indexed_height", lambda self: None)
    read = []
    monkeypatch.setattr(utxos, "backfill_many", lambda *a, **k: read.append(a) or 0)
    watcher = BlockWatcher(state)
    watcher._watch_doge_accounts()
    with contextlib.closing(state.token_index(chain).open()) as db:
        assert A in utxos.watching(db), "watched from the start"
    assert not core.asked and not read, "nothing to read back to: no block yet"
    assert state.setting("utxo_seeded:ltc") is None, "not done: nothing was asked"
    monkeypatch.setattr(LedgerIndex, "indexed_height", lambda self: floor)
    watcher._doge_watch_at = 0
    watcher._watch_doge_accounts()
    with contextlib.closing(state.token_index(chain).open()) as db:
        assert utxos.balance(db, A) == 101 * 100_000_000, "both coins below the floor"
    assert core.asked == [{f"addr({A})"}]
    assert state.setting("utxo_seeded:ltc") == "done"
    watcher._doge_watch_at = 0
    watcher._watch_doge_accounts()
    assert len(core.asked) == 1, "once"


def test_a_read_back_marked_done_at_block_0_is_done_again(client, monkeypatch):
    """A node that marked it done before this fix: the addresses watched before its index's first block are read back
    once more, and only those."""
    from arcade.web.watcher import BlockWatcher
    _app, state = client
    floor = LTC.activation_height
    state.set_setting("address:main:" + "ab" * 32, b58check_encode(56, b58check_decode(A)[1]))
    core = _Core([_coin(A, "aa" * 32, 0, 1.0, floor - 1441), _coin(B, "ee" * 32, 0, 0.3, floor - 1)])
    chain = _litecoin(state, monkeypatch, core)
    with contextlib.closing(state.token_index(chain).open()) as db:
        utxos.watch(db, A, 0, "account abababababab (derived)")       # watched before the index had a block
        utxos.watch(db, B, floor + 50, "watched since a block")          # read back properly then
        db.conn.commit()
    state.set_setting("utxo_seeded:ltc", "done")
    watcher = BlockWatcher(state)
    watcher._watch_doge_accounts()
    with contextlib.closing(state.token_index(chain).open()) as db:
        assert utxos.balance(db, A) == 100_000_000
        assert utxos.balance(db, B) == 0, "not asked again"
    assert core.asked == [{f"addr({A})"}]
    watcher._doge_watch_at = 0
    watcher._watch_doge_accounts()
    assert len(core.asked) == 1, "once"


def test_the_account_backfill_is_not_marked_done_before_the_index_has_a_block(client, monkeypatch):
    """/account/coins/backfill marked an address "nothing to read" while its chain's index had no block, so it was
    never asked again once the index had one."""
    from test_coinlist_web import _sign_in
    app, state = client
    pub = _sign_in(app)
    state.set_setting(f"address:{pub}", b58check_encode(NETWORKS["regtest"].pubkeyhash_version, bytes(range(20))))
    marked = lambda: {k: v for k, v in state.settings().items() if k.startswith("backfilled:")}  # noqa: E731
    monkeypatch.setattr(LedgerIndex, "indexed_height", lambda self: None)
    assert app.post("/account/coins/backfill", json={}).json() == {"started": []}
    assert not marked(), "asked again at the next visit"
    monkeypatch.setattr(LedgerIndex, "indexed_height", lambda self: 0)
    app.post("/account/coins/backfill", json={})
    assert list(marked().values()) and set(marked().values()) == {0}, "a block read, and nothing before the watch"
