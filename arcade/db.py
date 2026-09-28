"""SQLite storage, schema migrations, and the undo journal.

The undo journal is the heart of reorg safety. Every mutation to protocol state
goes through `StateDB`, which records enough information to reverse it. A reorg
is then handled by replaying the journal backwards, in exact reverse order, for
each disconnected block.

Rules this module enforces:
  * mutations are only legal inside `block_context(height)`
  * every mutation records an undo entry in the same transaction
  * rollback is exact reverse order (journal id descending)
"""

from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

# Tables whose mutations are journalled, mapped to their primary-key columns.
# A table not registered here cannot be mutated through StateDB -- deliberate, so
# that adding protocol state in a later milestone is a conscious act rather than
# something that silently escapes the undo journal.
JOURNALLED_TABLES: dict[str, tuple[str, ...]] = {}


def register_journalled_table(name: str, pk_columns: tuple[str, ...]) -> None:
    """Allow `name` to be mutated through StateDB, keyed by `pk_columns`.

    Milestones M2+ call this for each protocol state table they add.
    """
    if not pk_columns:
        raise StateError(f"table {name!r} needs at least one primary-key column")
    JOURNALLED_TABLES[name] = pk_columns


SCHEMA = """
-- Blocks Arcade has processed. This is our view of the chain, which may lag or
-- briefly disagree with the node during a reorg.
CREATE TABLE IF NOT EXISTS block (
    height       INTEGER PRIMARY KEY,
    hash         TEXT    NOT NULL UNIQUE,
    prev_hash    TEXT    NOT NULL,
    time         INTEGER NOT NULL,
    tx_count     INTEGER NOT NULL,
    processed_at INTEGER NOT NULL
);

-- No index on block(hash): `hash TEXT NOT NULL UNIQUE` already builds one, and
-- a second copy of it cost 0.8 MB in a 13 MB index for nothing (D-113).

-- The undo journal. One row per state mutation, ordered by `id`.
--
-- op      : 'insert' | 'update' | 'delete'
-- pk_json : JSON object of the row's primary key columns
-- old_json: JSON object of the row as it was BEFORE the mutation.
--           NULL for 'insert', because there was no prior row.
CREATE TABLE IF NOT EXISTS undo (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    height    INTEGER NOT NULL,
    tbl       TEXT    NOT NULL,
    op        TEXT    NOT NULL CHECK (op IN ('insert', 'update', 'delete')),
    pk_json   TEXT    NOT NULL,
    old_json  TEXT
);

CREATE INDEX IF NOT EXISTS undo_height_idx ON undo(height);

-- How far back the journal is kept. A reorg deeper than ChainFollower's
-- max_reorg_depth is refused outright as something a retry cannot fix, so an
-- undo row older than that can never be used -- and kept for ever it was the
-- only part of this index that grew without bound (D-113).


-- Single-row key/value metadata (schema version, indexed tip, etc).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


#: The journal stores a row as JSON so it can be replayed against any table.
#: JSON has no bytes, and a BLOB column is an ordinary thing for a table to
#: have -- an inscription's content is one. Rather than forbid blobs in
#: journalled tables, or make every such column hex and double its size on
#: disk, bytes are tagged on the way in and restored on the way out. The tag is
#: a two-key object no natural JSON value collides with.
_BYTES_TAG = "__bytes__"


def _to_json(row: dict[str, Any]) -> str:
    def default(value: Any) -> Any:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {_BYTES_TAG: base64.b64encode(bytes(value)).decode("ascii")}
        raise TypeError(f"cannot journal a {type(value).__name__}")

    return json.dumps(row, sort_keys=True, default=default)


def _from_json(text: str) -> dict[str, Any]:
    def hook(obj: dict[str, Any]) -> Any:
        if len(obj) == 1 and _BYTES_TAG in obj:
            return base64.b64decode(obj[_BYTES_TAG])
        return obj

    return json.loads(text, object_hook=hook)


class StateError(Exception):
    """A misuse of the state layer, such as mutating outside a block context."""


def without_comment(line: str) -> str:
    """A schema line with its `--` comment taken off and its comma off the end.

    The comment has to go before the declaration is cut out of the line, and it
    has to be found with the quotes respected. Left where it was written it
    rides into the `ALTER TABLE`, and the comma that was behind the column stays
    in front of it -- `ADD COLUMN "coins" INTEGER NOT NULL,` -- which SQLite
    answers "incomplete input". It did exactly that to every live swaps.sqlite
    on 2026-09-27, because `fill` and `bid` write their comments behind their
    columns rather than above them, and the sweep runs at startup.

    A comment only line has nothing left and comes back empty, which is how the
    caller skips it. A `--` inside a quoted default (`DEFAULT 'a--b'`) is part of
    the value and stays.
    """
    quote = None
    i = 0
    while i < len(line):
        char = line[i]
        if quote:
            if char == quote and line[i + 1:i + 2] == quote:
                i += 2                    # '' is one quote escaped, not the end of the literal
                continue
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "-" and line[i + 1:i + 2] == "-":
            return line[:i].strip().rstrip(",").strip()
        i += 1
    return line.strip().rstrip(",").strip()


def add_missing_columns(conn, schema_sql: str) -> list[str]:
    """Bring an existing database up to the columns its schema declares.

    `CREATE TABLE IF NOT EXISTS` does nothing to a table it finds, so a column
    added to a schema reaches new installations and silently misses every
    existing one. That bug cannot be seen from a test suite: tests build their
    database from nothing every time, so the newest install is the one that
    works and the oldest is the one that breaks (D-081).

    It cost a live trade tonight -- `offer` gained an `order` column, both
    machines had been running since before it, and the maker's node answered
    "table offer has no column named order" after the taker had already paid a
    message fee to ask. A sweep then found the mintpad's three columns missing
    from `job` in the same way, on a wallet that had made collections before
    they existed.

    So it is done by reading the schema rather than by remembering: every
    declared column that a present table lacks is added with its own
    declaration -- the line with its comment taken off, which is what
    `without_comment` is for. Columns carrying PRIMARY KEY, UNIQUE or
    REFERENCES are skipped -- SQLite cannot ALTER those in, and a table that
    needs one needs rebuilding rather than patching. Returns what it added, for
    the log.
    """
    import re

    added = []
    have_tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for block in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);",
                             schema_sql, re.S):
        table, body = block.group(1), block.group(2)
        if table not in have_tables:
            continue                      # CREATE made it in full; nothing to do
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for line in body.splitlines():
            line = without_comment(line)
            if not line:
                continue
            upper = line.upper()
            if upper.startswith(("PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK")):
                continue
            name = line.split()[0].strip('"')
            if name in present:
                continue
            declaration = line[len(line.split()[0]):].strip()
            if any(word in upper for word in ("PRIMARY KEY", "UNIQUE", "REFERENCES")):
                continue                  # not addable; a rebuild, not a patch
            conn.execute(f'ALTER TABLE {table} ADD COLUMN "{name}" {declaration}')
            added.append(f"{table}.{name}")
    return added

class Database:
    """Owns the SQLite connection and schema."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        # WAL: readers (the web UI) never block the writer (the indexer).
        self.conn.execute("PRAGMA journal_mode=WAL")
        # FULL rather than NORMAL: a torn write after a crash would mean silently
        # wrong protocol state, which is far worse than a slower indexer.
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        self.conn.executescript(SCHEMA)
        # Was created beside the UNIQUE constraint that already indexes that
        # column. Dropped here rather than left, because an index nobody
        # needs is still written on every block (D-113).
        self.conn.execute("DROP INDEX IF EXISTS block_hash_idx")
        current = self.get_meta("schema_version")
        if current is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        elif int(current) != SCHEMA_VERSION:
            raise StateError(
                f"database at {self.path} has schema version {current}, "
                f"this build expects {SCHEMA_VERSION}"
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- meta -----------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # --- chain view -----------------------------------------------------------

    def tip(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM block ORDER BY height DESC LIMIT 1").fetchone()

    def block_at(self, height: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM block WHERE height = ?", (height,)).fetchone()


class StateDB:
    """Mutates protocol state while recording an undo entry for every change.

    All mutations must happen inside `block_context`. That is not bureaucracy: a
    mutation outside a block has no height to roll back to, so it would survive a
    reorg and silently corrupt state.
    """

    def __init__(self, db: Database):
        self.db = db
        self._height: int | None = None

    # --- block lifecycle ------------------------------------------------------

    @contextmanager
    def block_context(
        self,
        height: int,
        block_hash: str,
        prev_hash: str,
        block_time: int,
        tx_count: int,
        processed_at: int,
    ) -> Iterator["StateDB"]:
        """Apply one block atomically.

        The block row and every mutation it causes commit together, or not at
        all. A crash mid-block therefore leaves no partially-applied block.
        """
        if self._height is not None:
            raise StateError(f"already inside block {self._height}; cannot nest block contexts")

        conn = self.db.conn
        conn.execute("BEGIN IMMEDIATE")
        self._height = height
        try:
            conn.execute(
                "INSERT INTO block(height, hash, prev_hash, time, tx_count, processed_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (height, block_hash, prev_hash, block_time, tx_count, processed_at),
            )
            yield self
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            self._forget_old_undo(height)
            conn.execute("COMMIT")
        finally:
            self._height = None

    #: Kept in step with ChainFollower.max_reorg_depth, and deliberately a
    #: little deeper: the follower refuses anything past its own limit, so
    #: rows below this can never be replayed by anybody.
    UNDO_KEEP = 1_000

    def _forget_old_undo(self, height: int) -> None:
        """Drop journal rows a reorg could no longer reach.

        Inside the block's own transaction, so a crash cannot lose the block
        and keep the pruning or the other way round. Cheap: an index on
        height makes it a range delete, and most blocks delete nothing.
        """
        floor = height - self.UNDO_KEEP
        if floor > 0:
            self.db.conn.execute("DELETE FROM undo WHERE height < ?", (floor,))

    def _require_context(self) -> int:
        if self._height is None:
            raise StateError("state mutations are only allowed inside block_context()")
        return self._height

    @staticmethod
    def _pk_columns(table: str) -> tuple[str, ...]:
        try:
            return JOURNALLED_TABLES[table]
        except KeyError:
            raise StateError(
                f"table {table!r} is not registered for journalling; "
                f"call register_journalled_table() first"
            ) from None

    def _fetch(self, table: str, pk: dict[str, Any]) -> dict[str, Any] | None:
        where = " AND ".join(f"{col} = ?" for col in pk)
        row = self.db.conn.execute(
            f"SELECT * FROM {table} WHERE {where}", tuple(pk.values())
        ).fetchone()
        return dict(row) if row else None

    def _journal(self, table: str, op: str, pk: dict[str, Any], old: dict[str, Any] | None) -> None:
        self.db.conn.execute(
            "INSERT INTO undo(height, tbl, op, pk_json, old_json) VALUES(?, ?, ?, ?, ?)",
            (
                self._require_context(),
                table,
                op,
                _to_json(pk),
                _to_json(old) if old is not None else None,
            ),
        )

    # --- journalled mutations -------------------------------------------------

    def insert(self, table: str, row: dict[str, Any]) -> None:
        self._require_context()
        pk_cols = self._pk_columns(table)
        missing = [c for c in pk_cols if c not in row]
        if missing:
            raise StateError(f"insert into {table!r} missing primary-key column(s) {missing}")
        pk = {c: row[c] for c in pk_cols}

        if self._fetch(table, pk) is not None:
            raise StateError(f"insert into {table!r}: row {pk} already exists")

        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        self.db.conn.execute(
            f"INSERT INTO {table}({columns}) VALUES({placeholders})", tuple(row.values())
        )
        self._journal(table, "insert", pk, None)

    def update(self, table: str, pk: dict[str, Any], changes: dict[str, Any]) -> None:
        self._require_context()
        pk_cols = self._pk_columns(table)
        if set(pk) != set(pk_cols):
            raise StateError(f"update on {table!r} expects primary key {pk_cols}, got {tuple(pk)}")
        if not changes:
            raise StateError(f"update on {table!r} with no changes")

        old = self._fetch(table, pk)
        if old is None:
            raise StateError(f"update on {table!r}: no row {pk}")

        assignments = ", ".join(f"{col} = ?" for col in changes)
        where = " AND ".join(f"{col} = ?" for col in pk)
        self.db.conn.execute(
            f"UPDATE {table} SET {assignments} WHERE {where}",
            tuple(changes.values()) + tuple(pk.values()),
        )
        self._journal(table, "update", pk, old)

    def delete(self, table: str, pk: dict[str, Any]) -> None:
        self._require_context()
        pk_cols = self._pk_columns(table)
        if set(pk) != set(pk_cols):
            raise StateError(f"delete on {table!r} expects primary key {pk_cols}, got {tuple(pk)}")

        old = self._fetch(table, pk)
        if old is None:
            raise StateError(f"delete on {table!r}: no row {pk}")

        where = " AND ".join(f"{col} = ?" for col in pk)
        self.db.conn.execute(f"DELETE FROM {table} WHERE {where}", tuple(pk.values()))
        self._journal(table, "delete", pk, old)

    # --- reorg ----------------------------------------------------------------

    def rollback_block(self, height: int) -> int:
        """Undo every mutation made by the block at `height`, then forget it.

        Entries are replayed strictly in reverse insertion order. That ordering is
        load-bearing: if a block inserted a row and then updated it, undoing the
        update before the insert is the only order that leaves no trace.

        Returns the number of undo entries applied.
        """
        if self._height is not None:
            raise StateError("cannot roll back from inside a block context")

        conn = self.db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            entries = conn.execute(
                "SELECT * FROM undo WHERE height = ? ORDER BY id DESC", (height,)
            ).fetchall()

            for entry in entries:
                table = entry["tbl"]
                pk = _from_json(entry["pk_json"])
                old = _from_json(entry["old_json"]) if entry["old_json"] is not None else None
                where = " AND ".join(f"{col} = ?" for col in pk)

                if entry["op"] == "insert":
                    conn.execute(f"DELETE FROM {table} WHERE {where}", tuple(pk.values()))
                elif entry["op"] == "update":
                    assert old is not None
                    assignments = ", ".join(f"{col} = ?" for col in old)
                    conn.execute(
                        f"UPDATE {table} SET {assignments} WHERE {where}",
                        tuple(old.values()) + tuple(pk.values()),
                    )
                elif entry["op"] == "delete":
                    assert old is not None
                    columns = ", ".join(old)
                    placeholders = ", ".join("?" for _ in old)
                    conn.execute(
                        f"INSERT INTO {table}({columns}) VALUES({placeholders})",
                        tuple(old.values()),
                    )
                else:  # pragma: no cover -- guarded by a CHECK constraint
                    raise StateError(f"unknown undo op {entry['op']!r}")

            conn.execute("DELETE FROM undo WHERE height = ?", (height,))
            conn.execute("DELETE FROM block WHERE height = ?", (height,))
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        return len(entries)

    def rollback_to(self, height: int) -> int:
        """Roll back every block above `height`, newest first."""
        total = 0
        while True:
            tip = self.db.tip()
            if tip is None or tip["height"] <= height:
                return total
            total += self.rollback_block(tip["height"])
