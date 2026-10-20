"""One swap order book across every arcade, over the mesh (2026-10-19: "They should share the same order
books. So if one user requests the order book, it should be the same orderbook as every other client and operator
sees. They should work across Dogecoin arcades." and "Books should be updated as instantaneously as possible. So no
two users can fill the same order.").

Since 2026-10-17 a browser's listed swap is filed on its own node only (/account/orders/post), not inscribed, so no
other arcade ever saw it; and "being taken" (trade_orders.set_taking) never left the maker's node. Now every node
sits in one mesh room, `arcade-book`, and says there what changes in the orders whose home it is -- the orders it
answers for: its accounts' tab orders, the delegated orders it holds, its own standing orders:

    {"r": [{id, lister, network, body, state}], "full": 0|1}       state: live | taking | gone

zlib-compressed, in talk_relay's pieces (a room message is at most MAX_PAYLOAD bytes). A change goes out on the next
pass of the pool-watch thread (every 1.5 s); every FULL_EVERY seconds the whole of its live book goes again with
"full", so an arcade that joined late, or missed a word, catches up, and drops what is no longer there.

An arcade speaks only for its own orders: what arrives from node X is filed only for orders whose `node` is X (the
mesh signs every message with its node's key), so nobody can cancel or "take" another arcade's order. The order's
coins are never at stake either way -- the locks are (trade_orders.py).
"""
from __future__ import annotations

import json
import threading
import time
import zlib
from typing import Any, Callable

from .talk_relay import FRAME, HEAD, pieces

ROOM = "arcade-book"
FULL_EVERY = 30.0
SEND_PER_PASS = 6             # frames a pass may send: under the mesh's RATE (5/s) at a 1.5 s pass, with its burst


class BookRelay:
    def __init__(self, mesh: Callable[[], Any], book: Any, changed: Callable[[], None]):
        self._mesh, self._book, self._changed = mesh, book, changed
        self._lock = threading.Lock()
        self._token: str | None = None
        self._dirty: set[str] = set()
        self._full_at = 0.0
        self._outbox: list[bytes] = []
        self._parts: dict[bytes, dict] = {}
        book.on_change = self.note

    def note(self, ids: list[str]) -> None:
        """Orders changed on this node's book: said on the next pass (only those whose home this node is)."""
        with self._lock:
            self._dirty.update(i for i in ids if i)

    # --- one pass, from the pool-watch thread (watcher.POOL_SECONDS) --------------------------------------

    def tick(self, now: float | None = None) -> None:
        svc = self._mesh()
        if svc is None:
            return
        now = now or time.time()
        if self._token is None:
            self._token = svc.join(ROOM, svc.new_guest(), None).token
            self._full_at = 0.0                         # a new member: say everything once it is known
        else:
            try:
                svc.touch(self._token)
            except Exception:                           # noqa: BLE001 -- the session went: join again next pass
                self._token = None
                return
        self._hear(svc)
        me = getattr(svc, "node_id", "")
        with self._lock:
            dirty, self._dirty = self._dirty, set()
        if dirty and me:
            self._say(self._book.homed(me, sorted(dirty)), full=False)
        if me and now - self._full_at >= FULL_EVERY:
            self._full_at = now
            self._say([r for r in self._book.homed(me) if r["state"] != "gone"], full=True)
        self._send(svc)

    def _say(self, rows: list[dict], full: bool) -> None:
        if not rows and not full:
            return
        blob = zlib.compress(json.dumps({"r": rows, "full": 1 if full else 0},
                                        separators=(",", ":")).encode(), 9)
        with self._lock:
            # A newer full statement makes an unsent older one pointless.
            self._outbox.extend(pieces(blob))

    def _send(self, svc: Any) -> None:
        with self._lock:
            out, self._outbox = self._outbox[:SEND_PER_PASS], self._outbox[SEND_PER_PASS:]
        for i, frame in enumerate(out):
            try:
                svc.send(self._token, frame)
            except Exception:                           # noqa: BLE001 -- too fast, or the session went: next pass
                with self._lock:
                    self._outbox[:0] = out[i:]
                return

    def _hear(self, svc: Any) -> None:
        try:
            events = svc.take(self._token)
        except Exception:                               # noqa: BLE001
            self._token = None
            return
        now = time.time()
        moved = 0
        for ev in events:
            if getattr(ev, "type", "") != "message":
                continue
            frame, origin = bytes(ev.data), str(getattr(ev, "node", ""))
            if len(frame) < HEAD or not frame.startswith(FRAME) or not origin:
                continue
            mid, n, total = frame[3:11], frame[11], frame[12]
            if not 0 < total or n >= total:
                continue
            got = self._parts.setdefault(mid, {"bits": {}, "total": total, "at": now, "origin": origin})
            if got["origin"] != origin:
                continue
            got["bits"][n] = frame[HEAD:]
            if len(got["bits"]) < got["total"]:
                continue
            del self._parts[mid]
            try:
                unzip = zlib.decompressobj()
                said = json.loads(unzip.decompress(b"".join(got["bits"][i] for i in range(total)), 1 << 21))
                moved += self._book.apply_from(origin, said.get("r") or [], full=bool(said.get("full")))
            except Exception:                           # noqa: BLE001 -- not a statement this version reads
                continue
        for mid in [m for m, p in self._parts.items() if now - p["at"] > 60]:
            del self._parts[mid]
        if moved:
            self._changed()                             # every page of this arcade redraws its book now
