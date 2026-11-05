"""Each account's coins, carried between arcades, and what is known for certain about them (2026-10-25).

The operator: "each users valid, UTXO list should be stored alongside their encrypted wallet file ... also be stored in their
arcade, browser local storage ... any of the current arcade accounts that are held by more than one arcade. Like @apple
the UTXO's and balances cross arcade need to be reconciled." The plan, one decision at a time, is ~/arcade-work's
utxo-plan.md; what this module holds is the arithmetic every part of it shares.

**A list says which coins, COVERAGE says how far to trust it.** An arcade's `utxo` table is complete for an address
only over the blocks it has read for that address: from when watching began (`watched.since`) to the tip, plus any
range a read-back or a walk filled in (`covered` here). A list from another arcade, or from a browser, is merged
coin by coin, but every coin is checked against a chain before it counts (gettxout: still unspent, paying that
address), so the union can only lose what is spent and never gain what is not real. Coverage is merged the same way:
the union of ranges each source vouches for. What no source covers is a GAP, and a gap is what a walk reads.

**A walk is shared.** A wallet no arcade has seen is found by walking the blocks, and the walk is dealt out: the chain
is cut into equal segments, one per arcade with a node for it, each arcade starting at the top of its own (staggered)
and going on through the next one, so every segment is read by two different arcades (`segments`). Each job also
carries canaries -- addresses whose coins in its range are known -- so a reader that misses a kind of output is caught
even when two arcades running the same code agree on the wrong answer.
"""
from __future__ import annotations

import time
from typing import Any, Iterable

SCHEMA = """
-- Block ranges over which this index's coins for an address are complete, besides watching (`watched.since` to the
-- tip): a read-back from the floor, a list another arcade vouched for, a shared walk. Inclusive [lo, hi].
CREATE TABLE IF NOT EXISTS covered (
    address TEXT    NOT NULL,
    lo      INTEGER NOT NULL,
    hi      INTEGER NOT NULL,
    how     TEXT    NOT NULL DEFAULT '',
    at      REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (address, lo, hi)
);
"""

#: A list's version: a receiving arcade reads only what it knows.
VERSION = 1
#: Coins one browser copy may hand the node to check in one sign-in.
BROWSER_COINS_MAX = 20_000


def install(db) -> None:
    db.conn.executescript(SCHEMA)


# --- ranges ---------------------------------------------------------------------------------------------------------
Range = tuple[int, int]


def union(ranges: Iterable[Iterable[int]]) -> list[Range]:
    """Inclusive ranges merged: overlapping or touching ones become one, sorted."""
    out: list[list[int]] = []
    for lo, hi in sorted((int(a), int(b)) for a, b in ranges if int(a) <= int(b)):
        if out and lo <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(a, b) for a, b in out]


def gaps(covered: Iterable[Iterable[int]], lo: int, hi: int) -> list[Range]:
    """What of [lo, hi] the covered ranges leave out."""
    out, at = [], int(lo)
    for a, b in union(covered):
        if b < at:
            continue
        if a > hi:
            break
        if a > at:
            out.append((at, min(a - 1, hi)))
        at = max(at, b + 1)
        if at > hi:
            break
    if at <= hi:
        out.append((at, int(hi)))
    return out


def covers(covered: Iterable[Iterable[int]], lo: int, hi: int) -> bool:
    return not gaps(covered, lo, hi)


# --- what this index knows ------------------------------------------------------------------------------------------
def coverage(db, address: str, tip: int) -> list[Range]:
    """The ranges over which this index's coins for `address` are complete (watching, plus `covered`)."""
    ranges: list[Range] = []
    row = db.conn.execute("SELECT since FROM watched WHERE address = ?", (address,)).fetchone()
    # A watch begun while the index held no block says 0 (utxos.watch at indexed_height() or 0): nothing was read for
    # it, so it covers nothing (2026-11-05, the operator's Flopcoin imports: listed as "covered 0..tip" with no coins, so
    # Admin called them whole and another arcade or browser would take "no coins, all of it known" for the truth).
    # The same rule as app._rescan_done.
    if row is not None and int(row[0]) > 0 and tip >= int(row[0]):
        ranges.append((int(row[0]), int(tip)))
    try:
        for lo, hi in db.conn.execute("SELECT lo, hi FROM covered WHERE address = ?", (address,)):
            ranges.append((int(lo), int(hi)))
    except Exception:                                       # noqa: BLE001 -- an index from before this table
        pass
    return union(ranges)


def mark_covered(db, address: str, ranges: Iterable[Iterable[int]], how: str) -> None:
    """Record that this index's coins for `address` are complete over `ranges` (kept merged)."""
    install(db)
    have = [(int(a), int(b)) for a, b in db.conn.execute("SELECT lo, hi FROM covered WHERE address = ?", (address,))]
    merged = union(have + [tuple(r) for r in ranges])
    if merged == union(have):
        return
    db.conn.execute("DELETE FROM covered WHERE address = ?", (address,))
    now = time.time()
    db.conn.executemany("INSERT INTO covered (address, lo, hi, how, at) VALUES (?,?,?,?,?)",
                        [(address, a, b, how, now) for a, b in merged])


def listing(db, addresses: Iterable[str], tip: int, marks: dict[tuple[str, int], str] | None = None
            ) -> dict[str, dict[str, Any]]:
    """{address: {"coins": [[txid, vout, sats, height, mark]], "covered": [[lo, hi]]}} -- this index's list for some
    addresses, the shape every arcade and browser swaps. `marks`: (txid, vout) -> "ins:<id>" | "tok:<pid>"."""
    marks = marks or {}
    out: dict[str, dict[str, Any]] = {}
    for address in dict.fromkeys(addresses):
        rows = db.conn.execute(
            "SELECT txid, vout, value, height FROM utxo WHERE address = ? ORDER BY height, txid, vout", (address,))
        coins = [[t, int(v), int(s), int(h), marks.get((t, int(v)), "")] for t, v, s, h in rows]
        out[address] = {"coins": coins, "covered": [list(r) for r in coverage(db, address, tip)]}
    return out


# --- merging what several sources say -------------------------------------------------------------------------------
def merge(lists: Iterable[dict[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Several listings (this arcade's, other arcades', the browser's) as one: every coin any of them names (to be
    CHECKED before it counts) and, per address, the union of what they vouch for. A coin's mark is kept when any
    source marks it: a mark only ever keeps a coin from being spent as plain coins."""
    coins: dict[str, dict[tuple[str, int], list]] = {}
    covered: dict[str, list] = {}
    for one in lists:
        if not isinstance(one, dict):
            continue
        for address, said in one.items():
            if not isinstance(said, dict):
                continue
            here = coins.setdefault(address, {})
            for c in said.get("coins") or []:
                try:
                    txid, vout, sats, height = str(c[0]).lower(), int(c[1]), int(c[2]), int(c[3])
                    mark = str(c[4]) if len(c) > 4 and c[4] else ""
                except (TypeError, ValueError, IndexError):
                    continue
                if len(txid) != 64 or sats <= 0:
                    continue
                kept = here.get((txid, vout))
                if kept is None:
                    here[(txid, vout)] = [txid, vout, sats, height, mark]
                elif mark and not kept[4]:
                    kept[4] = mark
            for r in said.get("covered") or []:
                try:
                    covered.setdefault(address, []).append((int(r[0]), int(r[1])))
                except (TypeError, ValueError, IndexError):
                    continue
    return {a: {"coins": sorted(coins.get(a, {}).values(), key=lambda c: (c[3], c[0], c[1])),
                "covered": [list(r) for r in union(covered.get(a, []))]}
            for a in set(coins) | set(covered)}


# --- the shared walk ------------------------------------------------------------------------------------------------
def segments(lo: int, hi: int, readers: list[str]) -> list[dict[str, Any]]:
    """[lo, hi] dealt to `readers` (arcade ids) for a shared walk, every block read by two of them: the range is cut
    into one segment per reader, highest first; reader i starts at the top of segment i and goes on through segment
    i+1 (the last wraps to the first), so each segment has its own reader and the one above as its second. With one
    reader there is nobody to check it: one job, read once. Jobs walk DOWN (`top` .. `bottom`)."""
    readers = list(dict.fromkeys(readers))
    lo, hi = int(lo), int(hi)
    if hi < lo or not readers:
        return []
    n = min(len(readers), hi - lo + 1)
    readers = readers[:n]
    size = (hi - lo + 1) / n
    cuts = [(hi - round(i * size), hi - round((i + 1) * size) + 1) for i in range(n)]   # (top, bottom), highest first
    jobs = []
    for i, who in enumerate(readers):
        mine = [cuts[i]]
        if n > 1:
            mine.append(cuts[(i + 1) % n])
        for top, bottom in mine:
            jobs.append({"reader": who, "top": top, "bottom": bottom, "segment": cuts.index((top, bottom))})
    return jobs


def double_read(jobs: list[dict[str, Any]]) -> bool:
    """Every segment of a walk is read by two different readers (or the walk had only one)."""
    by: dict[int, set] = {}
    for j in jobs:
        by.setdefault(j["segment"], set()).add(j["reader"])
    return bool(by) and (len(by) == 1 or all(len(v) >= 2 for v in by.values()))


def agree(a: Iterable[Iterable], b: Iterable[Iterable]) -> tuple[set, set]:
    """Two readers' matches for the same segment: (only in a, only in b) as sets of (txid, vout)."""
    sa = {(str(x[0]).lower(), int(x[1])) for x in a}
    sb = {(str(x[0]).lower(), int(x[1])) for x in b}
    return sa - sb, sb - sa


def canaries_missing(found: Iterable[Iterable], canaries: Iterable[Iterable]) -> list[tuple[str, int]]:
    """The canary coins (known outputs in the job's range) a reader did not report: any is a failed job."""
    got = {(str(x[0]).lower(), int(x[1])) for x in found}
    return [(str(c[0]).lower(), int(c[1])) for c in canaries if (str(c[0]).lower(), int(c[1])) not in got]
