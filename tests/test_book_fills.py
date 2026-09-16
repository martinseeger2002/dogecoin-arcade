"""Filling a standing order with a swap.

A resting ask holds its tokens in `metadex_reserve`, where a swap could not
reach them: the only way to fill one was to cancel it first, wait a block, and
trade with somebody who could still see a price you had just withdrawn.

A swap may now take from that reserve and reduce the order by what it took.
Which orders it takes from is a pure function of indexed state -- cheapest
first, then oldest, then by txid -- so every node picks the same ones without
the swap having to name them. What protects the maker is the price: an order is
a public promise to sell at a price, and its reserve may be spent at that price
or better, never worse (D-062). The maker signs the transaction too; this is the
rule that still holds when a wallet signs something it did not read closely.
"""

import pytest

from arcade import inscriptions as I
from arcade import payload as P
from arcade.config import NETWORKS, Params
from arcade.db import Database
from arcade.state import Engine, StateDB, install_schema
from arcade.tx import ArcadeTransaction, EncodingClass

ALICE = "mfWxJ45yp2SFn7UciZyNpvDKrzbhyfKrY8"     # the maker, with tokens
BOB = "mkHS9ne12qx9pS9VojpwU5xtRd4T7X7ZUt"       # the taker, with coins
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


def tx(n, message, sender, height=None, inputs=(), outputs=()):
    return ArcadeTransaction(
        txid=f"{n:064x}", block_height=height if height is not None else 100 + n,
        position=0, encoding_class=EncodingClass.C, sender=sender,
        reference=None, payload=message.encode(), fee=0,
        inputs=tuple(inputs), outputs=tuple(outputs))


def feed(engine, state, transactions):
    for transaction in transactions:
        with state.block_context(transaction.block_height,
                                 f"h{transaction.block_height}", "p", 0, 1, 0):
            engine.process(transaction)


def swap(n, pid, tokens, coins, height=None):
    """Alice gives tokens, Bob gives coins, in one transaction both signed."""
    message = P.AnyData(data=I.Swap(
        give=I.Leg(kind=I.LEG_TOKEN, property_id=pid, amount=tokens),
        take=I.Leg(kind=I.LEG_COINS, amount=coins)).encode())
    # The shape a real one has: the seller puts in one small output and gets
    # it back plus the coins; the buyer funds the rest and takes the change.
    return tx(n, message, ALICE, height=height,
              inputs=((ALICE, 1000), (BOB, 20 * COIN)),
              outputs=((ALICE, 1000 + coins), (BOB, 20 * COIN - coins - 1000)))


def ask(pid, amount, coins):
    return P.MetaDExTrade(property_id_for_sale=pid, amount_for_sale=amount,
                          property_id_desired=0, amount_desired=coins)


def book(db):
    return [dict(r) for r in db.conn.execute(
        "SELECT * FROM book_order ORDER BY block_height, position")]


def held(db, address, pid):
    row = db.conn.execute("SELECT * FROM balance WHERE address=? AND property_id=?",
                          (address, pid)).fetchone()
    return (row["balance"], row["metadex_reserve"]) if row else (0, 0)


def reason(db, n):
    row = db.conn.execute("SELECT valid, invalid_reason FROM arcade_tx WHERE txid=?",
                          (f"{n:064x}",)).fetchone()
    return row["invalid_reason"] or ("valid" if row["valid"] else "invalid")


# --- the fill -----------------------------------------------------------------

def test_a_swap_takes_the_whole_ask_off_the_book(world):
    engine, state, db, pid = world
    feed(engine, state, [tx(2, ask(pid, 1000 * COIN, 8 * COIN), ALICE)])
    assert held(db, ALICE, pid) == (0, 1000 * COIN), "all of it is reserved"

    feed(engine, state, [swap(3, pid, 1000 * COIN, 8 * COIN)])
    assert reason(db, 3) == "valid"
    assert book(db) == [], "the order is gone: nothing is left of it"
    assert held(db, ALICE, pid) == (0, 0), "the reserve went with the tokens"
    assert held(db, BOB, pid) == (1000 * COIN, 0)


def test_part_of_an_ask_leaves_the_rest_standing(world):
    """Reduced by what moved, at the price it was posted at."""
    engine, state, db, pid = world
    feed(engine, state, [tx(2, ask(pid, 1000 * COIN, 8 * COIN), ALICE)])
    feed(engine, state, [swap(3, pid, 300 * COIN, 240_000_000)])   # 0.008 each
    assert reason(db, 3) == "valid"

    rest = book(db)
    assert len(rest) == 1
    assert rest[0]["sale_amount"] == 700 * COIN
    assert rest[0]["reserved"] == 700 * COIN
    assert rest[0]["want_amount"] == 8 * COIN - 240_000_000
    assert held(db, BOB, pid) == (300 * COIN, 0)
    assert held(db, ALICE, pid) == (0, 700 * COIN)


def test_the_rest_is_never_left_cheaper_than_it_was(world):
    """Rounding goes the maker's way: what is left keeps at least its price."""
    engine, state, db, pid = world
    feed(engine, state, [tx(2, ask(pid, 3 * COIN, 2 * COIN), ALICE)])   # 2/3 each
    feed(engine, state, [swap(3, pid, 1 * COIN, 66_666_667)])
    rest = book(db)[0]
    from fractions import Fraction
    was = Fraction(2 * COIN, 3 * COIN)
    now = Fraction(rest["want_amount"], rest["sale_amount"])
    assert now >= was, f"{now} is cheaper than the {was} that was promised"


def test_a_swap_below_the_asking_price_gets_nothing(world):
    """The guard. An order's reserve is spendable at its price or better."""
    engine, state, db, pid = world
    feed(engine, state, [tx(2, ask(pid, 1000 * COIN, 8 * COIN), ALICE)])
    feed(engine, state, [swap(3, pid, 1000 * COIN, 7 * COIN)])
    assert "insufficient balance" in reason(db, 3)
    assert len(book(db)) == 1, "the order is untouched"
    assert held(db, BOB, pid) == (0, 0), "and nothing moved"


def test_a_better_price_is_allowed(world):
    engine, state, db, pid = world
    feed(engine, state, [tx(2, ask(pid, 1000 * COIN, 8 * COIN), ALICE)])
    feed(engine, state, [swap(3, pid, 1000 * COIN, 9 * COIN)])
    assert reason(db, 3) == "valid"
    assert book(db) == []


def test_the_cheapest_order_is_filled_first(world):
    """Deterministic, so two nodes reading the same block agree."""
    engine, state, db, pid = world
    feed(engine, state, [tx(2, ask(pid, 500 * COIN, 5 * COIN), ALICE),       # 0.01
                         tx(3, ask(pid, 500 * COIN, 375_000_000), ALICE)])   # 0.0075
    assert held(db, ALICE, pid) == (0, 1000 * COIN), "both are reserved"

    feed(engine, state, [swap(4, pid, 500 * COIN, 5 * COIN)])                # 0.01
    assert reason(db, 4) == "valid"
    left = book(db)
    assert len(left) == 1 and left[0]["want_amount"] == 5 * COIN, \
        "the 0.0075 order went first; the 0.01 one is still there whole"
    assert left[0]["sale_amount"] == 500 * COIN


def test_a_free_balance_is_spent_before_the_book(world):
    """An order is only drawn on for what the loose balance cannot cover."""
    engine, state, db, pid = world
    feed(engine, state, [tx(2, ask(pid, 600 * COIN, 6 * COIN), ALICE)])
    assert held(db, ALICE, pid) == (400 * COIN, 600 * COIN)
    feed(engine, state, [swap(3, pid, 400 * COIN, 4 * COIN)])
    assert reason(db, 3) == "valid"
    assert book(db)[0]["sale_amount"] == 600 * COIN, "the order was not touched"
    assert held(db, ALICE, pid) == (0, 600 * COIN)


def test_somebody_elses_order_is_not_yours_to_fill(world):
    engine, state, db, pid = world
    feed(engine, state, [tx(2, ask(pid, 1000 * COIN, 8 * COIN), ALICE)])
    message = P.AnyData(data=I.Swap(
        give=I.Leg(kind=I.LEG_TOKEN, property_id=pid, amount=1000 * COIN),
        take=I.Leg(kind=I.LEG_COINS, amount=8 * COIN)).encode())
    # Bob is the seller here, and Bob has nothing on the book.
    feed(engine, state, [tx(3, message, BOB, inputs=((BOB, 1000), (ALICE, 20 * COIN)),
                            outputs=((BOB, 8 * COIN + 1000), (ALICE, 11 * COIN)))])
    assert "insufficient balance" in reason(db, 3)
    assert len(book(db)) == 1


def test_an_nft_swap_does_not_touch_the_book(world):
    """Only a token sold for coins is a fill. Everything else is a swap."""
    engine, state, db, pid = world
    feed(engine, state, [tx(2, ask(pid, 1000 * COIN, 8 * COIN), ALICE)])
    message = P.AnyData(data=I.Swap(
        give=I.Leg(kind=I.LEG_INSCRIPTION, txid=bytes.fromhex("ab" * 32)),
        take=I.Leg(kind=I.LEG_COINS, amount=8 * COIN)).encode())
    feed(engine, state, [tx(3, message, ALICE, inputs=((ALICE, 1000), (BOB, 20 * COIN)),
                            outputs=((ALICE, 8 * COIN + 1000), (BOB, 11 * COIN)))])
    assert "no such inscription" in reason(db, 3)
    assert len(book(db)) == 1 and book(db)[0]["reserved"] == 1000 * COIN


# --- the height it starts at --------------------------------------------------

def test_before_its_block_the_reserve_is_still_locked(tmp_path):
    """The rule makes valid what used to be invalid, so it starts at a height
    both nodes have agreed on rather than whenever somebody updates."""
    db = Database(tmp_path / "ledger.sqlite")
    install_schema(db)
    state = StateDB(db)
    params = Params(**{**NETWORKS["regtest"].__dict__, "fills_from": 500})
    engine = Engine(state, params)
    feed(engine, state, [tx(1, P.IssuanceFixed(
        ecosystem=2, property_type=2, previous_property_id=0, category="c",
        subcategory="s", name="Arcade Test", url="", data="", amount=1000 * COIN),
        ALICE, height=101)])
    pid = db.conn.execute("SELECT MAX(property_id) FROM property").fetchone()[0]
    feed(engine, state, [tx(2, ask(pid, 1000 * COIN, 8 * COIN), ALICE, height=102)])

    feed(engine, state, [swap(3, pid, 1000 * COIN, 8 * COIN, height=499)])
    assert "insufficient balance" in reason(db, 3), "not yet"
    feed(engine, state, [swap(4, pid, 1000 * COIN, 8 * COIN, height=500)])
    assert reason(db, 4) == "valid", "from this block on"
