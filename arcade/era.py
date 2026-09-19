"""Which chain era the wallet's own files belong to, and retiring them.

A floor is the block a chain is read from. Moving it makes a NEW ERA inside
the same network: everything below is still on the chain and is read by
nobody, so an index built under the old floor is discarded and rebuilt
(D-123). The wallet's own sqlite files were never part of that, and the
reassurance that kept them out of it was answering a different question --
"every row is keyed by network" is true, and a reset makes a new era inside
one network, so it guarantees nothing at all about a floor (a test machine, D-126).

**The rule for whether a table belongs here is whether the identifiers in it
are REISSUED** -- not whether it has a network column, which is an argument
about mainnet and says nothing about eras (a test machine):

* **Unique for ever: litter.** A txid or an inscription id names one
  transaction for all time. After a floor moves, a row holding one points at
  something real that no node reads: it resolves to nothing and fails
  visibly. Untidy, harmless, and not worth discarding somebody's records
  over.

* **Reissued: wrong answers.** Property ids, inscription numbers, editions
  and a collection's (creator, name) are handed out again from the bottom
  every era -- `next_property_id` is `MAX(property_id) + 1` over a table that
  was just emptied, and the second Pixel Skulls will have editions 1..333
  exactly as the first did. A row holding one of those does not go stale; it
  silently starts describing a DIFFERENT OBJECT. `approvals.sqlite` held
  "property 3, Arcade Test" into an era where property 3 is a token called
  Arcade: every page resolving that id shows the wrong token, every page
  trusting the stored name contradicts the chain, and it is the file that
  records what somebody AUTHORISED.

A dead reference fails where somebody can see it. A reissued one does not
fail at all, which is why only the second kind is swept.

So the rows of an older era are moved out of the live tables, into a JSON
file beside them named after the era they belonged to. Moved, not deleted --
the same rule the index follows, and for the same reason: a program that
discards on a config change discards on a mistaken one (D-123).

Per network, because these files hold both chains' rows and only one chain's
floor moves at a time.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Where the floor each network's local files were written under is recorded.
#: One file, so a new era retires everything at once rather than five tables
#: each remembering separately -- five chances to forget.
ERA_FILE = "eras.json"

#: Every local table that holds something the chain assigns, and the column
#: naming the network it belongs to. Order is only for a tidy log.
#:
#: Left out deliberately, by the rule above and by one other:
#:
#: * `pagedata.sqlite`, keyed by inscription txid -- unique for ever, so its
#:   rows are litter rather than wrong answers.
#: * `nodetalk.sqlite`'s `peer`, which holds pubkeys and no chain reference at
#:   all. Somebody you have talked to was LEARNED rather than derived, which
#:   is the line drawn around the address book, and it is drawn here for the
#:   same reason (a test machine). Its `letter` rows go: they are this node's record of
#:   transactions on a chain era nothing reads.
#: * `test.sqlite`, the messaging store, which holds that address book. Losing
#:   it is losing work, so it stays a human's decision and stays named in the
#:   release note.
ERA_TABLES: tuple[tuple[str, str, str], ...] = (
    ("approvals.sqlite", "request", "network"),
    ("swaps.sqlite", "offer", "network"),
    ("swaps.sqlite", "fill", "network"),
    ("swaps.sqlite", "bid", "network"),
    # The scan position too: a cursor left at a height below the new
    # floor is a promise to catch up over blocks nobody reads.
    ("swaps.sqlite", "cursor", "network"),
    ("nodetalk.sqlite", "letter", "network"),
    ("collections.sqlite", "job", "network"),
)


#: Tables with no network of their own, whose rows belong to a row in one of
#: the tables above: (file, table, its key, parent table, parent's key).
#:
#: `item` is 400 rows on a machine with four collection runs on it, and
#: retiring the run while leaving its items behind is not retiring it -- they
#: become unreachable rather than gone, which is the state that made the
#: whole audit necessary (a test machine).
ERA_CHILDREN: tuple[tuple[str, str, str, str, str], ...] = (
    ("collections.sqlite", "item", "job_id", "job", "id"),
)

#: Tables that record the floor they were written under. Their current-era
#: rows are kept even on the first sweep, which would otherwise be era-BLIND
#: rather than era-accurate: "nothing recorded which floor these belong to"
#: was taking rows that plainly belong to this one. A test machine's first sweep
#: retired a 333-piece run seven minutes after it finished (D-132).
KNOWS_ITS_FLOOR: frozenset[tuple[str, str]] = frozenset({
    ("collections.sqlite", "job"),
})


def recorded(home: Path) -> dict[str, int]:
    """The floor each network's local files were last written under."""
    try:
        data = json.loads((Path(home) / ERA_FILE).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): int(v) for k, v in data.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


def remember(home: Path, network: str, floor: int) -> None:
    eras = recorded(home)
    eras[network] = int(floor)
    path = Path(home) / ERA_FILE
    try:
        path.write_text(json.dumps(eras, indent=1, sort_keys=True) + "\n")
    except OSError as exc:                  # nothing is lost; it retries later
        log.warning("could not record the era for %s: %s", network, exc)


def _has_column(path: Path, table: str, column: str) -> bool:
    """Whether `table` in `path` has that column, without touching anything."""
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(path, timeout=10)
        try:
            return any(row[1] == column
                       for row in conn.execute(f"PRAGMA table_info({table})"))
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def a_run_in_flight(home: Path, network: str) -> str:
    """A collection run that is still going, said in a sentence, or "".

    Read straight from the jobs file rather than through `Jobs`, because this
    runs before anything else is built and must not drag the collection
    machinery into the startup path.
    """
    from .collections import UNFINISHED

    path = Path(home) / "collections.sqlite"
    if not path.exists():
        return ""
    try:
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            marks = ",".join("?" * len(UNFINISHED))
            row = conn.execute(
                f"SELECT id, name, status FROM job WHERE network = ? "
                f"AND status IN ({marks}) LIMIT 1",
                (network, *UNFINISHED)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return ""                      # no jobs table yet, or nothing to read
    if row is None:
        return ""
    return (f"the collection run {row['id']} ({row['name']!r}) is {row['status']} "
            f"and its pieces must not be moved out from under it")


def retire_old_rows(home: Path, network: str, floor: int | None) -> dict[str, int]:
    """Move any older era's rows for `network` out of the live tables.

    Returns {"<file>.<table>": rows moved}, empty when there was nothing to
    do -- which is every start but the first after a floor moves.

    A network with no floor at all (Dogecoin mainnet, which is not indexed
    yet) is left alone: there is no era to be wrong about.

    The FIRST run of this code retires whatever is there, because nothing
    recorded which era those rows belonged to and the honest assumption for a
    record naming a reusable id is that it is not from this one. That is a
    single sweep on one upgrade, everything kept in a file beside the store,
    and it is the sweep this was written for: the rows that prompted it are
    exactly the ones no floor move will ever come along and catch.

    The copy is written BEFORE anything is deleted. A backup made after the
    delete is missing exactly when the delete was the problem -- the same
    order the fill guard had to learn (D-119).
    """
    home = Path(home)
    if floor is None:
        return {}
    eras = recorded(home)
    was = eras.get(network)
    if was == int(floor):
        return {}

    busy = a_run_in_flight(home, network)
    if busy:
        # Nothing moves while a collection run is going. An update restarts
        # the service and the sweep runs at start, so a run that is paused or
        # part-sent would have its job and its items retired out from under
        # it: pieces broadcast, no job left to resume, which is the worst
        # state a run can be in (D-125). Not recorded either, so the next
        # start tries again once the run is finished (a test machine, D-132).
        log.warning("not retiring anything for %s: %s", network, busy)
        return {}

    kept: dict[str, list[dict[str, Any]]] = {}
    found: list[tuple[str, str, str, tuple[Any, ...], int]] = []
    wheres: list[tuple[str, str, str, tuple[Any, ...]]] = []
    for filename, table, column in ERA_TABLES:
        where, params = f"{column} = ?", (network,)
        if ((filename, table) in KNOWS_ITS_FLOOR
                and _has_column(home / filename, table, "floor")):
            # It says which floor it belongs to, so it is asked rather than
            # assumed -- and a row that says THIS one stays. Only when the
            # column is really there: this runs before anything migrates the
            # file, so a store written by an older version has no `floor` and
            # asking for one would abort the sweep on every start.
            where += " AND (floor IS NULL OR floor != ?)"
            params += (int(floor),)
        wheres.append((filename, table, where, params))
    # A queue, not a snapshot: a parent's rows add their children to it as
    # they are read, and the loop has to reach them.
    position = 0
    while position < len(wheres):
        filename, table, where, params = wheres[position]
        position += 1
        path = home / filename
        if not path.exists():
            continue
        try:
            conn = sqlite3.connect(path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                rows = [dict(r) for r in conn.execute(
                    f"SELECT * FROM {table} WHERE {where}", params)]
                if rows:
                    # Anything that hangs off these rows goes with them.
                    for child in ERA_CHILDREN:
                        kid_file, kid_table, kid_key, parent, parent_key = child
                        if kid_file != filename or parent != table:
                            continue
                        ids = [row[parent_key] for row in rows]
                        # In groups, because SQLite takes 999 variables in a
                        # statement by default and a machine with a thousand
                        # runs on it would get an error where it wanted a
                        # sweep -- and this aborts on errors it cannot read.
                        for at in range(0, len(ids), 400):
                            batch = tuple(ids[at:at + 400])
                            marks = ",".join("?" * len(batch))
                            wheres.append((kid_file, kid_table,
                                           f"{kid_key} IN ({marks})", batch))
            finally:
                conn.close()
        except sqlite3.Error as exc:
            if "no such table" in str(exc):
                # A store that exists but has never used this table. Skipping
                # it is right and must not stop the sweep: aborting on it
                # would abort on every start for ever, and an era that is
                # never recorded is a sweep that never finishes.
                continue
            # Anything else -- a lock, a corrupt page -- is transient or
            # serious, and either way this is not the moment to decide the
            # era is clean. The era is not recorded, so the next start tries
            # the whole sweep again.
            log.warning("could not read %s.%s: %s", filename, table, exc)
            return {}
        if not rows:
            continue
        kept.setdefault(filename, []).extend(
            dict(row, _table=table) for row in rows)
        found.append((filename, table, where, params, len(rows)))

    if not found:
        remember(home, network, floor)
        return {}

    for filename, rows in kept.items():
        out = home / f"{filename}.before-{floor}.json"
        count = 1
        while out.exists():
            count += 1
            out = home / f"{filename}.before-{floor}.{count}.json"
        try:
            out.write_text(json.dumps(
                {"network": network, "was": was, "floor": int(floor),
                 "retired_at": time.time(), "rows": rows},
                indent=1, default=str) + "\n")
        except OSError as exc:
            # Nothing has been deleted yet, and nothing will be.
            log.warning("old rows could not be copied to %s, so none were "
                        "moved: %s", out, exc)
            return {}

    moved: dict[str, int] = {}
    for filename, table, where, params, count in found:
        try:
            conn = sqlite3.connect(home / filename, timeout=10)
            try:
                conn.execute(f"DELETE FROM {table} WHERE {where}", params)
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.warning("could not clear %s.%s: %s", filename, table, exc)
            continue
        moved[f"{filename}.{table}"] = count
    if moved:
        log.warning("%s starts at %s now%s: %s moved out of the live tables "
                    "and kept beside them",
                    network, f"{int(floor):,}",
                    f", not {int(was):,}" if was is not None else
                    " and nothing recorded which floor these were written under",
                    ", ".join(f"{n} from {k}" for k, n in sorted(moved.items())))
    remember(home, network, floor)
    return moved
