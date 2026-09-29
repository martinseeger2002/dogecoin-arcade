"""A copy of the index, so a clone does not start from the beginning.

Re-reading a chain from its floor is correct and slow. Every node has
already done it, and what it produced is derived state — the same blocks
read by the same rules give the same rows on every machine — so there is
no reason for the next person to spend a day repeating the work before
their arcade shows anything.

**It is a convenience, not an authority.** A downloaded index is somebody
else's claim about the chain. What ships beside it is a manifest naming
the height it was taken at, the floor it belongs to, and the **consensus
hash** of the state at that height — which is the number `consensushash.py`
exists to produce, so that a second implementation can prove this one right
or wrong (D-006). A clone continues from that height; anybody who would
rather not trust it re-reads from the floor and compares the hash. The
bootstrap turns a day into a minute and costs nothing in what can be
checked afterwards.

**The messaging store is never in it.** That file holds private messages,
an address book somebody typed, and the identity key. Only the LEDGER
index is published: public chain state, derived, reproducible by anyone.

**It regenerates when it is asked for and has gone stale**, not on a timer.
A node nobody clones from never does the work, which is the same rule the
seat register follows. Taking it is cheap and safe while the indexer is
writing: `VACUUM INTO` makes a consistent copy of a live database, measured
at 0.24s for a 2.4 MB index, 948 KB gzipped.

A stale bootstrap is never WRONG, only further behind: a clone that takes a
week-old one reads a week of blocks instead of none. So the interval is a
convenience and nothing depends on it being short.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

#: Remake it when the copy on disk is older than this, or when the chain has
#: moved this many blocks past it -- whichever comes first.
STALE_SECONDS = 3600
STALE_BLOCKS = 100

#: Where the copies live, under the arcade's own home.
FOLDER = "bootstrap"


class BootstrapError(Exception):
    """The index could not be copied. Never fatal: a clone can re-read."""


@dataclass(frozen=True)
class Snapshot:
    """One published copy of an index, and what it claims about itself."""

    network: str
    path: Path                 # the gzipped copy
    manifest: Path
    height: int
    floor: int | None
    consensus: str
    index: str
    sha256: str
    bytes: int
    made: int

    def as_json(self) -> dict:
        return {
            "network": self.network,
            "height": self.height,
            "floor": self.floor,
            "consensus_hash": self.consensus,
            "index_digest": self.index,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "made": self.made,
            "file": self.path.name,
            # Said in the file rather than only on the page, because the
            # file is what somebody will still have in a month.
            "what": "A copy of one DogecoinArcade ledger index, gzipped. "
                    "Derived state: the same blocks read by the same rules "
                    "give the same rows on any machine. Re-read from `floor` "
                    "and check BOTH `consensus_hash` (balances, the DEx, "
                    "properties) and `index_digest` (inscriptions, "
                    "collections, tags, asks) against your own index at "
                    "`height` to prove it.",
        }


def folder(home: Path) -> Path:
    where = Path(home) / FOLDER
    where.mkdir(parents=True, exist_ok=True)
    return where


def _paths(home: Path, network: str) -> tuple[Path, Path]:
    where = folder(home)
    return where / f"{network}.sqlite.gz", where / f"{network}.json"


def published(home: Path, network: str) -> Snapshot | None:
    """The copy on disk, if there is one and its manifest still reads."""
    blob, manifest = _paths(home, network)
    if not blob.exists() or not manifest.exists():
        return None
    try:
        said = json.loads(manifest.read_text())
    except Exception:
        return None
    return Snapshot(network=network, path=blob, manifest=manifest,
                    height=int(said.get("height", 0)),
                    floor=said.get("floor"),
                    consensus=str(said.get("consensus_hash", "")),
                    index=str(said.get("index_digest", "")),
                    sha256=str(said.get("sha256", "")),
                    bytes=int(said.get("bytes", 0)),
                    made=int(said.get("made", 0)))


def stale(snapshot: Snapshot | None, tip: int | None, now: int | None = None) -> bool:
    """Whether it is worth making a new one."""
    if snapshot is None:
        return True
    now = int(now if now is not None else time.time())
    if now - snapshot.made > STALE_SECONDS:
        return True
    return tip is not None and tip - snapshot.height > STALE_BLOCKS


def make(home: Path, network: str, index_path: Path, floor: int | None,
         now: int | None = None) -> Snapshot:
    """Copy a live index, gzip it, and write down what it is.

    `VACUUM INTO` rather than a file copy: the indexer is writing, and a
    plain copy of a database mid-write is a database that opens and then
    lies. The copy is made into a temporary file and moved into place, so a
    clone downloading while one is being made gets the old one whole rather
    than the new one half-written.
    """
    from .consensushash import consensus_hash
    from .db import Database

    index_path = Path(index_path)
    if not index_path.exists():
        raise BootstrapError(f"there is no {network} index here yet")
    now = int(now if now is not None else time.time())
    blob, manifest = _paths(home, network)

    with tempfile.TemporaryDirectory(dir=str(folder(home))) as tmp:
        plain = Path(tmp) / f"{network}.sqlite"
        source = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
        try:
            source.execute("VACUUM INTO ?", (str(plain),))
        except sqlite3.Error as exc:
            raise BootstrapError(f"could not copy the index: {exc}") from exc
        finally:
            source.close()

        # Read the claim from the COPY, not from the live index: the live one
        # may have moved on between the copy and the question, and a manifest
        # describing a height the file is not at is worse than no manifest.
        db = Database(plain)
        try:
            height = _height(db)
            digest = consensus_hash(db)
            ours = index_digest(db)
        finally:
            db.close()

        packed = Path(tmp) / blob.name
        with open(plain, "rb") as raw, gzip.open(packed, "wb", compresslevel=6) as out:
            shutil.copyfileobj(raw, out)
        checksum = _sha256(packed)
        size = packed.stat().st_size

        snapshot = Snapshot(network=network, path=blob, manifest=manifest,
                            height=height, floor=floor, consensus=digest,
                            index=ours, sha256=checksum, bytes=size, made=now)
        manifest.write_text(json.dumps(snapshot.as_json(), indent=1) + "\n")
        packed.replace(blob)
    return snapshot


def _height(db) -> int:
    row = db.conn.execute("SELECT MAX(height) FROM block").fetchone()
    return int(row[0] or 0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def current(home: Path, network: str, index_path: Path, floor: int | None,
            tip: int | None = None) -> Snapshot | None:
    """The published copy, made or remade if it has gone stale.

    Returns None rather than raising when there is nothing to copy: an
    instance with no index yet simply has no bootstrap to offer, and that is
    a sentence on a page rather than an error.
    """
    have = published(home, network)
    if not stale(have, tip):
        return have
    try:
        return make(home, network, index_path, floor)
    except BootstrapError:
        return have


# --- proving a copy is honest -------------------------------------------------
#
# `consensus_hash` covers the Omni-shaped half of the state: balances, the
# DEx, the MetaDEx, crowdsales and properties. It does NOT cover this
# layer's own additions -- inscriptions, their moves, collection membership,
# tags, asks and offers -- because it mirrors omnicore's sections exactly, by
# design, so that a second implementation of THAT can be compared against
# ours (D-006).
#
# Which leaves a hole under a downloaded index: a doctored bootstrap could
# move an inscription's ownership and the consensus hash would not notice.
# So the manifest carries a second digest over exactly those tables, made
# the same way -- a defined column order, a defined row order, one SHA-256
# -- and a clone that re-reads from the floor can compare both.
#
# Whether the consensus hash itself should grow these sections is a
# consensus-layer decision and is the operator's, not this module's.

INDEX_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("inscription",
     "txid,number,creator,owner,block_height,position,content_type,"
     "content_len,sha256,chunks",
     "number"),
    ("inscription_move", "txid,inscription_txid,from_address,to_address,height",
     "height,txid"),
    ("collection_item", "creator,collection,edition,inscription_txid",
     "creator,collection,edition"),
    ("tag", "name,address,txid,height", "name"),
    ("nft_ask", "inscription_txid,seller,amount,property_id,height",
     "inscription_txid,height"),
    ("nft_offer", "txid,inscription_txid,buyer,amount,property_id,height",
     "txid"),
)


def index_digest(db) -> str:
    """One SHA-256 over this layer's own state, in a defined order.

    A table this build does not have is skipped rather than fatal: the
    digest then describes what was actually there, and two indexes with the
    same tables still agree. A table that exists and is empty contributes
    nothing, exactly as an empty section does above.
    """
    hasher = hashlib.sha256()
    for table, columns, order in INDEX_SECTIONS:
        try:
            rows = db.conn.execute(
                f"SELECT {columns} FROM {table} ORDER BY {order}").fetchall()
        except sqlite3.Error:
            continue                      # not in this build's schema
        hasher.update(f"[{table}]".encode("ascii"))
        for row in rows:
            record = "|".join("" if value is None else str(value)
                              for value in row)
            hasher.update(record.encode("utf-8"))
            hasher.update(b"\n")
    return hasher.hexdigest()


# --- the application itself ---------------------------------------------------
#
# The installer already fetches `source.tar.gz`, its `.sha256` and a
# `.rev` from a website. Serving exactly those three names from every
# instance means any node can be the place somebody installs from, and the
# installer needs no new option to point at one -- which is what makes a
# clone able to produce the next clone rather than sending people back to
# one website that can go away.

SOURCE_NAME = "source.tar.gz"

#: Never in the archive. `.venv` and the caches are this machine's; `.git`
#: is large and carries nothing the archive needs; `*.egg-info` is the
#: artefact that once made a wheel look correct when the configuration was
#: not (D-150); and `bootstrap/` is the published copies, which would put a
#: snapshot of the index inside the source of the program that makes it.
NOT_SOURCE = (".venv", ".git", "__pycache__", "build", "dist", "bootstrap",
              ".pytest_cache", ".mypy_cache")


#: The working plans, which a clone does not get (arcade/web/guide.py PRIVATE:
#: the program ships them to its operator and to nobody else).
NOT_PUBLISHED = tuple(f"arcade/web/docs/{name}" for name in (
    "multi-user.md", "voice-plan.md", "DECISIONS.md", "M0-notes.md", "M1-notes.md", "M2-notes.md", "03-companion-app-design.md", "p2p-messaging.md", "tokens-notes.md", "messaging/04-testnet-results.md"))


def _is_source(name: str) -> bool:
    if name in NOT_PUBLISHED:
        return False
    parts = Path(name).parts
    return not any(part in NOT_SOURCE or part.endswith(".egg-info")
                   or part.endswith(".pyc") for part in parts)


def source_archive(home: Path, checkout: Path, revision: str,
                   now: int | None = None) -> dict:
    """Pack this instance's own source, and say what it is.

    Deterministic where it can be: the members are sorted, and every one is
    stamped with the same time rather than with whatever the filesystem
    happens to say, so two nodes at the same revision produce the same
    bytes and the same sha256. Somebody comparing two clones' archives
    should not see a difference that is only a mtime.
    """
    import tarfile

    checkout = Path(checkout)
    if not (checkout / "arcade").is_dir():
        raise BootstrapError(f"{checkout} does not look like a checkout")
    now = int(now if now is not None else time.time())
    where = folder(home)
    archive = where / SOURCE_NAME

    with tempfile.TemporaryDirectory(dir=str(where)) as tmp:
        packing = Path(tmp) / SOURCE_NAME
        # gzip with mtime=0 as well, or the envelope carries a timestamp the
        # contents deliberately do not.
        with open(packing, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as zipped:
                with tarfile.open(fileobj=zipped, mode="w") as tar:
                    for path in sorted(checkout.rglob("*")):
                        name = str(path.relative_to(checkout))
                        if not _is_source(name) or not path.is_file():
                            continue
                        info = tar.gettarinfo(str(path), arcname=name)
                        info.mtime = 0
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        with open(path, "rb") as handle:
                            tar.addfile(info, handle)
        checksum = _sha256(packing)
        packing.replace(archive)

    (where / f"{SOURCE_NAME}.sha256").write_text(
        f"{checksum}  {SOURCE_NAME}\n")
    (where / "source.rev").write_text(f"{revision}\n")
    said = {"revision": revision, "sha256": checksum,
            "bytes": archive.stat().st_size, "made": now}
    (where / "source.json").write_text(json.dumps(said, indent=1) + "\n")
    return said


def source(home: Path) -> dict | None:
    """What has been packed, if anything."""
    try:
        return json.loads((folder(home) / "source.json").read_text())
    except Exception:
        return None
