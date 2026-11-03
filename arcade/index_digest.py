"""Fingerprints of an index, so arcades can prove to each other that they read the chain the same way.

2026-10-17: "The different Dogecoin arcade nodes should share their index with each other so that
we can verify everyone has the same index. For tokens and inscriptions and what not." Every arcade
builds its index from the chain alone, so two honest arcades on the same rules hold the same rows; one
that does not -- a bug, an old version, a borrowed chain read wrong -- should be found, and found at the
block where it went different.

What a block did is written in the index with its height: the protocol transactions it carried (and
whether each was valid), the inscriptions it completed, the pieces it moved, the @names it gave out, the
trades, offers and asks it made and the tokens it created. (The @name table is not among them: it holds
each name's CURRENT claim, which moves to a later block when a name is claimed again -- the claims
themselves are protocol transactions, hashed with the rest. Found 2026-10-18 by the first live check:
a fresh index and .25's "differed" at a block whose transactions were identical.) A block's FINGERPRINT is a hash over those
rows, in a fixed order, columns that never change after the block only -- an inscription's current owner
moves with every later transfer, so the moves are hashed, not the owner. A RANGE's fingerprint is a hash
over its blocks' fingerprints, so two arcades compare a hundred thousand blocks in one message and, when
they differ, halve the range until the first block that differs is found (arcade/mesh/indexcheck.py).

Rows are read from what is already indexed, so an index built before this existed is fingerprinted just
the same, back to its first block. Nothing new is written into the index.
"""
from __future__ import annotations

import hashlib
import threading
from typing import Any

#: The consensus tables a block writes, the columns that are fixed once written, and the order rows go in.
TABLES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("arcade_tx", ("txid", "position", "encoding_class", "message_type", "message_version", "sender",
                   "reference", "payload_hex", "valid"), "position, txid"),
    ("inscription", ("txid", "number", "creator", "position", "content_type", "content_len", "sha256",
                     "chunks"), "position, txid"),
    ("inscription_move", ("txid", "inscription", "from_address", "to_address", "position", "how"),
     "position, txid, inscription"),
    ("pair_trade", ("txid", "seq", "position", "taker", "maker", "maker_txid", "gave_property", "gave",
                    "got_property", "got"), "position, txid, seq"),
    ("nft_offer", ("txid", "position", "inscription", "buyer", "take_kind", "take_property", "take_amount"),
     "position, txid"),
    ("nft_ask", ("txid", "position", "inscription", "seller", "take_kind", "take_property", "take_amount"),
     "position, txid"),
)
#: property is keyed on creation_block rather than block_height.
PROPERTY = ("property_id", "ecosystem", "property_type", "issuer", "category", "subcategory", "name", "url",
            "data", "managed", "creation_txid")

#: Checkpoints fall on multiples of this, and this far under the tip, so a reorg never moves one.
EVERY = 100
SETTLED = 6


def _record(table: str, row: Any, columns: tuple[str, ...]) -> str:
    return table + "|" + "|".join("" if row[c] is None else str(row[c]) for c in columns) + "\n"


def _tables(db) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def block_rows(db, lo: int, hi: int) -> dict[int, list[str]]:
    """height -> the records of what that block did, in order, for every block in lo..hi that did anything."""
    have = _tables(db)
    out: dict[int, list[str]] = {}
    for table, columns, order in TABLES:
        if table not in have:
            continue
        sql = (f"SELECT block_height, {', '.join(columns)} FROM {table} "
               f"WHERE block_height BETWEEN ? AND ? ORDER BY block_height, {order}")
        for row in db.conn.execute(sql, (lo, hi)):
            out.setdefault(int(row["block_height"]), []).append(_record(table, row, columns))
    if "property" in have:
        sql = (f"SELECT creation_block, {', '.join(PROPERTY)} FROM property "
               "WHERE creation_block BETWEEN ? AND ? ORDER BY creation_block, property_id")
        for row in db.conn.execute(sql, (lo, hi)):
            out.setdefault(int(row["creation_block"]), []).append(_record("property", row, PROPERTY))
    for h in out:                      # one order however the tables were walked
        out[h].sort()
    return out


def block_digests(db, lo: int, hi: int) -> dict[int, str]:
    return {h: hashlib.sha256("".join(rows).encode()).hexdigest() for h, rows in block_rows(db, lo, hi).items()}


def range_digest(digests: dict[int, str], lo: int, hi: int) -> str:
    """One hash for blocks lo..hi: over each block that did anything, its height and fingerprint."""
    h = hashlib.sha256(f"{lo}-{hi}\n".encode())
    for height in sorted(x for x in digests if lo <= x <= hi):
        h.update(f"{height}:{digests[height]}\n".encode())
    return h.hexdigest()


def checkpoint(indexed: int | None, floor: int | None) -> int | None:
    """The newest height two arcades can compare at without a reorg moving it."""
    if indexed is None or floor is None:
        return None
    at = (indexed - SETTLED) // EVERY * EVERY
    return at if at >= floor else None


class Fingerprints:
    """One chain's index, fingerprinted: block fingerprints are kept once they are settled, so asking
    for a range again reads only the blocks added since."""

    def __init__(self, index, floor: int | None):
        self.index, self.floor = index, floor
        #: The index file this was read from (LedgerIndex.install moves it on): a new one is fingerprinted afresh.
        self.epoch = getattr(index, "epoch", 0)
        self._kept: dict[int, str] = {}
        self._kept_to: int | None = None          # every settled block up to here is in _kept
        self._lock = threading.Lock()

    def _upto(self, hi: int) -> dict[int, str]:
        with self._lock:
            start = self.floor if self._kept_to is None else self._kept_to + 1
            if start is not None and start <= hi:
                with self.index.open() as db:
                    self._kept.update(block_digests(db, start, hi))
                self._kept_to = hi
            return self._kept

    def tip(self) -> dict | None:
        """{h: indexed height, at: the checkpoint, d: fingerprint of floor..at, s: consensus hash at h, n: the names
        digest at h (every @name's current claim, ledger_snapshot.names_digest; 2026-11-02)}."""
        indexed = self.index.indexed_height()
        at = checkpoint(indexed, self.floor)
        if at is None:
            return None
        said = {"h": indexed, "at": at, "d": self.digest(self.floor, at)}
        try:
            from .consensushash import consensus_hash
            from .ledger_snapshot import names_digest
            with self.index.open() as db:
                state = consensus_hash(db)
                names = names_digest(db)
            if self.index.indexed_height() == indexed:      # still the same block: the hash is of that one
                said["s"] = state
                said["n"] = names
        except Exception:                                    # noqa: BLE001 -- the range still compares
            pass
        return said

    def digest(self, lo: int, hi: int) -> str:
        indexed = self.index.indexed_height()
        settled = checkpoint(indexed, self.floor)
        if settled is None or hi > settled:
            raise ValueError("that range is not settled here yet")
        with self._lock:
            if self._kept_to is not None and indexed is not None and indexed < self._kept_to:
                self._kept, self._kept_to = {}, None          # rebuilt, or taken back further than settled
        return range_digest(self._upto(hi), lo, hi)

    def rows(self, height: int) -> list[str]:
        with self.index.open() as db:
            return block_rows(db, height, height).get(height, [])
