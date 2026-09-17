"""A column added to a schema has to reach databases that already exist.

`CREATE TABLE IF NOT EXISTS` does nothing to a table it finds. So a column
added to a SCHEMA reaches new installations and silently misses every existing
one -- and a test suite cannot see it, because tests build their database from
nothing every time. The newest install is the one that works and the oldest is
the one that breaks, which is the worst way round.

It cost a live trade: `offer` gained an `order` column, both machines had been
running since before it, and the maker answered "table offer has no column
named order" after the taker had paid a message fee to ask. A sweep then found
the mintpad's three columns missing from `job` on the same machine.
"""

import importlib
import sqlite3
from pathlib import Path

import pytest

from arcade.db import add_missing_columns

#: Every module that keeps a database, and the schema it declares.
STORES = ["arcade.swap", "arcade.state", "arcade.approvals", "arcade.collections",
          "arcade.pagestore", "arcade.nodetalk", "arcade.messaging.store"]


def test_a_missing_column_is_added_from_the_schema_itself(tmp_path):
    """By reading the declaration, not by remembering to write a migration."""
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS thing (id TEXT PRIMARY KEY, name TEXT NOT NULL);")
    schema = '''
CREATE TABLE IF NOT EXISTS thing (
    id     TEXT PRIMARY KEY,
    name   TEXT NOT NULL,
    -- a comment, which is not a column
    later  TEXT NOT NULL DEFAULT '',
    count  INTEGER NOT NULL DEFAULT 0
);
'''
    assert add_missing_columns(conn, schema) == ["thing.later", "thing.count"]
    columns = {row[1] for row in conn.execute("PRAGMA table_info(thing)")}
    assert {"id", "name", "later", "count"} == columns
    assert add_missing_columns(conn, schema) == [], "and it is idempotent"


def test_a_table_that_is_not_there_is_left_to_the_schema(tmp_path):
    """CREATE TABLE made it in full; there is nothing to catch up."""
    conn = sqlite3.connect(tmp_path / "empty.sqlite")
    assert add_missing_columns(conn, "CREATE TABLE IF NOT EXISTS thing (\n  id TEXT\n);") == []


def test_a_key_column_is_not_patched_in(tmp_path):
    """SQLite cannot ALTER one in, and a table that needs one needs rebuilding
    -- which `inscription_move` is doing separately (D-080)."""
    conn = sqlite3.connect(tmp_path / "old.sqlite")
    conn.executescript("CREATE TABLE IF NOT EXISTS thing (id TEXT PRIMARY KEY);")
    schema = '''
CREATE TABLE IF NOT EXISTS thing (
    id    TEXT PRIMARY KEY,
    other TEXT UNIQUE,
    fine  TEXT NOT NULL DEFAULT ''
);
'''
    assert add_missing_columns(conn, schema) == ["thing.fine"], "the addable one only"


@pytest.mark.parametrize("module", STORES)
def test_every_store_catches_its_database_up(module):
    """Not a list somebody maintains: every store that installs a schema runs
    the catch-up beside it, so the next column added is not the next outage."""
    source = importlib.import_module(module)
    assert hasattr(source, "SCHEMA"), module
    text = Path(source.__file__).read_text()
    assert "add_missing_columns" in text, \
        f"{module} installs a schema without catching an older database up"
