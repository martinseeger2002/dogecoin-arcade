import sqlite3

import pytest

from ribbit.db import Database, StateDB, register_journalled_table
from ribbit.regtest import RegtestNode

# A toy state table, used to prove the undo journal works without waiting for the
# real protocol tables that arrive in M2.
BALANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS balance (
    address     TEXT    NOT NULL,
    property_id INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    PRIMARY KEY (address, property_id)
);
"""


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "ribbit.sqlite")
    database.conn.executescript(BALANCE_SCHEMA)
    register_journalled_table("balance", ("address", "property_id"))
    yield database
    database.close()


@pytest.fixture
def state(db):
    return StateDB(db)


@pytest.fixture(scope="session")
def regtest():
    """One regtest node shared across the session -- starting it is slow."""
    node = RegtestNode()
    try:
        node.start()
    except RuntimeError as exc:
        pytest.skip(f"regtest node unavailable: {exc}")
    yield node
    node.stop()
