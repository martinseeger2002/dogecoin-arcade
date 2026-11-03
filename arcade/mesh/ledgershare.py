"""Token/name index copies between linked arcades (arcade/ledger_snapshot.py, 2026-11-02).

On the wire (frames of kind "lx" on an ordinary link, never flooded), each one small:

    hi  {nets: {net: {h, sn}}}     how far this arcade's index of each chain is, and the copy it can serve (`sn`:
                                   {id, h, n, have: "all" or the pieces it has so far}, or null)
    sq  {net}                      this arcade is far behind: make a copy of that chain's index
    g   {id, net, what, snap[, i], z}   send copy `snap`'s manifest (what "manifest") or its piece i ("piece");
                                   `z` 1: the answer is zlib-compressed
    gb  {id, seq, end[, err][, z]} + a slice   the answer, in parts under a frame's size

Said when a link comes up and once a minute. A frame names no other arcade: what a link carries is that arcade's own
word. Serving runs off the mesh loop (a thread per piece read, at most SERVING_AT_ONCE at a time) and is bounded per
asker, so links, rooms and calls never wait behind it.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
import zlib
from typing import Any, Callable

from . import relay as _relay

log = logging.getLogger("arcade.mesh")

PART = 60 * 1024                 # a link frame carries at most 64 KB
SERVING_AT_ONCE = 2              # pieces this arcade sends at the same time, to everybody together
PIECES_PER_MINUTE = 120          # pieces one linked arcade may ask of this one
INFLATE_MAX = 4 * 1024 * 1024    # a piece is 1 MB and a manifest well under; anything larger is refused
WAIT_SECONDS = 300


def _inflate(data: bytes) -> bytes | None:
    d = zlib.decompressobj()
    try:
        out = d.decompress(data, INFLATE_MAX + 1)
    except zlib.error:
        return None
    if len(out) > INFLATE_MAX or d.unconsumed_tail or not d.eof:
        return None
    return out


class LedgerShare:
    kinds = ("lx",)

    def __init__(self, snapshots: Callable[[], dict]):
        self.snapshots = snapshots                 # {net: LedgerSnapshots}
        self.node = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._ids = itertools.count(1)
        self._wait: dict[int, dict] = {}
        self._asked: dict[str, list[float]] = {}
        self._serving: asyncio.Semaphore | None = None

    # ---------------------------------------------------------------- hooks
    def _hello(self) -> dict:
        nets = {}
        for net, snaps in (self.snapshots() or {}).items():
            try:
                nets[net] = {"h": snaps.indexed(), "sn": snaps.offer()}
            except Exception:                                  # noqa: BLE001 -- that index is not ready
                continue
        return {"t": "lx", "op": "hi", "nets": nets}

    async def link_up(self, link) -> None:
        self.loop = asyncio.get_running_loop()
        try:
            await link.send(await asyncio.to_thread(self._hello))
        except Exception:                                      # noqa: BLE001 -- the link went
            log.debug("lx link_up", exc_info=True)

    async def link_down(self, peer_id: str) -> None:
        for snaps in (self.snapshots() or {}).values():
            snaps.gone(peer_id)

    async def rebalance(self) -> None:
        self.loop = asyncio.get_running_loop()
        hello = await asyncio.to_thread(self._hello)
        for link in list(_relay.every_link(self.node)):
            try:
                await link.send(hello)
            except Exception:                                  # noqa: BLE001
                pass

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        op, peer = header.get("op"), link.peer_id
        if op == "gb":
            await self._part(link, header, blob)
            return
        if op == "hi":
            nets = header.get("nets")
            if not isinstance(nets, dict):
                return
            mine = self.snapshots() or {}
            for net, said in nets.items():
                snaps = mine.get(str(net))
                if snaps is not None and isinstance(said, dict):
                    snaps.heard(peer, said.get("sn"), said.get("h"))
            return
        snaps = (self.snapshots() or {}).get(str(header.get("net") or ""))
        if snaps is None:
            return
        if op == "sq":
            snaps.asked_to_make(peer)
        elif op == "g":
            asyncio.ensure_future(self._send(link, snaps, header))

    # ---------------------------------------------------------------- serving
    async def _send(self, link, snaps, header: dict) -> None:
        rid, what, sid = header.get("id"), header.get("what"), header.get("snap")
        if not isinstance(rid, int) or not isinstance(sid, str):
            return
        now = time.monotonic()
        asked = [t for t in self._asked.get(link.peer_id, []) if t > now - 60]
        # Only what is served counts (2026-11-02, live: refusals counted too, the asker asked again at once, and a
        # fetch sat at 97 of 411 pieces for ten minutes, every ask refused).
        busy = len(asked) >= PIECES_PER_MINUTE
        self._asked[link.peer_id] = asked if busy else asked + [now]
        if self._serving is None:
            self._serving = asyncio.Semaphore(SERVING_AT_ONCE)
        try:
            if busy:
                await link.send({"t": "lx", "op": "gb", "id": rid, "seq": 0, "end": True, "err": "busy"})
                return
            async with self._serving:
                if what == "manifest":
                    m = snaps.manifest(sid)
                    data = None if m is None else json.dumps(m, sort_keys=True, separators=(",", ":")).encode()
                elif what == "piece" and isinstance(header.get("i"), int):
                    data = await asyncio.to_thread(snaps.piece, sid, header["i"])
                else:
                    data = None
                if data is None:
                    await link.send({"t": "lx", "op": "gb", "id": rid, "seq": 0, "end": True, "err": "gone"})
                    return
                z = header.get("z") == 1
                if z:
                    data = await asyncio.to_thread(zlib.compress, data, 6)
                parts = [data[i:i + PART] for i in range(0, len(data), PART)] or [b""]
                for seq, part in enumerate(parts):
                    await link.send({"t": "lx", "op": "gb", "id": rid, "seq": seq, "end": seq == len(parts) - 1,
                                     **({"z": 1} if z else {})}, part)
        except Exception:                                      # noqa: BLE001 -- the link went
            log.debug("lx send", exc_info=True)

    # ---------------------------------------------------------------- asking (from the watcher's thread)
    def _post(self, node: str, frame: dict) -> bool:
        loop = self.loop
        link = _relay.reach(self.node, node)
        if loop is None or link is None:
            return False
        try:
            asyncio.run_coroutine_threadsafe(link.send({"t": "lx", **frame}), loop)
        except RuntimeError:                                   # the loop stopped
            return False
        return True

    def ask_make(self, node: str, net: str) -> None:
        self._post(node, {"op": "sq", "net": net})

    def _get(self, node: str, net: str, ask: dict) -> None:
        now = time.monotonic()
        for rid, w in list(self._wait.items()):
            if now - w["at"] > WAIT_SECONDS:
                self._wait.pop(rid, None)
        rid = next(self._ids)
        self._wait[rid] = {"net": net, "peer": node, **ask, "parts": [], "size": 0, "at": now}
        if not self._post(node, {"op": "g", "net": net, "id": rid, **ask, "z": 1}):
            self._wait.pop(rid, None)

    def ask_manifest(self, node: str, net: str, sid: str) -> None:
        self._get(node, net, {"what": "manifest", "snap": sid})

    def ask_piece(self, node: str, net: str, sid: str, i: int) -> None:
        self._get(node, net, {"what": "piece", "snap": sid, "i": i})

    async def _part(self, link, header: dict, blob: bytes) -> None:
        w = self._wait.get(header.get("id"))
        if w is None or w["peer"] != link.peer_id:
            return
        snaps = (self.snapshots() or {}).get(w["net"])
        if header.get("err") or header.get("seq") != len(w["parts"]) or w["size"] + len(blob) > INFLATE_MAX:
            self._wait.pop(header.get("id"), None)
            if snaps is not None and w["what"] == "piece":
                await asyncio.to_thread(snaps.piece_failed, w["peer"], w["snap"], int(w["i"]),
                                        header.get("err") == "busy")
            return
        w["parts"].append(blob)
        w["size"] += len(blob)
        w["z"] = w.get("z") or header.get("z") == 1
        if not header.get("end"):
            return
        self._wait.pop(header.get("id"), None)
        if snaps is None:
            return
        data = b"".join(w["parts"])
        if w.get("z"):
            data = await asyncio.to_thread(_inflate, data)
            if data is None:
                if w["what"] == "piece":
                    await asyncio.to_thread(snaps.piece_failed, w["peer"], w["snap"], int(w["i"]))
                return
        if w["what"] == "manifest":
            try:
                manifest = json.loads(data.decode())
            except ValueError:
                return
            await asyncio.to_thread(snaps.got_manifest, w["peer"], w["snap"], manifest)
        else:
            await asyncio.to_thread(snaps.got_piece, w["peer"], w["snap"], int(w["i"]), data)
