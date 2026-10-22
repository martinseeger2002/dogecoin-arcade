"""The Doginals index's shared work over the mesh (arcade/doginal_index.py; 2026-10-20: "the mesh network
shared, indexing and verification").

On the wire (frames of kind "dx" on an ordinary link, never flooded), each one small:

Every frame names its chain (`net`, and `base` in "hi"): one index per chain (2026-10-20, the Ords tab). Frames of an
older arcade (v1, no `net`) are not read.

    hi  {net, base, mode, walk, helped, v}   what this arcade does: walk (its own full index) or help (chunks)
    ls  {c, at, until}                   it holds chunk c's lease, has read up to block `at`, until `until`
    ld  {c}                              it let chunk c go (finished, or gave up)
    dn  {rows: [[c, hash, births, nd]...]}   its hashes of finished chunks (and its spend notes' digest, 2026-10-20);
                                         every one of them when a link comes up
    ng  {c, id}                          the asker holds the whole index: send chunk c's spend notes (ord_notes)
    nb  {id, seq, end, nd[, err]} + a slice of the packed notes      the answer, in parts under a frame's size
    sq  {}                               the asker is far behind: make a snapshot of the whole index (ord_snapshot)
    g   {id, what, snap[, i]}            send snapshot `snap`'s manifest (what "manifest") or its piece i ("piece")
    gb  {id, seq, end[, err]} + a slice                               the answer, in parts
  (a "hi" carries `sn`: the snapshot this arcade serves, whole or the pieces it has so far)

A frame names no other arcade: what a link carries is that arcade's own word (the link proves who it is), so nobody
can speak for another. Leases are a courtesy -- two helpers on one chunk cost a scan, never a wrong answer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import itertools
import threading
import time
from typing import Any

log = logging.getLogger("arcade.mesh")

ROWS_PER_FRAME = 500           # a "dn" frame stays well under the link's 64 KB frame


ASK_SECONDS = 15.0
PART = 60 * 1024               # a slice of packed notes in one frame (a link carries at most 64 KB)
NOTES_AT_ONCE = 4              # chunks' notes this arcade sends at the same time, to everybody together
MAX_NOTES = 64 * 1024 * 1024   # the packed notes of one chunk, at most
ANSWERS_PER_MINUTE = 120       # questions one linked arcade may ask of this one


class _Chain:
    """One chain's index's way out: what it posts goes out with its chain named, and its questions to a walker."""

    def __init__(self, share: "DxShare", net: str):
        self.share, self.net = share, net

    def post(self, frame: dict) -> None:
        self.share.post({**frame, "net": self.net})

    def ask(self, question: dict, node: str | None = None) -> dict:
        return self.share.ask(self.net, question, node=node)

    def fetch_notes(self, c: int, node: str, nd: str, hash_: str) -> None:
        self.share.fetch_notes(self.net, c, node, nd, hash_)

    def post_to(self, node: str, frame: dict) -> None:
        self.share.post_to(node, {**frame, "net": self.net})

    def ask_manifest(self, node: str, sid: str) -> None:
        self.share.get(self.net, node, {"what": "manifest", "snap": sid})

    def ask_piece(self, node: str, sid: str, i: int) -> None:
        self.share.get(self.net, node, {"what": "piece", "snap": sid, "i": i})


class DxShare:
    kinds = ("dx",)

    def __init__(self, indexes: Any):
        # {net: DoginalIndex}, or one index (its own chain), or a callable giving the dict (chains switched on later)
        if callable(indexes) and not hasattr(indexes, "net"):
            self._indexes = indexes
        else:
            fixed = indexes if isinstance(indexes, dict) else {indexes.net: indexes}
            self._indexes = lambda: fixed
        self.node = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._waiting: dict[int, tuple[str, asyncio.Future]] = {}
        self._asked: dict[str, list[float]] = {}
        self._notes_wait: dict[int, dict] = {}         # request id -> {net, c, peer, nd, hash, parts, size}
        self._get_wait: dict[int, dict] = {}           # request id -> {net, peer, what, snap, i, parts, size}
        self._serving = None                           # asyncio.Semaphore(NOTES_AT_ONCE), made on the loop
        self.indexes()

    def indexes(self) -> dict:
        got = dict(self._indexes() or {})
        for net, index in got.items():
            if not isinstance(getattr(index, "share", None), _Chain):
                index.share = _Chain(self, net)
            if getattr(index, "me", None) is None and getattr(self.node, "node_id", None):
                index.me = self.node.node_id
        return got

    # ---------------------------------------------------------------- hooks
    async def link_up(self, link) -> None:
        self.loop = asyncio.get_running_loop()
        import time
        from ..doginal_index import LEASE_SECONDS
        try:
            for net, index in (await asyncio.to_thread(self.indexes)).items():
                if index.mode() == "off":
                    continue
                await link.send({"t": "dx", **await asyncio.to_thread(index.hello)})
                rows = await asyncio.to_thread(index.all_mine)
                for i in range(0, len(rows), ROWS_PER_FRAME):
                    await link.send({"t": "dx", "op": "dn", "net": net, "rows": rows[i:i + ROWS_PER_FRAME]})
                for lane in list(index.chunks.lanes):              # every lane this arcade holds
                    await link.send({"t": "dx", "op": "ls", "net": net, "c": lane["c"], "at": lane["at"],
                                     "until": time.time() + LEASE_SECONDS})
        except Exception:                                      # noqa: BLE001 -- the link went
            log.debug("dx link_up", exc_info=True)

    async def link_down(self, peer_id: str) -> None:
        for index in self.indexes().values():
            index.chunks.gone(peer_id)

    async def rebalance(self) -> None:
        self.loop = asyncio.get_running_loop()
        for index in (await asyncio.to_thread(self.indexes)).values():
            if index.mode() != "off":
                await self._send_all({"t": "dx", **await asyncio.to_thread(index.hello)})

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        if header.get("op") == "nb":                           # a part of spend notes this arcade asked for
            await self._notes_part(link, header, blob)
            return
        if header.get("op") == "gb":                           # a part of a snapshot's manifest or piece
            await self._get_part(link, header, blob)
            return
        if header.get("op") == "a":                            # an answer to one of this arcade's questions
            got = self._waiting.get(header.get("id"))
            if got is not None and got[0] == link.peer_id and not got[1].done():
                got[1].set_result(header)
            return
        index = self.indexes().get(str(header.get("net") or ""))
        if index is None:
            return                                             # a chain not indexed here, or an older arcade
        if header.get("op") == "q":
            await self._answer(link, index, header)
            return
        if header.get("op") == "ng":
            asyncio.ensure_future(self._send_notes(link, index, header))
            return
        if header.get("op") == "g":
            asyncio.ensure_future(self._send_got(link, index, header))
            return
        if header.get("op") == "hi" and header.get("base") not in (None, index.base):
            return                                             # cut from another start: its chunks are not ours
        try:
            await asyncio.to_thread(index.heard, link.peer_id, header)
        except Exception:                                      # noqa: BLE001 -- not a word this version reads
            log.debug("dx frame", exc_info=True)

    # ---------------------------------------------------------------- questions (read only)
    async def _answer(self, link, index, header: dict) -> None:
        rid, peer, now = header.get("id"), link.peer_id, time.monotonic()
        if not isinstance(rid, int):
            return
        asked = [t for t in self._asked.get(peer, []) if t > now - 60]
        self._asked[peer] = asked + [now]
        said: dict = {"t": "dx", "op": "a", "id": rid}
        try:
            if len(asked) >= ANSWERS_PER_MINUTE:
                raise ValueError("too many questions this minute")
            got = await asyncio.to_thread(index.answer, header.get("question") or {})
            said.update(got, walked_to=index.walker.next_height())
        except Exception as exc:                               # noqa: BLE001 -- said back
            said["err"] = str(exc)[:200]
        try:
            await link.send(said)
        except Exception:                                      # noqa: BLE001
            pass

    def ask(self, net: str, question: dict, node: str | None = None) -> dict:
        """From a web thread: ask the furthest-walked linked arcade for this chain (or `node`). {"where"|"on", "from",
        "walked_to"}."""
        index = self.indexes().get(net)
        loop = self.loop
        if index is None or loop is None or self.node is None:
            raise ValueError("no linked arcade to ask")
        for node, _height in ([(node, 0)] if node else index.walkers()):
            link = self.node.links.get(node)
            if link is None:
                continue
            got = asyncio.run_coroutine_threadsafe(self._ask(link, net, question), loop).result(ASK_SECONDS + 1)
            if got.get("err"):
                raise ValueError(f"arcade {node[:12]}: {got['err']}")
            return {k: v for k, v in got.items() if k not in ("t", "op", "id")} | {"from": f"arcade {node[:12]}"}
        raise ValueError("no linked arcade walks this chain yet")

    async def _ask(self, link, net: str, question: dict) -> dict:
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._waiting[rid] = (link.peer_id, fut)
        try:
            await link.send({"t": "dx", "op": "q", "net": net, "id": rid, "question": question})
            return await asyncio.wait_for(fut, ASK_SECONDS)
        finally:
            self._waiting.pop(rid, None)

    # ---------------------------------------------------------------- spend notes (torrent-style, 2026-10-20)
    async def _send_notes(self, link, index, header: dict) -> None:
        """Chunk c's packed notes to the arcade that asked, in parts; "gone" when they are not kept here."""
        rid, c = header.get("id"), header.get("c")
        if not isinstance(rid, int) or not isinstance(c, int):
            return
        if self._serving is None:
            self._serving = asyncio.Semaphore(NOTES_AT_ONCE)
        async with self._serving:
            try:
                got = await asyncio.to_thread(index.notes_blob, c)
                if got is None:
                    await link.send({"t": "dx", "op": "nb", "id": rid, "seq": 0, "end": True, "err": "gone"})
                    return
                blob, nd = got
                parts = [blob[i:i + PART] for i in range(0, len(blob), PART)] or [b""]
                for seq, part in enumerate(parts):
                    await link.send({"t": "dx", "op": "nb", "id": rid, "seq": seq, "end": seq == len(parts) - 1,
                                     "nd": nd}, part)
            except Exception:                                  # noqa: BLE001 -- the link went
                log.debug("dx notes send", exc_info=True)

    def fetch_notes(self, net: str, c: int, node: str, nd: str, hash_: str) -> None:
        """From the index's thread: ask `node` for chunk c's notes; they reach index.got_notes (or fetch_failed)."""
        loop = self.loop
        link = self.node.links.get(node) if self.node is not None else None
        index = self.indexes().get(net)
        if loop is None or link is None or index is None:
            if index is not None:
                index.fetch_failed(node, c)
            return
        rid = next(self._ids)
        self._notes_wait[rid] = {"net": net, "c": c, "peer": node, "nd": nd, "hash": hash_, "parts": [], "size": 0,
                                 "at": time.monotonic()}
        try:
            asyncio.run_coroutine_threadsafe(
                link.send({"t": "dx", "op": "ng", "net": net, "c": c, "id": rid}), loop)
        except RuntimeError:                                   # the loop stopped
            self._notes_wait.pop(rid, None)

    async def _notes_part(self, link, header: dict, blob: bytes) -> None:
        w = self._notes_wait.get(header.get("id"))
        if w is None or w["peer"] != link.peer_id:
            return
        index = self.indexes().get(w["net"])
        if header.get("err") or index is None or header.get("seq") != len(w["parts"]) \
                or w["size"] + len(blob) > MAX_NOTES:
            self._notes_wait.pop(header.get("id"), None)
            if index is not None:
                await asyncio.to_thread(index.fetch_failed, w["peer"], w["c"])
            return
        w["parts"].append(blob)
        w["size"] += len(blob)
        if header.get("end"):
            self._notes_wait.pop(header.get("id"), None)
            await asyncio.to_thread(index.got_notes, w["peer"], w["c"], b"".join(w["parts"]), w["nd"], w["hash"])

    # ---------------------------------------------------------------- snapshots (ord_snapshot, 2026-10-20)
    async def _send_got(self, link, index, header: dict) -> None:
        rid, what, sid = header.get("id"), header.get("what"), header.get("snap")
        if not isinstance(rid, int) or not isinstance(sid, str):
            return
        if self._serving is None:
            self._serving = asyncio.Semaphore(NOTES_AT_ONCE)
        async with self._serving:
            try:
                snaps = index.snapshots
                if what == "manifest":
                    m = snaps.manifest(sid)
                    data = None if m is None else json.dumps(m, sort_keys=True, separators=(",", ":")).encode()
                elif what == "piece" and isinstance(header.get("i"), int):
                    data = await asyncio.to_thread(snaps.piece, sid, header["i"])
                else:
                    data = None
                if data is None:
                    await link.send({"t": "dx", "op": "gb", "id": rid, "seq": 0, "end": True, "err": "gone"})
                    return
                parts = [data[i:i + PART] for i in range(0, len(data), PART)] or [b""]
                for seq, part in enumerate(parts):
                    await link.send({"t": "dx", "op": "gb", "id": rid, "seq": seq, "end": seq == len(parts) - 1}, part)
            except Exception:                                  # noqa: BLE001 -- the link went
                log.debug("dx snapshot send", exc_info=True)

    def get(self, net: str, node: str, ask: dict) -> None:
        """From the index's thread: ask `node` for a snapshot's manifest or piece; it reaches Snapshots.got_*."""
        loop = self.loop
        link = self.node.links.get(node) if self.node is not None else None
        if loop is None or link is None:
            return
        now = time.monotonic()
        for rid, w in list(self._get_wait.items()):
            if now - w["at"] > 300:
                self._get_wait.pop(rid, None)
        rid = next(self._ids)
        self._get_wait[rid] = {"net": net, "peer": node, **ask, "parts": [], "size": 0, "at": now}
        try:
            asyncio.run_coroutine_threadsafe(link.send({"t": "dx", "op": "g", "net": net, "id": rid, **ask}), loop)
        except RuntimeError:
            self._get_wait.pop(rid, None)

    async def _get_part(self, link, header: dict, blob: bytes) -> None:
        w = self._get_wait.get(header.get("id"))
        if w is None or w["peer"] != link.peer_id:
            return
        if header.get("err") or header.get("seq") != len(w["parts"]) or w["size"] + len(blob) > 8 * 1024 * 1024:
            self._get_wait.pop(header.get("id"), None)
            index = self.indexes().get(w["net"])
            if index is not None and w["what"] == "piece":
                await asyncio.to_thread(index.snapshots.piece_failed, w["peer"], w["snap"], int(w["i"]))
            return
        w["parts"].append(blob)
        w["size"] += len(blob)
        if not header.get("end"):
            return
        self._get_wait.pop(header.get("id"), None)
        index = self.indexes().get(w["net"])
        if index is None:
            return
        data = b"".join(w["parts"])
        snaps = index.snapshots
        if w["what"] == "manifest":
            try:
                manifest = json.loads(data.decode())
            except ValueError:
                return
            await asyncio.to_thread(snaps.got_manifest, w["peer"], w["snap"], manifest)
        else:
            await asyncio.to_thread(snaps.got_piece, w["peer"], w["snap"], int(w["i"]), data)

    def post_to(self, node: str, frame: dict) -> None:
        loop = self.loop
        link = self.node.links.get(node) if self.node is not None else None
        if loop is None or link is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(link.send({"t": "dx", **frame}), loop)
        except RuntimeError:
            pass

    # ---------------------------------------------------------------- saying
    def post(self, frame: dict) -> None:
        """From the index's thread: say this to every linked arcade."""
        loop = self.loop
        if loop is None or self.node is None:
            return
        rows = frame.get("rows")
        frames = ([{**frame, "rows": rows[i:i + ROWS_PER_FRAME]} for i in range(0, len(rows), ROWS_PER_FRAME)]
                  if isinstance(rows, list) else [frame])
        for f in frames:
            try:
                asyncio.run_coroutine_threadsafe(self._send_all({"t": "dx", **f}), loop)
            except RuntimeError:                               # the loop stopped
                return

    async def _send_all(self, frame: dict) -> None:
        for link in list((self.node.links if self.node else {}).values()):
            try:
                await link.send(frame)
            except Exception:                                  # noqa: BLE001
                pass
