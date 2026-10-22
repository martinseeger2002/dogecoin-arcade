"""What this wallet remembers for an inscribed page.

Why the page cannot simply use localStorage
-------------------------------------------
An inscribed page runs in a sandboxed frame with an opaque origin -- no
cookies, no reach into the wallet -- and an opaque origin has no storage: the
browser throws on `localStorage` itself. The one flag that would give it
storage, `allow-same-origin`, would give it this wallet's origin, and with it
every page in the wallet. That is not a trade.

So the wallet remembers on the page's behalf. The page talks to the frame
around it (the viewer, which is the wallet's own page) and the viewer keeps
the data here, on disk, under the inscription's id. The page never learns
which wallet it is running in and cannot reach another page's data, because
the viewer knows which frame it is talking to and files everything under that
frame's inscription. The same page on the phone, over the tunnel, sees the
same data, which localStorage would not have given it either.

What it is not
--------------
Not the chain. Nothing here is inscribed, shared, or provable; it is this
wallet's memory of what a page told it, the way a browser's localStorage is
that browser's. A high score, a settings panel, a half-finished game. A page
that wants something durable and public inscribes it.
"""

from __future__ import annotations

import sqlite3
import time
from .db import add_missing_columns
from pathlib import Path

#: A key is a name, not a document.
MAX_KEY = 256
#: One value: enough for a saved game, not for a file. 64 KiB, as characters.
MAX_VALUE = 64 * 1024
#: Everything one page may keep, so a page in a loop cannot fill the disk.
MAX_TOTAL = 1024 * 1024
MAX_KEYS = 1000
#: Earlier values kept per key, newest first: a game that saved over its own player (a failed read
#: taken for a new player, 2026-10-20) can be brought back. Read through op "history"; capped so the
#: history of one page never holds more than MAX_TOTAL either.
HISTORY = 10


class StoreError(ValueError):
    """The page asked for something the store does not do."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS item(
    inscription TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated     REAL NOT NULL,
    PRIMARY KEY(inscription, key)
);
CREATE TABLE IF NOT EXISTS item_history(
    inscription TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated     REAL NOT NULL,
    replaced    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS item_history_by_key ON item_history(inscription, key, replaced);
"""


class PageStore:
    """Key-value memory per inscription, in one sqlite file."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        add_missing_columns(conn, SCHEMA)
        return conn

    def items(self, inscription: str) -> dict[str, str]:
        with self._open() as conn:
            rows = conn.execute("SELECT key, value FROM item WHERE inscription=? ORDER BY key",
                                (inscription,)).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set(self, inscription: str, key: str, value: str) -> None:
        key, value = str(key), str(value)
        if not key or len(key) > MAX_KEY:
            raise StoreError(f"a key is 1 to {MAX_KEY} characters")
        if len(value) > MAX_VALUE:
            raise StoreError(f"a value is at most {MAX_VALUE:,} characters")
        with self._open() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(key) + LENGTH(value)), 0) AS size "
                "FROM item WHERE inscription=? AND key<>?", (inscription, key)).fetchone()
            if row["n"] >= MAX_KEYS:
                raise StoreError(f"a page may keep at most {MAX_KEYS} keys")
            if row["size"] + len(key) + len(value) > MAX_TOTAL:
                raise StoreError(f"a page may keep at most {MAX_TOTAL:,} characters in all")
            self._keep(conn, inscription, key, value)
            conn.execute("INSERT INTO item(inscription, key, value, updated) VALUES(?,?,?,?) "
                         "ON CONFLICT(inscription, key) DO UPDATE SET value=excluded.value, "
                         "updated=excluded.updated", (inscription, key, value, time.time()))

    def remove(self, inscription: str, key: str) -> None:
        with self._open() as conn:
            self._keep(conn, inscription, str(key), None)
            conn.execute("DELETE FROM item WHERE inscription=? AND key=?", (inscription, str(key)))

    def clear(self, inscription: str) -> None:
        with self._open() as conn:
            for row in conn.execute("SELECT key FROM item WHERE inscription=?", (inscription,)).fetchall():
                self._keep(conn, inscription, row["key"], None)
            conn.execute("DELETE FROM item WHERE inscription=?", (inscription,))

    @staticmethod
    def _keep(conn: sqlite3.Connection, inscription: str, key: str, new: str | None) -> None:
        """File the value `key` is about to lose, then trim to HISTORY per key and MAX_TOTAL per page."""
        old = conn.execute("SELECT value, updated FROM item WHERE inscription=? AND key=?",
                           (inscription, key)).fetchone()
        if old is None or old["value"] == new:
            return
        conn.execute("INSERT INTO item_history(inscription, key, value, updated, replaced) VALUES(?,?,?,?,?)",
                     (inscription, key, old["value"], old["updated"], time.time()))
        conn.execute("DELETE FROM item_history WHERE rowid IN (SELECT rowid FROM item_history "
                     "WHERE inscription=? AND key=? ORDER BY replaced DESC, rowid DESC LIMIT -1 OFFSET ?)",
                     (inscription, key, HISTORY))
        rows = conn.execute("SELECT rowid, LENGTH(key) + LENGTH(value) AS n FROM item_history "
                            "WHERE inscription=? ORDER BY replaced DESC, rowid DESC", (inscription,)).fetchall()
        total, drop = 0, []
        for r in rows:
            total += r["n"]
            if total > MAX_TOTAL:
                drop.append(r["rowid"])
        conn.executemany("DELETE FROM item_history WHERE rowid=?", [(d,) for d in drop])

    def history(self, inscription: str, key: str | None = None) -> dict[str, list[dict]]:
        """Earlier values, newest first: {key: [{value, updated, replaced}, ...]}."""
        sql = "SELECT key, value, updated, replaced FROM item_history WHERE inscription=?"
        args: tuple = (inscription,)
        if key is not None:
            sql, args = sql + " AND key=?", (inscription, str(key))
        with self._open() as conn:
            rows = conn.execute(sql + " ORDER BY replaced DESC, rowid DESC", args).fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["key"], []).append(
                {"value": r["value"], "updated": r["updated"], "replaced": r["replaced"]})
        return out

    def size(self, inscription: str) -> int:
        with self._open() as conn:
            return int(conn.execute(
                "SELECT COALESCE(SUM(LENGTH(key) + LENGTH(value)), 0) FROM item "
                "WHERE inscription=?", (inscription,)).fetchone()[0])
