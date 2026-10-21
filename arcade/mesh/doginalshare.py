"""The Doginals index's shared work over the mesh (arcade/doginal_index.py; 2026-10-20: "the mesh network
shared, indexing and verification").

On the wire (frames of kind "dx" on an ordinary link, never flooded), each one small:

Every frame names its chain (`net`, and `base` in "hi"): one index per chain (2026-10-20, the Ords tab). Frames of an
older arcade (v1, no `net`) are not read.

    hi  {net, base, mode, walk, helped, v}   what this arcade does: walk (its own full index) or help (chunks)
    ls  {c, at, until}                   it holds chunk c's lease, has read up to block `at`, until `until`
    ld  {c}                              it let chunk c go (finished, or gave up)
    dn  {rows: [[c, hash, births]...]}   its hashes of finished chunks; every one of them when a link comes up

A frame names no other arcade: what a link carries is that arcade's own word (the link proves who it is), so nobody
can speak for another. Leases are a courtesy -- two helpers on one chunk cost a scan, never a wrong answer.
"""
from __future__ import annotations

import asyncio
import logging
import itertools
import threading
import time
from typing import Any

log = logging.getLogger("arcade.mesh")

ROWS_PER_FRAME = 500           # a "dn" frame stays well under the link's 64 KB frame


ASK_SECONDS = 15.0
ANSWERS_PER_MINUTE = 120       # questions one linked arcade may ask of this one


class _Chain:
    """One chain's index's way out: what it posts goes out with its chain named, and its questions to a walker."""

    def __init__(self, share: "DxShare", net: str):
        self.share, self.net = share, net

    def post(self, frame: dict) -> None:
        self.share.post({**frame, "net": self.net})

    def ask(self, question: dict) -> dict:
        return self.share.ask(self.net, question)


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
        self.indexes()

    def indexes(self) -> dict:
        got = dict(self._indexes() or {})
        for net, index in got.items():
            if not isinstance(getattr(index, "share", None), _Chain):
                index.share = _Chain(self, net)
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

    def ask(self, net: str, question: dict) -> dict:
        """From a web thread: ask the furthest-walked linked arcade for this chain. {"where"|"on", "from", "walked_to"}."""
        index = self.indexes().get(net)
        loop = self.loop
        if index is None or loop is None or self.node is None:
            raise ValueError("no linked arcade to ask")
        for node, _height in index.walkers():
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
