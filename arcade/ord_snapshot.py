"""The whole ord index, fetched from the arcades that hold it, a piece from each (2026-10-20: "Anyone who wants to
hold the full index can request it, and it will be served torrent style from the rest of its peers that hold it").

An arcade holding a chain's index (mode walk, on a big machine) makes a SNAPSHOT of it when another asks for one and
its last is old: a copy of the index database at one block, without the work in hand (spend notes, leases), cut into
PIECE-sized pieces. Its MANIFEST names the chain, the rules, the block the copy was walked to, its size, and every
piece's sha256; the manifest's own sha256 is the snapshot's id.

An arcade that asks to hold the index and is far behind fetches the newest snapshot: the manifest from its maker, then
the pieces from every arcade that has them -- the maker, other holders that fetched it, arcades still fetching it
(each says which pieces it has) -- several at once, each checked against the manifest as it comes.

Whole, the copy is checked before it is used, against what this arcade knows and the maker cannot change:
* every chunk in it hashes to what the other arcades said of that chunk, wherever two of them agree;
* its moves -- where every inscription went, block by block -- match another holder's, asked of a sample of chunks
  (with no second holder linked, it is used, and the Ords tab says it is one arcade's copy).
Then it takes the old index's place, and the walk goes on from its block.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("arcade.ord_snapshot")

PIECE = 1024 * 1024
#: A snapshot is made again only when the last is this old, so a burst of askers costs one copy.
SNAP_EVERY = 6 * 3600
#: Fetch a snapshot only when it is at least this many blocks ahead of the index here (else walking on is cheaper).
WORTH_BLOCKS = 20_000
#: Pieces asked for at once, each of another arcade where it can.
IN_FLIGHT = 6
PIECE_SECONDS = 60
#: A fetch that got nothing (no manifest, no piece) for this long is let go of, and the newest copy started on (2026-10-22:
#: @yourfirstname's Pepecoin walk sat at its first block all day on a snapshot the maker had long let go of and made
#: again under a new id -- nobody offered the old one, so no piece ever came, and the walk waits while a fetch runs).
FETCH_STALL = 600
#: How often where a fetch got to is written beside its .part (a restart loses at most this much).
FETCH_SAVE = 15
#: Chunks whose moves are compared with a second holder, picked at random through the copy.
MOVES_SAMPLE = 64
#: Tables a snapshot leaves out: work in hand, not the index.
WORK = ("notes", "pnote", "stitched")
WORK_META = ("lanes", "lease", "said_upto")
#: A snapshot nobody has fetched from for this long is let go of: it is made again when someone asks (the operator
#: 2026-10-20, slimming: a whole second copy of the index kept on disk for nobody).
SNAP_IDLE = 2 * 3600


class SnapshotError(ValueError):
    pass


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def manifest_id(manifest: dict) -> str:
    return _sha(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())


def moves_digest(store, c: int, base: int, chunk: int) -> str | None:
    """One chunk's moves -- what the walk did in each of its blocks -- as one hash; None until it is all walked."""
    lo = base + c * chunk
    rows = store.q("SELECT moves FROM block WHERE height>=? AND height<? ORDER BY height", (lo, lo + chunk))
    if len(rows) != chunk or any(r[0] is None for r in rows):
        return None
    return _sha("".join(r[0] for r in rows).encode())


class Snapshots:
    """One chain's snapshots: the one this arcade made or fetched (served, piece by piece), and the one it is
    fetching. Files beside the index: <index>.snap (whole) and <index>.part (filling)."""

    def __init__(self, index: Any):
        self.index = index
        self.lock = threading.RLock()                # got_* may answer inside step (a link that replies at once)
        self.made: dict | None = None            # {"manifest", "id", "path"} of the whole snapshot here
        self.fetch: dict | None = None           # the one being fetched: {"id", "manifest", "have", "asked", ...}
        self.offers: dict[str, dict] = {}        # node -> its "sn" word: {"id", "h", "n", "have" (all or a list)}
        self.said = ""                           # what happened last, for the Ords tab
        self.single = False                      # the index here is one arcade's copy (no second holder checked it)
        base = Path(index.store.path)
        self.snap_path = base.with_name(base.name + ".snap")
        self.part_path = base.with_name(base.name + ".part")
        self.manifest_path = base.with_name(base.name + ".snap.json")
        # Where a fetch got to, beside its .part (2026-10-22: @yourfirstname's fetch went from 420 of 2,978 pieces
        # back to 13 when its arcade restarted -- the pieces were only in memory, and the .part was thrown away).
        self.fetch_path = base.with_name(base.name + ".part.json")
        self.served_at = time.time()
        from .doginal_index import LAYOUT
        from . import ord_rules
        try:
            kept = json.loads(self.manifest_path.read_text())
            # Of this layout AND these rules (2026-10-21, rules 3: a snapshot walked under 2 lost inscriptions -- kept,
            # it was offered and its gigabytes stayed on the disk; an arcade fetching refused it, by its manifest).
            if self.snap_path.exists() and self.snap_path.stat().st_size == kept["manifest"]["size"] \
                    and kept["manifest"].get("layout", 1) == LAYOUT \
                    and kept["manifest"].get("rules") == self.index.rules:
                self.made = {**kept, "path": self.snap_path}
            else:
                self.forget()
        except (OSError, ValueError, KeyError):
            pass
        self._resume()

    def _resume(self) -> None:
        """Carry on a fetch an arcade restart cut short: its manifest and the pieces it had, each checked again
        against the manifest's hash (a piece half written when the arcade stopped is asked for again)."""
        from .doginal_index import LAYOUT
        try:
            kept = json.loads(self.fetch_path.read_text())
            m = kept["manifest"]
            if manifest_id(m) != kept["id"] or m.get("net") != self.index.net or m.get("rules") != self.index.rules \
                    or m.get("layout", 1) != LAYOUT or m.get("piece") != PIECE \
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
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            return
        now = time.time()
        self.fetch = {"id": kept["id"], "from": str(kept.get("from") or ""), "manifest": m, "have": have, "asked": {},
                      "started": now, "moved": now, "saved": now}
        self.said = (f"fetching the index (carried on after a restart): {len(have)} of {len(m['pieces'])} pieces")
        log.info("ord index %s: snapshot fetch carried on, %d of %d pieces kept", self.index.net, len(have),
                 len(m["pieces"]))

    def _save_fetch(self, force: bool = False) -> None:
        """Where the fetch got to, kept beside its .part (every FETCH_SAVE seconds, and at its manifest)."""
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
            log.debug("ord snapshot: could not keep where the fetch got to", exc_info=True)

    def forget(self) -> None:
        """Let go of the snapshot made here (old, of another layout, or not fetched from for SNAP_IDLE)."""
        with self.lock:
            self.made = None
            for path in (self.snap_path, self.manifest_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def tidy(self) -> None:
        if self.made is not None and time.time() - max(self.served_at, self.made["manifest"]["at"]) > SNAP_IDLE:
            log.info("ord index %s: snapshot not fetched from for %d h: let go of", self.index.net, SNAP_IDLE // 3600)
            self.forget()

    # --- making one ------------------------------------------------------------------------------------------------

    def make(self) -> dict:
        """A snapshot of the index as it is now (between two walk passes), its manifest kept beside it."""
        store = self.index.store
        tmp = self.snap_path.with_name(self.snap_path.name + ".tmp")
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        with store._lock:
            store._db.execute("VACUUM INTO ?", (str(tmp),))
        db = sqlite3.connect(tmp, isolation_level=None)
        try:
            for table in WORK:
                db.execute(f"DELETE FROM {table}")
            db.execute(f"DELETE FROM meta WHERE k IN ({','.join('?' * len(WORK_META))})", WORK_META)
            height = json.loads((db.execute("SELECT v FROM meta WHERE k='walk_next'").fetchone() or ["0"])[0])
            db.execute("VACUUM")
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
        from . import ord_rules
        from .doginal_index import LAYOUT
        manifest = {"net": self.index.net, "base": self.index.base, "rules": self.index.rules, "layout": LAYOUT,
                    "height": height,
                    "size": size, "piece": PIECE, "pieces": hashes, "at": int(time.time())}
        os.replace(tmp, self.snap_path)
        sid = manifest_id(manifest)
        self.manifest_path.write_text(json.dumps({"manifest": manifest, "id": sid}))
        self.made = {"manifest": manifest, "id": sid, "path": self.snap_path}
        log.info("ord index %s: snapshot at block %s, %.1f MB", self.index.net, height, size / 1e6)
        return self.made

    def want_made(self) -> bool:
        """Should this arcade make one now: it holds the index, walks on a big machine, and its last is old."""
        from .doginal_index import BIG_FROM_CORES
        if self.index.mode() != "walk" or (os.cpu_count() or 1) < BIG_FROM_CORES:
            return False
        if self.index.walker.next_height() <= self.index.base:
            return False
        return self.made is None or time.time() - self.made["manifest"]["at"] > SNAP_EVERY

    # --- what this arcade offers -----------------------------------------------------------------------------------

    def offer(self) -> dict | None:
        """This arcade's word in its "hi": the snapshot it can serve, whole or in part."""
        if self.made is not None:
            m = self.made["manifest"]
            return {"id": self.made["id"], "h": m["height"], "n": len(m["pieces"]), "have": "all"}
        f = self.fetch
        if f is not None and f.get("manifest") and f["have"]:
            m = f["manifest"]
            return {"id": f["id"], "h": m["height"], "n": len(m["pieces"]), "have": sorted(f["have"])}
        return None

    def heard_offer(self, node: str, word: Any) -> None:
        if isinstance(word, dict) and isinstance(word.get("id"), str) and len(word["id"]) == 64:
            self.offers[node] = {"id": word["id"], "h": int(word.get("h") or 0), "n": int(word.get("n") or 0),
                                 "have": word.get("have"), "at": time.time()}
        else:
            self.offers.pop(node, None)

    def manifest(self, sid: str) -> dict | None:
        if self.made is not None and self.made["id"] == sid:
            return self.made["manifest"]
        if self.fetch is not None and self.fetch["id"] == sid and self.fetch.get("manifest"):
            return self.fetch["manifest"]
        return None

    def piece(self, sid: str, i: int) -> bytes | None:
        """Piece i of snapshot `sid`, when this arcade has it (whole, or fetched already)."""
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
        """Why this arcade does not fetch a whole index ("" when it may). 2026-10-20: .25 -- four cores, a spinning
        disk, the live site -- fetched two 6 GB copies at once, and the site stopped answering. Only a big machine
        fetches one, with three times its size free."""
        from .doginal_index import BIG_FROM_CORES
        if (os.cpu_count() or 1) < BIG_FROM_CORES:
            return f"this machine has fewer than {BIG_FROM_CORES} cores: it walks on rather than fetch the whole index"
        if size is not None:
            import shutil
            free = shutil.disk_usage(self.part_path.parent).free
            if free < 3 * size:
                return (f"not enough free disk to fetch the whole index ({size / 1e9:.1f} GB; "
                        f"{free / 1e9:.1f} GB free, {3 * size / 1e9:.1f} GB needed)")
        return ""

    def best_offer(self) -> tuple[str, dict] | None:
        """The newest snapshot offered whole by a linked arcade that holds the index, worth fetching."""
        if self.may_fetch():
            return None
        holders = {n for n, _h in self.index.walkers()}
        whole = [(o["h"], n, o) for n, o in self.offers.items()
                 if o.get("have") == "all" and n in holders and time.time() - o["at"] < 600]
        if not whole:
            return None
        h, node, o = max(whole)
        if h - self.index.walker.next_height() < WORTH_BLOCKS:
            return None
        return node, o

    def step(self, share: Any) -> None:
        """One pass of fetching: start on the best offer, keep IN_FLIGHT pieces asked, finish when whole."""
        if share is None or not hasattr(share, "ask_manifest"):
            return
        with self.lock:
            f = self.fetch
            if f is None:
                best = self.best_offer()
                if best is None:
                    return
                node, o = best
                self.fetch = f = {"id": o["id"], "from": node, "manifest": None, "have": set(), "asked": {},
                                  "started": time.time()}
                self.said = f"fetching the index from arcade {node[:12]} (block {o['h']:,})"
            # Walked past the copy's block from this arcade's own Core meanwhile (2026-10-22: "Both"): no use.
            h = int((f.get("manifest") or {}).get("height") or (self.offers.get(f["from"]) or {}).get("h") or 0)
            if h and self.index.walker.next_height() >= h:
                self._drop(f"walked past block {h:,} from this arcade's own node before the copy came: let go of")
                return
            if time.time() - f.get("moved", f["started"]) > FETCH_STALL:
                if any(o.get("id") == f["id"] and time.time() - o.get("at", 0) < 600 for o in self.offers.values()):
                    # Still offered (a slow or broken link, a restart over there): keep every piece, ask again.
                    log.info("ord index %s: snapshot %s sent nothing for %d min: asked again, %d pieces kept",
                             self.index.net, f["id"][:12], FETCH_STALL // 60, len(f["have"]))
                    f["asked"], f["moved"], f["manifest_asked"] = {}, time.time(), 0
                    holder = next((n for n, o in self.offers.items() if o.get("id") == f["id"]
                                   and o.get("have") == "all"), None)
                    if holder:
                        f["from"] = holder
                else:
                    log.info("ord index %s: snapshot %s from %s sent nothing for %d min and nobody offers it: let go "
                             "of", self.index.net, f["id"][:12], f["from"][:12], FETCH_STALL // 60)
                    self._drop(f"the index fetch from arcade {f['from'][:12]} stalled: started again on the newest "
                               f"copy")
                    return
            if f["manifest"] is None:
                if time.time() - f.get("manifest_asked", 0) > PIECE_SECONDS:
                    f["manifest_asked"] = time.time()
                    share.ask_manifest(f["from"], f["id"])
                return
            m = f["manifest"]
            now = time.time()
            for i, (node, at) in list(f["asked"].items()):
                if now - at > PIECE_SECONDS:
                    f["asked"].pop(i)
            missing = [i for i in range(len(m["pieces"])) if i not in f["have"] and i not in f["asked"]]
            if not missing and not f["asked"]:
                self._finish()
                return
            busy = {node for node, _at in f["asked"].values()}
            for i in missing[:max(0, IN_FLIGHT - len(f["asked"]))]:
                has = [n for n, o in self.offers.items() if o["id"] == f["id"]
                       and (o.get("have") == "all" or i in (o.get("have") or []))]
                if not has:
                    continue
                free = [n for n in has if n not in busy] or has
                node = random.choice(free)
                busy.add(node)
                f["asked"][i] = (node, now)
                share.ask_piece(node, f["id"], i)

    def got_manifest(self, node: str, sid: str, manifest: Any) -> None:
        from . import ord_rules
        from .doginal_index import LAYOUT
        with self.lock:
            f = self.fetch
            if f is None or f["id"] != sid or f["manifest"] is not None:
                return
            if not isinstance(manifest, dict) or manifest_id(manifest) != sid:
                self._drop(f"arcade {node[:12]} sent a manifest that is not the one it offered")
                return
            if manifest.get("net") != self.index.net or manifest.get("base") != self.index.base \
                    or manifest.get("rules") != self.index.rules or manifest.get("piece") != PIECE \
                    or manifest.get("layout", 1) != LAYOUT:
                self._drop("the snapshot is of another chain, start, rules or layout")
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
                self.offers.pop(node, None)              # a wrong piece: that arcade is not asked again for this one
                log.warning("ord snapshot: arcade %s sent a wrong piece %d", node[:12], i)
                return
            with open(self.part_path, "r+b") as out:
                out.seek(i * m["piece"])
                out.write(data)
            f["have"].add(i)
            f["moved"] = time.time()
            self._save_fetch()
            self.said = f"fetching the index: {len(f['have'])} of {len(m['pieces'])} pieces"

    def piece_failed(self, node: str, sid: str, i: int) -> None:
        """`node` no longer has piece i (it finished and put its copy to use, or let it go): ask elsewhere now."""
        with self.lock:
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
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _finish(self) -> None:
        """Whole: check the copy, then put it in the index's place (called with the lock held)."""
        f = self.fetch
        try:
            single = self.check(self.part_path, f["manifest"], f["from"])
        except SnapshotError as exc:
            log.warning("ord snapshot %s: refused: %s", self.index.net, exc)
            self._drop(f"the fetched index was refused: {exc}")
            return
        m = f["manifest"]
        self.fetch = None
        try:
            self.fetch_path.unlink()
        except FileNotFoundError:
            pass
        self.index.install(self.part_path)
        self.single = single
        self.said = (f"the index came from the arcades that hold it, at block {m['height']:,}"
                     + (" (one arcade's copy: no second holder to compare with)" if single else ""))

    def check(self, path: Path, manifest: dict, maker: str) -> bool:
        """The copy against what this arcade knows (every chunk two arcades agree on) and another holder's moves.
        True when no second holder could be asked. Raises SnapshotError when it is wrong."""
        from .doginal_index import AGREE, CHUNK, Store, chunk_hash_of
        with open(path, "rb") as f:
            for i, want in enumerate(manifest["pieces"]):
                if _sha(f.read(manifest["piece"])) != want:
                    raise SnapshotError(f"piece {i} is not the manifest's")
        copy = Store(path, self.index.base, self.index.dialect, self.index.net)
        try:
            words = self.index.store.all_notes_words(10 ** 9)
            for c, by in words.items():
                tally: dict[str, int] = {}
                for node, (h, _nd, _n) in by.items():
                    if node != maker:
                        tally[h] = tally.get(h, 0) + 1
                agreed = [h for h, n in tally.items() if n >= AGREE]
                if not agreed:
                    continue
                lo = self.index.base + c * CHUNK
                rows = copy.q("SELECT line FROM block WHERE height>=? AND height<? ORDER BY height", (lo, lo + CHUNK))
                if len(rows) == CHUNK and chunk_hash_of([r[0] for r in rows]) != agreed[0]:
                    raise SnapshotError(f"chunk {c} is not what the arcades agree it is")
            walked = int(copy.meta("walk_next", self.index.base))
            done = (walked - self.index.base) // CHUNK
            others = [n for n, h in self.index.walkers() if n != maker]
            if not others or done <= 0:
                return True
            sample = sorted(random.sample(range(done), min(MOVES_SAMPLE, done)))
            ours = {c: moves_digest(copy, c, self.index.base, CHUNK) for c in sample}
            asked = 0
            for node in others:
                try:
                    theirs = self.index.share.ask({"what": "moves", "chunks": sample}, node=node).get("moves") or {}
                except Exception:                        # noqa: BLE001 -- not answering: ask the next
                    continue
                asked += 1
                for c in sample:
                    t = theirs.get(str(c))
                    if t is not None and ours[c] is not None and t != ours[c]:
                        raise SnapshotError(f"its moves in chunk {c} are not arcade {node[:12]}'s")
                break
            return asked == 0
        finally:
            copy.close()
