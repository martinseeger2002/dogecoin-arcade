"""Collection runs that belong to an account, not to this node's wallet.

Why this is a separate book and not `collections.Jobs` with an owner column
--------------------------------------------------------------------------
`Runner.resume_interrupted` (collections.py:702) walks every row in `jobs.list()`
and starts a thread for each one that was `running` when the process stopped,
with no filter for whose run it is. And `_run` takes its signer from
`self.make_sender(rpc, chain.params)` (collections.py:759), whose default makes a
sender out of the node's own RPC wallet; the row's `sender` column is never
consulted to decide that. So an account's run written into that book would be
picked up by the next restart and inscribed with the node's coins and the node's
key, which is the thing docs/multi-user.md §5 and `/account/inscribe` exist to
make impossible. A separate book is also what keeps `collections.py` untouched:
an account run has no thread at all, because nothing in here may sign for it.

What `running` means here
-------------------------
For the operator's Runner, `running` means "a thread is going right now". For an
account it means "a piece is on its way and the browser is expected to come back
for the next offer". Nothing in this module starts anything: `due()` is the
question a request asks, and the answer is the same run the account started
before it closed the tab. That is the whole resume story -- no thread to resume,
and the same piece rebuilt identically when the offer it was offered has expired.

One piece per item
------------------
`create` refuses a build with an item that needs more than one chunk rather than
accepting it and failing forty pieces in. An item that needs two chunks needs two
transactions that chain onto each other, so it needs a block between them, and
that is the operator Runner's `_fund`/`_wait_for_block` machinery. A collection
that half-inscribes is worse than one that has not started, so the refusal
happens at the review, before anything is paid for, naming the items.

Retention
---------
`folder` is a build the account uploaded, and it stays on this node until the
account deletes the run. Nothing ages it out yet -- an operator should know that
a finished run keeps its images forever. `delete` removes rows only: a directory
that has no rule written for it is not a directory to remove here.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import inscribe as inscribelib
from .db import add_missing_columns

SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    id          TEXT PRIMARY KEY,
    created     REAL NOT NULL,
    network     TEXT NOT NULL,
    -- The account, which is an address here for the same reason the messaging
    -- book keys on one: it is what the browser proves a key for, and a name can
    -- be taken by anyone. §6's "one active run at a time" is a query on this.
    account     TEXT NOT NULL,
    -- Where the pieces come from and where their change lands. Same address as
    -- `account` today; separate columns because an account can point a run at
    -- an address it has added since the run was written down.
    address     TEXT NOT NULL,
    name        TEXT NOT NULL,
    folder      TEXT NOT NULL,
    status      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    items       INTEGER NOT NULL,
    fee         REAL NOT NULL,
    dust        REAL NOT NULL,
    -- The chain's floor when this run was written down, for the reason in
    -- collections.py: a run belongs to a chain era, and after the floor moves
    -- its pieces are history nobody reads.
    floor       INTEGER
);
CREATE TABLE IF NOT EXISTS piece (
    run_id       TEXT NOT NULL,
    edition      INTEGER NOT NULL,
    name         TEXT NOT NULL,
    image        TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size         INTEGER NOT NULL,
    json         TEXT NOT NULL,
    -- Eight bytes, chosen once when the run was written down and never
    -- regenerated: a retried piece has to inscribe the same piece rather than
    -- a new one next to it. One per item, as in the operator's book -- a
    -- collection is a set of inscriptions that share a folder and a name, not
    -- one inscription spread over a hundred transactions.
    inscription_id TEXT NOT NULL,
    status       TEXT NOT NULL,
    txid         TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, edition)
);
CREATE INDEX IF NOT EXISTS piece_status_idx ON piece(run_id, status);
CREATE INDEX IF NOT EXISTS run_account_idx ON run(account, status);
"""

#: `open` is written but nothing is on its way; `running` is at least one piece
#: broadcast and the rest still owed. There is no `pausing`: a run with no thread
#: cannot be mid-piece in the way that word means for the operator's Runner.
STATUSES = ("open", "running", "done", "failed", "stopped")

#: What a piece is doing. `pending` has never been offered, `sending` has an
#: offer out with no broadcast yet -- the difference is only so a page can say
#: which one the account is being asked about.
PIECE_STATUSES = ("pending", "sending", "sent", "failed")


class TooBig(ValueError):
    """An item of this build needs more than one transaction."""

    def __init__(self, names: list[str]):
        self.names = names
        super().__init__(
            "every item has to fit in one transaction, and "
            + str(len(names)) + " of these do not: " + ", ".join(names[:5])
            + ("" if len(names) <= 5 else ", ...")
            + ". Nothing has been paid for.")


class Runs:
    """Account collection runs, on disk. One connection per call, as in the
    operator's book: the web thread and whatever else is asking are different
    threads and there is no runner thread to coordinate with."""

    def __init__(self, path: Path):
        self.path = Path(path)
        with self._open() as conn:
            conn.executescript(SCHEMA)
            add_missing_columns(conn, SCHEMA)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    # -- writing a run down --

    def create(self, account: str, address: str, build: Any, network: str,
               name: str = "", floor: int | None = None) -> str:
        """Write the run and every piece, each with the id its tx will carry.

        Takes the same `Build` object `collections.read_build` returns; the only
        fields read off it are `folder`, `collection`, and the five fields of
        each item, so a run can be written from a folder or from a test.
        """
        too_big = []
        for item in build.items:
            est = inscribelib.estimate(item.size, item.content_type, item.json)
            if int(est.chunks) > 1:
                too_big.append(item.name)
        if too_big:
            raise TooBig(too_big)

        fee = dust = 0.0
        run_id = secrets.token_hex(6)
        with self._open() as conn:
            conn.execute("BEGIN")
            for item in build.items:
                est = inscribelib.estimate(item.size, item.content_type, item.json)
                fee += est.fee
                dust += est.dust
                conn.execute(
                    "INSERT INTO piece (run_id, edition, name, image, content_type, "
                    "size, json, inscription_id, status) VALUES (?,?,?,?,?,?,?,?,?)",
                    (run_id, item.edition, item.name, item.image, item.content_type,
                     item.size, item.json, secrets.token_bytes(8).hex(), "pending"))
            conn.execute(
                "INSERT INTO run (id, created, network, account, address, name, "
                "folder, status, items, fee, dust, floor) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, time.time(), network, account, address,
                 name or build.collection, str(build.folder), "open",
                 len(build.items), round(fee, 8), round(dust, 8), floor))
            conn.execute("COMMIT")
        return run_id

    # -- asking --

    def get(self, run_id: str) -> dict | None:
        with self._open() as conn:
            row = conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            run = dict(row)
            run.update(_progress(conn, run_id))
            return run

    def list(self, account: str | None = None,
             network: str | None = None) -> list[dict]:
        with self._open() as conn:
            sql, args = "SELECT * FROM run", []
            if account:
                sql += " WHERE account = ?"
                args.append(account)
            if network:
                sql += (" WHERE" if "WHERE" not in sql else " AND") + " network = ?"
                args.append(network)
            rows = conn.execute(sql + " ORDER BY created DESC", args).fetchall()
            out = []
            for row in rows:
                run = dict(row)
                run.update(_progress(conn, run["id"]))
                out.append(run)
            return out

    def due(self, account: str, network: str) -> dict | None:
        """The run this account owes pieces to, if it owes any.

        §6's "one active run at a time", as an actual rule rather than as
        `OFFERS_WAITING`, which counts a pile of unsigned offers and cannot tell
        one run's four from four single inscriptions. Newest first, because an
        account that started a second run before this code existed gets asked
        about the one it touched last.
        """
        with self._open() as conn:
            row = conn.execute(
                "SELECT * FROM run WHERE account = ? AND network = ? "
                "AND status IN ('open', 'running') ORDER BY created DESC LIMIT 1",
                (account, network)).fetchone()
            if row is None:
                return None
            run = dict(row)
            run.update(_progress(conn, run["id"]))
            return run

    def next_piece(self, run_id: str) -> dict | None:
        """The lowest edition not yet on the chain. One being offered comes
        before one never offered, so a piece the browser was already given an
        offer for is finished before the next is offered -- which is also what
        makes an expired offer for piece 7 rebuild the same piece 7."""
        with self._open() as conn:
            row = conn.execute(
                "SELECT * FROM piece WHERE run_id = ? "
                "AND status IN ('sending', 'pending') "
                "ORDER BY status = 'pending', edition LIMIT 1", (run_id,)).fetchone()
            return dict(row) if row else None

    def previous_txid(self, run_id: str) -> str:
        """The last piece this run put on the chain, which is the output the
        next piece spends. Empty for the first piece, which funds from the
        account's own coins."""
        with self._open() as conn:
            row = conn.execute(
                "SELECT txid FROM piece WHERE run_id = ? AND status = 'sent' "
                "ORDER BY edition DESC LIMIT 1", (run_id,)).fetchone()
            return "" if row is None else row["txid"]

    def pieces(self, run_id: str, status: str | None = None) -> list[dict]:
        with self._open() as conn:
            sql, args = "SELECT * FROM piece WHERE run_id = ?", [run_id]
            if status:
                sql += " AND status = ?"
                args.append(status)
            return [dict(row) for row in conn.execute(sql + " ORDER BY edition", args)]

    # -- answering --

    def offer_piece(self, run_id: str, edition: int) -> None:
        """Piece N is out there as an offer. Not a broadcast: nothing here
        broadcasts, which is the whole point of the book."""
        with self._open() as conn:
            conn.execute("UPDATE piece SET status = 'sending' WHERE run_id = ? "
                         "AND edition = ? AND status = 'pending'", (run_id, edition))
            self._moved(conn, run_id)

    def record_piece(self, run_id: str, edition: int, txid: str) -> None:
        """The chain has the piece. Called when the node accepts the broadcast,
        so a crash after this and before anything else loses nothing."""
        with self._open() as conn:
            conn.execute("UPDATE piece SET status = 'sent', txid = ?, error = '' "
                         "WHERE run_id = ? AND edition = ?", (txid, run_id, edition))
            self._moved(conn, run_id)

    def piece_failed(self, run_id: str, edition: int, error: str) -> None:
        with self._open() as conn:
            conn.execute("UPDATE piece SET status = 'failed', error = ? "
                         "WHERE run_id = ? AND edition = ?", (error, run_id, edition))

    def retry_failed(self, run_id: str) -> int:
        """Failed pieces back to pending for a resume. Their inscription id is
        already written down, so a retry inscribes the same piece, not a new
        one -- which is the only reason a retry is allowed at all."""
        with self._open() as conn:
            return conn.execute(
                "UPDATE piece SET status = 'pending', error = '' WHERE run_id = ? "
                "AND status = 'failed'", (run_id,)).rowcount

    def set_status(self, run_id: str, status: str, note: str | None = None,
                   error: str | None = None,
                   only_from: tuple[str, ...] = ()) -> bool:
        """Write the status down. False if `only_from` said otherwise -- one
        atomic compare-and-set, for the same reason as in collections.py: a
        check in Python first is wrong because the answer changes between
        asking and writing."""
        assert status in STATUSES, status
        with self._open() as conn:
            sets, args = ["status = ?"], [status]
            if note is not None:
                sets.append("note = ?")
                args.append(note)
            if error is not None:
                sets.append("error = ?")
                args.append(error)
            args.append(run_id)
            where = "WHERE id = ?"
            if only_from:
                where += f" AND status IN ({','.join('?' * len(only_from))})"
                args += list(only_from)
            return conn.execute(
                f"UPDATE run SET {', '.join(sets)} {where}", args).rowcount > 0

    def note(self, run_id: str, note: str) -> None:
        with self._open() as conn:
            conn.execute("UPDATE run SET note = ? WHERE id = ?", (note, run_id))

    def delete(self, run_id: str) -> None:
        """Rows only. The uploaded build stays on disk -- see the retention
        note at the top of this file."""
        with self._open() as conn:
            conn.execute("DELETE FROM piece WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM run WHERE id = ?", (run_id,))

    # -- --

    def _moved(self, conn: sqlite3.Connection, run_id: str) -> None:
        """Keep the run's status honest about its pieces.

        `done` is derived, never asked for: the last `record_piece` is the only
        event that can make a run finished, and a run whose status is set by
        hand can disagree with its own pieces, which is how a run looks
        finished while owing thirty pieces.
        """
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM piece WHERE run_id = ? "
            "AND status IN ('pending', 'sending')", (run_id,)).fetchone()["n"]
        if not left:
            conn.execute("UPDATE run SET status = 'done' WHERE id = ? "
                         "AND status != 'done'", (run_id,))
        else:
            conn.execute("UPDATE run SET status = 'running' WHERE id = ? "
                         "AND status = 'open'", (run_id,))


def _progress(conn: sqlite3.Connection, run_id: str) -> dict:
    counts = {"pending": 0, "sending": 0, "sent": 0, "failed": 0}
    for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM piece WHERE run_id = ? "
            "GROUP BY status", (run_id,)):
        counts[row["status"]] = row["n"]
    return {"sent": counts["sent"], "pending": counts["pending"],
            "sending": counts["sending"], "failed_pieces": counts["failed"],
            # What the next one is called, for the sentence on the page.
            "next": _next_label(conn, run_id)}


def _next_label(conn: sqlite3.Connection, run_id: str) -> str:
    row = conn.execute(
        "SELECT name FROM piece WHERE run_id = ? AND status IN ('pending','sending') "
        "ORDER BY status = 'pending', edition LIMIT 1", (run_id,)).fetchone()
    return "" if row is None else row["name"]
