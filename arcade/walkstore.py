"""A shared walk kept on disk, so a restart does not throw it away (2026-11-05: Flopcoin imports said "21 days";
no walk had finished in three days because every reload of the arcade started each one again from block zero).

The chain is cut into fixed CHUNKS rather than one piece per reader, so what is done stays done whoever reads next:
each chunk is read by two different readers (one when only one can read), every read is written down the moment it
comes back (`put_read`), and a walk that was cut short picks up its reads (`reads`) and asks only for the rest. A walk
is for a fixed set of addresses over a fixed lo..hi; imports made meanwhile wait for the next one (as before).
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS walk (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    network    TEXT NOT NULL,
    lo         INTEGER NOT NULL,
    hi         INTEGER NOT NULL,
    addresses  TEXT NOT NULL,                -- JSON list, sorted
    chunk      INTEGER NOT NULL,             -- blocks per chunk
    began      REAL NOT NULL,
    ended      REAL,                         -- NULL while it is not finished
    outcome    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS walk_read (
    walk       INTEGER NOT NULL,
    top        INTEGER NOT NULL,
    bottom     INTEGER NOT NULL,
    reader     TEXT NOT NULL,
    ok         INTEGER NOT NULL,
    found      TEXT NOT NULL DEFAULT '[]',   -- [[txid, vout, sats, height, address]]
    missing    INTEGER NOT NULL DEFAULT 0,   -- canaries the reader missed
    error      TEXT NOT NULL DEFAULT '',
    seconds    REAL NOT NULL DEFAULT 0,
    at         REAL NOT NULL,
    PRIMARY KEY (walk, top, reader)
);
"""

#: The fewest blocks in a chunk, and about how many chunks a walk is cut into: big enough that a reader's overhead
#: (a job posted and polled, block files opened) is small beside its reading, small enough that a restart loses little.
CHUNK_MIN = 20_000
CHUNKS_ABOUT = 48


def chunk_size(lo: int, hi: int) -> int:
    return max(CHUNK_MIN, math.ceil((int(hi) - int(lo) + 1) / CHUNKS_ABOUT))


def chunks(lo: int, hi: int, size: int) -> list[tuple[int, int]]:
    """[(top, bottom)] covering lo..hi, highest first."""
    out, top = [], int(hi)
    while top >= int(lo):
        bottom = max(int(lo), top - int(size) + 1)
        out.append((top, bottom))
        top = bottom - 1
    return out


class WalkStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript(SCHEMA)

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def start(self, network: str, lo: int, hi: int, addresses: list[str]) -> dict[str, Any]:
        with self._lock, self._db() as db:
            cur = db.execute("INSERT INTO walk (network, lo, hi, addresses, chunk, began) VALUES (?,?,?,?,?,?)",
                             (network, int(lo), int(hi), json.dumps(sorted(set(addresses))), chunk_size(lo, hi),
                              time.time()))
            return self.get(int(cur.lastrowid))

    def get(self, walk_id: int) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM walk WHERE id = ?", (int(walk_id),)).fetchone()
        return _walk(row) if row is not None else None

    def unfinished(self, network: str | None = None) -> list[dict[str, Any]]:
        """Walks cut short, oldest first."""
        with self._db() as db:
            rows = db.execute("SELECT * FROM walk WHERE ended IS NULL" + (" AND network = ?" if network else "")
                              + " ORDER BY id", (network,) if network else ()).fetchall()
        return [_walk(r) for r in rows]

    def put_read(self, walk_id: int, top: int, bottom: int, reader: str, ok: bool, found: list, missing: int = 0,
                 error: str = "", seconds: float = 0.0) -> None:
        """One reader's read of one chunk. A good read is never replaced by a failed one of the same reader."""
        with self._lock, self._db() as db:
            had = db.execute("SELECT ok FROM walk_read WHERE walk=? AND top=? AND reader=?",
                             (int(walk_id), int(top), reader)).fetchone()
            if had is not None and had["ok"] and not ok:
                return
            db.execute("INSERT OR REPLACE INTO walk_read (walk, top, bottom, reader, ok, found, missing, error, seconds,"
                       " at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (int(walk_id), int(top), int(bottom), reader, 1 if ok else 0, json.dumps(found or []),
                        int(missing or 0), str(error or "")[:200], float(seconds or 0), time.time()))

    def reads(self, walk_id: int) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM walk_read WHERE walk = ? ORDER BY top DESC, at", (int(walk_id),)).fetchall()
        return [{**dict(r), "ok": bool(r["ok"]), "found": json.loads(r["found"] or "[]")} for r in rows]

    def finish(self, walk_id: int, outcome: str) -> None:
        with self._lock, self._db() as db:
            db.execute("UPDATE walk SET ended = ?, outcome = ? WHERE id = ?", (time.time(), outcome[:200], int(walk_id)))

    def pace(self, network: str) -> float | None:
        """Blocks a second the last walks on `network` read, all readers together (reads done / their walk's time)."""
        with self._db() as db:
            rows = db.execute("SELECT w.began, w.ended, (SELECT SUM(r.top - r.bottom + 1) FROM walk_read r"
                              " WHERE r.walk = w.id AND r.ok) AS blocks FROM walk w WHERE w.network = ? AND"
                              " w.ended IS NOT NULL ORDER BY w.id DESC LIMIT 3", (network,)).fetchall()
        paces = [r["blocks"] / max(1.0, r["ended"] - r["began"]) for r in rows if r["blocks"]]
        return max(paces) if paces else None

    def forget_old(self, keep: int = 30) -> None:
        """Finished walks beyond the last `keep`, and their reads."""
        with self._lock, self._db() as db:
            old = [r[0] for r in db.execute("SELECT id FROM walk WHERE ended IS NOT NULL ORDER BY id DESC LIMIT -1 "
                                            "OFFSET ?", (int(keep),))]
            for wid in old:
                db.execute("DELETE FROM walk_read WHERE walk = ?", (wid,))
                db.execute("DELETE FROM walk WHERE id = ?", (wid,))


def _walk(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["addresses"] = json.loads(d["addresses"] or "[]")
    return d
