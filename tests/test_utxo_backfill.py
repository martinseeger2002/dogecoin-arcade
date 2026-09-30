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
