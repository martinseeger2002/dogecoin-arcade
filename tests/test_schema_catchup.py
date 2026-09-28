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

The catch-up broke in the same shape as the thing it fixes, on 2026-09-27: it
copied a column's whole line into the `ALTER TABLE`, comment included, and every
`swaps.sqlite` that already existed refused to open. So the tests here build the
OLD database -- including an old copy of every schema the package ships, taken
apart column by column -- rather than a new one.
"""

import importlib
import re
import sqlite3
from pathlib import Path

import pytest

import arcade
from arcade.db import add_missing_columns, without_comment

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


# --- the comment that came along into the ALTER -------------------------------

TABLE_BODY = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);", re.S)
NOT_A_COLUMN = ("PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK")


def schema_modules():
    """Every module in the package that declares a schema. Found by looking,
    for the reason in the test above: a list of them is a list somebody forgets
    to update, and the schemas that are not on it are the ones that break."""
    root = Path(arcade.__file__).parent
    modules = []
    for path in sorted(root.rglob("*.py")):
        if re.search(r"^SCHEMA\w*\s*=", path.read_text(), re.M):
            parts = path.relative_to(root).with_suffix("").parts
            modules.append("arcade." + ".".join(parts))
    return modules


def commented_tables(schema_sql):
    """Each table that writes a comment behind one of its columns, with the line
    an old database would be missing: (table, column, line number, lines).

    The last such column, because that is the newest one, which is the one that
    is missing from an old database. Key columns are passed over -- the sweep
    cannot ALTER those in, so there would be nothing to catch up.
    """
    found = []
    for block in TABLE_BODY.finditer(schema_sql):
        table, lines = block.group(1), block.group(2).splitlines()
        chosen = None
        for number, raw in enumerate(lines):
            line = without_comment(raw)
            if not line or "--" not in raw:
                continue
            upper = line.upper()
            if upper.startswith(NOT_A_COLUMN):
                continue
            if any(word in upper for word in ("PRIMARY KEY", "UNIQUE", "REFERENCES")):
                continue
            chosen = (line.split()[0].strip('"'), number)
        if chosen:
            found.append((table, chosen[0], chosen[1], lines))
    return found


def one_table(table, lines, drop=None):
    """One table from the schema's own lines, as a database of its own -- built
    from the declarations with their comments off and the commas put back by
    hand, which is what lets any one of them be left out. `drop` is the line
    number to leave out, and that is how an OLD database gets made. The last
    column is the awkward case, since its comma belongs to the line before it.
    """
    parts = [without_comment(raw) for raw in lines]
    if drop is not None:
        parts[drop] = ""
    body = ",\n".join(part for part in parts if part)
    conn = sqlite3.connect(":memory:")
    conn.execute(f"CREATE TABLE {table} (\n{body}\n);")
    return conn


def columns_of(conn, table):
    """What the table says about its columns, without their position: an
    `ADD COLUMN` lands at the end, where a new install might have written it in
    the middle. Type, nullability, default and key are what has to match."""
    return {tuple(row[1:]) for row in conn.execute(f"PRAGMA table_info({table})")}


def test_a_comment_behind_a_column_is_not_part_of_the_declaration(tmp_path):
    """`fill` and `bid` write their comments behind their columns rather than
    above them. The sweep copied the whole line, so the ALTER read
    `ADD COLUMN "coins" INTEGER NOT NULL, -- the most this wallet will pay`:
    SQLite saw the comma, said "incomplete input", and every swaps.sqlite that
    already existed failed to start on 2026-09-27. New databases, which take the
    CREATE and skip the ALTER, were fine."""
    conn = sqlite3.connect(tmp_path / "old.sqlite")
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS fill (id TEXT PRIMARY KEY, network TEXT NOT NULL);")
    schema = '''
CREATE TABLE IF NOT EXISTS fill (
    id      TEXT PRIMARY KEY,           -- the txid of the question
    network TEXT NOT NULL,
    coins   INTEGER NOT NULL,           -- the most this wallet will pay
    note    TEXT NOT NULL DEFAULT ''    -- the buyer's own words, if it has any
);
'''
    assert add_missing_columns(conn, schema) == ["fill.coins", "fill.note"]
    conn.execute("INSERT INTO fill (id, network, coins) VALUES ('00', 'test', 5)")
    assert conn.execute("SELECT note FROM fill").fetchone()[0] == "", "and its default came with it"


def test_two_dashes_inside_a_default_are_the_value_not_a_comment(tmp_path):
    """The quote, not the dash, decides where the declaration ends."""
    conn = sqlite3.connect(tmp_path / "old.sqlite")
    conn.executescript("CREATE TABLE IF NOT EXISTS thing (id TEXT PRIMARY KEY);")
    schema = """
CREATE TABLE IF NOT EXISTS thing (
    id   TEXT PRIMARY KEY,
    span TEXT NOT NULL DEFAULT 'a--b'
);
"""
    assert add_missing_columns(conn, schema) == ["thing.span"]
    conn.execute("INSERT INTO thing (id) VALUES ('x')")
    assert conn.execute("SELECT span FROM thing").fetchone()[0] == "a--b"


def test_the_schemas_are_actually_being_looked_at():
    """The sweep below is worth nothing if it found nowhere to run."""
    checked = [(module, table) for module in schema_modules()
               for table, *_ in commented_tables(importlib.import_module(module).SCHEMA)]
    assert len(checked) > 3, f"only {checked} -- the schemas with comments are not being found"


@pytest.mark.parametrize("module", schema_modules())
def test_the_sweep_runs_against_an_old_copy_of_every_schema(module):
    """Not a made-up schema: this takes each one the package ships, removes the
    column it would have to add back, and runs the real catch-up on it. Every
    schema that comments behind a column is in here, so the next one written
    that way is tested the day it is written."""
    source = importlib.import_module(module)
    tables = commented_tables(source.SCHEMA)
    if not tables:
        pytest.skip(f"{module} writes no comment behind a column")
    for table, column, number, lines in tables:
        old = one_table(table, lines, drop=number)
        fresh = one_table(table, lines)
        assert add_missing_columns(old, source.SCHEMA) == [f"{table}.{column}"], \
            f"{module}: {table}.{column} is what an old database lacks"
        assert columns_of(old, table) == columns_of(fresh, table), \
            f"{module}: {table} caught up to a different table than a new install gets"
