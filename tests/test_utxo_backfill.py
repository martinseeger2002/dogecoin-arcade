"""Coins an address held before this node watched it (2026-09-30: an account
brought from another arcade showed "zero balance ... has @tag")."""

import contextlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import node, _settled                          # noqa: F401,E402

from arcade import utxos                                                  # noqa: E402


def test_coins_paid_before_the_watch_are_found_and_spent_ones_are_not(node):
    app, state, rpc = node
    chain = state.messaging
    index = state.token_index(chain)
    rpc.call("generate", 101)                    # coins the node wallet can spend
    held = rpc.call("getnewaddress")
    start = rpc.call("getblockcount") + 1
    kept = rpc.call("sendtoaddress", held, 1.5)
    gone = rpc.call("sendtoaddress", held, 0.7)
    rpc.call("generate", 1)
    # spend the second coin away: it must not be counted
    raw = rpc.call("createrawtransaction", [{"txid": gone, "vout": next(
        o["n"] for o in rpc.call("getrawtransaction", gone, 1)["vout"]
        if held in (o["scriptPubKey"].get("addresses") or []))}],
        {rpc.call("getnewaddress"): 0.69})
    rpc.call("sendrawtransaction", rpc.call("signrawtransaction", raw)["hex"])
    rpc.call("generate", 1)
    end = rpc.call("getblockcount")
    _settled(state, rpc)
    with contextlib.closing(index.open()) as db:
        assert utxos.balance(db, held) == 0, "not watched, so not known"
    filed = utxos.backfill(index, rpc, chain.params, held, start, end)
    assert filed == 1
    with contextlib.closing(index.open()) as db:
        rows = utxos.unspent(db, held)
    assert [(r["txid"], r["value"]) for r in rows] == [(kept, 150_000_000)]


def test_one_read_back_at_a_time_and_paced(monkeypatch):
    """2026-10-19: an account brought to pepecoinarcade.com started five read-backs at once (55,000 P test blocks
    among them) and the arcade's pages stopped answering. One at a time, resting between blocks."""
    from arcade import utxos
    rested, held = [], []

    class Rpc:
        def get_block_hash(self, height):
            return f"h{height}"

        def get_block(self, h, verbosity):
            held.append(utxos._BACKFILL_ONE.locked())
            return {"tx": []}
    monkeypatch.setattr(utxos.time, "sleep", lambda s: rested.append(s))
    import contextlib

    class Index:
        def open(self):
            return contextlib.nullcontext(None)
    assert utxos.backfill_many(Index(), Rpc(), None, {"nSomebody"}, 1, 5) == 0
    assert held == [True] * 5, "the lock is held for the whole pass"
    assert len(rested) == 5 and all(0 <= s <= 1.0 for s in rested), "a rest after every block"
    assert not utxos._BACKFILL_ONE.locked()


def test_a_broadcast_the_pool_dropped_is_not_leaving(monkeypatch):
    """2026-10-19: @apple read 11.23 "leaving" on one arcade and 0.01 on the other; the pool held 0.01. A broadcast
    the node's mempool no longer holds spends nothing after its first few seconds."""
    from arcade.web import account as A
    pool = {("ee" * 32, 0)}
    flights = A.Flights(pool=lambda net: pool)
    flights._all.append(A.InFlight(txid="ff" * 32, pubkey="ab" * 32, network="test",
                                   spent=(("aa" * 32, 0), ("ee" * 32, 0)), made=()))
    assert ("aa" * 32, 0) in flights.spent_by("ab" * 32, "test"), "just broadcast: counted"
    flights._all[0].at -= A.FLIGHT_GRACE + 1
    assert flights.spent_by("ab" * 32, "test") == frozenset(pool), "dropped by the pool: only what the pool spends"
