"""Who runs the arcades this one is linked to (2026-10-20: "In the admin panel, I wanna see a list of other
operators").

On the wire (frames of kind "op" on an ordinary link, never flooded): each arcade says, when a link comes up and once
a minute after,

    me  {tag, name, version, hosts}   its operator's @name (or its node's own, for an arcade whose operator is its node,
                                      as on .25), the arcade's name, its version, and the names it is published under

Its own word about itself only: the link proves which arcade is speaking.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

log = logging.getLogger("arcade.mesh")


class OperatorsShare:
    kinds = ("op",)

    def __init__(self, about: Callable[[], dict]):
        self.about = about                       # this arcade's own {tag, name, version, hosts}
        self.node = None
        self.peers: dict[str, dict] = {}         # peer id -> what it said, and when

    async def link_up(self, link) -> None:
        await self._say(link)

    async def link_down(self, peer_id: str) -> None:
        if peer_id in self.peers:
            self.peers[peer_id]["gone"] = time.time()

    async def rebalance(self) -> None:
        for link in list((self.node.links if self.node else {}).values()):
            await self._say(link)

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        if header.get("op") != "me":
            return
        hosts = header.get("hosts") if isinstance(header.get("hosts"), list) else []
        self.peers[link.peer_id] = {
            "tag": str(header.get("tag") or "")[:24], "name": str(header.get("name") or "")[:60],
            "version": str(header.get("version") or "")[:40], "hosts": [str(h)[:100] for h in hosts[:4]],
            "at": time.time()}

    async def _say(self, link) -> None:
        try:
            said = await asyncio.to_thread(self.about)
            await link.send({"t": "op", "op": "me", **said})
        except Exception:                                  # noqa: BLE001 -- said again next minute
            log.debug("operators: could not say who runs this arcade", exc_info=True)

    def status(self) -> list[dict]:
        """Every arcade heard from, linked now or not, newest first."""
        linked = set(self.node.links) if self.node else set()
        rows = [{"node": peer[:12], "linked": peer in linked, **{k: v for k, v in said.items() if k != "gone"}}
                for peer, said in self.peers.items()]
        return sorted(rows, key=lambda r: (not r["linked"], -r["at"]))
