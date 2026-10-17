"""Whose swap listings are answerable right now (2026-10-17: "If they have to keep their
arcade page open to keep their swap active ... every 1.5 seconds ... make sure that it is not
listing listings that are not active").

A swap listed from a browser (wallet.listOrder) is answered by that browser: its open, unlocked
arcade page takes the proposals. Closed, the listing still stood on every arcade until it lapsed,
and whoever took it waited for an answer that never came ("aborted -- the other side walked away").

So the page says it is there (POST /account/orders/alive, every few seconds while it holds listed
swaps and its wallet is open), and this node keeps a mesh room for that lister while it does:
`arcade-orders/<address>`. The mesh tells every node which rooms are live (its SUBs), so any
arcade can ask `room_live` and show a browser's listing only while its lister's page is open.
The node leaves the room ALIVE_FOR seconds after the last word from the page, checked on the
pool-watch thread every 1.5 s (watcher.POOL_SECONDS).
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

#: How long a lister counts as present after its page last said so (the page says it every 3 s).
ALIVE_FOR = 12.0
MEMBER = "lister"


def room(address: str) -> str:
    return f"arcade-orders/{address}"


class Presence:
    def __init__(self, mesh: Callable[[], Any], changed: Callable[[], None]):
        self._mesh, self._changed = mesh, changed
        self._held: dict[str, list] = {}          # address -> [mesh session token, last heard]
        self._lock = threading.Lock()

    def alive(self, addresses: list[str]) -> None:
        """The lister's page is open: join (or keep) its room on the mesh."""
        svc = self._mesh()
        if svc is None:
            return
        now, joined = time.time(), False
        for address in {a for a in addresses if a}:
            with self._lock:
                held = self._held.get(address)
            if held is None:
                session = svc.join(room(address), MEMBER, None)
                with self._lock:
                    self._held[address] = [session.token, now]
                joined = True
            else:
                held[1] = now
                svc.touch(held[0])
        if joined:
            self._changed()

    def expire(self, now: float | None = None) -> None:
        """Leave the rooms of listers whose page has gone quiet."""
        now = now or time.time()
        with self._lock:
            gone = [(a, h) for a, h in self._held.items() if now - h[1] > ALIVE_FOR]
            for a, _ in gone:
                del self._held[a]
        svc = self._mesh() if gone else None
        for _a, held in gone:
            try:
                svc.leave(held[0])
            except Exception:                     # noqa: BLE001 -- gone is gone
                pass
        if gone:
            self._changed()

    def live(self, address: str) -> bool:
        """Is this lister's page open anywhere on the mesh (here, or on the node it uses)?"""
        with self._lock:
            if address in self._held:
                return True
        svc = self._mesh()
        try:
            return bool(svc is not None and svc.room_live(room(address)))
        except Exception:                         # noqa: BLE001
            return False
