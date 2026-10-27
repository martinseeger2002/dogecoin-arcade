"""Coins on their way, from anybody, read from each chain's mempool (2026-10-17: "add incoming that reads from
the mempool to all of the chains")."""
from arcade.config import TESTNET
from arcade.pool_coins import PoolCoins
from arcade.script import b58check_encode
from arcade.txbuild import p2pkh_script

ALICE = b58check_encode(TESTNET.pubkeyhash_version, bytes(20))
BOB = b58check_encode(TESTNET.pubkeyhash_version, bytes(range(20)))


class Pool:
    def __init__(self):
        self.txs, self.reads = {}, 0

    def call(self, method, *args):
        if method == "getrawmempool":
            return list(self.txs)
        if method == "getrawtransaction":
            self.reads += 1
            return self.txs[args[0]]
        raise AssertionError(method)


def _tx(*outs):
    return {"vout": [{"n": n, "value": v, "scriptPubKey": {"hex": p2pkh_script(a).hex()}} for n, (a, v) in enumerate(outs)]}


def test_a_payment_in_the_pool_is_arriving_until_it_leaves_the_pool():
    rpc, pool = Pool(), PoolCoins("test")
    rpc.txs["aa" * 32] = _tx((ALICE, 0.764), (BOB, 3))
    assert pool.refresh(rpc, TESTNET)
    assert pool.to(ALICE) == [{"txid": "aa" * 32, "vout": 0, "value": 76_400_000, "address": ALICE}]
    assert [c["value"] for c in pool.to(BOB)] == [300_000_000]
    assert not pool.refresh(rpc, TESTNET) and rpc.reads == 1, "each transaction read once"
    del rpc.txs["aa" * 32]                                    # mined (the index has it now) or dropped
    assert pool.refresh(rpc, TESTNET) and pool.to(ALICE) == []


def test_the_wallet_says_what_is_arriving_on_each_chain(client):
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from test_account_claim import _sign_in
    app, state = client
    pubkey = _sign_in(app)
    state.set_setting(f"address:{pubkey}", BOB)               # the account's address on the tag chain
    said = app.get("/account").json()
    row = next(c for c in said["chains"] if c["address"])
    pool = state.pool_coins(row["network"])
    pool._outs["bb" * 32] = [(0, row["address"], 5_000_000), (1, "someone else", 9)]
    again = next(c for c in app.get("/account").json()["chains"] if c["network"] == row["network"])
    assert again["arriving"] == 5_000_000
    assert again["spendable"] == row["spendable"], "shown, never spent: a stranger's unconfirmed coin"


from test_web import app_state, client   # noqa: E402,F401


def _spending(prev, *outs):
    tx = _tx(*outs)
    tx["vin"] = [{"txid": t, "vout": v} for t, v in prev] + [{"coinbase": "00"}]
    return tx


def test_what_a_waiting_transaction_spends_is_leaving_on_every_chain():
    """2026-10-27: on Kotia (and every chain but the two Pepecoin ones) the wallet's "leaving" read an empty pool, so a
    coin a waiting send had spent counted again beside the send's change: over 20 KOT shown, 4.08 held."""
    import time

    from arcade.pool_coins import pool_spent
    from arcade.web.account import Flights
    from arcade.funding import Unsigned

    rpc, pool = Pool(), PoolCoins("kot")
    coin = ("cc" * 32, 1)
    rpc.txs["dd" * 32] = _spending([coin], (BOB, 10), (ALICE, 8.97))
    rpc.txs["ee" * 32] = _spending([("ff" * 32, 0)], (BOB, 1))
    pool.refresh(rpc, TESTNET)
    flights = Flights()
    assert pool.spent() == (frozenset({coin, ("ff" * 32, 0)}), frozenset())
    assert pool_spent(pool, flights, "kot") == {coin, ("ff" * 32, 0)}
    # Our own send, in the pool but not read through yet (a pool of thousands): its coins are leaving all the same.
    ours = ("12" * 32, 0)
    flights.add("ab" * 32, "34" * 32, Unsigned(raw="", inputs=[{"txid": ours[0], "vout": 0, "value": 5}],
                                               outputs=[]), ALICE, network="kot")
    pool._ids = pool._ids | {"34" * 32}
    assert ours in pool_spent(pool, flights, "kot")
    # Mined: it leaves the pool, and with it what it spent.
    del rpc.txs["dd" * 32]
    pool.refresh(rpc, TESTNET)
    assert coin not in pool_spent(pool, flights, "kot")
    # A pool the watcher has not read lately is not believed: the flights count instead (Flights.spent_by).
    pool.read_at = time.time() - 3600
    import pytest
    with pytest.raises(LookupError):
        pool_spent(pool, flights, "kot")
    book = Flights(pool=lambda net: pool_spent(pool, flights, net))
    book.add("ab" * 32, "56" * 32, Unsigned(raw="", inputs=[{"txid": "78" * 32, "vout": 2, "value": 5}], outputs=[]),
             ALICE, network="kot")
    assert ("78" * 32, 2) in book.spent_by("ab" * 32, "kot")
