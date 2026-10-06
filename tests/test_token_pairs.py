"""Token/token pairs (Params.token_pairs_from, 2026-10-06, the operator: "token to token
pairs on the token exchange so that people can exchange one token for another").

An order may sell one token for another. Both sides are balances the ledger
holds, so the engine matches a crossing order in its own block: the new order
takes the resting ones at THEIR price, best first and then oldest, partially if
need be, and whatever is left rests on the book with its tokens held back.
"""

import dataclasses

import pytest

from arcade import payload as P
from arcade.config import NETWORKS
from arcade.db import Database
from arcade.state import Engine, StateDB, install_schema
from arcade.tx import ArcadeTransaction, EncodingClass

ALICE = "mfWxJ45yp2SFn7UciZyNpvDKrzbhyfKrY8"
BOB = "mkHS9ne12qx9pS9VojpwU5xtRd4T7X7ZUt"
CAROL = "n2eMqTT929pb1RDNuqEnxdaLau1rxy3efi"


def issue(n, name, amount, sender):
    return tx(n, P.IssuanceFixed(ecosystem=2, property_type=1, previous_property_id=0,
                                 category="c", subcategory="s", name=name, url="",
                                 data="", amount=amount), sender)


def make_world(tmp_path, params):
    db = Database(tmp_path / "ledger.sqlite")
    install_schema(db)
    state = StateDB(db)
    engine = Engine(state, params)
    feed(engine, state, [issue(1, "LOGS", 1000, ALICE), issue(2, "GOLD", 1000, BOB),
                         issue(3, "ORE", 1000, CAROL)])
    ids = [r[0] for r in db.conn.execute("SELECT property_id FROM property ORDER BY property_id")]
    return engine, state, db, ids[-3], ids[-2], ids[-1]


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path, NETWORKS["regtest"])


def tx(n, message, sender, height=None):
    return ArcadeTransaction(
        txid=f"{n:064x}", block_height=height if height is not None else 100 + n,
        position=0, encoding_class=EncodingClass.C, sender=sender,
        reference=None, payload=message.encode(), fee=0)


def feed(engine, state, transactions):
    for t in transactions:
        with state.block_context(t.block_height, f"h{t.block_height}", "p", 0, 1, 0):
            engine.process(t)


def reason(db, n):
    row = db.conn.execute("SELECT valid, invalid_reason FROM arcade_tx WHERE txid=?",
                          (f"{n:064x}",)).fetchone()
    return row["invalid_reason"] or ("valid" if row["valid"] else "invalid")


def order(sell, amount, want, desired):
    return P.MetaDExTrade(property_id_for_sale=sell, amount_for_sale=amount,
                          property_id_desired=want, amount_desired=desired)


def book(db):
    return [dict(r) for r in db.conn.execute(
        "SELECT * FROM book_order ORDER BY block_height, position")]


def bal(db, address, pid):
    row = db.conn.execute("SELECT * FROM balance WHERE address=? AND property_id=?",
                          (address, pid)).fetchone()
    return (row["balance"], row["metadex_reserve"]) if row else (0, 0)


def trades(db):
    return [dict(r) for r in db.conn.execute("SELECT * FROM pair_trade ORDER BY txid, seq")]


def total(db, pid):
    return db.conn.execute("SELECT SUM(balance + metadex_reserve) FROM balance WHERE property_id=?",
                           (pid,)).fetchone()[0]


def test_before_its_height_a_token_pair_is_still_refused(tmp_path):
    params = dataclasses.replace(NETWORKS["regtest"], token_pairs_from=500)
    engine, state, db, LOGS, GOLD, _ = make_world(tmp_path, params)
    feed(engine, state, [tx(10, order(LOGS, 20, GOLD, 30), ALICE)])          # height 110
    assert "this chain's coin" in reason(db, 10)
    feed(engine, state, [tx(11, order(LOGS, 20, GOLD, 30), ALICE, height=500)])
    assert reason(db, 11) == "valid"
    assert bal(db, ALICE, LOGS) == (980, 20)


def test_an_order_with_nothing_to_meet_rests_and_holds_its_tokens(world):
    engine, state, db, LOGS, GOLD, _ = world
    feed(engine, state, [tx(10, order(LOGS, 20, GOLD, 30), ALICE)])
    assert reason(db, 10) == "valid"
    (o,) = book(db)
    assert (o["sale_property"], o["sale_amount"], o["want_property"], o["want_amount"], o["reserved"]) \
        == (LOGS, 20, GOLD, 30, 20)
    assert bal(db, ALICE, LOGS) == (980, 20)
    assert trades(db) == []


def test_a_crossing_order_fills_at_the_resting_price(world):
    """Alice asks 30 GOLD for 20 LOGS. Bob offers 40 GOLD for them: he pays
    Alice's price, 30, and keeps the 10 he did not need to spend."""
    engine, state, db, LOGS, GOLD, _ = world
    feed(engine, state, [tx(10, order(LOGS, 20, GOLD, 30), ALICE),
                         tx(11, order(GOLD, 40, LOGS, 20), BOB)])
    assert reason(db, 11) == "valid"
    assert bal(db, ALICE, GOLD) == (30, 0) and bal(db, ALICE, LOGS) == (980, 0)
    # Bob's 40 GOLD bought all 20 LOGS (all there was) for 30; 10 GOLD of his
    # order is left, resting at his own price (40 GOLD for 20 LOGS -> 10 for 5)
    assert bal(db, BOB, LOGS) == (20, 0)
    (rest,) = book(db)
    assert (rest["address"], rest["sale_property"], rest["sale_amount"], rest["want_amount"]) == (BOB, GOLD, 10, 5)
    assert bal(db, BOB, GOLD) == (960, 10)
    (t,) = trades(db)
    assert (t["taker"], t["maker"], t["gave_property"], t["gave"], t["got_property"], t["got"]) \
        == (BOB, ALICE, GOLD, 30, LOGS, 20)


def test_an_order_that_does_not_reach_the_price_rests_beside_it(world):
    engine, state, db, LOGS, GOLD, _ = world
    feed(engine, state, [tx(10, order(LOGS, 20, GOLD, 30), ALICE),
                         tx(11, order(GOLD, 20, LOGS, 20), BOB)])      # 1 GOLD each: too low
    assert len(book(db)) == 2 and trades(db) == []
    assert bal(db, BOB, GOLD) == (980, 20)


def test_a_partial_fill_keeps_the_resting_price_for_what_is_left(world):
    engine, state, db, LOGS, GOLD, _ = world
    feed(engine, state, [tx(10, order(LOGS, 20, GOLD, 30), ALICE),       # 1.5 GOLD each
                         tx(11, order(GOLD, 15, LOGS, 10), BOB)])        # takes 10 of them
    assert bal(db, BOB, LOGS) == (10, 0) and bal(db, ALICE, GOLD) == (15, 0)
    (rest,) = book(db)
    assert (rest["address"], rest["sale_amount"], rest["want_amount"], rest["reserved"]) == (ALICE, 10, 15, 10)
    assert bal(db, ALICE, LOGS) == (980, 10)


def test_best_price_first_then_oldest(world):
    engine, state, db, LOGS, GOLD, ORE = world
    feed(engine, state, [
        tx(10, order(LOGS, 10, GOLD, 20), ALICE),     # 2.0 each, first
        tx(11, order(LOGS, 10, GOLD, 15), ALICE),     # 1.5 each: best
        tx(12, order(LOGS, 10, GOLD, 20), ALICE),     # 2.0 each, second
        tx(13, order(GOLD, 35, LOGS, 20), BOB),       # buys 20 at up to 1.75 each
    ])
    got = [(t["maker_txid"][-2:], t["gave"], t["got"]) for t in trades(db)]
    # the 1.5 order fills fully (15 for 10); the 2.0 orders are dearer than
    # Bob's 1.75, so matching stops there and the rest of his GOLD rests
    assert got == [("0b", 15, 10)]
    rest = {o["txid"][-2:]: (o["sale_amount"], o["want_amount"]) for o in book(db)}
    assert rest == {"0a": (10, 20), "0c": (10, 20), "0d": (20, 12)}   # Bob's 20 GOLD left wants 12 LOGS (ceil 20*20/35)


def test_cheaper_resting_orders_fill_before_dearer_and_oldest_first_at_one_price(world):
    engine, state, db, LOGS, GOLD, _ = world
    feed(engine, state, [
        tx(10, order(LOGS, 10, GOLD, 20), ALICE),
        tx(11, order(LOGS, 10, GOLD, 20), ALICE),
        tx(12, order(LOGS, 10, GOLD, 15), ALICE),
        tx(13, order(GOLD, 100, LOGS, 25), BOB),      # 4 GOLD each: takes everything up to 25 LOGS
    ])
    got = [(t["maker_txid"][-2:], t["gave"], t["got"]) for t in trades(db)]
    # Bob's 100 GOLD: 15 for the cheap 10, then 20 for 10 from the older 2.0 order,
    # then 20 for the last 10 (the 25 desired is a price, not a cap: his GOLD keeps buying)
    assert got == [("0c", 15, 10), ("0a", 20, 10), ("0b", 20, 10)]
    assert bal(db, BOB, LOGS) == (30, 0)
    (rest,) = book(db)
    assert (rest["address"], rest["sale_amount"]) == (BOB, 45)


def test_own_orders_are_never_met(world):
    engine, state, db, LOGS, GOLD, _ = world
    feed(engine, state, [tx(10, order(LOGS, 20, GOLD, 30), ALICE)])
    # give Alice GOLD the easy way: Bob trades with her
    feed(engine, state, [tx(12, order(GOLD, 30, LOGS, 20), BOB)])
    assert bal(db, ALICE, GOLD)[0] == 30
    feed(engine, state, [tx(13, order(LOGS, 5, GOLD, 5), ALICE),
                         tx(14, order(GOLD, 30, LOGS, 5), ALICE)])   # would cross her own ask
    assert [t["txid"][-2:] for t in trades(db)] == ["0c"]
    assert len(book(db)) == 2


def test_nothing_is_made_or_lost(world):
    engine, state, db, LOGS, GOLD, _ = world
    before = (total(db, LOGS), total(db, GOLD))
    feed(engine, state, [
        tx(10, order(LOGS, 7, GOLD, 11), ALICE),
        tx(11, order(LOGS, 13, GOLD, 17), ALICE),
        tx(12, order(GOLD, 29, LOGS, 9), BOB),
        tx(13, order(GOLD, 3, LOGS, 2), BOB),
    ])
    assert (total(db, LOGS), total(db, GOLD)) == before
    reserved = {pid: db.conn.execute("SELECT SUM(metadex_reserve) FROM balance WHERE property_id=?",
                                     (pid,)).fetchone()[0] for pid in (LOGS, GOLD)}
    on_book = {pid: sum(o["reserved"] for o in book(db) if o["sale_property"] == pid) for pid in (LOGS, GOLD)}
    assert reserved == on_book, "every reserved token belongs to an order on the book"


def test_a_cancel_gives_back_what_is_left(world):
    engine, state, db, LOGS, GOLD, _ = world
    feed(engine, state, [tx(10, order(LOGS, 20, GOLD, 30), ALICE),
                         tx(11, order(GOLD, 15, LOGS, 10), BOB),
                         tx(12, P.MetaDExCancelPair(property_id_for_sale=LOGS, property_id_desired=GOLD), ALICE)])
    assert reason(db, 12) == "valid"
    assert book(db) == [] and bal(db, ALICE, LOGS) == (990, 0)


def test_a_reorg_puts_the_match_back(world):
    engine, state, db, LOGS, GOLD, _ = world
    feed(engine, state, [tx(10, order(LOGS, 20, GOLD, 30), ALICE, height=300)])
    feed(engine, state, [tx(11, order(GOLD, 30, LOGS, 20), BOB, height=301)])
    assert trades(db) and bal(db, BOB, LOGS) == (20, 0)
    state.rollback_block(301)
    assert trades(db) == [] and bal(db, BOB, LOGS) == (0, 0) and bal(db, BOB, GOLD) == (1000, 0)
    (o,) = book(db)
    assert o["address"] == ALICE and o["sale_amount"] == 20
    assert bal(db, ALICE, LOGS) == (980, 20)


def test_coin_pairs_are_unchanged_by_token_pairs(world):
    """A token/token order never meets a coin order and never shows in a coin book."""
    engine, state, db, LOGS, GOLD, _ = world
    feed(engine, state, [tx(10, order(LOGS, 20, 0, 5 * 10 ** 8), ALICE),
                         tx(11, order(GOLD, 40, LOGS, 20), BOB)])
    assert trades(db) == []
    assert len(book(db)) == 2
