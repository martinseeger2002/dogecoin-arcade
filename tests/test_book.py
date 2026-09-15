"""The order book: standing public orders, priced as exact integers.

Nothing is matched here. A fill is a swap -- one transaction both sides sign,
which is the only way a coin leg and a token leg move together (D-048). What
the engine does is keep the book honest: what is offered for sale is held
back, what cannot be honoured is refused, and a cancel gives it back.
"""

import pytest

from arcade import payload as P
from arcade.config import NETWORKS
from arcade.db import Database
from arcade.state import Engine, StateDB, install_schema
from arcade.tx import ArcadeTransaction, EncodingClass

ALICE = "mfWxJ45yp2SFn7UciZyNpvDKrzbhyfKrY8"
BOB = "mkHS9ne12qx9pS9VojpwU5xtRd4T7X7ZUt"
COIN = 10 ** 8


@pytest.fixture
def world(tmp_path):
    db = Database(tmp_path / "ledger.sqlite")
    install_schema(db)
    state = StateDB(db)
    engine = Engine(state, NETWORKS["regtest"])
    feed(engine, state, [tx(1, P.IssuanceFixed(
        ecosystem=2, property_type=2, previous_property_id=0, category="c",
        subcategory="s", name="Arcade Test", url="", data="",
        amount=1000 * COIN), ALICE)])
    pid = db.conn.execute("SELECT MAX(property_id) FROM property").fetchone()[0]
    return engine, state, db, pid


def tx(n, message, sender, reference=None, height=None):
    return ArcadeTransaction(
        txid=f"{n:064x}", block_height=height if height is not None else 100 + n,
        position=0, encoding_class=EncodingClass.C, sender=sender,
        reference=reference, payload=message.encode(), fee=0)


def feed(engine, state, transactions):
    for transaction in transactions:
        with state.block_context(transaction.block_height,
                                 f"h{transaction.block_height}", "p", 0, 1, 0):
            engine.process(transaction)


def reason(db, n):
    row = db.conn.execute("SELECT valid, invalid_reason FROM arcade_tx WHERE txid=?",
                          (f"{n:064x}",)).fetchone()
    return row["invalid_reason"] or ("valid" if row["valid"] else "invalid")


def book(db):
    return [dict(r) for r in db.conn.execute(
        "SELECT * FROM book_order ORDER BY block_height, position")]


def held(db, address, pid):
    row = db.conn.execute("SELECT * FROM balance WHERE address=? AND property_id=?",
                          (address, pid)).fetchone()
    return (row["balance"], row["metadex_reserve"]) if row else (0, 0)


def ask(pid, amount, coins):
    """Sell `amount` of a token for `coins`."""
    return P.MetaDExTrade(property_id_for_sale=pid, amount_for_sale=amount,
                          property_id_desired=0, amount_desired=coins)


def bid(pid, amount, coins):
    """Buy `amount` of a token with `coins`."""
    return P.MetaDExTrade(property_id_for_sale=0, amount_for_sale=coins,
                          property_id_desired=pid, amount_desired=amount)


def test_an_ask_holds_back_what_it_offers(world):
    engine, state, db, PID = world
    assert held(db, ALICE, PID) == (1000 * COIN, 0)

    feed(engine, state, [tx(2, ask(PID, 100 * COIN, 1 * COIN), ALICE)])
    assert reason(db, 2) == "valid"
    (order,) = book(db)
    assert order["address"] == ALICE and order["sale_property"] == PID
    assert order["sale_amount"] == 100 * COIN and order["want_amount"] == 1 * COIN
    assert order["reserved"] == 100 * COIN
    assert held(db, ALICE, PID) == (900 * COIN, 100 * COIN), \
        "what is on the book is not also spendable"

    # More than is left, counting what the first order already holds.
    feed(engine, state, [tx(3, ask(PID, 950 * COIN, 9 * COIN), ALICE)])
    assert "holds 90000000000" in reason(db, 3)
    assert len(book(db)) == 1


def test_a_bid_reserves_nothing_and_says_so(world):
    """Coins cannot be held back: there is no covenant that would hold them
    and still let the wallet live. A bid is an intent (D-048)."""
    engine, state, db, PID = world
    feed(engine, state, [tx(2, bid(PID, 100 * COIN, 1 * COIN), BOB)])
    assert reason(db, 2) == "valid"
    (order,) = book(db)
    assert order["sale_property"] == 0 and order["want_property"] == PID
    assert order["reserved"] == 0
    assert held(db, BOB, PID) == (0, 0), "a bidder need not hold the token at all"


def test_what_cannot_be_an_order_is_refused(world):
    engine, state, db, PID = world
    feed(engine, state, [
        tx(2, P.MetaDExTrade(property_id_for_sale=PID, amount_for_sale=1,
                             property_id_desired=PID, amount_desired=1), ALICE),
        tx(3, P.MetaDExTrade(property_id_for_sale=PID, amount_for_sale=1,
                             property_id_desired=4, amount_desired=1), ALICE),
        tx(4, ask(PID, 0, COIN), ALICE),
        tx(5, ask(99, COIN, COIN), ALICE),
    ])
    assert "two different sides" in reason(db, 2)
    assert "this chain's coin" in reason(db, 3)
    assert "out of range" in reason(db, 4)
    assert "property 99 does not exist" in reason(db, 5)
    assert book(db) == []


def test_a_cancel_gives_back_what_was_held(world):
    engine, state, db, PID = world
    feed(engine, state, [
        tx(2, ask(PID, 100 * COIN, 1 * COIN), ALICE),
        tx(3, ask(PID, 50 * COIN, 1 * COIN), ALICE),      # a different price
        tx(4, ask(PID, 200 * COIN, 2 * COIN), ALICE),     # same price as the first
    ])
    assert len(book(db)) == 3
    assert held(db, ALICE, PID) == (650 * COIN, 350 * COIN)

    # One price: 1 coin for 100 and 2 for 200 are the same price, and both go.
    feed(engine, state, [tx(5, P.MetaDExCancelPrice(
        property_id_for_sale=PID, amount_for_sale=100 * COIN,
        property_id_desired=0, amount_desired=1 * COIN), ALICE)])
    assert reason(db, 5) == "valid"
    assert [o["sale_amount"] for o in book(db)] == [50 * COIN]
    assert held(db, ALICE, PID) == (950 * COIN, 50 * COIN)

    # And the rest of the pair.
    feed(engine, state, [tx(6, P.MetaDExCancelPair(
        property_id_for_sale=PID, property_id_desired=0), ALICE)])
    assert book(db) == []
    assert held(db, ALICE, PID) == (1000 * COIN, 0), "everything is spendable again"

    # A cancel that matches nothing is refused rather than silently doing nothing.
    feed(engine, state, [tx(7, P.MetaDExCancelPair(
        property_id_for_sale=PID, property_id_desired=0), ALICE)])
    assert "no order of yours" in reason(db, 7)


def test_only_your_own_orders_are_yours_to_cancel(world):
    engine, state, db, PID = world
    feed(engine, state, [
        tx(2, ask(PID, 100 * COIN, 1 * COIN), ALICE),
        tx(3, P.MetaDExCancelPair(property_id_for_sale=PID, property_id_desired=0), BOB),
    ])
    assert "no order of yours" in reason(db, 3)
    assert len(book(db)) == 1 and held(db, ALICE, PID) == (900 * COIN, 100 * COIN)


def test_a_reorg_puts_the_book_back(world):
    engine, state, db, PID = world
    feed(engine, state, [tx(2, ask(PID, 100 * COIN, 1 * COIN), ALICE, height=200)])
    assert len(book(db)) == 1 and held(db, ALICE, PID) == (900 * COIN, 100 * COIN)
    state.rollback_block(200)
    assert book(db) == [] and held(db, ALICE, PID) == (1000 * COIN, 0)
