"""A book that shows the prices people are offering, not the prices from ten
minutes ago.

An order is a public statement of a price. Until it was in a block nobody could
see it -- not the person who placed it, whose own list said they had no orders,
and not the person who would have taken it. A book that lags the chain by a
block is a book nobody can trade on, so the pool is read into it.

What the pool must not do is change the ledger. Nothing here is written down:
the tokens an ask sells are reserved when its block lands and not before, and
two nodes still agree about the chain when their pools differ.
"""

import pytest

from arcade import payload as P
from arcade.config import NETWORKS
from arcade.encoding import encode_class_c
from arcade.ledger import LedgerIndex
from arcade.txbuild import op_return_script

PARAMS = NETWORKS["regtest"]
TOKEN = 2147483651
MAKER = "mMakerAddressWithTokensToSell1111"
OTHER = "mSomebodyElseEntirely22222222222"


class Node:
    def __init__(self):
        self.pool: dict[str, dict] = {}
        self.asked: list[str] = []

    def call(self, method, *args, **kwargs):
        self.asked.append(method)
        if method == "getrawmempool":
            return list(self.pool)
        if method == "getrawtransaction":
            return self.pool[args[0]]
        raise AssertionError(method)


def a_transaction(txid: str, message) -> dict:
    payload = message.encode()
    return {
        "txid": txid,
        "vin": [{"txid": "b2" * 32, "vout": 0}],
        "vout": [{"n": 0, "value": 0,
                  "scriptPubKey": {"hex": op_return_script(
                      encode_class_c(payload)).hex()}},
                 {"n": 1, "value": 0.01,
                  "scriptPubKey": {"hex": "76a914" + "11" * 20 + "88ac"}}],
    }


def an_ask(tokens: int = 1000_00000000, coins: int = 8_00000000):
    """Selling tokens for coins."""
    return P.MetaDExTrade(property_id_for_sale=TOKEN, amount_for_sale=tokens,
                          property_id_desired=0, amount_desired=coins)


def a_bid(tokens: int = 500_00000000, coins: int = 3_00000000):
    return P.MetaDExTrade(property_id_for_sale=0, amount_for_sale=coins,
                          property_id_desired=TOKEN, amount_desired=tokens)


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
        db.conn.execute(
            "INSERT INTO property(property_id,ecosystem,property_type,issuer,name,"
            "total_tokens,creation_txid,creation_block) VALUES(?,?,?,?,?,?,?,?)",
            (TOKEN, 2, 2, MAKER, "Arcade Test", 10 ** 14, "ab" * 32, 100))
        db.conn.commit()
    made.node = node
    from arcade.script import OutputType
    from arcade.tx import PrevOut
    monkeypatch.setattr("arcade.indexer.PrevOutCache.lookup",
                        lambda self, txid, n: PrevOut(address=MAKER, value=10 ** 8,
                                                      type=OutputType.PUBKEYHASH))
    return made


def test_an_ask_in_the_pool_is_on_the_book(index):
    index.node.pool = {"c1" * 32: a_transaction("c1" * 32, an_ask())}
    book = index.book(TOKEN)
    assert len(book["asks"]) == 1 and not book["bids"]
    ask = book["asks"][0]
    assert ask["address"] == MAKER
    assert ask["tokens"] == 1000_00000000 and ask["coins"] == 8_00000000
    assert ask["pending"] and ask["block_height"] == 0
    assert TOKEN in index.book_pairs(), "a pair nobody has mined yet is still a pair"


def test_a_bid_in_the_pool_is_on_the_other_side(index):
    index.node.pool = {"c2" * 32: a_transaction("c2" * 32, a_bid())}
    book = index.book(TOKEN)
    assert len(book["bids"]) == 1 and not book["asks"]


def test_nothing_from_the_pool_is_written_down(index):
    """The reserve an ask holds moves when its block lands, not before."""
    index.node.pool = {"c1" * 32: a_transaction("c1" * 32, an_ask())}
    index.book(TOKEN)
    with index.open() as db:
        assert db.conn.execute("SELECT COUNT(*) FROM book_order").fetchone()[0] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM balance WHERE metadex_reserve != 0").fetchone()[0] == 0


def test_the_pool_is_sorted_in_with_the_chain(index):
    """A better price in the pool belongs above a worse one in a block."""
    with index.open() as db:
        db.conn.execute(
            "INSERT INTO book_order(txid,block_height,position,address,sale_property,"
            "sale_amount,want_property,want_amount,reserved) VALUES(?,?,?,?,?,?,?,?,?)",
            ("dd" * 32, 500, 0, OTHER, TOKEN, 1000_00000000, 0, 9_00000000, 1000_00000000))
        db.conn.commit()
    index.node.pool = {"c1" * 32: a_transaction("c1" * 32, an_ask(coins=8_00000000))}
    asks = index.book(TOKEN)["asks"]
    assert [a["txid"][:2] for a in asks] == ["c1", "dd"], "cheapest first, pool or not"


def test_a_cancel_in_the_pool_takes_the_order_off(index):
    """A price somebody has withdrawn is not a price, block or no block."""
    with index.open() as db:
        db.conn.execute(
            "INSERT INTO book_order(txid,block_height,position,address,sale_property,"
            "sale_amount,want_property,want_amount,reserved) VALUES(?,?,?,?,?,?,?,?,?)",
            ("dd" * 32, 500, 0, MAKER, TOKEN, 1000_00000000, 0, 9_00000000, 1000_00000000))
        db.conn.commit()
    assert len(index.book(TOKEN, pool=False)["asks"]) == 1

    index.node.pool = {"c9" * 32: a_transaction("c9" * 32, P.MetaDExCancelPair(
        property_id_for_sale=TOKEN, property_id_desired=0))}
    assert index.book(TOKEN)["asks"] == []
    with index.open() as db:                      # and the ledger still has it
        assert db.conn.execute("SELECT COUNT(*) FROM book_order").fetchone()[0] == 1


def test_a_cancel_only_reaches_its_own_orders(index):
    with index.open() as db:
        db.conn.execute(
            "INSERT INTO book_order(txid,block_height,position,address,sale_property,"
            "sale_amount,want_property,want_amount,reserved) VALUES(?,?,?,?,?,?,?,?,?)",
            ("dd" * 32, 500, 0, OTHER, TOKEN, 1000_00000000, 0, 9_00000000, 1000_00000000))
        db.conn.commit()
    index.node.pool = {"c9" * 32: a_transaction("c9" * 32, P.MetaDExCancelPair(
        property_id_for_sale=TOKEN, property_id_desired=0))}
    assert len(index.book(TOKEN)["asks"]) == 1, "somebody else's order is not yours to cancel"


def test_a_cancel_at_a_price_is_exact(index):
    """3 for 2 and 6 for 4 are the same price; 3 for 2 and 3 for 2.000001 are not."""
    with index.open() as db:
        db.conn.execute(
            "INSERT INTO book_order(txid,block_height,position,address,sale_property,"
            "sale_amount,want_property,want_amount,reserved) VALUES(?,?,?,?,?,?,?,?,?)",
            ("dd" * 32, 500, 0, MAKER, TOKEN, 2000_00000000, 0, 16_00000000, 0))
        db.conn.commit()
    same = P.MetaDExCancelPrice(property_id_for_sale=TOKEN, amount_for_sale=1000_00000000,
                                property_id_desired=0, amount_desired=8_00000000)
    index.node.pool = {"c9" * 32: a_transaction("c9" * 32, same)}
    assert index.book(TOKEN)["asks"] == [], "the same price, written differently"

    other = P.MetaDExCancelPrice(property_id_for_sale=TOKEN, amount_for_sale=1000_00000000,
                                 property_id_desired=0, amount_desired=8_00000001)
    index.node.pool = {"ca" * 32: a_transaction("ca" * 32, other)}
    assert len(index.book(TOKEN)["asks"]) == 1, "a different price is a different price"


def test_the_refusals_the_indexer_makes(index):
    """Same rules, minus the ones only a block can answer."""
    both = P.MetaDExTrade(property_id_for_sale=TOKEN, amount_for_sale=5,
                          property_id_desired=TOKEN + 1, amount_desired=5)
    index.node.pool = {"c1" * 32: a_transaction("c1" * 32, both)}
    assert index.book(TOKEN)["asks"] == [], "one side of an order is the coin"

    unknown = P.MetaDExTrade(property_id_for_sale=99999, amount_for_sale=5,
                             property_id_desired=0, amount_desired=5)
    index.node.pool = {"c2" * 32: a_transaction("c2" * 32, unknown)}
    assert index.book(99999)["asks"] == [], "that property does not exist"


def test_your_own_order_is_yours_before_it_is_mined(index):
    """The list said "you have no orders" the moment after placing one."""
    index.node.pool = {"c1" * 32: a_transaction("c1" * 32, an_ask())}
    mine = index.orders_of([MAKER])
    assert len(mine) == 1 and mine[0]["pending"]
    assert index.orders_of([OTHER]) == []


def test_a_node_that_is_not_there_is_not_an_error(index, monkeypatch):
    def broken():
        raise OSError("connection refused")

    monkeypatch.setattr(index, "_rpc", broken)
    assert index.pending_orders() == ([], set())
    assert index.book(TOKEN) == {"asks": [], "bids": []}


def test_at_one_price_the_oldest_is_first(index):
    """Price, then time. At the same price the order placed first is filled
    first, and a maker who queues behind somebody is entitled to rely on that.

    The pool sorts LAST within a price, not first: an unmined order has
    block_height 0, so a naive time sort put the newest thing on the book
    ahead of orders that had stood for hours -- time priority backwards
    (D-083).
    """
    with index.open() as db:
        for txid, height, tokens, coins in (
            ("aa" * 32, 700, 100_00000000, 1_00000000),    # 0.01, later
            ("bb" * 32, 500, 100_00000000, 1_00000000),    # 0.01, earliest
            ("cc" * 32, 600, 100_00000000, 50000000),      # 0.005, cheapest
        ):
            db.conn.execute(
                "INSERT INTO book_order(txid,block_height,position,address,"
                "sale_property,sale_amount,want_property,want_amount,reserved) "
                "VALUES(?,?,0,?,?,?,0,?,?)",
                (txid, height, MAKER, TOKEN, tokens, coins, tokens))
        db.conn.commit()
    index.node.pool = {"c1" * 32: a_transaction("c1" * 32, an_ask(
        tokens=100_00000000, coins=1_00000000))}          # 0.01, unmined

    order = [a["txid"][:2] for a in index.book(TOKEN)["asks"]]
    assert order == ["cc", "bb", "aa", "c1"], (
        "cheapest first; then oldest first at the same price; the pool last "
        "because it is the newest of all")
