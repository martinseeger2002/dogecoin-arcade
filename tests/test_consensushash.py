"""The consensus hash: determinism, ordering, and the skip rules."""

import pytest

from ribbit.config import REGTEST
from ribbit.consensushash import (
    balance_records,
    consensus_breakdown,
    consensus_hash,
    property_records,
)
from ribbit.db import Database, StateDB
from ribbit.state import Engine, install_schema


@pytest.fixture
def eng(tmp_path):
    db = Database(tmp_path / "ch.sqlite")
    install_schema(db)
    state = StateDB(db)
    yield state, Engine(state, REGTEST)
    db.close()


def seed(state, engine, entries):
    with state.block_context(1, "h1", "h0", 60, 0, 0):
        for address, property_id, amount in entries:
            engine.credit(address, property_id, amount)


def test_empty_state_hashes_the_empty_string(eng):
    state, engine = eng
    # SHA-256 of no input at all.
    assert consensus_hash(state.db) == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_hash_is_deterministic(eng):
    state, engine = eng
    seed(state, engine, [("addrB", 3, 10), ("addrA", 4, 20)])
    assert consensus_hash(state.db) == consensus_hash(state.db)


def test_balances_are_ordered_by_address_then_property(eng):
    state, engine = eng
    seed(state, engine, [
        ("zebra", 5, 1), ("alpha", 9, 2), ("alpha", 3, 3), ("mike", 7, 4),
    ])
    records = balance_records(state.db)
    assert records == [
        "alpha|3|3|0|0|0",
        "alpha|9|2|0|0|0",
        "mike|7|4|0|0|0",
        "zebra|5|1|0|0|0",
    ]


def test_insertion_order_does_not_affect_the_hash(eng, tmp_path):
    state_a, engine_a = eng
    seed(state_a, engine_a, [("addrA", 3, 10), ("addrB", 4, 20), ("addrC", 5, 30)])
    hash_a = consensus_hash(state_a.db)

    db_b = Database(tmp_path / "other.sqlite")
    install_schema(db_b)
    state_b = StateDB(db_b)
    engine_b = Engine(state_b, REGTEST)
    seed(state_b, engine_b, [("addrC", 5, 30), ("addrA", 3, 10), ("addrB", 4, 20)])
    assert consensus_hash(db_b) == hash_a
    db_b.close()


def test_fully_spent_balance_hashes_as_if_it_never_existed(eng, tmp_path):
    """The skip rule (consensushash.cpp:55) is load-bearing, not an optimisation."""
    state, engine = eng
    seed(state, engine, [("addrA", 3, 10), ("addrB", 4, 5)])
    with state.block_context(2, "h2", "h1", 120, 0, 0):
        engine.debit("addrB", 4, 5)      # leaves a row with all-zero buckets
    hash_with_zero_row = consensus_hash(state.db)

    db2 = Database(tmp_path / "clean.sqlite")
    install_schema(db2)
    state2 = StateDB(db2)
    engine2 = Engine(state2, REGTEST)
    seed(state2, engine2, [("addrA", 3, 10)])   # addrB never appears at all
    assert consensus_hash(db2) == hash_with_zero_row
    db2.close()


def test_different_balances_give_different_hashes(eng):
    state, engine = eng
    seed(state, engine, [("addrA", 3, 10)])
    before = consensus_hash(state.db)
    with state.block_context(2, "h2", "h1", 120, 0, 0):
        engine.credit("addrA", 3, 1)
    assert consensus_hash(state.db) != before


def test_reserve_buckets_are_part_of_the_hash(eng):
    state, engine = eng
    seed(state, engine, [("addrA", 3, 10)])
    before = consensus_hash(state.db)
    with state.block_context(2, "h2", "h1", 120, 0, 0):
        engine.credit("addrA", 3, 5, bucket="metadex_reserve")
    assert consensus_hash(state.db) != before
    assert balance_records(state.db) == ["addrA|3|10|0|0|5"]


def test_properties_section_hashes_id_and_issuer_only(eng):
    state, engine = eng
    from ribbit import payload as P
    from ribbit.tx import EncodingClass, RibbitTransaction

    msg = P.IssuanceFixed(
        ecosystem=1, property_type=2, previous_property_id=0,
        category="cat", subcategory="sub", name="Name", url="u", data="d", amount=5,
    )
    rtx = RibbitTransaction(
        txid="a" * 64, block_height=1, position=0, encoding_class=EncodingClass.C,
        sender="issuerAddr", reference="other", payload=msg.encode(), fee=0,
    )
    with state.block_context(1, "h1", "h0", 60, 1, 0):
        engine.process(rtx)

    assert property_records(state.db) == ["3|issuerAddr"]


def test_breakdown_totals_match_the_hash(eng):
    state, engine = eng
    seed(state, engine, [("addrA", 3, 10), ("addrB", 4, 20)])
    breakdown = consensus_breakdown(state.db)
    assert breakdown.total == consensus_hash(state.db)
    assert len(breakdown.records) == 2


def test_crowdsale_section_is_empty_and_contributes_nothing(eng):
    """D-008 dropped crowdsales; the empty section must not perturb the hash."""
    state, engine = eng
    seed(state, engine, [("addrA", 3, 10)])
    breakdown = consensus_breakdown(state.db)
    # An empty section digests to SHA-256 of nothing.
    assert breakdown.crowdsales == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
