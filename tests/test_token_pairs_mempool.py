"""Token/token orders read from the mempool (2026-10-06, the operator: "Can't the bid
and ask orders be read from mempool?" -> "Ok").

A pair order in the pool is on its pair's book at once, marked pending, after the
mined orders at its price; one that crosses the mined book is marked as about to
trade, because the engine fills it in the block it lands in. A pool cancel takes
an order off now. Coin books never see a pair order, and nothing is written down.
"""

import pytest

from arcade import payload as P
from test_book_mempool import Node, a_transaction, MAKER, PARAMS            # noqa: F401
from arcade.ledger import LedgerIndex

LOGS, GOLD = 2147483651, 2147483652
COIN = 10 ** 8


@pytest.fixture
def index(tmp_path, monkeypatch):
    node = Node()

    class Rpc:
        def __enter__(self):
            return node

        def __exit__(self, *exc):
            return False

    made = LedgerIndex(tmp_path / "ledger.sqlite", PARAMS, lambda: Rpc())
    with made.open() as db:
        for pid, name in ((LOGS, "LOGS"), (GOLD, "GOLD")):
            db.conn.execute(
                "INSERT INTO property(property_id,ecosystem,property_type,issuer,name,"
                "total_tokens,creation_txid,creation_block) VALUES(?,?,?,?,?,?,?,?)",
                (pid, 2, 2, MAKER, name, 10 ** 14, f"{pid:064x}", 100))
        db.conn.commit()
    made.node = node
    from arcade.script import OutputType
    from arcade.tx import PrevOut
    monkeypatch.setattr("arcade.indexer.PrevOutCache.lookup",
                        lambda self, txid, n: PrevOut(address=MAKER, value=10 ** 8,
                                                      type=OutputType.PUBKEYHASH))
    return made


def sell(logs, gold):
    return P.MetaDExTrade(property_id_for_sale=LOGS, amount_for_sale=logs * COIN,
                          property_id_desired=GOLD, amount_desired=gold * COIN)


def buy(logs, gold):
    return P.MetaDExTrade(property_id_for_sale=GOLD, amount_for_sale=gold * COIN,
                          property_id_desired=LOGS, amount_desired=logs * COIN)


def rest(index, txid, sale, sale_amount, want, want_amount):
    with index.open() as db:
        db.conn.execute("INSERT INTO book_order(txid,block_height,position,address,sale_property,"
                        "sale_amount,want_property,want_amount,reserved) VALUES(?,?,?,?,?,?,?,?,?)",
                        (txid, 200, 1, "mSomebodyMined111111111111111111", sale, sale_amount,
                         want, want_amount, sale_amount))
        db.conn.commit()


def test_a_pair_order_in_the_pool_is_on_its_book_at_once(index):
    index.node.pool = {"c1" * 32: a_transaction("c1" * 32, sell(20, 30))}
    book = index.pair_book(LOGS, GOLD)
    (ask,) = book["asks"]
    assert ask["pending"] and not ask["crosses"]
    assert (ask["base"], ask["quote"]) == (20 * COIN, 30 * COIN)
    assert (LOGS, GOLD) in index.token_pairs(), "a pair nobody has mined yet is still listed"
    # the same order seen from the other way round is a bid on GOLD/LOGS
    assert len(index.pair_book(GOLD, LOGS)["bids"]) == 1


def test_a_pending_order_that_crosses_the_book_is_marked_as_about_to_trade(index):
    rest(index, "aa" * 32, LOGS, 20 * COIN, GOLD, 30 * COIN)          # mined: 1.5 GOLD each
    index.node.pool = {"c2" * 32: a_transaction("c2" * 32, buy(20, 40))}  # pending: 2 each
    book = index.pair_book(LOGS, GOLD)
    (bid,) = book["bids"]
    assert bid["pending"] and bid["crosses"]


def test_a_pending_order_at_the_same_price_queues_behind_the_mined_one(index):
    rest(index, "aa" * 32, LOGS, 20 * COIN, GOLD, 30 * COIN)
    index.node.pool = {"c3" * 32: a_transaction("c3" * 32, sell(20, 30))}
    asks = index.pair_book(LOGS, GOLD)["asks"]
    assert [bool(a.get("pending")) for a in asks] == [False, True]


def test_a_cancel_in_the_pool_takes_a_pair_order_off_now(index):
    rest(index, "aa" * 32, LOGS, 20 * COIN, GOLD, 30 * COIN)
    with index.open() as db:     # the mined order is the pool sender's own
        db.conn.execute("UPDATE book_order SET address=?", (MAKER,))
        db.conn.commit()
    index.node.pool = {"c4" * 32: a_transaction("c4" * 32, P.MetaDExCancelPair(
        property_id_for_sale=LOGS, property_id_desired=GOLD))}
    assert index.pair_book(LOGS, GOLD)["asks"] == []


def test_coin_books_never_see_a_pair_order_and_nothing_is_written_down(index):
    index.node.pool = {"c5" * 32: a_transaction("c5" * 32, sell(20, 30))}
    assert index.book(LOGS) == {"asks": [], "bids": []}
    assert LOGS not in index.book_pairs()
    assert index.pending_pair_sales(MAKER, LOGS) == 20 * COIN, "spoken for in the pool"
    with index.open() as db:
        assert db.conn.execute("SELECT COUNT(*) FROM book_order").fetchone()[0] == 0
