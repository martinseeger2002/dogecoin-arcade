"""Welcoming a new operator (2026-10-27, SadFrog: "the new operators node should ... broadcast their address to the
inbox of every other node operator on the arcade asking for a single pep test - in that notification there should be
a way for other operators to sign a transaction in 1 click to send a single pep"; "One ask, closes when paid"; "a
highscores list for operators ... of people "welcoming" new operators - if 2 operators send a pep test in the same
block, count it for both operators as a point").

Three kinds of record, each signed by the node key of the arcade that wrote it (its mesh id, `a`), passed from arcade
to arcade over the whole mesh (arcade/mesh/welcomeshare.py) and kept in `welcome.json`:

    ask     {k, a, at, addr, tag}            a new arcade asks for 1 PEP test at `addr` (its payout address on the
                                             messaging chain). One per arcade: a newer one replaces the older, and
                                             one that is closed is never opened again.
    sent    {k, a, at, ask, txid, tag}       arcade `a` paid the ask of arcade `ask` in `txid`. One per (a, ask).
    closed  {k, a, at, block, height, txids} the asker's word that it was paid: the first block with a payment from
                                             a `sent` record, and every such payment in that block (`txids`).
                                             Empty `txids`: coins came some other way (it mined a block).

The score is one point per closed ask for each arcade whose `sent` txid is in that ask's `txids`: two arcades that
paid in the same block both score. A txid counts for the arcade that said it first. Everything here is each arcade's
own word, like the operator tags of mesh/operators.py: it is a welcome board, and no payment, permission or vouching
reads it.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import nacl.exceptions
import nacl.signing

#: What one new arcade asks for, in koinu: one PEP test.
AMOUNT = 100_000_000
#: An open ask is passed on and shown for this long, then forgotten (a closed one stays for the scores).
ASK_DAYS = 14
#: Sent and closed records are kept this long (the board's memory).
KEEP_DAYS = 365
#: A record dated further ahead than this is refused (clocks differ a little).
SKEW = 600
#: At most this many records are kept; the oldest go first.
MAX_RECORDS = 5000
#: At most this many txids in a closed record (payments in one block).
MAX_TXIDS = 20

KINDS = ("ask", "sent", "closed")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{25,64}$")


def _canon(record: dict) -> bytes:
    return json.dumps({k: v for k, v in record.items() if k != "sig"}, sort_keys=True,
                      separators=(",", ":")).encode()


def sign(record: dict, signing_key: nacl.signing.SigningKey) -> dict:
    """`record` with its author (`a`, the key's mesh id) and signature (`sig`)."""
    out = {**record, "a": bytes(signing_key.verify_key).hex()}
    if "txids" in out:
        out["txids"] = sorted(set(out["txids"]))
    out["sig"] = signing_key.sign(_canon(out)).signature.hex()
    return out


def _tag_ok(value) -> bool:
    """"" or an @name exactly as the tag rules have it (normalised, allowed): anything else would have to be changed
    to be kept, and a changed record no longer carries its author's signature."""
    from .mesh.operators import said_tag
    return value == "" or (isinstance(value, str) and said_tag(value) == value)


def clean(record: Any, now: float | None = None) -> dict | None:
    """`record` checked and cut to its fields, or None: its shape, its age, and its author's signature."""
    if not isinstance(record, dict) or record.get("k") not in KINDS:
        return None
    now = time.time() if now is None else now
    a, sig, at = record.get("a"), record.get("sig"), record.get("at")
    if not (isinstance(a, str) and _HEX64.match(a) and isinstance(sig, str) and len(sig) == 128
            and isinstance(at, (int, float)) and not isinstance(at, bool)):
        return None
    days = ASK_DAYS if record["k"] == "ask" else KEEP_DAYS
    if not now - days * 86400 <= at <= now + SKEW:
        return None
    out: dict = {"k": record["k"], "a": a, "at": int(at), "sig": sig}
    if record["k"] == "ask":
        addr = record.get("addr")
        if not (isinstance(addr, str) and _ADDRESS.match(addr)):
            return None
        if not _tag_ok(record.get("tag", "")):
            return None
        out.update(addr=addr, tag=record.get("tag", ""))
    elif record["k"] == "sent":
        ask, txid = record.get("ask"), record.get("txid")
        if not (isinstance(ask, str) and _HEX64.match(ask) and ask != a and isinstance(txid, str)
                and _HEX64.match(txid)):
            return None
        if not _tag_ok(record.get("tag", "")):
            return None
        out.update(ask=ask, txid=txid, tag=record.get("tag", ""))
    else:
        block, height, txids = record.get("block"), record.get("height"), record.get("txids")
        if not (isinstance(block, str) and (block == "" or _HEX64.match(block))
                and isinstance(height, int) and not isinstance(height, bool) and height >= 0
                and isinstance(txids, list) and len(txids) <= MAX_TXIDS
                and all(isinstance(t, str) and _HEX64.match(t) for t in txids) and txids == sorted(set(txids))):
            return None
        out.update(block=block, height=height, txids=txids)
    try:
        nacl.signing.VerifyKey(bytes.fromhex(a)).verify(_canon(out), bytes.fromhex(sig))
    except (nacl.exceptions.BadSignatureError, ValueError):
        return None
    return out


def _key(record: dict) -> tuple:
    """Records with the same key are one record: the newest stands (an ask, a closing), or the first (a payment)."""
    if record["k"] == "sent":
        return ("sent", record["a"], record["ask"])
    return (record["k"], record["a"])


class Book:
    """Every record this arcade knows, kept in `path` (None: in memory only, for tests)."""

    def __init__(self, path: Path | str | None):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.records: dict[tuple, dict] = {}
        if self.path is not None:
            try:
                kept = json.loads(self.path.read_text())
            except (OSError, ValueError):
                kept = []
            for r in kept if isinstance(kept, list) else []:
                r = clean(r)
                if r is not None:
                    self.records[_key(r)] = r

    def add(self, record: Any, now: float | None = None) -> dict | None:
        """Keep `record` if it is valid and new to this arcade: the record kept, or None (invalid, or known)."""
        r = clean(record, now)
        if r is None:
            return None
        with self.lock:
            key = _key(r)
            old = self.records.get(key)
            if old is not None:
                if r["k"] == "sent" or r["at"] <= old["at"]:
                    return None
            if r["k"] == "ask" and ("closed", r["a"]) in self.records:
                return None                                # a closed ask is never opened again
            self.records[key] = r
            self._prune(now)
            self._save()
        return r

    def _prune(self, now: float | None) -> None:
        now = time.time() if now is None else now
        for key, r in list(self.records.items()):
            days = ASK_DAYS if r["k"] == "ask" and ("closed", r["a"]) not in self.records else KEEP_DAYS
            if r["at"] < now - days * 86400:
                del self.records[key]
        if len(self.records) > MAX_RECORDS:
            for key, _ in sorted(self.records.items(), key=lambda kv: kv[1]["at"])[:len(self.records) - MAX_RECORDS]:
                del self.records[key]

    def _save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(self.records.values())))
        tmp.replace(self.path)

    def all(self) -> list[dict]:
        with self.lock:
            return sorted(self.records.values(), key=lambda r: r["at"])

    def of_kind(self, kind: str) -> list[dict]:
        return [r for r in self.all() if r["k"] == kind]

    def ask_of(self, node: str) -> dict | None:
        return self.records.get(("ask", node))

    def closed_of(self, node: str) -> dict | None:
        return self.records.get(("closed", node))

    def sent_to(self, node: str) -> list[dict]:
        return [r for r in self.of_kind("sent") if r["ask"] == node]

    def open_asks(self) -> list[dict]:
        """Asks not closed yet, newest first."""
        closed = {r["a"] for r in self.of_kind("closed")}
        return sorted((r for r in self.of_kind("ask") if r["a"] not in closed), key=lambda r: -r["at"])

    def tags(self) -> dict[str, str]:
        """Each arcade's latest word for its operator's @name."""
        out: dict[str, str] = {}
        for r in self.all():
            if r.get("tag"):
                out[r["a"]] = r["tag"]
        return out


def scores(records: Iterable[dict]) -> list[dict]:
    """The welcome board: [{node, tag, points, last}], most points first. One point per closed ask for each arcade
    whose payment is in that ask's first paid block (`txids`); a txid counts for the arcade that said it first."""
    records = sorted(records, key=lambda r: r["at"])
    first_claim: dict[str, dict] = {}
    for r in records:
        if r["k"] == "sent":
            first_claim.setdefault(r["txid"], r)
    tags: dict[str, str] = {}
    for r in records:
        if r.get("tag"):
            tags[r["a"]] = r["tag"]
    board: dict[str, dict] = {}
    for c in (r for r in records if r["k"] == "closed"):
        for txid in c["txids"]:
            s = first_claim.get(txid)
            if s is None or s["ask"] != c["a"]:
                continue
            row = board.setdefault(s["a"], {"node": s["a"], "points": 0, "last": 0})
            row["points"] += 1
            row["last"] = max(row["last"], c["at"])
    for row in board.values():
        row["tag"] = tags.get(row["node"], "")
    return sorted(board.values(), key=lambda r: (-r["points"], -r["last"]))


def paid_block(found: Iterable[tuple[str, str, int]]) -> tuple[str, int, list[str]] | None:
    """From the asker's own look at each `sent` payment it was told of, (txid, block hash, height) for each one that
    is in a block and pays it: the first such block, its height, and every one of those txids in it. None: none yet."""
    found = [f for f in found if f[1]]
    if not found:
        return None
    height = min(f[2] for f in found)
    block = next(f[1] for f in found if f[2] == height)
    return block, height, sorted({f[0] for f in found if f[1] == block})
