"""A token/name index fetched from linked arcades, a piece from each, instead of read block by block from Core.

2026-11-02: an arcade whose Core is at 100% still did not know its operator's @name (@dogefeed), because its
own index reads Core 500 blocks at a time and trailed by hours. "No daily copies"; "a full confirmation of every index
should be done as a background task after the index is complete in a way that does not hog resources". He chose
"copy on request": a holder makes a copy only when an arcade far behind asks, the copy goes out in pieces from every
arcade that has it, and it is used only when most linked holders agree with it.

How it goes (the ord index does the same with its own copies, arcade/ord_snapshot.py):

* MAKING. An arcade whose index is current, and whose operator lets it serve (setting `serve_index_copies`, on unless
  said off; a phone turns it off), makes a SNAPSHOT when a linked arcade asks for one ("sq") and its last is old or
  missing: `VACUUM INTO` a copy of the index, without this arcade's own wallet tables (PRIVATE: which addresses it
  watches and their coins), cut into PIECE-sized pieces. Its MANIFEST names the chain, its floor, the block it was
  taken at and that block's hash, the copy's fingerprint of floor..checkpoint (index_digest), its consensus hash
  (consensushash) and its names digest, its size and every piece's sha256. The manifest's own sha256 is its id. A copy
  nobody fetched from for SNAP_IDLE is deleted: nothing is kept on disk for nobody.
* FETCHING. An arcade whose index is at least WORTH_BLOCKS behind the newest copy offered, with its own Core (or the
  one it borrows) at least as far as that copy's block, fetches it: the manifest from its maker, then the pieces from
  every arcade that has them -- the maker, and arcades fetching or holding the same copy (each says which pieces it
  has) -- several at once, each checked against the manifest as it comes. Its own index goes on reading meanwhile; if
  it gets past the copy's block first, the copy is let go of.
* CHECKING, before it is used:
  - every piece is the manifest's;
  - the copy's block at its height is the block this arcade's Core has there (the same chain);
  - its fingerprint of floor..checkpoint, worked out here from the copy itself, is the manifest's, and so are its
    consensus hash and names digest;
  - the VOTERS -- linked arcades that hold this same index at least up to that checkpoint (indexcheck "tip") -- are
    asked for their fingerprint of floor..checkpoint: at least two must agree with the copy, and more than half of
    those that answered (2026-11-02: "Majority of linked", of those storing the index). The maker counts
    once. Otherwise the copy is refused and the index reads Core as before.
* INSTALLING. The copy takes the old index's place (LedgerIndex.install). This arcade's accounts are watched again
  from the copy's block, and their coins read back the way a new install's are (watcher._watch_accounts).
* CONFIRMING (`Confirmation`). Then, at the lowest priority, a second index is built from this arcade's own Core
  beside it (<index>.confirm): a bounded pass only while the index is current, no copy is being fetched and no call
  is relayed here. When it reaches the copy's checkpoint, the fingerprints of floor..checkpoint are compared; when it
  reaches the same height as the index, so are the consensus hash and the names. All the same: "confirmed", the second
  index is deleted. Anything different: the index this arcade built itself takes the copy's place, and the maker is
  named in Admin -> Chains.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import random
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("arcade.ledger_snapshot")

PIECE = 1024 * 1024
LAYOUT = 1
#: This arcade's own: which addresses it watches and their coins. Never in a copy.
PRIVATE = ("utxo", "activity", "watched", "covered")        # coins, history, watched addresses, coin read-backs
#: A copy is made again only when the last is this old, so a burst of askers costs one copy.
SNAP_EVERY = 6 * 3600
#: ... and never while somebody fetched a piece of it this recently: a new copy is a new id to every fetcher.
SNAP_SERVING = 15 * 60
#: A copy nobody fetched from for this long is deleted.
SNAP_IDLE = 2 * 3600
#: An ask to make one counts this long.
ASKED_FRESH = 15 * 60
#: Asked of the holders at most this often.
ASK_EVERY = 10 * 60
#: Worth fetching only this far ahead of the index here: below it, reading Core is about as quick.
WORTH_BLOCKS = 50_000
#: A copy is made only by an index within this many blocks of its Core.
CURRENT_WITHIN = 20
IN_FLIGHT = 6
PIECE_SECONDS = 60
FETCH_STALL = 600
FETCH_SAVE = 15
#: How long an arcade that said "busy" is not asked for a piece.
BUSY_PAUSE = 10.0
#: How often a fetch in progress is stepped on its own thread.
FETCH_TICK = 0.25
#: Offers heard within this long count.
OFFER_FRESH = 600


class SnapshotError(ValueError):
    pass


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def manifest_id(manifest: dict) -> str:
    return _sha(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())


def names_digest(db) -> str:
    """One hash over every @name's current claim (the tag table is not in the fingerprints or the consensus hash)."""
    h = hashlib.sha256()
    try:
        rows = db.conn.execute("SELECT tag, address, block_height FROM tag ORDER BY tag")
    except sqlite3.OperationalError:
        return h.hexdigest()
    for r in rows:
        h.update(f"{r[0]}|{r[1]}|{r[2]}\n".encode())
    return h.hexdigest()


def describe(db, floor: int | None) -> dict:
    """What a copy (or an index) is, read from the file itself: its height and block hash there, the fingerprint of
    floor..checkpoint, the consensus hash and the names digest."""
    from . import index_digest
    from .consensushash import consensus_hash
    row = db.conn.execute("SELECT height, hash FROM block ORDER BY height DESC LIMIT 1").fetchone()
    if row is None:
        raise SnapshotError("the copy holds no block")
    height, block_hash = int(row[0]), str(row[1])
    at = index_digest.checkpoint(height, floor)
    d = None
    if at is not None:
        d = index_digest.range_digest(index_digest.block_digests(db, floor, at), floor, at)
    return {"height": height, "hash": block_hash, "cp": at, "d": d, "s": consensus_hash(db), "n": names_digest(db)}


class _File:
    """A Database-like handle on a plain SQLite file, for describe()."""

    def __init__(self, path: Path, readonly: bool = True):
        uri = f"file:{path}?mode=ro" if readonly else str(path)
        self.conn = sqlite3.connect(uri, uri=readonly, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()


class LedgerSnapshots:
    """One chain's copies: the one this arcade made or fetched (served piece by piece), the one it is fetching, and
    what came of the last. Files beside the index: <index>.snap, .snap.json, .part, .part.json, .copy.json."""

    def __init__(self, index: Any, net: str, *, setting: Callable[[str, Any], Any] | None = None):
        self.index = index
        self.net = net
        self.setting = setting or (lambda key, default=None: default)
        self.lock = threading.RLock()
        self.made: dict | None = None
        self.fetch: dict | None = None
        self.offers: dict[str, dict] = {}          # node -> {"id", "h", "n", "have", "at"}
        self.heights: dict[str, dict] = {}         # node -> {"h", "at"}: how far its index is
        self.asked_by: dict[str, float] = {}       # node -> when it asked this arcade to make one
        self.asked_at = 0.0                        # when this arcade last asked the holders to make one
        self.making = False
        self.refused: dict[str, float] = {}        # copy id -> when it was refused: never fetched again
        self.paused: dict[str, float] = {}         # node -> not asked for a piece until then (it said "busy")
        self.said = ""
        self.served_at = time.time()
        base = Path(index.path)
        self.snap_path = base.with_name(base.name + ".snap")
        self.manifest_path = base.with_name(base.name + ".snap.json")
        self.part_path = base.with_name(base.name + ".part")
        self.fetch_path = base.with_name(base.name + ".part.json")
        self.copy_path = base.with_name(base.name + ".copy.json")
        #: Where the index came from when it was a copy: {"from", "height", "at", "voters", "agreed", ...}; the
        #: Confirmation keeps its progress here too.
        self.copy: dict | None = None
        try:
            self.copy = json.loads(self.copy_path.read_text())
        except (OSError, ValueError):
            self.copy = None
        try:
            kept = json.loads(self.manifest_path.read_text())
            if self.snap_path.exists() and self.snap_path.stat().st_size == kept["manifest"]["size"] \
                    and kept["manifest"].get("layout") == LAYOUT:
                self.made = {**kept, "path": self.snap_path}
            else:
                self.forget()
        except (OSError, ValueError, KeyError):
            pass
        self._resume()

    # --- facts about this chain's index ----------------------------------------------------------------------------

    @property
    def floor(self) -> int | None:
        return self.index.params.index_start

    def _floors(self) -> dict:
        return {"floor": self.index.params.index_start, "activation": self.index.params.activation_height}

    def indexed(self) -> int:
        return int(self.index.indexed_height() or 0)

    def serving(self) -> bool:
        return self.setting("serve_index_copies", True) is not False

    def fetching_allowed(self) -> bool:
        return self.setting("fetch_index_copies", True) is not False

    # --- remembering ------------------------------------------------------------------------------------------------

    def _resume(self) -> None:
        try:
            kept = json.loads(self.fetch_path.read_text())
            m = kept["manifest"]
            if manifest_id(m) != kept["id"] or m.get("net") != self.net or m.get("layout") != LAYOUT \
                    or {k: m.get(k) for k in ("floor", "activation")} != self._floors() or m.get("piece") != PIECE \
                    or not self.part_path.exists() or self.part_path.stat().st_size != int(m["size"]):
                raise ValueError("not this chain's, or its .part is gone")
            have = set()
            with open(self.part_path, "rb") as f:
                for i in sorted(int(i) for i in kept.get("have") or ()):
                    if 0 <= i < len(m["pieces"]):
                        f.seek(i * m["piece"])
                        if _sha(f.read(m["piece"])) == m["pieces"][i]:
                            have.add(i)
        except (OSError, ValueError, KeyError, TypeError):
            for path in (self.fetch_path,):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
            return
        now = time.time()
        self.fetch = {"id": kept["id"], "from": str(kept.get("from") or ""), "manifest": m, "have": have, "asked": {},
                      "started": now, "moved": now, "saved": now}
        self.said = f"fetching the index (carried on after a restart): {len(have)} of {len(m['pieces'])} pieces"

    def _save_fetch(self, force: bool = False) -> None:
        f = self.fetch
        if f is None or f.get("manifest") is None or (not force and time.time() - f.get("saved", 0) < FETCH_SAVE):
            return
        f["saved"] = time.time()
        tmp = self.fetch_path.with_name(self.fetch_path.name + ".tmp")
        try:
            tmp.write_text(json.dumps({"id": f["id"], "from": f["from"], "manifest": f["manifest"],
                                       "have": sorted(f["have"])}))
            tmp.replace(self.fetch_path)
        except OSError:
            log.debug("ledger snapshot: could not keep where the fetch got to", exc_info=True)

    def save_copy(self) -> None:
        if self.copy is None:
            with contextlib.suppress(FileNotFoundError):
                self.copy_path.unlink()
            return
        tmp = self.copy_path.with_name(self.copy_path.name + ".tmp")
        tmp.write_text(json.dumps(self.copy))
        tmp.replace(self.copy_path)

    def forget(self) -> None:
        with self.lock:
            self.made = None
            for path in (self.snap_path, self.manifest_path):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()

    def tidy(self) -> None:
        if self.made is not None and time.time() - max(self.served_at, self.made["manifest"]["at"]) > SNAP_IDLE:
            log.info("ledger %s: index copy not fetched from for %d h: deleted", self.net, SNAP_IDLE // 3600)
            self.forget()

    # --- making one ------------------------------------------------------------------------------------------------

    def make(self) -> dict:
        """A copy of the index as it is now, without this arcade's own wallet tables, its manifest kept beside it."""
        tmp = self.snap_path.with_name(self.snap_path.name + ".tmp")
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        source = sqlite3.connect(f"file:{self.index.path}?mode=ro", uri=True)
        try:
            source.execute("VACUUM INTO ?", (str(tmp),))
        finally:
            source.close()
        db = _File(tmp, readonly=False)
        try:
            have = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in PRIVATE:
                if table in have:
                    db.conn.execute(f"DELETE FROM {table}")
            db.conn.commit()
            said = describe(db, self.floor)
            db.conn.execute("VACUUM")
        finally:
            db.close()
        hashes, size = [], 0
        with open(tmp, "rb") as f:
            while True:
                piece = f.read(PIECE)
                if not piece:
                    break
                hashes.append(_sha(piece))
                size += len(piece)
        manifest = {"net": self.net, **self._floors(), "layout": LAYOUT, **said,
                    "size": size, "piece": PIECE, "pieces": hashes, "at": int(time.time())}
        os.replace(tmp, self.snap_path)
        sid = manifest_id(manifest)
        self.manifest_path.write_text(json.dumps({"manifest": manifest, "id": sid}))
        with self.lock:
            self.made = {"manifest": manifest, "id": sid, "path": self.snap_path}
        log.info("ledger %s: index copy made at block %s, %.1f MB", self.net, said["height"], size / 1e6)
        return self.made

    def want_made(self, core_tip: int | None) -> bool:
        """Make one now: somebody asked lately, this arcade may serve, its index is current, and its last is old."""
        now = time.time()
        if not self.serving() or not any(now - at < ASKED_FRESH for at in self.asked_by.values()):
            return False
        if getattr(self.index, "stopped", None) is not None or core_tip is None:
            return False
        if core_tip - self.indexed() > CURRENT_WITHIN or self.fetch is not None:
            return False
        if self.made is None:
            return True
        if now - self.served_at < SNAP_SERVING:
            return False
        return now - self.made["manifest"]["at"] > SNAP_EVERY

    def make_in_background(self, core_tip: int | None) -> bool:
        """Start one making in a thread of its own, when it is wanted and none is being made."""
        with self.lock:
            if self.making or not self.want_made(core_tip):
                return False
            self.making = True

        def run():
            try:
                self.make()
            except Exception:                                  # noqa: BLE001 -- asked again later
                log.warning("ledger %s: could not make an index copy", self.net, exc_info=True)
            finally:
                self.making = False
        threading.Thread(target=run, name=f"arcade-{self.net}-index-copy", daemon=True).start()
        return True

    # --- what this arcade offers -----------------------------------------------------------------------------------

    def offer(self) -> dict | None:
        if self.made is not None and self.serving():
            m = self.made["manifest"]
            return {"id": self.made["id"], "h": m["height"], "n": len(m["pieces"]), "have": "all"}
        f = self.fetch
        if f is not None and f.get("manifest") and f["have"] and self.serving():
            m = f["manifest"]
            return {"id": f["id"], "h": m["height"], "n": len(m["pieces"]), "have": sorted(f["have"])}
        return None

    def heard(self, node: str, word: Any, height: Any) -> None:
        if isinstance(word, dict) and isinstance(word.get("id"), str) and len(word["id"]) == 64:
            have = word.get("have")
            if have != "all":
                have = [i for i in have if isinstance(i, int)] if isinstance(have, list) else []
            self.offers[node] = {"id": word["id"], "h": int(word.get("h") or 0), "n": int(word.get("n") or 0),
                                 "have": have, "at": time.time()}
        else:
            self.offers.pop(node, None)
        if isinstance(height, int):
            self.heights[node] = {"h": height, "at": time.time()}

    def gone(self, node: str) -> None:
        self.offers.pop(node, None)
        self.heights.pop(node, None)

    def asked_to_make(self, node: str) -> None:
        self.asked_by[node] = time.time()

    def manifest(self, sid: str) -> dict | None:
        if self.made is not None and self.made["id"] == sid:
            return self.made["manifest"]
        if self.fetch is not None and self.fetch["id"] == sid and self.fetch.get("manifest"):
            return self.fetch["manifest"]
        return None

    def piece(self, sid: str, i: int) -> bytes | None:
        if not self.serving():
            return None
        for have, path in ((self.made, self.snap_path), (self.fetch, self.part_path)):
            if have is None or have["id"] != sid:
                continue
            if have is self.fetch and i not in have["have"]:
                return None
            self.served_at = time.time()
            m = have["manifest"]
            if not 0 <= i < len(m["pieces"]):
                return None
            with open(path, "rb") as f:
                f.seek(i * m["piece"])
                return f.read(m["piece"])
        return None

    # --- fetching one ----------------------------------------------------------------------------------------------

    def may_fetch(self, size: int | None = None) -> str:
        if not self.fetching_allowed():
            return "fetching index copies is turned off here"
        if size is not None:
            free = shutil.disk_usage(self.part_path.parent).free
            if free < 3 * size:
                return (f"not enough free disk to fetch the index ({size / 1e6:.0f} MB; "
                        f"{free / 1e6:.0f} MB free, {3 * size / 1e6:.0f} MB needed)")
        return ""

    def far_behind(self, core_tip: int | None) -> int | None:
        """How far the linked arcades' indexes are ahead of this one, when that is worth a copy (None otherwise)."""
        now = time.time()
        best = max((w["h"] for w in self.heights.values() if now - w["at"] < OFFER_FRESH), default=0)
        if core_tip is not None:
            best = min(best, core_tip)                   # a copy past this arcade's Core cannot be checked here
        gap = best - self.indexed()
        return gap if gap >= WORTH_BLOCKS else None

    def best_offer(self, core_tip: int | None) -> tuple[str, dict] | None:
        if self.may_fetch() or core_tip is None:
            return None
        now = time.time()
        whole = [(o["h"], n, o) for n, o in self.offers.items()
                 if o.get("have") == "all" and now - o["at"] < OFFER_FRESH and o["h"] <= core_tip
                 and o["id"] not in self.refused]
        if not whole:
            return None
        h, node, o = max(whole)
        if h - self.indexed() < WORTH_BLOCKS:
            return None
        return node, o

    def step(self, share: Any, core_tip: int | None, verify: Callable[[dict, str], dict] | None = None) -> None:
        """One pass: ask for a copy when far behind and none is offered, start on the best offer, keep IN_FLIGHT
        pieces asked, check and install when whole. `verify(manifest, maker)` asks the voters (see module doc)."""
        if share is None:
            return
        self.tidy()
        with self.lock:
            f = self.fetch
            if f is None:
                best = self.best_offer(core_tip)
                if best is None:
                    if self.far_behind(core_tip) and not self.may_fetch() \
                            and time.time() - self.asked_at > ASK_EVERY:
                        holders = sorted(self.heights, key=lambda n: -self.heights[n]["h"])[:3]
                        if holders:
                            self.asked_at = time.time()
                            for node in holders:
                                share.ask_make(node, self.net)
                            if not any(time.time() - at < ASK_EVERY for at in self.refused.values()):
                                self.said = (f"far behind: asked {len(holders)} linked arcade"
                                             f"{'s' if len(holders) > 1 else ''} for a copy of the index")
                    return
                node, o = best
                self.fetch = f = {"id": o["id"], "from": node, "manifest": None, "have": set(), "asked": {},
                                  "started": time.time()}
                self.said = f"fetching the index from arcade {node[:12]} (block {o['h']:,})"
                log.info("ledger %s: fetching an index copy from %s at block %s", self.net, node[:12], o["h"])
            h = int((f.get("manifest") or {}).get("height") or (self.offers.get(f["from"]) or {}).get("h") or 0)
            if h and self.indexed() >= h:
                self._drop(f"read past block {h:,} from this arcade's own node before the copy came: let go of")
                return
            if time.time() - f.get("moved", f["started"]) > FETCH_STALL:
                holder = next((n for n, o in self.offers.items() if o.get("id") == f["id"] and o.get("have") == "all"
                               and time.time() - o["at"] < OFFER_FRESH), None)
                if holder:
                    f["asked"], f["moved"], f["manifest_asked"], f["from"] = {}, time.time(), 0, holder
                else:
                    self._drop(f"the index copy from arcade {f['from'][:12]} stalled: let go of")
                    return
            if f["manifest"] is None:
                if time.time() - f.get("manifest_asked", 0) > PIECE_SECONDS:
                    f["manifest_asked"] = time.time()
                    share.ask_manifest(f["from"], self.net, f["id"])
                return
            m = f["manifest"]
            now = time.time()
            for i, (node, at) in list(f["asked"].items()):
                if now - at > PIECE_SECONDS:
                    f["asked"].pop(i)
            missing = [i for i in range(len(m["pieces"])) if i not in f["have"] and i not in f["asked"]]
            if not missing and not f["asked"]:
                # Checking a whole copy reads all of it and asks every holder: minutes on a big chain, so in a
                # thread of its own, never on the watcher's.
                if not f.get("checking"):
                    f["checking"] = True
                    self.said = f"checking the fetched index ({len(m['pieces'])} pieces) against the linked arcades"
                    threading.Thread(target=self._finish, args=(verify,), name=f"arcade-{self.net}-copy-check",
                                     daemon=True).start()
                return
            busy = {node for node, _at in f["asked"].values()}
            for i in missing[:max(0, IN_FLIGHT - len(f["asked"]))]:
                has = [n for n, o in self.offers.items() if o["id"] == f["id"]
                       and (o.get("have") == "all" or i in (o.get("have") or []))
                       and self.paused.get(n, 0) < now]
                if not has:
                    continue
                free = [n for n in has if n not in busy] or has
                node = random.choice(free)
                busy.add(node)
                f["asked"][i] = (node, now)
                share.ask_piece(node, self.net, f["id"], i)

    def keep_fetching(self, share: Any, tip: Callable[[], int | None], verify) -> bool:
        """While a copy is being fetched, step it every FETCH_TICK seconds on a thread of its own (2026-11-02, live: on
        a new arcade the watcher's pass took two minutes per chain reading its node, so a fetch stepped -- six pieces
        asked -- once every few minutes). True when one was started."""
        with self.lock:
            if self.fetch is None or getattr(self, "_fetcher", None) is not None:
                return False
            self._fetcher = True

        def run():
            try:
                while self.fetch is not None:
                    try:
                        self.step(share, tip(), verify=verify)
                    except Exception:                          # noqa: BLE001 -- the next tick tries again
                        log.debug("ledger %s: fetch step", self.net, exc_info=True)
                    time.sleep(FETCH_TICK)
            finally:
                self._fetcher = None
        threading.Thread(target=run, name=f"arcade-{self.net}-copy-fetch", daemon=True).start()
        return True

    def got_manifest(self, node: str, sid: str, manifest: Any) -> None:
        with self.lock:
            f = self.fetch
            if f is None or f["id"] != sid or f["manifest"] is not None:
                return
            if not isinstance(manifest, dict) or manifest_id(manifest) != sid:
                self._drop(f"arcade {node[:12]} sent a manifest that is not the one it offered")
                return
            if manifest.get("net") != self.net or manifest.get("layout") != LAYOUT \
                    or {k: manifest.get(k) for k in ("floor", "activation")} != self._floors() \
                    or manifest.get("piece") != PIECE or not isinstance(manifest.get("pieces"), list) \
                    or int(manifest.get("size") or 0) > len(manifest["pieces"]) * PIECE:
                self._drop("the copy is of another chain, floor or layout")
                return
            why = self.may_fetch(int(manifest.get("size") or 0))
            if why:
                self._drop(why)
                return
            f["manifest"] = manifest
            f["moved"] = time.time()
            with open(self.part_path, "wb") as out:
                out.truncate(int(manifest["size"]))
            self._save_fetch(force=True)

    def got_piece(self, node: str, sid: str, i: int, data: bytes) -> None:
        with self.lock:
            f = self.fetch
            if f is None or f["id"] != sid or f["manifest"] is None or i in f["have"]:
                return
            f["asked"].pop(i, None)
            m = f["manifest"]
            if not 0 <= i < len(m["pieces"]) or _sha(data) != m["pieces"][i]:
                self.offers.pop(node, None)
                log.warning("ledger snapshot: arcade %s sent a wrong piece %d", node[:12], i)
                return
            with open(self.part_path, "r+b") as out:
                out.seek(i * m["piece"])
                out.write(data)
            f["have"].add(i)
            f["moved"] = time.time()
            self._save_fetch()
            self.said = f"fetching the index: {len(f['have'])} of {len(m['pieces'])} pieces"

    def piece_failed(self, node: str, sid: str, i: int, busy: bool = False) -> None:
        """`node` did not send piece i: gone (asked elsewhere now), or busy (not asked again for BUSY_PAUSE)."""
        with self.lock:
            if busy:
                self.paused[node] = time.time() + BUSY_PAUSE
            f = self.fetch
            if f is not None and f["id"] == sid and f["asked"].get(i, ("",))[0] == node:
                f["asked"].pop(i, None)
                o = self.offers.get(node)
                if o is not None and o.get("have") != "all":
                    self.offers.pop(node, None)

    def _drop(self, why: str) -> None:
        self.said = why
        self.fetch = None
        for path in (self.part_path, self.fetch_path):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    def _finish(self, verify) -> None:
        with self.lock:
            f = self.fetch
        if f is None or f.get("manifest") is None:
            return
        m = f["manifest"]
        try:
            votes = self.check(self.part_path, m, f["from"], verify)
        except SnapshotError as exc:
            log.warning("ledger %s: index copy refused: %s", self.net, exc)
            with self.lock:
                self.refused[f["id"]] = time.time()
                self._drop(f"the fetched index was refused: {exc}")
            return
        if self.indexed() >= int(m["height"]):
            with self.lock:
                self._drop(f"read past block {m['height']:,} from this arcade's own node while the copy was checked")
            return
        try:
            self.index.install(self.part_path)
        except OSError as exc:
            # Windows will not move a database file another thread still has open: the copy is let go of and the
            # index reads its own node as before.
            log.warning("ledger %s: the index copy could not be put in place: %s", self.net, exc)
            with self.lock:
                self.refused[f["id"]] = time.time()
                self._drop(f"the checked copy could not be put in place ({exc}); reading this arcade's own node")
            return
        with self.lock:
            self.fetch = None
        with contextlib.suppress(FileNotFoundError):
            self.fetch_path.unlink()
        self.copy = {"from": f["from"], "height": m["height"], "cp": m["cp"], "made": m["at"],
                     "installed": int(time.time()), **votes, "confirmed_to": None, "state": "confirming",
                     "d": m["d"], "s": m["s"], "n": m["n"]}
        self.save_copy()
        self.said = (f"the index came from {votes['agreed']} of {votes['voters']} linked arcades that hold it, "
                     f"at block {m['height']:,}; confirming it from this arcade's own node in the background")
        log.info("ledger %s: index copy installed at block %s (%d of %d holders agreed)", self.net, m["height"],
                 votes["agreed"], votes["voters"])

    def check(self, path: Path, manifest: dict, maker: str, verify) -> dict:
        """The copy against itself, this arcade's Core, and the voters. {"voters", "agreed"}; SnapshotError if wrong."""
        with open(path, "rb") as f:
            for i, want in enumerate(manifest["pieces"]):
                if _sha(f.read(manifest["piece"])) != want:
                    raise SnapshotError(f"piece {i} is not the manifest's")
        db = _File(path)
        try:
            said = describe(db, self.floor)
        except sqlite3.DatabaseError as exc:
            raise SnapshotError(f"the copy is not an index: {exc}") from None
        finally:
            db.close()
        for key in ("height", "hash", "cp", "d", "s", "n"):
            if said[key] != manifest.get(key):
                raise SnapshotError(f"its {key} is not what its manifest says")
        try:
            with self.index._rpc() as rpc:
                theirs = rpc.call("getblockhash", said["height"])
        except Exception as exc:                               # noqa: BLE001
            raise SnapshotError(f"this arcade's node could not say which block is at {said['height']:,}: {exc}")
        if theirs != said["hash"]:
            raise SnapshotError(f"its block {said['height']:,} is not the one this arcade's node has")
        if said["cp"] is None or said["d"] is None:
            raise SnapshotError("the copy has no settled checkpoint to compare")
        if verify is None:
            raise SnapshotError("no linked arcade to compare it with")
        votes = verify(manifest, maker)                       # {peer: fingerprint of floor..cp}
        voters = len(votes)
        agreed = sum(1 for d in votes.values() if d == said["d"])
        if agreed < 2 or agreed * 2 <= voters:
            raise SnapshotError(f"only {agreed} of {voters} linked arcades holding the index agree with it")
        return {"voters": voters, "agreed": agreed}

    # --- for Admin -> Chains -----------------------------------------------------------------------------------------

    def status(self) -> dict:
        out: dict = {"said": self.said, "serving": self.serving(), "fetching_allowed": self.fetching_allowed()}
        if self.made is not None:
            out["made"] = {"height": self.made["manifest"]["height"], "mb": round(self.made["manifest"]["size"] / 1e6)}
        f = self.fetch
        if f is not None:
            m = f.get("manifest") or {}
            out["fetch"] = {"from": f["from"][:12], "have": len(f["have"]), "of": len(m.get("pieces") or []),
                            "height": m.get("height")}
        if self.copy is not None:
            out["copy"] = {k: v for k, v in self.copy.items() if k not in ("d", "s", "n")}
            out["copy"]["from"] = str(self.copy.get("from") or "")[:12]
        return out



#: Blocks the second index reads in one confirmation pass, and how long it waits between two.
CONFIRM_BLOCKS = 500
CONFIRM_EVERY = 15.0


class Confirmation:
    """The full confirmation of an index that came as a copy (2026-11-02): a second index built from this
    arcade's own Core beside it (<index>.confirm), a bounded pass at a time, only when `step(idle=True)` is asked.

    When the second index has a settled checkpoint at or past the copy's, the fingerprints of floor..checkpoint are
    compared. When it has caught up with the index itself (the same height), the consensus hash and the names are
    compared too. All the same: "confirmed", and the second index is deleted. Anything different: the second index --
    built here, from this arcade's own Core -- takes the copy's place ("rebuilt"), and the copy's maker is named."""

    def __init__(self, snaps: LedgerSnapshots, make_index: Callable[[Path], Any]):
        self.snaps = snaps
        self.make_index = make_index
        base = Path(snaps.index.path)
        self.path = base.with_name(base.name + ".confirm")
        self._shadow = None
        self.last = 0.0
        self.busy = False

    def wanted(self) -> bool:
        copy = self.snaps.copy
        return bool(copy) and copy.get("state") in ("confirming", "differs")

    def shadow(self):
        if self._shadow is None:
            self._shadow = self.make_index(self.path)
        return self._shadow

    def step(self, idle: bool) -> bool:
        """Start one pass in a thread of its own when it is wanted, this arcade is idle and the last was long enough
        ago. True when one was started."""
        if not idle or self.busy or not self.wanted() or time.monotonic() - self.last < CONFIRM_EVERY:
            return False
        self.busy, self.last = True, time.monotonic()

        def run():
            try:
                self.one_pass()
            except Exception:                                  # noqa: BLE001 -- the next pass tries again
                log.warning("ledger %s: confirming the index copy", self.snaps.net, exc_info=True)
            finally:
                self.busy = False
        threading.Thread(target=run, name=f"arcade-{self.snaps.net}-confirm", daemon=True).start()
        return True

    def one_pass(self) -> None:
        from . import index_digest
        snaps, copy = self.snaps, self.snaps.copy
        if not copy:
            return
        shadow = self.shadow()
        shadow.sync(max_blocks=CONFIRM_BLOCKS)
        if shadow.stopped is not None:
            copy["note"] = f"confirming paused: {shadow.stopped.reason}"[:200]
            snaps.save_copy()
            return
        h = int(shadow.indexed_height() or 0)
        floor = snaps.floor
        copy["confirmed_to"] = min(h, int(copy["height"]))
        cp = copy.get("cp")
        if cp is not None and not copy.get("blocks_checked") and h >= cp + index_digest.SETTLED:
            with shadow.open() as db:
                d = index_digest.range_digest(index_digest.block_digests(db, floor, cp), floor, cp)
            if d == copy.get("d"):
                copy["blocks_checked"] = True
            else:
                copy["state"], copy["differs"] = "differs", f"its blocks up to {cp:,}"
                log.warning("ledger %s: the index copy from %s reads blocks up to %s differently from this arcade's "
                            "own node; rebuilding from it", snaps.net, str(copy.get("from"))[:12], cp)
        main = snaps.indexed()
        if h >= main and (copy.get("blocks_checked") or copy.get("state") == "differs"):
            if copy.get("state") != "differs":
                with shadow.open() as db:
                    ours = (self._state(db), names_digest(db))
                with snaps.index.open() as db:
                    theirs = (self._state(db), names_digest(db))
                if snaps.indexed() != h:
                    snaps.save_copy()
                    return                                     # it moved meanwhile: compared on a later pass
                if ours == theirs:
                    copy["state"], copy["confirmed"] = "confirmed", int(time.time())
                    copy["confirmed_to"] = h
                    log.info("ledger %s: the index copy is confirmed from this arcade's own node", snaps.net)
                    self._remove()
                    snaps.save_copy()
                    return
                copy["state"], copy["differs"] = "differs", "its balances or names"
            self._replace(h)
        snaps.save_copy()

    @staticmethod
    def _state(db) -> str:
        from .consensushash import consensus_hash
        return consensus_hash(db)

    def _replace(self, h: int) -> None:
        """The index this arcade built itself takes the copy's place."""
        snaps = self.snaps
        shadow = self.shadow()
        shadow.release()
        _checkpointed(self.path)
        snaps.index.install(self.path)
        self._shadow = None
        snaps.copy.update(state="rebuilt", rebuilt=int(time.time()), confirmed_to=h)
        log.warning("ledger %s: replaced the index copy from %s with the one built from this arcade's own node",
                    snaps.net, str(snaps.copy.get("from"))[:12])

    def _remove(self) -> None:
        if self._shadow is not None:
            self._shadow.release()
            self._shadow = None
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                Path(str(self.path) + suffix).unlink()


def _checkpointed(path: Path) -> None:
    """Fold a database's write-ahead log into the file, so the file alone is the whole of it."""
    try:
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error:
        log.debug("could not checkpoint %s", path, exc_info=True)
