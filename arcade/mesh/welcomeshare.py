"""Welcome records over the whole mesh (arcade/welcome.py, 2026-10-27: a new operator's ask for 1 PEP test reaches
"every other node operator on the arcade", not only the arcades it links to).

On the wire (frames of kind "wl"): {"t": "wl", "recs": [record, ...]}, at most BATCH records a frame. Each record is
signed by the arcade that wrote it, so it is passed on as it came and checked by everyone who keeps it:
- when a link comes up, each side sends every record it keeps (an arcade that joins later still hears an open ask);
- a record new to this arcade is kept and sent on to every other link; one it knows already goes no further, so a
  record crosses the mesh once.
A link may bring at most ASKS_PER_HOUR new asks an hour (one key per ask costs nothing to make); the rest are dropped.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from .. import welcome as welcomelib

log = logging.getLogger("arcade.mesh")

BATCH = 40
MAX_IN_FRAME = 100
ASKS_PER_HOUR = 30


class WelcomeShare:
    kinds = ("wl",)

    def __init__(self, book: welcomelib.Book, tick: Callable[[], list] | None = None):
        self.book = book
        self.tick = tick                              # off thread, every minute: this arcade's own new records
        self.node = None
        self._asks: dict[str, list[float]] = {}       # peer id -> when it brought new asks (the last hour)

    async def link_up(self, link) -> None:
        await self._send(link, self.book.all())

    async def link_down(self, peer_id: str) -> None:
        self._asks.pop(peer_id, None)

    async def rebalance(self) -> None:
        if self.tick is None:
            return
        try:
            mine = await asyncio.to_thread(self.tick)
        except Exception:                              # noqa: BLE001 -- tried again next minute
            log.debug("welcome: this arcade's own check failed", exc_info=True)
            return
        await self.spread(mine or [])

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        recs = header.get("recs")
        if not isinstance(recs, list):
            return
        now = time.time()
        recent = [t for t in self._asks.get(link.peer_id, []) if t > now - 3600]
        fresh = []
        for rec in recs[:MAX_IN_FRAME]:
            if isinstance(rec, dict) and rec.get("k") == "ask" and \
                    self.book.ask_of(str(rec.get("a"))) is None and len(recent) >= ASKS_PER_HOUR:
                continue
            kept = await asyncio.to_thread(self.book.add, rec)
            if kept is not None:
                if kept["k"] == "ask":
                    recent.append(now)
                fresh.append(kept)
        self._asks[link.peer_id] = recent
        if fresh:
            await self.spread(fresh, skip=link.peer_id)

    async def spread(self, records: list, skip: str | None = None) -> None:
        """Send `records` (already kept here) to every link but `skip`."""
        if not records or self.node is None:
            return
        for peer, link in list(self.node.links.items()):
            if peer != skip:
                await self._send(link, records)

    async def _send(self, link, records: list) -> None:
        for i in range(0, len(records), BATCH):
            try:
                await link.send({"t": "wl", "recs": records[i:i + BATCH]})
            except Exception:                          # noqa: BLE001 -- that link is going; the next one hears it
                log.debug("welcome: could not send to %s", getattr(link, "peer_id", "?")[:12], exc_info=True)
                return
