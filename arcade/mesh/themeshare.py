"""Themes offered between arcades (arcade/packages.py; 2026-11-04, SadFrog: a guide "for people making new themes, how to
make them and how to add them to the shared themes other operators can enable").

An operator offers an installed theme with `sharetheme <name>` or Window -> Build -> Share a theme. Every linked arcade
is told the offer; its operator sees it under Shared by others and may install it, and nothing installs by itself. A
theme is values only -- colours, a corner radius, at most a font -- so no code travels. Node modules are not offered
here: they run code, and the charter's review marks come first (docs/arcade-lite/PACKAGES.md, "Offering a package").

On the wire (frames of kind "pk" on an ordinary link, never flooded; every one under the link's 64 KB):

    hi    {v, o: [[id, name, title, version, summary, bytes]...]}   the themes this arcade offers (link up; on change)
    get   {id, pid}                                                send me that theme's files
    part  {id, seq, end[, err]} + a slice of the files             zlib of json {path: base64}, in parts of PART;
                                                                   err "gone" when it is not offered, "busy" when
                                                                   asked too often

A fetched theme is kept only when its files hash to the id that was offered (packages.package_id) and it passes the
same check as one installed from a folder (packages.read).
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any

from .. import packages as packagelib

log = logging.getLogger("arcade.mesh")

VERSION = 1
SHARED = "lite_themes_shared"      # settings.json: the ids of the installed themes this arcade offers
MOST_OFFERS = 50
PART = 60 * 1024
HELLO_EVERY = 600.0
PEER_TTL = 6 * 3600.0
FETCH_TIMEOUT = 120.0
ANSWERS_PER_MINUTE = 20


def pack(files: dict[str, bytes]) -> bytes:
    return zlib.compress(json.dumps({k: base64.b64encode(v).decode() for k, v in files.items()}).encode(), 6)


def unpack(data: bytes) -> dict[str, bytes] | None:
    """A theme's files as sent, or None: not zlib, past a package's size, or not {path: base64}."""
    d = zlib.decompressobj()
    try:
        raw = d.decompress(data, 4 * packagelib.MOST_BYTES + 1)
    except zlib.error:
        return None
    if len(raw) > 4 * packagelib.MOST_BYTES or d.unconsumed_tail or not d.eof:
        return None
    try:
        got = json.loads(raw.decode("utf-8"))
        files = {str(k): base64.b64decode(v, validate=True) for k, v in got.items()} if isinstance(got, dict) else None
    except (ValueError, TypeError):
        return None
    if not files or len(files) > packagelib.MOST_FILES or sum(map(len, files.values())) > packagelib.MOST_BYTES:
        return None
    # Only a theme's own names, checked before anything is written: never a path out of the folder it lands in.
    if any(rel != packagelib.MANIFEST and not packagelib.FILES.match(rel) for rel in files):
        return None
    return files


def _offer(row: Any) -> dict | None:
    """One "hi" row, checked: [id, name, title, version, summary, bytes]."""
    if not isinstance(row, list) or len(row) < 6:
        return None
    pid, name, title, version, summary, size = row[:6]
    if not isinstance(pid, str) or len(pid) != 64 or not isinstance(name, str) or not packagelib.NAME.match(name):
        return None
    try:
        int(pid, 16)
    except ValueError:
        return None
    return {"id": pid.lower(), "name": name, "title": str(title or name)[:60], "version": str(version or "")[:14],
            "summary": str(summary or "")[:200], "bytes": size if isinstance(size, int) and size >= 0 else 0}


class ThemeShare:
    kinds = ("pk",)

    def __init__(self, state, clock=time.monotonic):
        self.state = state
        self.clock = clock
        self.node = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.peers: dict[str, dict] = {}            # peer -> {"at", "offers": {id: row}}
        self._said: dict[str, tuple[list, float]] = {}
        self._asked: dict[str, list[float]] = {}
        self._ids = itertools.count(1)
        self._waiting: dict[int, dict] = {}         # fetch id -> {"peer", "pid", "parts", "done": Future}

    # ---------------------------------------------------------------- what this arcade offers, and what it is offered
    def offers(self) -> list[list]:
        settings = self.state.settings()
        have = settings.get(packagelib.SETTING) or {}
        out = []
        for pid in (settings.get(SHARED) or [])[:MOST_OFFERS]:
            row = have.get(pid)
            if not isinstance(row, dict) or row.get("kind") != "theme":
                continue
            try:
                size = sum(p.stat().st_size for p in packagelib.home_of(self.state.home, pid).rglob("*") if p.is_file())
            except (packagelib.PackageError, OSError):
                continue
            out.append([pid, row["name"], row.get("title") or row["name"], row.get("version") or "",
                        row.get("summary") or "", size])
        return out

    def offered(self) -> list[dict]:
        """What linked arcades offer, newest word first: each theme with the arcades offering it."""
        now, seen = self.clock(), {}
        for peer, said in self.peers.items():
            if now - said["at"] >= PEER_TTL:
                continue
            for pid, row in said["offers"].items():
                seen.setdefault(pid, {**row, "from": []})["from"].append(peer)
        return sorted(seen.values(), key=lambda r: (r["title"].lower(), r["id"]))

    def _linked(self) -> dict:
        return dict(getattr(self.node, "links", None) or {})

    # ---------------------------------------------------------------- the link's hooks (node.add_extension)
    async def link_up(self, link) -> None:
        self.loop = asyncio.get_running_loop()
        await self._hello(link)

    async def link_down(self, peer_id: str) -> None:
        self._said.pop(peer_id, None)
        for fid, w in list(self._waiting.items()):
            if w["peer"] == peer_id and not w["done"].done():
                w["done"].set_exception(RuntimeError("that arcade went away"))

    async def rebalance(self) -> None:
        self.loop = asyncio.get_running_loop()
        now = self.clock()
        for peer in [p for p, s in self.peers.items() if now - s["at"] >= PEER_TTL and p not in self._linked()]:
            self.peers.pop(peer, None)
        offers = await asyncio.to_thread(self.offers)
        for peer, link in self._linked().items():
            said, at = self._said.get(peer, (None, -1e18))
            if said != offers or now - at >= HELLO_EVERY:
                await self._hello(link, offers)

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        op, peer = header.get("op"), link.peer_id
        try:
            if op == "hi":
                rows = header.get("o")
                if isinstance(rows, list):
                    offers = {o["id"]: o for o in map(_offer, rows[:MOST_OFFERS]) if o}
                    self.peers[peer] = {"at": self.clock(), "offers": offers}
            elif op == "get":
                if self._too_many(peer):
                    await self._send(link, {"op": "part", "id": header.get("id"), "seq": 0, "end": True, "err": "busy"})
                    return
                asyncio.ensure_future(self._serve(link, header))
            elif op == "part":
                self._part(peer, header, blob)
        except Exception:                                      # noqa: BLE001 -- not a word this version reads
            log.debug("pk frame", exc_info=True)

    async def _hello(self, link, offers: list | None = None) -> None:
        offers = offers if offers is not None else await asyncio.to_thread(self.offers)
        self._said[link.peer_id] = (offers, self.clock())
        await self._send(link, {"op": "hi", "v": VERSION, "o": offers})

    # ---------------------------------------------------------------- serving a theme, and fetching one
    async def _serve(self, link, header: dict) -> None:
        fid, pid = header.get("id"), str(header.get("pid") or "")
        if pid not in {o[0] for o in await asyncio.to_thread(self.offers)}:
            await self._send(link, {"op": "part", "id": fid, "seq": 0, "end": True, "err": "gone"})
            return
        _, files = await asyncio.to_thread(packagelib.read, packagelib.home_of(self.state.home, pid))
        data = pack(files)
        parts = [data[i:i + PART] for i in range(0, len(data), PART)] or [b""]
        for seq, piece in enumerate(parts):
            await self._send(link, {"op": "part", "id": fid, "seq": seq, "end": seq == len(parts) - 1}, piece)

    def _part(self, peer: str, header: dict, blob: bytes) -> None:
        w = self._waiting.get(header.get("id"))
        if w is None or w["peer"] != peer or w["done"].done():
            return
        if header.get("err"):
            w["done"].set_exception(RuntimeError({"gone": "that arcade no longer offers it",
                                                  "busy": "that arcade is busy; try again in a minute"}
                                                 .get(header["err"], "that arcade refused")))
            return
        if header.get("seq") != len(w["parts"]):
            w["done"].set_exception(RuntimeError("the theme arrived out of order"))
            return
        w["parts"].append(blob)
        if sum(map(len, w["parts"])) > 4 * packagelib.MOST_BYTES:
            w["done"].set_exception(RuntimeError("the theme is too big"))
        elif header.get("end"):
            w["done"].set_result(b"".join(w["parts"]))

    async def fetch(self, pid: str, peer: str | None = None) -> tuple[str, dict[str, bytes]]:
        """(the arcade it came from, a theme's files) from a linked arcade that offers it, checked against its id."""
        offering = [p for p, s in self.peers.items() if pid in s["offers"] and p in self._linked()]
        if peer:
            offering = [p for p in offering if p == peer]
        if not offering:
            raise RuntimeError("no linked arcade offers that theme now")
        link = self._linked()[offering[0]]
        fid = next(self._ids)
        done = asyncio.get_running_loop().create_future()
        self._waiting[fid] = {"peer": offering[0], "pid": pid, "parts": [], "done": done}
        try:
            await self._send(link, {"op": "get", "id": fid, "pid": pid})
            data = await asyncio.wait_for(done, FETCH_TIMEOUT)
        finally:
            self._waiting.pop(fid, None)
        files = unpack(data)
        if files is None or packagelib.package_id(files) != pid:
            raise RuntimeError("what came does not match the theme's id; nothing was kept")
        return offering[0], files

    def install(self, pid: str, peer: str | None = None, timeout: float = FETCH_TIMEOUT + 10) -> tuple[str, dict]:
        """Fetch a theme and keep it (from a web route's thread). (id, its record for settings.json "packages")."""
        if self.loop is None:
            raise RuntimeError("the mesh is not running")
        peer, files = asyncio.run_coroutine_threadsafe(self.fetch(pid, peer), self.loop).result(timeout)
        with tempfile.TemporaryDirectory(prefix="theme-") as tmp:
            folder = Path(tmp) / "theme"
            for rel, data in files.items():
                (folder / rel).parent.mkdir(parents=True, exist_ok=True)
                (folder / rel).write_bytes(data)
            manifest, _ = packagelib.read(folder)                 # the same check as a folder on this machine
            if manifest["kind"] != "theme":
                raise RuntimeError("only themes are taken from other arcades")
            return packagelib.install(self.state.home, folder, source=f"mesh:{peer[:12]}")

    def _too_many(self, peer: str) -> bool:
        now = self.clock()
        asked = [t for t in self._asked.get(peer, []) if t > now - 60]
        self._asked[peer] = asked + [now]
        return len(asked) >= ANSWERS_PER_MINUTE

    async def _send(self, link, frame: dict, blob: bytes = b"") -> None:
        try:
            if blob:
                await link.send({"t": "pk", **frame}, blob)
            else:
                await link.send({"t": "pk", **frame})
        except Exception:                                      # noqa: BLE001 -- the link went
            pass
