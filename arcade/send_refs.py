"""A page's own name for a send it asked for, so asking again is safe (2026-10-21).

A game's POST /r/send reached the node, the browser lost the reply ("Load failed"), and
the game asked again with a fresh POST -- and could have paid twice. A page may now name
each send with `ref` (1..64 characters of its own choosing). Asked again with the same
ref, by the same wallet on the same chain, the arcade answers with the FIRST request, as
it stands now, instead of filing a second one.

The key is (scope, network, ref). The scope is the wallet that would send: the signed-in
reader's address on the public path, "node" for the node's own wallet. Only a request
that was filed is remembered -- a refusal at once (a 4xx) is not, so a corrected retry is
judged afresh. Kept 7 days in a small SQLite file, so a restart forgets nothing.

Nothing here decides whether a send happens; it only stops the same one being asked twice.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator

#: How long a ref is remembered.
KEEP_FOR = 7 * 24 * 3600
#: A ref's length, in characters.
MAX_REF = 64
#: A node's own wallet, as a scope (an address is never this word).
NODE = "node"

SCHEMA = """
CREATE TABLE IF NOT EXISTS send_ref (
    scope       TEXT NOT NULL,             -- the sending wallet's address, or "node"
    network     TEXT NOT NULL,
    ref         TEXT NOT NULL,
    request_id  TEXT NOT NULL,
    source      TEXT NOT NULL,             -- reader (a question for a reader) | node (the approvals queue)
    told        TEXT NOT NULL,             -- the last known answer, JSON
    created     REAL NOT NULL,
    updated     REAL NOT NULL,
    PRIMARY KEY (scope, network, ref)
);
CREATE INDEX IF NOT EXISTS send_ref_id ON send_ref(request_id);
CREATE INDEX IF NOT EXISTS send_ref_created ON send_ref(created);
"""


def check_ref(value: Any) -> str | None:
    """The ref a body gave, None for none; ValueError with the limit for anything else."""
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_REF:
        raise ValueError(f"ref must be a string of 1 to {MAX_REF} characters, the page's own "
                         "name for this send (asked again with the same ref, it is not sent twice)")
    return value


class SendRefs:
    """The refs pages gave their sends, and the per-ref lock that makes a retry wait."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._mutex = threading.Lock()                       # only around the dict below
        self._locks: dict[tuple, list] = {}                  # key -> [lock, users]
        self._pruned = 0.0
        with self._open() as conn:
            conn.executescript(SCHEMA)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @contextlib.contextmanager
    def held(self, key: tuple) -> Iterator[None]:
        """One request per key at a time: a second POST with the same ref waits for the first.
        Only this key's lock is held while the first one files or sends."""
        with self._mutex:
            entry = self._locks.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        entry[0].acquire()
        try:
            yield
        finally:
            entry[0].release()
            with self._mutex:
                entry[1] -= 1
                if entry[1] <= 0:
                    self._locks.pop(key, None)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        kept = dict(row)
        kept["told"] = json.loads(kept["told"])
        return kept

    def find(self, scope: str, network: str, ref: str) -> dict | None:
        """The request this ref named, if it was filed within KEEP_FOR."""
        with self._open() as conn:
            row = conn.execute("SELECT * FROM send_ref WHERE scope=? AND network=? AND ref=? "
                               "AND created>?", (scope, network, ref, time.time() - KEEP_FOR)
                               ).fetchone()
        return self._row(row)

    def by_id(self, request_id: str) -> dict | None:
        with self._open() as conn:
            row = conn.execute("SELECT * FROM send_ref WHERE request_id=? AND created>? "
                               "ORDER BY created DESC LIMIT 1",
                               (request_id, time.time() - KEEP_FOR)).fetchone()
        return self._row(row)

    def remember(self, scope: str, network: str, ref: str, request_id: str, source: str,
                 told: dict) -> None:
        now = time.time()
        with self._open() as conn:
            conn.execute("INSERT OR REPLACE INTO send_ref(scope,network,ref,request_id,source,"
                         "told,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                         (scope, network, ref, request_id, source,
                          json.dumps(told, default=str), now, now))
            if now - self._pruned > 3600:
                conn.execute("DELETE FROM send_ref WHERE created<=?", (now - KEEP_FOR,))
                self._pruned = now

    def update(self, request_id: str, **changes: Any) -> None:
        """What became of a remembered request (status, txid, error), kept with it."""
        with self._open() as conn:
            rows = conn.execute("SELECT scope,network,ref,told FROM send_ref WHERE request_id=?",
                                (request_id,)).fetchall()
            for row in rows:
                told = {**json.loads(row["told"]), **changes}
                conn.execute("UPDATE send_ref SET told=?, updated=? WHERE scope=? AND network=? "
                             "AND ref=?", (json.dumps(told, default=str), time.time(),
                                           row["scope"], row["network"], row["ref"]))
