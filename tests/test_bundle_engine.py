"""The bundle swap in the engine (2026-10-04, the operator: "You should be able to trade
multiple items" / "items and Gold" / "how many of that item"): two named parties,
several legs each way, all of it or none."""

import pytest

from arcade import inscriptions as I
from arcade import payload as P
from arcade.config import NETWORKS
from arcade.db import Database
from arcade.script import b58check_decode, b58check_encode, hash160
from arcade.state import Engine, StateDB, install_schema
from arcade.tx import ArcadeTransaction, EncodingClass

V = NETWORKS["regtest"].pubkeyhash_version
ALICE = b58check_encode(V, hash160(b"alice"))
BOB = b58check_encode(V, hash160(b"bob"))
EVE = b58check_encode(V, hash160(b"eve"))
COIN = 100_000_000


def raw(address):
    version, body = b58check_decode(address)
    return bytes([version]) + body


@pytest.fixture
def world(tmp_path):
    db = Database(tmp_path / "ledger.sqlite")
    install_schema(db)
    state = StateDB(db)
    return Engine(state, NETWORKS["regtest"]), state, db


def run(eng, state, n, payload, sender, cls=EncodingClass.B, inputs=(), outputs=()):
    rtx = ArcadeTransaction(txid=f"{n:064x}", block_height=100 + n, position=0,
                            encoding_class=cls, sender=sender, reference=None,
                            payload=payload, fee=0, inputs=inputs, outputs=outputs)
    with state.block_context(rtx.block_height, f"h{n}", "p", 0, 1, 0):
        return eng.process(rtx)


def piece(eng, state, n, owner):
    run(eng, state, n, P.AnyData(data=I.plan(f"piece {n}".encode(), "text/plain")[0]).encode(),
        owner, inputs=((owner, 1000),))
    return bytes.fromhex(f"{n:064x}")


def token(eng, state, n, owner, amount):
    msg = P.IssuanceFixed(ecosystem=1, property_type=2, previous_property_id=0,
                          category="c", subcategory="s", name=f"Token {n}", url="",
                          data="", amount=amount)
    assert run(eng, state, n, msg.encode(), owner, cls=EncodingClass.C).valid
    return max(r[0] for r in state.db.conn.execute("SELECT property_id FROM property"))


def owner(db, txid):
    return db.conn.execute("SELECT owner FROM inscription WHERE txid=?", (txid.hex(),)).fetchone()[0]


def balance(eng, address, pid):
    return eng.get_balance(address, pid)["balance"]


def bundle(give, take, a=ALICE, b=BOB):
    return P.AnyData(data=I.Bundle(a=raw(a), b=raw(b), give=tuple(give), take=tuple(take)).encode()).encode()


def both(extra=()):
    return ((ALICE, 50_000), (BOB, 50_000)) + tuple(extra)


def test_several_pieces_and_tokens_for_a_piece_move_together(world):
    eng, state, db = world
    s1, s2 = piece(eng, state, 1, ALICE), piece(eng, state, 2, ALICE)
    ring = piece(eng, state, 3, BOB)
    gold, logs = token(eng, state, 4, ALICE, 1000), token(eng, state, 5, ALICE, 500)
    give = [I.Leg(I.LEG_INSCRIPTION, txid=s1), I.Leg(I.LEG_INSCRIPTION, txid=s2),
            I.Leg(I.LEG_TOKEN, property_id=gold, amount=50),
            I.Leg(I.LEG_TOKEN, property_id=logs, amount=12)]
    take = [I.Leg(I.LEG_INSCRIPTION, txid=ring)]
    done = run(eng, state, 10, bundle(give, take), ALICE, inputs=both())
    assert done.valid, done.reason
    assert (owner(db, s1), owner(db, s2), owner(db, ring)) == (BOB, BOB, ALICE)
    assert (balance(eng, BOB, gold), balance(eng, BOB, logs)) == (50, 12)
    assert (balance(eng, ALICE, gold), balance(eng, ALICE, logs)) == (950, 488)


def test_one_leg_short_moves_nothing(world):
    eng, state, db = world
    s1 = piece(eng, state, 1, ALICE)
    ring = piece(eng, state, 2, BOB)
    gold = token(eng, state, 3, ALICE, 40)
    give = [I.Leg(I.LEG_INSCRIPTION, txid=s1), I.Leg(I.LEG_TOKEN, property_id=gold, amount=30),
            I.Leg(I.LEG_TOKEN, property_id=gold, amount=30)]
    done = run(eng, state, 10, bundle(give, [I.Leg(I.LEG_INSCRIPTION, txid=ring)]), ALICE,
               inputs=both())
    assert not done.valid and "insufficient" in done.reason, \
        "two legs of one token cannot each count the same balance"
    assert (owner(db, s1), owner(db, ring), balance(eng, ALICE, gold)) == (ALICE, BOB, 40)


def test_both_parties_sign_and_nobody_else(world):
    eng, state, db = world
    s1, ring = piece(eng, state, 1, ALICE), piece(eng, state, 2, BOB)
    give, take = [I.Leg(I.LEG_INSCRIPTION, txid=s1)], [I.Leg(I.LEG_INSCRIPTION, txid=ring)]
    alone = run(eng, state, 10, bundle(give, take), ALICE, inputs=((ALICE, 9000),))
    assert not alone.valid and "both parties" in alone.reason
    crowd = run(eng, state, 11, bundle(give, take), ALICE, inputs=both(((EVE, 1000),)))
    assert not crowd.valid and "nobody else" in crowd.reason
    assert (owner(db, s1), owner(db, ring)) == (ALICE, BOB)


def test_coins_count_what_the_transaction_leaves_the_taker(world):
    eng, state, db = world
    s1, s2 = piece(eng, state, 1, ALICE), piece(eng, state, 2, ALICE)
    give = [I.Leg(I.LEG_INSCRIPTION, txid=s1), I.Leg(I.LEG_INSCRIPTION, txid=s2)]
    take = [I.Leg(I.LEG_COINS, amount=2 * COIN)]
    short = run(eng, state, 10, bundle(give, take), ALICE, inputs=((ALICE, 1000), (BOB, 5 * COIN)),
                outputs=((ALICE, 1000 + COIN), (BOB, 4 * COIN - 1000)))
    assert not short.valid and "paid" in short.reason
    paid = run(eng, state, 11, bundle(give, take), ALICE, inputs=((ALICE, 1000), (BOB, 5 * COIN)),
               outputs=((ALICE, 1000 + 2 * COIN), (BOB, 3 * COIN - 1000)))
    assert paid.valid, paid.reason
    assert (owner(db, s1), owner(db, s2)) == (BOB, BOB)


def test_a_chain_that_has_not_announced_bundles_reads_none(tmp_path):
    from dataclasses import replace
    db = Database(tmp_path / "l.sqlite")
    install_schema(db)
    state = StateDB(db)
    eng = Engine(state, replace(NETWORKS["regtest"], bundles_from=None))
    s1, ring = piece(eng, state, 1, ALICE), piece(eng, state, 2, BOB)
    done = run(eng, state, 10, bundle([I.Leg(I.LEG_INSCRIPTION, txid=s1)],
                                      [I.Leg(I.LEG_INSCRIPTION, txid=ring)]), ALICE, inputs=both())
    assert not done.valid and "not read on this chain" in done.reason
    assert owner(db, s1) == ALICE


def test_the_wire_format_round_trips_and_refuses_junk():
    give = tuple(I.Leg(I.LEG_TOKEN, property_id=3 + i, amount=i + 1) for i in range(40))
    take = (I.Leg(I.LEG_COINS, amount=5), I.Leg(I.LEG_INSCRIPTION, txid=b"x" * 32))
    b = I.Bundle(a=raw(ALICE), b=raw(BOB), give=give, take=take)
    assert I.parse(b.encode()) == b
    assert I.parse(b.encode() + bytes(29)) == b, "Class B padding is read past"
    with pytest.raises(I.InscriptionError):
        I.parse(b.encode() + b"\x01")
    with pytest.raises(I.InscriptionError):
        I.Bundle(a=raw(ALICE), b=raw(BOB), give=(), take=take).encode()
