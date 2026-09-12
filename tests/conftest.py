import sqlite3

import pytest

from arcade.db import Database, StateDB, register_journalled_table
from arcade.regtest import RegtestNode

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
    database = Database(tmp_path / "arcade.sqlite")
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


@pytest.fixture
def no_nodes(monkeypatch):
    """Guarantee that nothing under test can reach a real node.

    `datadir=Path("/nonexistent")` looks airtight and is not: credential loading
    falls back to a default location, so on any machine with a node running the
    "offline" web tests quietly talked to it. They then passed or failed
    according to what happened to be running, which is the opposite of a test.
    a test machine found this because the same test failed there and passed here.

    Both doors are shut: explicit credentials, and the discovery fallback.
    """
    def refuse(*args, **kwargs):
        raise RuntimeError("no node available (test)")

    import arcade.config
    import arcade.discovery
    import arcade.web.state

    monkeypatch.setattr(arcade.config, "load_rpc_credentials", refuse)
    monkeypatch.setattr(arcade.web.state, "load_rpc_credentials", refuse,
                        raising=False)
    monkeypatch.setattr(arcade.discovery, "best", lambda *a, **k: None)
    return None
