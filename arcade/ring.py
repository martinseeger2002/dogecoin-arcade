"""Ringing a player whose app is closed (2026-10-23: "So that we can call at any time, even if the application
is not open through push notifications as long as we have push notifications enabled for the app just like the
Wallet access and camera and microphone access switches" ... "Keeping the game agnostic"; who may ring: "My contacts
only").

Any app asks the arcade to ring an address (arcade.realtime.ring). The ring wakes that person's phone (arcade/push.py:
an EMPTY push, the service worker then asks its own node what it was) only when ALL of these hold, checked by the
RECIPIENT'S node:

  * the person turned on Notifications on that app's card (per account, kept on their node: `ring:<pubkey>` apps);
  * the caller is in their address book -- the browser hands its node the addresses in the book (`allow`), since the
    book itself lives in the browser and the node never reads it;
  * the caller is who they say: the ring carries the caller's own certificate (cert_message, signed by their key for
    the node they ring through), checked against that node like a realtime join;
  * not too often: one ring a pair every RING_GAP seconds, RING_HOUR an hour.

**Over the mesh, the talk relay's way** (talk_relay.py): a node holds the room `arcade!ring/<address>` for each of its
accounts that has a phone to wake and an app switched on; a caller's node joins it as a guest, says the ring, and
stays SPEAK_FOR seconds. Only the node that hosts the person hears it. `!` is no part of a game's name
(web/realtime.GAME), so no page can join these rooms through arcade.realtime.

A ring says who (their @name, from the chain), which app, an optional line (RING_LINE characters), and a short `link`
the app gets back when its notification is tapped (arcade.realtime.ringed()). Nothing else.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

ROOM = "arcade!ring/"
SPEAK_FOR = 60.0
RING_GAP = 15             # seconds between two rings from the same caller to the same person
RING_HOUR = 30            # rings a pair an hour
SEND_MINUTE = 20          # rings one caller may send a minute, to anybody
RING_LINE = 80
RING_LINK = 200
KEEP = 600                # how long a ring is remembered for its notification's tap
ALLOW_MAX = 5000          # addresses in one person's book
APPS_MAX = 200


def room(address: str) -> str:
    return ROOM + address


def clean_line(text: Any, most: int) -> str:
    return " ".join(str(text or "").split())[:most]


class Rings:
    """This node's side of ringing: who it holds rooms for, what it heard, how often."""

    def __init__(self, mesh: Callable[[], Any]):
        self._mesh = mesh
        self._lock = threading.Lock()
        self._held: dict[str, str] = {}                      # address -> session token
        self._speaking: dict[str, list] = {}                 # address -> [token, last]
        self._pairs: dict[tuple[str, str], list[float]] = {}  # (caller, callee) -> times
        self._sent: dict[str, list[float]] = {}              # caller -> times
        self.heard: OrderedDict[str, dict] = OrderedDict()   # ring id -> the ring, for its tap

    # --- limits -------------------------------------------------------------------------------

    def _times(self, box: dict, key, within: float, now: float) -> list[float]:
        kept = [t for t in box.get(key, []) if now - t < within]
        box[key] = kept
        return kept

    def may_send(self, caller: str, now: float | None = None) -> bool:
        now = now or time.time()
        with self._lock:
            times = self._times(self._sent, caller, 60, now)
            if len(times) >= SEND_MINUTE:
                return False
            times.append(now)
            return True

    def may_ring(self, caller: str, callee: str, now: float | None = None) -> bool:
        now = now or time.time()
        with self._lock:
            times = self._times(self._pairs, (caller, callee), 3600, now)
            if len(times) >= RING_HOUR or (times and now - times[-1] < RING_GAP):
                return False
            times.append(now)
            return True

    # --- the recipient's node --------------------------------------------------------------------

    def hold(self, addresses: set[str]) -> None:
        """Be in the ring room of exactly these addresses (accounts here with a phone and an app switched on)."""
        svc = self._mesh()
        if svc is None or getattr(svc, "loop", None) is None:
            return
        with self._lock:
            held = dict(self._held)
        for address in set(held) - addresses:
            try:
                svc.leave(held[address])
            except Exception:                                # noqa: BLE001
                pass
            with self._lock:
                self._held.pop(address, None)
        for address in addresses:
            token = held.get(address)
            try:
                if token is None:
                    token = svc.join(room(address), svc.new_guest(), None).token
                    with self._lock:
                        self._held[address] = token
                else:
                    svc.touch(token)
            except Exception:                                # noqa: BLE001 -- left meanwhile: joined next pass
                with self._lock:
                    self._held.pop(address, None)

    def heard_now(self) -> list[tuple[str, dict, str]]:
        """(callee address, the ring as said, the node it came through) for every ring the held rooms heard."""
        svc = self._mesh()
        if svc is None:
            return []
        with self._lock:
            held = list(self._held.items())
        out = []
        for address, token in held:
            try:
                events = svc.take(token)
            except Exception:                                # noqa: BLE001
                with self._lock:
                    self._held.pop(address, None)
                continue
            for ev in events:
                if getattr(ev, "type", "") != "message":
                    continue
                try:
                    said = json.loads(bytes(ev.data).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(said, dict) and said.get("t") == "ring":
                    out.append((address, said, str(getattr(ev, "node", "") or "")))
        return out

    def keep(self, ring: dict) -> None:
        with self._lock:
            self.heard[ring["id"]] = ring
            now = time.time()
            while self.heard and now - next(iter(self.heard.values()))["at"] > KEEP:
                self.heard.popitem(last=False)

    def get(self, ring_id: str) -> dict | None:
        with self._lock:
            return self.heard.get(str(ring_id or ""))

    # --- the caller's node -----------------------------------------------------------------------

    def live(self, address: str) -> bool:
        svc = self._mesh()
        try:
            return bool(svc is not None and svc.room_live(room(address)))
        except Exception:                                    # noqa: BLE001
            return False

    def say(self, address: str, ring: dict) -> bool:
        """Say a ring in that person's room, joining it as a guest for SPEAK_FOR seconds."""
        svc = self._mesh()
        if svc is None or getattr(svc, "loop", None) is None:
            return False
        now = time.time()
        with self._lock:
            speaking = self._speaking.get(address)
        if speaking is None:
            speaking = [svc.join(room(address), svc.new_guest(), None).token, now]
            with self._lock:
                self._speaking[address] = speaking
            time.sleep(0.3)                                  # the room's other members hear of the newcomer first
        speaking[1] = now
        svc.send(speaking[0], json.dumps(ring, separators=(",", ":")).encode("utf-8"))
        return True

    def expire(self, now: float | None = None) -> None:
        now = now or time.time()
        svc = self._mesh()
        with self._lock:
            gone = [(a, s) for a, s in self._speaking.items() if now - s[1] > SPEAK_FOR]
            for a, _ in gone:
                del self._speaking[a]
        for _, s in gone:
            try:
                svc.leave(s[0])
            except Exception:                                # noqa: BLE001
                pass


def new_id() -> str:
    return secrets.token_hex(8)
