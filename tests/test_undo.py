"""The undo journal must reverse any block exactly, leaving no trace."""

import pytest

from arcade.db import StateError


def balances(db):
    return {
        (r["address"], r["property_id"]): r["amount"]
        for r in db.conn.execute("SELECT * FROM balance")
    }


def test_insert_is_undone(db, state):
    with state.block_context(1, "aa", "00", 100, 0, 0):
        state.insert("balance", {"address": "A", "property_id": 3, "amount": 50})
    assert balances(db) == {("A", 3): 50}

    state.rollback_block(1)
    assert balances(db) == {}
    assert db.tip() is None


def test_update_is_undone_to_prior_value(db, state):
    with state.block_context(1, "aa", "00", 100, 0, 0):
        state.insert("balance", {"address": "A", "property_id": 3, "amount": 50})
    with state.block_context(2, "bb", "aa", 200, 0, 0):
        state.update("balance", {"address": "A", "property_id": 3}, {"amount": 75})

    assert balances(db) == {("A", 3): 75}
    state.rollback_block(2)
    assert balances(db) == {("A", 3): 50}, "update must restore the prior amount, not delete"
    assert db.tip()["height"] == 1


def test_delete_is_undone(db, state):
    with state.block_context(1, "aa", "00", 100, 0, 0):
        state.insert("balance", {"address": "A", "property_id": 3, "amount": 50})
    with state.block_context(2, "bb", "aa", 200, 0, 0):
        state.delete("balance", {"address": "A", "property_id": 3})

    assert balances(db) == {}
    state.rollback_block(2)
    assert balances(db) == {("A", 3): 50}


def test_insert_then_update_in_one_block_unwinds_in_reverse(db, state):
    """The ordering case: undoing the insert before the update would leave a row."""
    with state.block_context(1, "aa", "00", 100, 0, 0):
        state.insert("balance", {"address": "A", "property_id": 3, "amount": 10})
        state.update("balance", {"address": "A", "property_id": 3}, {"amount": 20})
        state.update("balance", {"address": "A", "property_id": 3}, {"amount": 30})

    assert balances(db) == {("A", 3): 30}
    applied = state.rollback_block(1)
    assert applied == 3
    assert balances(db) == {}, "reverse-order replay must remove the row entirely"


def test_delete_then_reinsert_in_one_block(db, state):
    with state.block_context(1, "aa", "00", 100, 0, 0):
        state.insert("balance", {"address": "A", "property_id": 3, "amount": 10})
    with state.block_context(2, "bb", "aa", 200, 0, 0):
        state.delete("balance", {"address": "A", "property_id": 3})
        state.insert("balance", {"address": "A", "property_id": 3, "amount": 99})

    assert balances(db) == {("A", 3): 99}
    state.rollback_block(2)
    assert balances(db) == {("A", 3): 10}


def test_rollback_to_unwinds_many_blocks(db, state):
    for height in range(1, 6):
        with state.block_context(height, f"h{height}", f"h{height - 1}", height * 100, 0, 0):
            state.insert("balance", {"address": f"A{height}", "property_id": 3, "amount": height})

    assert len(balances(db)) == 5
    state.rollback_to(2)
    assert db.tip()["height"] == 2
    assert set(balances(db)) == {("A1", 3), ("A2", 3)}


def test_failed_block_leaves_nothing_behind(db, state):
    """A block that raises mid-way must not be half-applied."""
    with pytest.raises(StateError):
        with state.block_context(1, "aa", "00", 100, 0, 0):
            state.insert("balance", {"address": "A", "property_id": 3, "amount": 10})
            state.insert("balance", {"address": "A", "property_id": 3, "amount": 20})  # dup PK

    assert balances(db) == {}
    assert db.tip() is None, "the block row itself must roll back too"


def test_mutation_outside_block_context_is_refused(db, state):
    with pytest.raises(StateError, match="only allowed inside block_context"):
        state.insert("balance", {"address": "A", "property_id": 3, "amount": 10})


def test_unregistered_table_is_refused(db, state):
    with state.block_context(1, "aa", "00", 100, 0, 0):
        with pytest.raises(StateError, match="not registered for journalling"):
            state.insert("meta", {"key": "x", "value": "y"})
