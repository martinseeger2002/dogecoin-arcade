"""Inscribing a whole collection: a HashLips build folder, item by item.

The HashLips Art Engine leaves behind `build/images/1.png ... N.png` and
`build/json/_metadata.json` (every item's metadata in one array, also written
out one file per item as `json/1.json`). Each item's metadata is an object
like

    {"name": "Doge Punks #1", "description": "...", "image": "ipfs://.../1.png",
     "dna": "8b9f...", "edition": 1, "date": 1690000000000,
     "attributes": [{"trait_type": "Background", "value": "Blue"}, ...],
     "compiler": "HashLips Art Engine"}

and that object goes into the inscription's JSON field exactly as it is --
compacted, not changed -- so the collection, the edition and the traits are on
the chain with the picture and every node files them the same way
(`inscriptions.collection_of`). Nothing here invents a format: what people
have on disk is what goes up.

A collection is thousands of inscriptions and hours of blocks, so the run is a
JOB, kept in its own SQLite file under the arcade's home rather than in a
thread's memory. Every piece that goes out is written down the moment the node
accepts it, before anything else can fail. That is what makes the three things
asked of it possible:

  pause    -- the thread stops between one piece and the next and says so;
  resume   -- the next piece is the first one not yet written down;
  a crash  -- the same, on the next start: a job that was running is resumed,
              and pieces that went out in the seconds before the crash but were
              never written down are found in the index (`chunks_seen`) rather
              than sent twice.

A piece sent twice is not a disaster -- the engine refuses the duplicate -- but
it is a fee for nothing, and the job's whole job is not to pay twice.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import inscribe as inscribelib
from . import inscriptions as I

log = logging.getLogger("arcade.collections")

IMAGE_SUFFIXES = (".png", ".gif", ".jpg", ".jpeg", ".webp", ".svg", ".mp4", ".webm")

#: How many pieces to give their own output in one split. A split is one
#: block's wait, once; the pieces then go as fast as the node takes them. A
#: whole 10,000-item collection at once would need more coins than most
#: wallets hold on one address, so the run tops up in batches of this many.
SPLIT_BATCH = 120

#: Between checks while waiting on a block or on the send lock.
POLL = 3.0


class CollectionError(Exception):
    """The folder is not a collection this understands."""


# --- reading a HashLips build -------------------------------------------------

@dataclass
class Item:
    edition: int
    name: str
    json: str            # the item's metadata, compact, exactly as inscribed
    image: str           # path relative to the build folder
    content_type: str
    size: int


@dataclass
class Build:
    folder: Path
    items: list[Item]
    collection: str      # what the names say the set is called
    problems: list[str] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return sum(item.size for item in self.items)


def compact(data: Any) -> str:
    """The JSON as it goes on chain: same data, no whitespace to pay for."""
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def find_build(folder: Path) -> Path:
    """The build folder, given it or something near it.

    People point at `build`, at `build/json`, at the project folder above
    `build`, or at the `_metadata.json` file itself. All of those mean the
    same folder.
    """
    folder = Path(folder).expanduser()
    if folder.is_file():
        folder = folder.parent
    candidates = (folder, folder.parent, folder / "build")
    for candidate in candidates:
        if (candidate / "json" / "_metadata.json").is_file():
            return candidate
    for candidate in candidates:
        if (candidate / "json").is_dir() and any(
                p.suffix == ".json" for p in (candidate / "json").iterdir()):
            return candidate
    if (folder / "_metadata.json").is_file():
        return folder
    raise CollectionError(
        f"{folder} does not look like a HashLips build: no json/_metadata.json "
        f"and no json/<edition>.json files.")


def read_build(folder: Path) -> Build:
    """Everything in a HashLips build folder, checked against itself.

    An item with no image, or an image with no metadata, is a problem the
    interface shows before anything is paid for -- half a collection on the
    chain with the numbering out of step is not something to find out about
    afterwards.
    """
    folder = find_build(folder)
    json_dir = folder / "json" if (folder / "json").is_dir() else folder
    images_dir = folder / "images" if (folder / "images").is_dir() else folder
    problems: list[str] = []

    metadata: list[Any] = []
    combined = json_dir / "_metadata.json"
    if combined.is_file():
        try:
            metadata = json.loads(combined.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CollectionError(f"{combined} is not valid JSON: {exc}") from None
        if not isinstance(metadata, list):
            raise CollectionError(f"{combined} should be a list of items.")
    else:
        for path in sorted(json_dir.glob("*.json")):
            if not path.stem.isdigit():
                continue
            try:
                metadata.append(json.loads(path.read_text(encoding="utf-8")))
            except ValueError as exc:
                problems.append(f"{path.name} is not valid JSON: {exc}")
    if not metadata:
        raise CollectionError(f"no items found in {json_dir}.")

    images: dict[str, Path] = {}
    for path in images_dir.iterdir():
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            images[path.stem] = path

    items: list[Item] = []
    seen: set[int] = set()
    for n, entry in enumerate(metadata, 1):
        if not isinstance(entry, dict):
            problems.append(f"item {n} is not an object")
            continue
        edition = entry.get("edition")
        if isinstance(edition, bool) or not isinstance(edition, int):
            member = I.collection_of(compact(entry))
            edition = member[1] if member and member[1] is not None else n
        if edition in seen:
            problems.append(f"edition {edition} appears twice")
            continue
        seen.add(edition)
        name = entry.get("name") if isinstance(entry.get("name"), str) else f"#{edition}"
        image_field = entry.get("image") if isinstance(entry.get("image"), str) else ""
        stem = Path(image_field.rsplit("/", 1)[-1]).stem if image_field else ""
        path = images.get(str(edition)) or (images.get(stem) if stem else None)
        if path is None:
            problems.append(f"{name}: no image for edition {edition} in {images_dir}")
            continue
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        items.append(Item(edition=edition, name=name, json=compact(entry),
                          image=str(path.relative_to(folder)),
                          content_type=kind, size=path.stat().st_size))
    items.sort(key=lambda item: item.edition)
    if len(images) > len(items):
        spare = len(images) - len(items)
        problems.append(f"{spare} image(s) in {images_dir} have no metadata "
                        f"and will not be inscribed")

    collection = ""
    for item in items:
        member = I.collection_of(item.json)
        if member:
            collection = member[0]
            break
    return Build(folder=folder, items=items, collection=collection,
                 problems=problems)


def estimate_build(build: Build) -> dict[str, Any]:
    """What the whole set will cost, item by item, before anything is spent."""
    chunks = fee = dust = 0.0
    for item in build.items:
        est = inscribelib.estimate(item.size, item.content_type, item.json)
        chunks += est.chunks
        fee += est.fee
        dust += est.dust
    return {"items": len(build.items), "bytes": build.bytes, "chunks": int(chunks),
            "fee": round(fee, 8), "dust": round(dust, 8),
            "total": round(fee + dust, 8)}


# --- the job store ------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS job (
    id          TEXT PRIMARY KEY,
    created     REAL NOT NULL,
    network     TEXT NOT NULL,
    sender      TEXT NOT NULL,
    name        TEXT NOT NULL,
    folder      TEXT NOT NULL,
    status      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    items       INTEGER NOT NULL,
    chunks      INTEGER NOT NULL,
    fee         REAL NOT NULL,
    dust        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS item (
    job_id        TEXT NOT NULL,
    edition       INTEGER NOT NULL,
    name          TEXT NOT NULL,
    image         TEXT NOT NULL,
    content_type  TEXT NOT NULL,
    size          INTEGER NOT NULL,
    json          TEXT NOT NULL,
    inscription_id TEXT NOT NULL,
    chunks        INTEGER NOT NULL,
    status        TEXT NOT NULL,
    txids         TEXT NOT NULL DEFAULT '{}',
    txid          TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (job_id, edition)
);
CREATE INDEX IF NOT EXISTS item_status_idx ON item(job_id, status);
"""

#: A job is one of these. `running` survives a restart on purpose: it is
#: what tells the next start to pick the job back up.
STATUSES = ("running", "pausing", "paused", "done", "failed")


class Jobs:
    """Collection runs, on disk. One connection per call; the runner thread
    and the web thread each open their own."""

    def __init__(self, path: Path):
        self.path = Path(path)
        with self._open() as conn:
            conn.executescript(SCHEMA)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def create(self, network: str, sender: str, build: Build,
               name: str = "") -> str:
        """Write the job down, every item with the id its pieces will carry."""
        cost = estimate_build(build)
        job_id = secrets.token_hex(6)
        with self._open() as conn:
            conn.execute("BEGIN")
            conn.execute(
                "INSERT INTO job (id, created, network, sender, name, folder, "
                "status, items, chunks, fee, dust) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, time.time(), network, sender, name or build.collection,
                 str(build.folder), "paused", cost["items"], cost["chunks"],
                 cost["fee"], cost["dust"]))
            for item in build.items:
                est = inscribelib.estimate(item.size, item.content_type, item.json)
                conn.execute(
                    "INSERT INTO item (job_id, edition, name, image, content_type, "
                    "size, json, inscription_id, chunks, status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (job_id, item.edition, item.name, item.image, item.content_type,
                     item.size, item.json, secrets.token_bytes(8).hex(),
                     est.chunks, "pending"))
            conn.execute("COMMIT")
        return job_id

    def get(self, job_id: str) -> dict | None:
        with self._open() as conn:
            row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            job = dict(row)
            job.update(self._progress(conn, job_id))
            return job

    def list(self, network: str | None = None) -> list[dict]:
        with self._open() as conn:
            sql, args = "SELECT * FROM job", []
            if network:
                sql += " WHERE network = ?"
                args.append(network)
            rows = conn.execute(sql + " ORDER BY created DESC", args).fetchall()
            out = []
            for row in rows:
                job = dict(row)
                job.update(self._progress(conn, job["id"]))
                out.append(job)
            return out

    @staticmethod
    def _progress(conn: sqlite3.Connection, job_id: str) -> dict:
        counts = {"pending": 0, "sending": 0, "sent": 0, "failed": 0}
        for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM item WHERE job_id = ? "
                "GROUP BY status", (job_id,)):
            counts[row["status"]] = row["n"]
        pieces = conn.execute(
            "SELECT txids FROM item WHERE job_id = ? AND txids != '{}'",
            (job_id,)).fetchall()
        sent_chunks = sum(len(json.loads(row["txids"])) for row in pieces)
        return {"sent": counts["sent"], "pending": counts["pending"],
                "sending": counts["sending"], "failed_items": counts["failed"],
                "sent_chunks": sent_chunks}

    def items(self, job_id: str, status: str | None = None,
              limit: int | None = None, offset: int = 0) -> list[dict]:
        with self._open() as conn:
            sql, args = "SELECT * FROM item WHERE job_id = ?", [job_id]
            if status:
                sql += " AND status = ?"
                args.append(status)
            sql += " ORDER BY edition"
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                args += [limit, offset]
            return [dict(row) for row in conn.execute(sql, args)]

    def next_item(self, job_id: str) -> dict | None:
        """The lowest edition still to go: one half sent comes before one not
        started, so a crash mid-item is finished before the next begins."""
        with self._open() as conn:
            row = conn.execute(
                "SELECT * FROM item WHERE job_id = ? AND status IN ('sending', 'pending') "
                "ORDER BY status = 'pending', edition LIMIT 1", (job_id,)).fetchone()
            return dict(row) if row else None

    def upcoming(self, job_id: str, items: int) -> tuple[int, int]:
        """(pieces, sats per piece) the next `items` items need, for a split.

        The piece is sized for the LARGEST item ahead, so one split serves
        every item in the batch rather than the smaller ones only.
        """
        with self._open() as conn:
            rows = conn.execute(
                "SELECT chunks, size, content_type, json FROM item "
                "WHERE job_id = ? AND status IN ('sending', 'pending') "
                "ORDER BY edition LIMIT ?", (job_id, items)).fetchall()
        pieces = sum(row["chunks"] for row in rows)
        biggest = 0
        for row in rows:
            est = inscribelib.estimate(row["size"], row["content_type"], row["json"])
            biggest = max(biggest, inscribelib.piece_size_for(est))
        return pieces, biggest

    def set_status(self, job_id: str, status: str, note: str | None = None,
                   error: str | None = None) -> None:
        assert status in STATUSES, status
        with self._open() as conn:
            sets, args = ["status = ?"], [status]
            if note is not None:
                sets.append("note = ?"); args.append(note)
            if error is not None:
                sets.append("error = ?"); args.append(error)
            args.append(job_id)
            conn.execute(f"UPDATE job SET {', '.join(sets)} WHERE id = ?", args)

    def note(self, job_id: str, note: str) -> None:
        with self._open() as conn:
            conn.execute("UPDATE job SET note = ? WHERE id = ?", (note, job_id))

    def item_status(self, job_id: str, edition: int, status: str,
                    error: str = "") -> None:
        with self._open() as conn:
            conn.execute("UPDATE item SET status = ?, error = ? WHERE job_id = ? "
                         "AND edition = ?", (status, error, job_id, edition))

    def record_piece(self, job_id: str, edition: int, countdown: int, txid: str,
                     manifest: bool) -> None:
        """One piece is on its way. Written before anything else happens."""
        with self._open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT txids FROM item WHERE job_id = ? AND edition = ?",
                               (job_id, edition)).fetchone()
            pieces = json.loads(row["txids"]) if row else {}
            pieces[str(countdown)] = txid
            conn.execute(
                "UPDATE item SET txids = ?, status = 'sending'"
                + (", txid = ?" if manifest else "")
                + " WHERE job_id = ? AND edition = ?",
                (json.dumps(pieces), *( [txid] if manifest else [] ), job_id, edition))
            conn.execute("COMMIT")

    def delete(self, job_id: str) -> None:
        with self._open() as conn:
            conn.execute("DELETE FROM item WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM job WHERE id = ?", (job_id,))


# --- the runner ---------------------------------------------------------------

class Runner:
    """Sends a job's items on a thread, one piece at a time, writing as it goes.

    `chain_for(network)` gives the chain context (with `.rpc()` and
    `.params`), `index_for(network)` its ledger index, and `send_lock` is the
    application's one-send-at-a-time lock as a pair of (begin, end) callables
    -- held per item, not per job, so a message can get a word in between
    two items of a run that takes all night.
    """

    def __init__(self, jobs: Jobs, chain_for: Callable[[str], Any],
                 index_for: Callable[[str], Any],
                 send_lock: tuple[Callable[[], bool], Callable[[], None]] | None = None,
                 make_sender: Callable[[Any, Any], Any] | None = None):
        self.jobs = jobs
        self.chain_for = chain_for
        self.index_for = index_for
        self.begin_send, self.end_send = send_lock or ((lambda: True), (lambda: None))
        self.make_sender = make_sender or self._default_sender
        self._threads: dict[str, threading.Thread] = {}
        self._stop: set[str] = set()
        self._lock = threading.Lock()

    @staticmethod
    def _default_sender(rpc: Any, params: Any) -> Any:
        from .messaging.sender import MessageSender
        # public_only=True: an inscription is public, uncompressed, unencrypted
        # data. Nothing sealed passes this flag (D-014).
        return MessageSender(rpc, params, public_only=True)

    # -- control --

    def running(self, job_id: str) -> bool:
        thread = self._threads.get(job_id)
        return thread is not None and thread.is_alive()

    def start(self, job_id: str) -> bool:
        """Run (or resume) a job. False if it is already running."""
        with self._lock:
            if self.running(job_id):
                return False
            self._stop.discard(job_id)
            self.jobs.set_status(job_id, "running", note="starting", error="")
            thread = threading.Thread(target=self._run, args=(job_id,),
                                      name=f"arcade-collection-{job_id}", daemon=True)
            self._threads[job_id] = thread
            thread.start()
            return True

    def pause(self, job_id: str) -> None:
        """Stop after the piece in flight. The thread reports when it has."""
        with self._lock:
            if not self.running(job_id):
                job = self.jobs.get(job_id)
                if job and job["status"] in ("running", "pausing"):
                    self.jobs.set_status(job_id, "paused", note="paused")
                return
            self._stop.add(job_id)
            self.jobs.set_status(job_id, "pausing", note="finishing the piece in flight")

    def resume_interrupted(self) -> list[str]:
        """Pick up every job that was running when the process last stopped.

        Called once at startup. A job that was `running` or `pausing` in the
        store and has no thread was cut off -- by a crash, a restart, a
        power cut -- and the whole point of writing it down was this.
        """
        resumed = []
        for job in self.jobs.list():
            if job["status"] in ("running", "pausing") and not self.running(job["id"]):
                self.jobs.note(job["id"], "resuming after a restart")
                if self.start(job["id"]):
                    resumed.append(job["id"])
        return resumed

    def _stopping(self, job_id: str) -> bool:
        return job_id in self._stop

    # -- the work --

    def _run(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        try:
            chain = self.chain_for(job["network"])
            index = self.index_for(job["network"])
            with chain.rpc() as rpc:
                sender_obj = self.make_sender(rpc, chain.params)
                while True:
                    if self._stopping(job_id):
                        self.jobs.set_status(job_id, "paused", note="paused")
                        return
                    item = self.jobs.next_item(job_id)
                    if item is None:
                        break
                    job = self.jobs.get(job_id)
                    self._hold_send_lock(job_id)
                    if self._stopping(job_id):
                        self.jobs.set_status(job_id, "paused", note="paused")
                        return
                    try:
                        outcome = self._send_item(job, item, sender_obj, index)
                    finally:
                        self.end_send()
                    if outcome == "paused":
                        self.jobs.set_status(job_id, "paused", note="paused")
                        return
            failed = self.jobs.get(job_id)["failed_items"]
            if failed:
                self.jobs.set_status(job_id, "failed",
                                     note=f"{failed} item(s) could not be sent")
            else:
                self.jobs.set_status(job_id, "done", note="every item is on its way")
        except Exception as exc:
            log.exception("collection %s stopped", job_id)
            self.jobs.set_status(job_id, "paused", note="stopped", error=str(exc))
        finally:
            self._stop.discard(job_id)

    def _hold_send_lock(self, job_id: str) -> None:
        """Wait for the application's send lock, checking for a pause meanwhile."""
        waited = False
        while not self.begin_send():
            if not waited:
                self.jobs.note(job_id, "waiting for another send to finish")
                waited = True
            if self._stopping(job_id):
                return
            time.sleep(POLL)

    def _send_item(self, job: dict, item: dict, sender_obj: Any, index: Any) -> str:
        """One item: whatever pieces of it are not yet on their way.

        Returns "sent", "failed" or "paused". Every piece is written down as
        the node accepts it; a crash between two pieces loses nothing, and a
        crash between the node accepting a piece and the write is what
        `chunks_seen` covers on the way back in.
        """
        job_id, edition, address = job["id"], item["edition"], job["sender"]
        folder = Path(job["folder"])
        label = f"{item['name']} ({job['sent'] + 1} of {job['items']})"
        try:
            content = (folder / item["image"]).read_bytes()
            plan = inscribelib.plan(content, item["content_type"], item["json"],
                                    inscription_id=bytes.fromhex(item["inscription_id"]))
        except Exception as exc:
            self.jobs.item_status(job_id, edition, "failed", str(exc))
            self.jobs.note(job_id, f"{label}: {exc}")
            return "failed"

        done: dict[str, str] = json.loads(item["txids"])
        try:
            # Pieces the index has already seen under this id count as sent,
            # whether or not this store heard about them: a crash between the
            # broadcast and the write leaves exactly that gap.
            for seen in index.chunks_seen(address, item["inscription_id"]):
                done.setdefault(str(seen["countdown"]), seen["txid"])
            if item["txid"] and index.inscription(item["txid"]):
                self.jobs.item_status(job_id, edition, "sent")
                return "sent"
        except Exception:
            pass                        # the index is a convenience here, not a need

        last = plan.chunks - 1
        remaining = [(last - n, payload) for n, payload in enumerate(plan.payloads)
                     if str(last - n) not in done]
        if not remaining:
            self.jobs.item_status(job_id, edition, "sent")
            return "sent"

        # Fund a batch ahead, so the pieces go out at the node's pace rather
        # than a block apart. Only when the address has run short: a split
        # that is not needed is a fee and a block for nothing.
        try:
            self._fund(job, plan, sender_obj, address, len(remaining))
        except Exception as exc:
            self.jobs.set_status(job_id, "paused", note=f"{label}: could not fund",
                                 error=str(exc))
            return "paused"
        if self._stopping(job_id):
            return "paused"

        self.jobs.item_status(job_id, edition, "sending")
        self.jobs.note(job_id, f"sending {label}: {len(remaining)} piece(s)")

        def approve(n: int, total: int, prepared: Any) -> bool:
            return not self._stopping(job_id)

        def on_broadcast(n: int, total: int, txid: str) -> None:
            countdown = remaining[n - 1][0]
            self.jobs.record_piece(job_id, edition, countdown, txid,
                                   manifest=(countdown == last))
            self.jobs.note(job_id, f"{label}: piece {n} of {total} sent")

        from .messaging.sender import PartialSend
        try:
            txids = sender_obj.send_all(address, [p for _, p in remaining],
                                        approve=approve, on_broadcast=on_broadcast)
        except PartialSend:
            if self._stopping(job_id):
                return "paused"
            raise
        except Exception as exc:
            self.jobs.item_status(job_id, edition, "failed", str(exc))
            self.jobs.note(job_id, f"{label}: {exc}")
            return "failed"
        if not txids and self._stopping(job_id):
            return "paused"
        self.jobs.item_status(job_id, edition, "sent")
        return "sent"

    def _fund(self, job: dict, plan: Any, sender_obj: Any, address: str,
              need: int) -> None:
        """Give the address confirmed outputs for the pieces ahead.

        If it has enough already nothing happens. If not: wait for what this
        run already sent to confirm (a split that spent unconfirmed change
        would chain on it and the node refuses long chains), then split for
        the next batch and wait the one block that takes.
        """
        job_id = job["id"]
        ahead, piece = self.jobs.upcoming(job_id, 200)
        piece = max(piece, inscribelib.piece_size(plan))
        if sender_obj.spendable_outputs(address, at_least=piece // 2, minconf=1) >= need:
            return
        deadline = time.time() + 1800
        while (sender_obj.spendable_outputs(address, minconf=0)
               > sender_obj.spendable_outputs(address, minconf=1)):
            self.jobs.note(job_id, "waiting for a block before splitting the wallet again")
            if self._stopping(job_id) or time.time() > deadline:
                return
            time.sleep(POLL)
        wanted = max(need, min(SPLIT_BATCH, ahead))
        sender_obj.ensure_outputs(
            address, wanted, each_sats=piece,
            on_progress=lambda text, done, total: self.jobs.note(job_id, text))
