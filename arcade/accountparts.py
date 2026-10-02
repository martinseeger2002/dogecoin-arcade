"""One inscription that needs more than one transaction, and which of them are
still owed.

Why there is a book at all
--------------------------
`/account/inscribe` answers one request with one unsigned transaction, and that
is enough for anything under 7,646 bytes. Anything bigger is several: one
transaction that splits the account's coins into one output per piece, then one
per piece, each spending its own output. That is the shape the carriage was
built for -- `arcade/inscriptions.py`: "a wallet split into separate outputs
funds them all at once, so a megabyte goes out in one pass" -- and the node has
always done exactly that for itself (`inscribe.prepare_wallet` →
`sender.ensure_outputs` → `sender.split_outputs`), which splits, waits ONE
confirmation, and then sends every chunk together. An account can use neither,
because both are wallet calls, so its browser makes the requests instead: N+1 of
them, one signature at a time.

The one block is not a detail. A piece spends an output of the split, so while
the split sits in the mempool every piece broadcast is a grandchild of it, and
what the chain counts is the whole package under that parent: about a hundred
kilobytes, which is two of these transactions. The third came back
`too-long-mempool-chain`, measured. So the book's job is to know that the split
is out and pieces 3 to 26 are owed -- and to keep offering nothing until the
split is in a block, after which they all go at once and none waits on another.

A browser is not a place to keep a promise. It closes, the phone locks, the
offer expires at five minutes. So something has to know that the split went out
and chunks 3 to 26 are still owed, and it cannot be the node's own job book:
`collections.Runner.resume_interrupted` starts a thread for every `running` row
it finds and signs with the node's own wallet, which is the one thing an
account's business may never be mixed with (docs/multi-user.md §5). Hence a
third book, beside `accountruns.py`.

Nothing here starts anything, exactly as in `accountruns.py`. `due()` is the
question a request asks. The pieces go out when the account's own tab comes back
for the next offer, and never any faster.

Why this node keeps no copy of the file
---------------------------------------
An account's file is bytes it typed into a request, not a folder it uploaded --
`/account/inscribe` was never meant to hold on to it, and a node that kept every
file every account ever inscribed would be a very different machine than the one
described in §6. So what is written down is not the file but everything needed
to CHECK a file that is supplied again: the hash of the whole thing, the hash of
every piece, the framing (`chunk_len` and the first chunk's manifest prefix), and
the 8-byte inscription id that ties the pieces together. A resume re-supplies
the file and is refused by digest if it is not the same file -- which matters
because a piece is permanent and paid for, and pieces of two different files
would assemble into a third file nobody chose.

Two unfinished jobs at once is allowed
--------------------------------------
`accountruns.due()` enforces §6's "one active run at a time" because a collection
is one set of coins and two runs against one folder is how an edition gets paid
for twice. These are not that: each job funded its own split, so a second job
costs its owner money and nothing else, which is the same reasoning behind
`OFFERS_WAITING` -- an allowance is a number for the thing that is free to ask
for. `find()` is what keeps the common case honest: the same file again
continues the job that is waiting rather than starting a second copy beside it.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from .db import add_missing_columns

SCHEMA = """
CREATE TABLE IF NOT EXISTS part (
    id          TEXT PRIMARY KEY,
    created     REAL NOT NULL,
    network     TEXT NOT NULL,
    -- The account, by pubkey, as in `run.account`: it is what the browser
    -- proves a key for, and a name can be taken by anyone.
    account     TEXT NOT NULL,
    address     TEXT NOT NULL,
    name        TEXT NOT NULL,
    content_type TEXT NOT NULL,
    json        TEXT NOT NULL,
    -- What the file was, so a resume can be checked and a page can say what it
    -- is waiting for. `sha256` is over the content alone, which is also what
    -- the inscription's own Manifest carries -- the same number, used here to
    -- find the job again rather than to prove the assembly.
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    -- The 8 bytes every chunk of this inscription carries. Drawn once, here, so
    -- that chunk 9 after a week belongs to the same inscription as chunk 1
    -- before it. `inscribe.plan` would have drawn one itself and lost it.
    inscription_id TEXT NOT NULL,
    chunks      INTEGER NOT NULL,
    -- Bytes of CONTENT in every chunk but the last, and the leading bytes of
    -- the stream that are not content at all. Together they let the browser
    -- slice the file at exactly the boundaries the node planned, without the
    -- browser ever having to encode a Manifest.
    chunk_len   INTEGER NOT NULL,
    manifest    TEXT NOT NULL,
    -- What each split output is worth, which is what each piece transaction
    -- starts with. Recorded because a piece has to be built the same way after
    -- a restart as before it, and `inscribe.piece_size` would answer differently
    -- if the fee tables ever moved.
    piece       INTEGER NOT NULL,
    split_txid  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    fee         REAL NOT NULL,
    dust        REAL NOT NULL,
    floor       INTEGER
);
CREATE TABLE IF NOT EXISTS chunk (
    part_id   TEXT NOT NULL,
    n         INTEGER NOT NULL,       -- 0 .. chunks-1, the order plan() gave
    digest    BLOB NOT NULL,          -- sha256 of that chunk's CONTENT bytes
    status    TEXT NOT NULL,
    txid      TEXT NOT NULL DEFAULT '',
    error     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (part_id, n)
);
CREATE INDEX IF NOT EXISTS chunk_part_status_idx ON chunk(part_id, status);
CREATE INDEX IF NOT EXISTS part_account_idx ON part(account, status);
CREATE INDEX IF NOT EXISTS part_sha_idx ON part(sha256, network, account);
"""

#: `open` is written down with no transaction out. `running` is the split on the
#: chain (or at least accepted by the node) with pieces still owed. `failed` is
#: derived like a run's: pieces remain and every one this node tried to build
#: was refused, which is not a finished thing. `stopped` is a person's filing,
#: and no request walks it back from the inside.
STATUSES = ("open", "running", "done", "failed", "stopped")

#: As in `accountruns.PIECE_STATUSES`. `sending` means an offer is standing, and
#: is only there so a page can say which chunk the account is being asked about.
CHUNK_STATUSES = ("pending", "sending", "sent", "failed")


class Parts:
    """Half-finished inscriptions, on disk. One connection per call, as in the
    other two books: the web thread is the only one that writes, and it is not
    always the same thread."""

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

    # -- writing a job down --

    def create(self, account: str, address: str, network: str, name: str,
               content_type: str, json_text: str, size: int, sha256: str,
               inscription_id: str, contents: list[bytes], piece: int,
               chunk_len: int, manifest: bytes, fee: float, dust: float,
               floor: int | None = None) -> str:
        """Write the job and one row per chunk, each with its own digest.

        `contents` is the file sliced exactly as the plan sliced it, so
        `b"".join(contents[1:])` is the file minus whatever the Manifest ate from
        the front; the digests of these are what a chunk request is checked
        against, and they are computed HERE, from the bytes that produced the
        plan, rather than ever being re-derived from something shorter.
        """
        job = secrets.token_hex(6)
        with self._open() as conn:
            conn.execute("BEGIN")
            for n, body in enumerate(contents):
                conn.execute(
                    "INSERT INTO chunk (part_id, n, digest, status) "
                    "VALUES (?,?,?,?)",
                    (job, n, hashlib.sha256(body).digest(), "pending"))
            conn.execute(
                "INSERT INTO part (id, created, network, account, address, name, "
                "content_type, json, size, sha256, inscription_id, chunks, "
                "chunk_len, manifest, piece, status, fee, dust, floor) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job, time.time(), network, account, address, name,
                 content_type, json_text, int(size), sha256, inscription_id,
                 len(contents), int(chunk_len), manifest.hex(), int(piece),
                 "open", round(fee, 8), round(dust, 8), floor))
            conn.execute("COMMIT")
        return job

    # -- asking --

    def get(self, job: str) -> dict | None:
        with self._open() as conn:
            row = conn.execute("SELECT * FROM part WHERE id = ?", (job,)).fetchone()
            if row is None:
                return None
            part = dict(row)
            part.update(_progress(conn, job))
            return part

    def list(self, account: str | None = None,
             network: str | None = None) -> list[dict]:
        with self._open() as conn:
            sql, args = "SELECT * FROM part", []
            if account:
                sql += " WHERE account = ?"
                args.append(account)
            if network:
                sql += (" WHERE" if "WHERE" not in sql else " AND") + " network = ?"
                args.append(network)
            rows = conn.execute(sql + " ORDER BY created DESC", args).fetchall()
            out = []
            for row in rows:
                part = dict(row)
                part.update(_progress(conn, part["id"]))
                out.append(part)
            return out

    def due(self, account: str, network: str) -> dict | None:
        """The job this account owes chunks to, newest first."""
        with self._open() as conn:
            row = conn.execute(
                "SELECT * FROM part WHERE account = ? AND network = ? "
                "AND status IN ('open', 'running', 'failed') "
                "ORDER BY created DESC LIMIT 1", (account, network)).fetchone()
            if row is None:
                return None
            part = dict(row)
            part.update(_progress(conn, part["id"]))
            return part

    def find(self, account: str, network: str, sha256: str) -> dict | None:
        """The unfinished job for THIS file, which is how a resume is recognised.

        Matching on the content hash rather than on an id the browser may no
        longer holds is deliberate: the file is the thing the person still has,
        and asking them to have kept a six-byte token from a tab they closed
        would be asking them to remember a number nobody remembers.
        """
        with self._open() as conn:
            row = conn.execute(
                "SELECT * FROM part WHERE account = ? AND network = ? "
                "AND sha256 = ? AND status IN ('open', 'running', 'failed') "
                "ORDER BY created DESC LIMIT 1",
                (account, network, sha256)).fetchone()
            if row is None:
                return None
            part = dict(row)
            part.update(_progress(conn, part["id"]))
            return part

    def chunks(self, job: str, status: str | None = None) -> list[dict]:
        with self._open() as conn:
            sql, args = "SELECT * FROM chunk WHERE part_id = ?", [job]
            if status:
                sql += " AND status = ?"
                args.append(status)
            return [dict(r) for r in conn.execute(sql + " ORDER BY n", args)]

    def next_chunk(self, job: str) -> dict | None:
        """The lowest piece still owed. An offered one comes before one never
        offered, which is also what rebuilds the same piece after an offer
        expired rather than skipping it and paying for a hole."""
        with self._open() as conn:
            row = conn.execute(
                "SELECT * FROM chunk WHERE part_id = ? "
                "AND status IN ('sending', 'pending') "
                "ORDER BY status = 'pending', n LIMIT 1", (job,)).fetchone()
            return dict(row) if row else None

    # -- answering --

    def note_split(self, job: str, txid: str) -> None:
        """The split is out, so every piece now has an output to spend.

        Called from the offer's `done` hook, which is the only place that knows a
        broadcast happened. The pieces DO wait for this transaction's BLOCK --
        not because they chain but because they are its grandchildren, and the
        package under an unconfirmed parent is capped at about a hundred
        kilobytes (`_inscribe_piece` in web/app.py, and the module docstring).
        What they do not wait for is a block each, which is the difference
        between a megabyte in one pass and one piece per ten minutes.
        """
        with self._open() as conn:
            conn.execute("UPDATE part SET split_txid = ?, status = 'running' "
                         "WHERE id = ? AND status != 'done'", (txid, job))

    def reserved(self, network: str = "") -> frozenset:
        """The split outputs still owed to a piece, as (txid, vout).

        A split pays its outputs to the account's own address, so once it is out
        they look like any other coin of that account -- and the next send took
        them (2026-10-01: a script inscribed a few small files right after a
        7-piece one, the small ones spent its split, and every piece of it came
        back `bad-txns-inputs-spent`). Every funding choice reads this through
        `spent_by`, so nothing else is paid out of them until the job is
        finished or given up.
        """
        sql = ("SELECT p.split_txid, c.n FROM part p JOIN chunk c ON c.part_id = p.id "
               "WHERE p.split_txid != '' AND p.status IN ('open', 'running', 'failed') "
               "AND c.status != 'sent'")
        args: list = []
        if network:
            sql += " AND p.network = ?"
            args.append(network)
        with self._open() as conn:
            return frozenset((r["split_txid"], int(r["n"]))
                             for r in conn.execute(sql, args))

    def resplit(self, job: str) -> None:
        """Forget a split whose outputs are gone, so the job is offered a new one.

        Only for a job with no piece on the chain: every piece then spends an
        output of the new split, numbered as before."""
        with self._open() as conn:
            conn.execute("UPDATE part SET split_txid = '', status = 'open', "
                         "note = 'its split was spent by another send; split again' "
                         "WHERE id = ?", (job,))
            conn.execute("UPDATE chunk SET status = 'pending', error = '' "
                         "WHERE part_id = ? AND status != 'sent'", (job,))

    def offer_chunk(self, job: str, n: int) -> None:
        with self._open() as conn:
            conn.execute("UPDATE chunk SET status = 'sending' WHERE part_id = ? "
                         "AND n = ? AND status = 'pending'", (job, n))
            self._moved(conn, job)

    def record_chunk(self, job: str, n: int, txid: str) -> None:
        with self._open() as conn:
            conn.execute("UPDATE chunk SET status = 'sent', txid = ?, error = '' "
                         "WHERE part_id = ? AND n = ?", (txid, job, n))
            self._moved(conn, job)

    def chunk_failed(self, job: str, n: int, error: str) -> None:
        """This node could not build that piece. Not a refused broadcast -- a
        refused broadcast leaves the chunk `sending` so it is offered again."""
        with self._open() as conn:
            conn.execute("UPDATE chunk SET status = 'failed', error = ? "
                         "WHERE part_id = ? AND n = ?", (error, job, n))
            self._moved(conn, job)

    def retry_failed(self, job: str) -> int:
        with self._open() as conn:
            left = conn.execute(
                "UPDATE chunk SET status = 'pending', error = '' "
                "WHERE part_id = ? AND status = 'failed'", (job,)).rowcount
            self._moved(conn, job)
            return left

    def set_status(self, job: str, status: str, note: str | None = None,
                   error: str | None = None,
                   only_from: tuple[str, ...] = ()) -> bool:
        assert status in STATUSES, status
        with self._open() as conn:
            sets, args = ["status = ?"], [status]
            if note is not None:
                sets.append("note = ?")
                args.append(note)
            if error is not None:
                sets.append("error = ?")
                args.append(error)
            args.append(job)
            where = "WHERE id = ?"
            if only_from:
                where += f" AND status IN ({','.join('?' * len(only_from))})"
                args += list(only_from)
            return conn.execute(
                f"UPDATE part SET {', '.join(sets)} {where}", args).rowcount > 0

    def delete(self, job: str) -> None:
        with self._open() as conn:
            conn.execute("DELETE FROM chunk WHERE part_id = ?", (job,))
            conn.execute("DELETE FROM part WHERE id = ?", (job,))

    # -- --

    def _moved(self, conn: sqlite3.Connection, job: str) -> None:
        """Keep the job's status honest about its chunks, as in `accountruns`.

        `done` is never set by hand: the last `record_chunk` is the only event
        that can finish an inscription, and a job set to `done` by a request that
        merely counted something is a job that says it is on the chain while
        owing eleven pieces.
        """
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM chunk WHERE part_id = ? "
            "AND status IN ('pending', 'sending')", (job,)).fetchone()["n"]
        refused = conn.execute(
            "SELECT COUNT(*) AS n FROM chunk WHERE part_id = ? "
            "AND status = 'failed'", (job,)).fetchone()["n"]
        if not left and not refused:
            conn.execute("UPDATE part SET status = 'done' WHERE id = ? "
                         "AND status != 'done'", (job,))
        elif not left:
            conn.execute("UPDATE part SET status = 'failed' WHERE id = ? "
                         "AND status != 'failed'", (job,))
        else:
            conn.execute("UPDATE part SET status = 'running' WHERE id = ? "
                         "AND status IN ('open', 'failed')", (job,))


def _progress(conn: sqlite3.Connection, job: str) -> dict:
    counts = {"pending": 0, "sending": 0, "sent": 0, "failed": 0}
    for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM chunk WHERE part_id = ? "
            "GROUP BY status", (job,)):
        counts[row["status"]] = row["n"]
    return {"sent": counts["sent"], "pending": counts["pending"],
            "sending": counts["sending"], "failed_chunks": counts["failed"],
            "next": _next_number(conn, job)}


def _next_number(conn: sqlite3.Connection, job: str) -> int | None:
    row = conn.execute(
        "SELECT n FROM chunk WHERE part_id = ? AND status IN ('pending','sending') "
        "ORDER BY status = 'pending', n LIMIT 1", (job,)).fetchone()
    return None if row is None else int(row["n"])
