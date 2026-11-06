"""Which collection roots this node uses, and the items each owns (arcade/collection_json.py for the format).

Read from this node's own inscription index, a root at a time in chain order (inscription number):

* a root whose parts are not all indexed yet waits and is tried again;
* a root that names an item an earlier root already owns is "claimed": not used (2026-10-17, first to publish);
* a root with "replaces" takes the place of that earlier root when, and only when, the same address published both
  (the operator: the same publisher corrects); the earlier one becomes "replaced" and its items are let go first.

Nothing here believes a page: every byte comes from the index, which read it from the chain.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import collection_json as C

SCHEMA = """
CREATE TABLE IF NOT EXISTS ext_root (
    txid       TEXT PRIMARY KEY,
    number     INTEGER NOT NULL,           -- the root's inscription number: chain order, first to publish
    publisher  TEXT NOT NULL,              -- the address that inscribed it
    network    TEXT NOT NULL,              -- where the root is inscribed
    status     TEXT NOT NULL,              -- active | waiting | claimed | replaced | refused
    reason     TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL DEFAULT '',
    chain      TEXT NOT NULL DEFAULT '',   -- where its items live
    supply     INTEGER NOT NULL DEFAULT 0,
    traits     TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    cover      TEXT NOT NULL DEFAULT '',
    missing    INTEGER NOT NULL DEFAULT 0,
    replaces   TEXT NOT NULL DEFAULT '',
    seen       REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ext_item (
    chain      TEXT NOT NULL,
    -- id: the item's inscription id on its chain
    id         TEXT NOT NULL,
    root       TEXT NOT NULL,
    number     INTEGER NOT NULL,
    traits     TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (chain, id)
);
CREATE INDEX IF NOT EXISTS ext_item_root ON ext_item(root, number);
CREATE TABLE IF NOT EXISTS ext_cursor (network TEXT PRIMARY KEY, number INTEGER NOT NULL);
"""


class Registry:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        with self._open() as conn:
            conn.executescript(SCHEMA)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # --- reading the chain ------------------------------------------------------------------------------------------

    def scan(self, index: Any, network: str, limit: int = 200) -> int:
        """Read new collection inscriptions on `network` and try every root still waiting. Returns roots settled."""
        with self._lock:
            with self._open() as conn:
                row = conn.execute("SELECT number FROM ext_cursor WHERE network=?", (network,)).fetchone()
                last = int(row["number"]) if row else -1
            with index.open() as db:
                fresh = db.conn.execute(
                    "SELECT txid, number, creator FROM inscription WHERE content_type=? AND number > ? "
                    "ORDER BY number LIMIT ?", (C.CONTENT_TYPE, last, int(limit))).fetchall()
            for r in fresh:
                last = max(last, int(r["number"]))
                body = _content(index, r["txid"])
                try:
                    said = json.loads(body) if body else {}
                except ValueError:
                    said = {}
                if isinstance(said, dict) and said.get("p") == C.ROOT:
                    with self._open() as conn:
                        conn.execute("INSERT OR IGNORE INTO ext_root(txid, number, publisher, network, status, seen) "
                                     "VALUES(?,?,?,?, 'waiting', ?)",
                                     (r["txid"], int(r["number"]), r["creator"] or "", network, time.time()))
            if fresh:
                with self._open() as conn:
                    conn.execute("INSERT OR REPLACE INTO ext_cursor(network, number) VALUES(?,?)", (network, last))
            with self._open() as conn:
                waiting = conn.execute("SELECT * FROM ext_root WHERE status='waiting' AND network=? ORDER BY number",
                                       (network,)).fetchall()
            settled = 0
            for root in waiting:
                if self._settle(index, dict(root)):
                    settled += 1
            return settled

    def _settle(self, index: Any, root: dict) -> bool:
        body = _content(index, root["txid"])
        try:
            said = C.parse_root(body or b"")
        except C.CollectionError as exc:
            return self._mark(root["txid"], "refused", str(exc))
        parts = []
        for pid in said["parts"]:
            raw = _content(index, pid)
            if raw is None:
                return False                                   # a part not indexed yet: tried again next pass
            try:
                parts.append(C.parse_part(raw, said["traits"]))
            except C.CollectionError as exc:
                return self._mark(root["txid"], "refused", f"part {pid[:12]}: {exc}")
        try:
            coll = C.assemble(said, parts)
        except C.CollectionError as exc:
            return self._mark(root["txid"], "refused", str(exc))
        with self._open() as conn:
            old = None
            if coll.replaces:
                old = conn.execute("SELECT * FROM ext_root WHERE txid=?", (coll.replaces,)).fetchone()
                if old is None or old["status"] != "active":
                    return self._mark(root["txid"], "refused", "it replaces a collection that is not in use")
                if old["publisher"] != root["publisher"]:
                    return self._mark(root["txid"], "refused", "only the address that published a collection replaces it")
            mine = {coll.replaces} if old is not None else set()
            for item in coll.items:
                owner = conn.execute("SELECT root FROM ext_item WHERE chain=? AND id=?",
                                     (coll.chain, item.id)).fetchone()
                if owner is not None and owner["root"] not in mine:
                    return self._mark(root["txid"], "claimed",
                                      f"{item.id} already belongs to collection {owner['root'][:12]}…")
            if old is not None:
                conn.execute("DELETE FROM ext_item WHERE root=?", (old["txid"],))
                conn.execute("UPDATE ext_root SET status='replaced', reason=? WHERE txid=?",
                             (f"replaced by {root['txid']}", old["txid"]))
            conn.executemany("INSERT INTO ext_item(chain, id, root, number, traits) VALUES(?,?,?,?,?)",
                             [(coll.chain, i.id, root["txid"], i.number, json.dumps(i.traits)) for i in coll.items])
            conn.execute("UPDATE ext_root SET status='active', reason='', name=?, chain=?, supply=?, traits=?, "
                         "description=?, cover=?, missing=?, replaces=? WHERE txid=?",
                         (coll.name, coll.chain, coll.supply, json.dumps(coll.traits), coll.description, coll.cover,
                          len(coll.missing), coll.replaces, root["txid"]))
        return True

    def _mark(self, txid: str, status: str, reason: str) -> bool:
        with self._open() as conn:
            conn.execute("UPDATE ext_root SET status=?, reason=? WHERE txid=?", (status, reason[:300], txid))
        return True

    # --- what pages read --------------------------------------------------------------------------------------------

    def active(self) -> list[dict]:
        with self._open() as conn:
            rows = conn.execute("SELECT r.*, (SELECT COUNT(*) FROM ext_item i WHERE i.root=r.txid) AS count "
                                "FROM ext_root r WHERE status='active' ORDER BY name COLLATE NOCASE").fetchall()
        return [{**dict(r), "traits": json.loads(r["traits"])} for r in rows]

    def root(self, txid: str) -> dict | None:
        with self._open() as conn:
            r = conn.execute("SELECT * FROM ext_root WHERE txid=?", (txid,)).fetchone()
        return {**dict(r), "traits": json.loads(r["traits"])} if r else None

    def items(self, root: str, offset: int = 0, limit: int = 60, ids: set[str] | None = None) -> list[dict]:
        with self._open() as conn:
            rows = conn.execute("SELECT * FROM ext_item WHERE root=? ORDER BY number LIMIT ? OFFSET ?",
                                (root, int(limit), int(offset))).fetchall() if ids is None else [
                r for r in conn.execute("SELECT * FROM ext_item WHERE root=? ORDER BY number", (root,))
                if r["id"] in ids]
        return [{**dict(r), "traits": json.loads(r["traits"])} for r in rows]

    def item(self, chain: str, item_id: str) -> dict | None:
        """Which collection an item belongs to, and its number and traits."""
        with self._open() as conn:
            r = conn.execute("SELECT i.*, r.name FROM ext_item i JOIN ext_root r ON r.txid=i.root "
                             "WHERE i.chain=? AND i.id=?", (chain, item_id)).fetchone()
        return {**dict(r), "traits": json.loads(r["traits"])} if r else None

    def items_of(self, chain: str, item_ids: list[str]) -> dict[str, dict]:
        """`item` for many at once (the Wallet's Doginals, 2026-11-06): {id: {root, number, name}}, one query per 500
        ids, traits left out."""
        out: dict[str, dict] = {}
        with self._open() as conn:
            for at in range(0, len(item_ids), 500):
                part = list(item_ids[at:at + 500])
                for r in conn.execute("SELECT i.id, i.root, i.number, r.name FROM ext_item i JOIN ext_root r ON "
                                      f"r.txid=i.root WHERE i.chain=? AND i.id IN ({','.join('?' * len(part))})",
                                      (chain, *part)):
                    out[r["id"]] = {"root": r["root"], "number": r["number"], "name": r["name"]}
        return out

    def all_roots(self) -> list[dict]:
        with self._open() as conn:
            return [dict(r) for r in conn.execute("SELECT txid, number, publisher, status, reason, name, chain "
                                                  "FROM ext_root ORDER BY number")]


def _content(index: Any, txid: str) -> bytes | None:
    try:
        found = index.inscription_content(txid)
    except Exception:                                          # noqa: BLE001 -- not readable just now
        return None
    return bytes(found[1]) if found else None
