"""Swap messages over the mesh -- the game server -- instead of the chain (2026-10-17:
"Maybe the swap messages should travel through the game server rather than the blockchain?").

A lock-script swap is a conversation between two pages: propose, accept, locked, claimed. Each
step was a sealed program message carried by a transaction: a fee every time, and seconds to
tens of seconds before the other side heard it. The same sealed bytes now go through the mesh
when the other page is reachable there, in well under a second and for nothing; when it is not,
the chain carries them exactly as before. The swap's own safety is unchanged -- the messages are
sealed to the other page's key either way, and the locks, not the messages, hold the coins.

Two rooms per page key (its messaging key, hex):

    arcade-inbox/<key>   held by the page's node while the page is open (its heartbeat): the
                         mesh says it is live, so a sender knows the mesh will reach it
    arcade-talk/<key>    where it is spoken to: the page's node listens there, and a sender's
                         node joins (as a guest) to speak -- a node only hears a member it knows

A room message is at most mesh.node.MAX_PAYLOAD bytes, so a sealed message goes in pieces,
put back together by the listening node and kept for the page to read (`read`).
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from .mesh.node import MAX_PAYLOAD

ALIVE_FOR = 12.0          # seconds after the page's last heartbeat that its rooms are let go
KEEP_FOR = 15 * 60        # how long a heard message waits to be read
SPEAK_FOR = 120.0         # how long a sender's node stays in a talk room after speaking
FRAME = b"AT1"            # magic | message id (8) | piece (1) | pieces (1) | bytes
HEAD = len(FRAME) + 10
PIECE = MAX_PAYLOAD - HEAD


def inbox(key: str) -> str:
    return f"arcade-inbox/{key}"


def talk(key: str) -> str:
    return f"arcade-talk/{key}"


def pieces(payload: bytes, msg_id: bytes | None = None) -> list[bytes]:
    msg_id = msg_id or os.urandom(8)
    chunks = [payload[i:i + PIECE] for i in range(0, len(payload), PIECE)] or [b""]
    if len(chunks) > 255:
        raise ValueError("too big to carry over the mesh")
    return [FRAME + msg_id + bytes([n, len(chunks)]) + c for n, c in enumerate(chunks)]


class TalkRelay:
    def __init__(self, mesh: Callable[[], Any]):
        self._mesh = mesh
        self._lock = threading.Lock()
        self._held: dict[str, dict] = {}                    # key -> {inbox, talk tokens, last}
        self._speaking: dict[str, list] = {}                # key -> [token, last]
        self._parts: dict[bytes, dict] = {}                 # message id -> {n: bytes, total, at}
        self._heard: dict[str, OrderedDict] = {}            # key -> seq -> (payload, at)
        self._seq = 0

    # --- the receiving page's side ------------------------------------------------------

    def hold(self, key: str) -> None:
        """The page with this messaging key is open here: keep its two rooms."""
        svc = self._mesh()
        if svc is None or not key:
            return
        now = time.time()
        with self._lock:
            held = self._held.get(key)
        if held is None:
            a = svc.join(inbox(key), svc.new_guest(), None)
            b = svc.join(talk(key), svc.new_guest(), None)
            with self._lock:
                self._held[key] = {"inbox": a.token, "talk": b.token, "last": now}
        else:
            held["last"] = now
            svc.touch(held["inbox"])
            svc.touch(held["talk"])

    def live(self, key: str) -> bool:
        with self._lock:
            if key in self._held:
                return True
        svc = self._mesh()
        try:
            return bool(svc is not None and svc.room_live(inbox(key)))
        except Exception:                                   # noqa: BLE001
            return False

    def drain(self) -> None:
        """Put what the talk rooms heard back together, for their pages to read."""
        svc = self._mesh()
        if svc is None:
            return
        with self._lock:
            held = list(self._held.items())
        now = time.time()
        for key, h in held:
            try:
                events = svc.take(h["talk"])
            except Exception:                               # noqa: BLE001 -- left the room meanwhile
                continue
            for ev in events:
                if getattr(ev, "type", "") == "message":
                    self._piece(key, bytes(ev.data), now)
        with self._lock:
            for mid in [m for m, p in self._parts.items() if now - p["at"] > 60]:
                del self._parts[mid]
            for key, box in self._heard.items():
                while box and now - next(iter(box.values()))[1] > KEEP_FOR:
                    box.popitem(last=False)

    def _piece(self, key: str, frame: bytes, now: float) -> None:
        if len(frame) < HEAD or not frame.startswith(FRAME):
            return
        mid, n, total = frame[3:11], frame[11], frame[12]
        if not 0 < total or n >= total:
            return
        with self._lock:
            got = self._parts.setdefault(mid, {"bits": {}, "total": total, "at": now})
            got["bits"][n] = frame[HEAD:]
            if len(got["bits"]) < got["total"]:
                return
            payload = b"".join(got["bits"][i] for i in range(got["total"]))
            del self._parts[mid]
            self._seq += 1
            box = self._heard.setdefault(key, OrderedDict())
            box[self._seq] = (payload, now)
            while len(box) > 500:
                box.popitem(last=False)

    def read(self, key: str, after: int = 0) -> tuple[list[tuple[int, str]], int]:
        """Messages heard for `key` after `after`: [(seq, hex)], and the cursor to ask from next."""
        self.drain()
        with self._lock:
            box = self._heard.get(key, OrderedDict())
            out = [(seq, p.hex()) for seq, (p, _at) in box.items() if seq > after]
            return out, max([after] + [s for s, _ in out])

    # --- the sending page's side --------------------------------------------------------

    def send(self, to_key: str, payload: bytes) -> bool:
        """Speak to the page with `to_key` over the mesh, if it is reachable there; False means
        use the chain instead."""
        svc = self._mesh()
        if svc is None or not to_key or not self.live(to_key):
            return False
        frames = pieces(payload)
        now = time.time()
        with self._lock:
            speaking = self._speaking.get(to_key)
            if speaking is not None and now - speaking[1] > SPEAK_FOR:
                speaking = None
        if speaking is None:
            session = svc.join(talk(to_key), svc.new_guest(), None)
            speaking = [session.token, now]
            # A node hears only a member it knows: give the join a moment to arrive first.
            time.sleep(0.3)
        speaking[1] = now
        with self._lock:
            self._speaking[to_key] = speaking
        try:
            for f in frames:
                svc.send(speaking[0], f)
        except Exception:                                   # noqa: BLE001 -- let the chain carry it
            with self._lock:
                self._speaking.pop(to_key, None)
            return False
        return True

    # --- housekeeping (the pool-watch thread, every 1.5 s) ------------------------------

    def expire(self, now: float | None = None) -> None:
        now = now or time.time()
        svc = self._mesh()
        with self._lock:
            gone = [(k, h) for k, h in self._held.items() if now - h["last"] > ALIVE_FOR]
            for k, _ in gone:
                del self._held[k]
            quiet = [(k, s) for k, s in self._speaking.items() if now - s[1] > SPEAK_FOR]
            for k, _ in quiet:
                del self._speaking[k]
        for _k, h in gone:
            for tok in (h["inbox"], h["talk"]):
                try:
                    svc.leave(tok)
                except Exception:                           # noqa: BLE001
                    pass
        for _k, s in quiet:
            try:
                svc.leave(s[0])
            except Exception:                               # noqa: BLE001
                pass
