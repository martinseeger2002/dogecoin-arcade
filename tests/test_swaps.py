"""A swap: two parties trade in one transaction, or nothing moves."""

import pytest

from arcade import inscriptions as I
from arcade import payload as P
from arcade.config import NETWORKS
from arcade.db import Database
from arcade.script import address_to_bytes
from arcade.state import Engine, StateDB, install_schema
from arcade.tx import ArcadeTransaction, EncodingClass

SELLER = "mfWxJ45yp2SFn7UciZyNpvDKrzbhyfKrY8"   # regtest-style addresses, any
BUYER = "mkHS9ne12qx9pS9VojpwU5xtRd4T7X7ZUt"    # base58check with a 20-byte hash
OTHER = "mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn"


def hexid(n: int) -> str:
    return f"{n:064x}"


@pytest.fixture
def engine(tmp_path):
    db = Database(tmp_path / "ledger.sqlite")
    install_schema(db)
    state = StateDB(db)
    return Engine(state, NETWORKS["regtest"]), state, db


def feed(engine, state, transactions):
    for transaction in transactions:
        with state.block_context(transaction.block_height,
                                 f"hash{transaction.block_height}", "prev", 0, 1, 0):
            engine.process(transaction)


def tx(n, payload, sender, *, inputs=(), outputs=(), reference=None,
       encoding=EncodingClass.C, height=None):
    return ArcadeTransaction(
        txid=hexid(n), block_height=height if height is not None else 100 + n,
        position=0, encoding_class=encoding, sender=sender, reference=reference,
        payload=P.AnyData(data=payload).encode(), fee=0,
        inputs=tuple(inputs), outputs=tuple(outputs))


def inscribe(engine, state, n, owner):
    bodies = I.plan(b"art" * 10, "text/plain", '{"name":"Piece #%d"}' % n)
    assert len(bodies) == 1
    feed(engine, state, [ArcadeTransaction(
        txid=hexid(n), block_height=10 + n, position=0, encoding_class=EncodingClass.B,
        sender=owner, reference=None, payload=P.AnyData(data=bodies[0]).encode(), fee=0)])
    return bytes.fromhex(hexid(n))


def swap_payload(_buyer, give, take):
    # The buyer is read off the inputs, not the payload (inscriptions.Swap);
    # the argument stays so each test still says who it means to be buying.
    return I.Swap(give=give, take=take).encode()


def owner_of(db, txid: bytes) -> str:
    return db.conn.execute("SELECT owner FROM inscription WHERE txid=?",
                           (txid.hex(),)).fetchone()["owner"]


def status_of(db, n: int) -> str:
    row = db.conn.execute("SELECT invalid_reason FROM arcade_tx WHERE txid=?",
                          (hexid(n),)).fetchone()
    return row["invalid_reason"] or "valid"


def test_the_payload_round_trips():
    give = I.Leg(I.LEG_INSCRIPTION, txid=bytes(range(32)))
    take = I.Leg(I.LEG_COINS, amount=5 * 10 ** 8)
    raw = swap_payload(BUYER, give, take)
    parsed = I.parse(raw)
    assert isinstance(parsed, I.Swap)
    assert parsed.give == give and parsed.take == take
    assert I.parse(swap_payload(BUYER, I.Leg(I.LEG_TOKEN, property_id=7, amount=12),
                                give)).take == give
    # The biggest swap there is -- an inscription for an inscription -- fits
    # an OP_RETURN with the Class C marker and the AnyData type header: 76
    # bytes of payload is the ceiling (encoding.max_class_c_payload).
    from arcade.encoding import max_class_c_payload
    biggest = swap_payload(BUYER, give, I.Leg(I.LEG_INSCRIPTION, txid=bytes(32)))
    assert len(biggest) + 4 <= max_class_c_payload()
    assert len(P.AnyData(data=biggest).encode()) <= max_class_c_payload()
    with pytest.raises(I.InscriptionError, match="truncated"):
        I.parse(raw[:-1])
    with pytest.raises(I.InscriptionError, match="nothing after"):
        I.parse(raw + b"\x00")
    with pytest.raises(I.InscriptionError, match="unknown swap leg"):
        I.Leg(9).encode()


def test_an_inscription_for_coins(engine):
    eng, state, db = engine
    piece = inscribe(eng, state, 1, SELLER)
    payload = swap_payload(BUYER, I.Leg(I.LEG_INSCRIPTION, txid=piece),
                           I.Leg(I.LEG_COINS, amount=5 * 10 ** 8))
    feed(eng, state, [tx(2, payload, SELLER,
                         inputs=[(SELLER, 10 ** 6), (BUYER, 6 * 10 ** 8)],
                         outputs=[(None, 0), (SELLER, 5 * 10 ** 8 + 10 ** 6),
                                  (BUYER, 10 ** 6), (BUYER, 9 * 10 ** 7)])])
    assert status_of(db, 2) == "valid"
    assert owner_of(db, piece) == BUYER


def test_the_buyer_is_whoever_signed_second(engine):
    """No name in the payload: the first input that is not the seller's is
    the buyer, so the thing bought goes to the address that paid for it."""
    eng, state, db = engine
    piece = inscribe(eng, state, 1, SELLER)
    payload = swap_payload(BUYER, I.Leg(I.LEG_INSCRIPTION, txid=piece),
                           I.Leg(I.LEG_COINS, amount=5 * 10 ** 8))
    feed(eng, state, [tx(2, payload, SELLER,
                         inputs=[(SELLER, 10 ** 6), (OTHER, 6 * 10 ** 8), (BUYER, 1)],
                         outputs=[(None, 0), (SELLER, 5 * 10 ** 8 + 10 ** 6)])])
    assert status_of(db, 2) == "valid"
    assert owner_of(db, piece) == OTHER


def test_it_is_refused_when_the_coins_fall_short(engine):
    """The seller is paid net of what the seller put in: a buyer who routes
    the seller's own input back to them has paid nothing."""
    eng, state, db = engine
    piece = inscribe(eng, state, 1, SELLER)
    payload = swap_payload(BUYER, I.Leg(I.LEG_INSCRIPTION, txid=piece),
                           I.Leg(I.LEG_COINS, amount=5 * 10 ** 8))
    feed(eng, state, [tx(2, payload, SELLER,
                         inputs=[(SELLER, 5 * 10 ** 8), (BUYER, 10 ** 6)],
                         outputs=[(None, 0), (SELLER, 5 * 10 ** 8), (BUYER, 10 ** 6)])])
    assert "is paid 0 satoshis" in status_of(db, 2)
    assert owner_of(db, piece) == SELLER


def test_one_inscription_for_another_and_nothing_half_done(engine):
    eng, state, db = engine
    mine = inscribe(eng, state, 1, SELLER)
    yours = inscribe(eng, state, 2, BUYER)
    theirs = inscribe(eng, state, 3, OTHER)
    # The buyer offers something that is not theirs: neither side moves.
    bad = swap_payload(BUYER, I.Leg(I.LEG_INSCRIPTION, txid=mine),
                       I.Leg(I.LEG_INSCRIPTION, txid=theirs))
    feed(eng, state, [tx(4, bad, SELLER, inputs=[(SELLER, 10 ** 6), (BUYER, 10 ** 6)],
                         outputs=[(None, 0), (SELLER, 10 ** 6), (BUYER, 10 ** 6)])])
    assert "not " + BUYER + "'s to give" in status_of(db, 4)
    assert owner_of(db, mine) == SELLER and owner_of(db, theirs) == OTHER

    good = swap_payload(BUYER, I.Leg(I.LEG_INSCRIPTION, txid=mine),
                        I.Leg(I.LEG_INSCRIPTION, txid=yours))
    feed(eng, state, [tx(5, good, SELLER, inputs=[(SELLER, 10 ** 6), (BUYER, 10 ** 6)],
                         outputs=[(None, 0), (SELLER, 10 ** 6), (BUYER, 10 ** 6)])])
    assert status_of(db, 5) == "valid"
    assert owner_of(db, mine) == BUYER and owner_of(db, yours) == SELLER


def test_tokens_for_an_inscription(engine):
    eng, state, db = engine
    piece = inscribe(eng, state, 1, SELLER)
    feed(eng, state, [ArcadeTransaction(
        txid=hexid(2), block_height=50, position=0, encoding_class=EncodingClass.C,
        sender=BUYER, reference=None, fee=0,
        payload=P.IssuanceFixed(ecosystem=2, property_type=2, previous_property_id=0,
                                category="c", subcategory="s", name="Hat coin",
                                url="", data="", amount=1000).encode())])
    pid = db.conn.execute("SELECT MAX(property_id) FROM property").fetchone()[0]
    payload = swap_payload(BUYER, I.Leg(I.LEG_INSCRIPTION, txid=piece),
                           I.Leg(I.LEG_TOKEN, property_id=pid, amount=250))
    feed(eng, state, [tx(3, payload, SELLER, inputs=[(SELLER, 10 ** 6), (BUYER, 10 ** 6)],
                         outputs=[(None, 0), (SELLER, 10 ** 6), (BUYER, 10 ** 6)])])
    assert status_of(db, 3) == "valid"
    assert owner_of(db, piece) == BUYER
    assert eng.get_balance(SELLER, pid)["balance"] == 250
    assert eng.get_balance(BUYER, pid)["balance"] == 750


def test_class_b_and_one_party_and_early_swaps_are_refused(engine, tmp_path):
    eng, state, db = engine
    piece = inscribe(eng, state, 1, SELLER)
    payload = swap_payload(BUYER, I.Leg(I.LEG_INSCRIPTION, txid=piece),
                           I.Leg(I.LEG_COINS, amount=1))
    feed(eng, state, [tx(2, payload, SELLER, encoding=EncodingClass.B,
                         inputs=[(SELLER, 1), (BUYER, 1)], outputs=[(SELLER, 1)])])
    assert "Class C" in status_of(db, 2)
    feed(eng, state, [tx(3, swap_payload(SELLER, I.Leg(I.LEG_INSCRIPTION, txid=piece),
                                         I.Leg(I.LEG_COINS, amount=1)), SELLER,
                         inputs=[(SELLER, 1)], outputs=[(SELLER, 1)])])
    assert "two parties" in status_of(db, 3)
    assert owner_of(db, piece) == SELLER

    # A chain where swaps are not read yet, and one where they start later.
    from dataclasses import replace
    for params, reason in ((replace(NETWORKS["regtest"], swaps_from=None), "not read"),
                           (replace(NETWORKS["regtest"], swaps_from=10 ** 6), "from block")):
        other_db = Database(tmp_path / f"{reason[:3]}.sqlite")
        install_schema(other_db)
        other_state = StateDB(other_db)
        other = Engine(other_state, params)
        piece2 = inscribe(other, other_state, 1, SELLER)
        feed(other, other_state, [tx(2, swap_payload(BUYER, I.Leg(I.LEG_INSCRIPTION, txid=piece2),
                                                     I.Leg(I.LEG_COINS, amount=1)), SELLER,
                                     inputs=[(SELLER, 1), (BUYER, 2)], outputs=[(SELLER, 2)])])
        assert reason in status_of(other_db, 2)
        assert owner_of(other_db, piece2) == SELLER


def test_a_reorg_gives_both_sides_back(engine):
    eng, state, db = engine
    mine = inscribe(eng, state, 1, SELLER)
    yours = inscribe(eng, state, 2, BUYER)
    payload = swap_payload(BUYER, I.Leg(I.LEG_INSCRIPTION, txid=mine),
                           I.Leg(I.LEG_INSCRIPTION, txid=yours))
    feed(eng, state, [tx(3, payload, SELLER, inputs=[(SELLER, 10 ** 6), (BUYER, 10 ** 6)],
                         outputs=[(None, 0), (SELLER, 10 ** 6), (BUYER, 10 ** 6)],
                         height=200)])
    assert owner_of(db, mine) == BUYER
    state.rollback_block(200)
    assert owner_of(db, mine) == SELLER and owner_of(db, yours) == BUYER


def test_an_offer_is_written_down_and_a_bad_one_is_not(engine):
    """An offer is said on the chain so it reaches whoever holds the piece,
    published key or not (D-042). Nothing is locked by one."""
    eng, state, db = engine
    piece = inscribe(eng, state, 1, SELLER)

    good = I.Offer(txid=piece, take=I.Leg(I.LEG_COINS, amount=3 * 10 ** 8)).encode()
    feed(eng, state, [tx(2, good, BUYER, height=200)])
    assert status_of(db, 2) == "valid"
    (row,) = db.conn.execute("SELECT * FROM nft_offer").fetchall()
    assert row["inscription"] == piece.hex() and row["buyer"] == BUYER
    assert row["take_kind"] == I.LEG_COINS and row["take_amount"] == 3 * 10 ** 8
    assert owner_of(db, piece) == SELLER, "an offer moves nothing"

    # For something this chain has never seen, and for your own piece.
    missing = I.Offer(txid=bytes(32), take=I.Leg(I.LEG_COINS, amount=1)).encode()
    feed(eng, state, [tx(3, missing, BUYER)])
    assert "no such inscription" in status_of(db, 3)
    mine = I.Offer(txid=piece, take=I.Leg(I.LEG_COINS, amount=1)).encode()
    feed(eng, state, [tx(4, mine, SELLER)])
    assert "already yours" in status_of(db, 4)
    # A price of nothing cannot even be encoded, and a hand-made one that
    # carries zero anyway is refused by the engine.
    with pytest.raises(I.InscriptionError, match="needs an amount"):
        I.Leg(I.LEG_COINS, amount=0).encode()
    nothing = (I.MAGIC + bytes([I.VERSION, I.KIND_OFFER]) + piece
               + bytes([I.LEG_COINS]) + (0).to_bytes(8, "big"))
    feed(eng, state, [tx(5, nothing, BUYER)])
    assert "not an offer" in status_of(db, 5)
    assert db.conn.execute("SELECT COUNT(*) FROM nft_offer").fetchone()[0] == 1

    # A reorg takes it back with everything else in its block.
    state.rollback_block(200)
    assert db.conn.execute("SELECT COUNT(*) FROM nft_offer").fetchone()[0] == 0


def test_an_offer_fits_one_op_return():
    """So making one costs a flat fee and no dust."""
    from arcade.encoding import max_class_c_payload

    for take in (I.Leg(I.LEG_COINS, amount=10 ** 8),
                 I.Leg(I.LEG_TOKEN, property_id=65535, amount=10 ** 12)):
        raw = I.Offer(txid=bytes(range(32)), take=take).encode()
        assert len(P.AnyData(data=raw).encode()) <= max_class_c_payload()
        parsed = I.parse(raw)
        assert isinstance(parsed, I.Offer) and parsed.take == take
    with pytest.raises(I.InscriptionError, match="truncated offer"):
        I.parse(I.Offer(txid=bytes(32), take=I.Leg(I.LEG_COINS, amount=1)).encode()[:20])
