"""Relay hops: a link to an arcade no direct link can reach, carried by the arcades in between (2026-11-02:
"do you think relay hops would be a good idea like they do with Meshtastic?").

Two arcades behind routers that each pick a new port for every destination cannot link directly (introduce.py), and an
arcade can need something only such an arcade holds (coverage.py). Then the frames between them go hop by hop along the
path the mesh already knows to each of them -- the first way the other's SUB came (node._via) -- not flooded as a radio
mesh floods: one copy, one path, at most RELAY_HOPS hops.

On the wire, one kind of frame on ordinary links:

    rl  {o, to, q, bh, in, hops, sig} + blob      from `o` to `to`; `in` is the frame being carried, `bh` the sha256 of
                                                  its blob; signed by `o` (all but hops), so no arcade on the way can
                                                  forge or change one; `q` is fresh and seen once, as a SUB's is

`in` is "rl-open" / "rl-yes" / "rl-shut" between the two ends, or a frame of a kind in CARRIED: Core sharing, the ord
index's sharing, index copies and index checks -- reads of public chain data. Calls never go this way (they have media
relays), and nothing else rides a relayed link. The arcades on the way read what they carry, as the end arcades do; it
is the same public data.

Every relaying arcade limits what it carries: RELAY_RATE bytes a second for any one sender, RELAY_TOTAL for all.
A relayed link is shut after RELAY_IDLE seconds with nothing on it, or as soon as the two arcades link directly.

To the extensions a relayed link is a link (RelayLink: peer_id, send, close): node.reach(peer) gives the direct link
or this one; node.reachable() is both kinds' peers.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

log = logging.getLogger("arcade.mesh")

RELAY_HOPS = 3
RELAY_RATE = 2_000_000
RELAY_TOTAL = 8_000_000
RELAY_IDLE = 300.0
OPEN_WAIT = 20.0
#: The frame kinds a relayed link carries: coreshare "cr", the ord index "dx", index copies "lx", index checks "ix".
CARRIED = ("cr", "dx", "lx", "ix")


class _Bytes:
    def __init__(self, rate: float):
        self.rate, self.level, self.at = rate, rate * 4, time.monotonic()

    def take(self, n: int) -> bool:
        now = time.monotonic()
        self.level = min(self.rate * 4, self.level + (now - self.at) * self.rate)
        self.at = now
        if self.level < n:
            return False
        self.level -= n
        return True


class RelayLink:
    """A link to `peer_id` carried by other arcades. Walks like a Link where the extensions use one."""

    outbound = False
    relayed = True
    peer_listen = None

    def __init__(self, relays: "Relays", peer_id: str):
        self.relays, self.peer_id = relays, peer_id
        self.rtt = None
        self.used = time.monotonic()

    async def send(self, header: dict, blob: bytes = b"") -> None:
        await self.relays.send(self.peer_id, header, blob)

    async def close(self) -> None:
        await self.relays.shut(self.peer_id)


class Relays:
    def __init__(self, node):
        self.node = node
        self.links: dict[str, RelayLink] = {}
        self.opening: dict[str, asyncio.Future] = {}
        self.carried = 0                                   # bytes this arcade carried for others
        self._by_origin: dict[str, _Bytes] = {}
        self._all = _Bytes(RELAY_TOTAL)

    # ---------------------------------------------------------------- the two ends
    def _riders(self):
        return [e for e in self.node.extensions if set(getattr(e, "kinds", ())) & set(CARRIED)]

    async def open(self, peer: str, wait: float = OPEN_WAIT) -> RelayLink | None:
        """A relayed link to `peer` (None when no way there answers in `wait` seconds)."""
        node = self.node
        if peer in self.links:
            return self.links[peer]
        if peer == node.node_id or peer in node.links or self._next(peer) is None:
            return None
        fut = self.opening.get(peer)
        if fut is None or fut.done():
            fut = self.opening[peer] = asyncio.get_running_loop().create_future()
            await self.send(peer, {"t": "rl-open"})
        try:
            await asyncio.wait_for(asyncio.shield(fut), wait)
        except asyncio.TimeoutError:
            return None
        finally:
            if fut.done():
                self.opening.pop(peer, None)
        return self.links.get(peer)

    async def _up(self, peer: str) -> None:
        if peer in self.links or peer in self.node.links:
            return
        link = self.links[peer] = RelayLink(self, peer)
        log.info("mesh: relayed link to %s", peer[:12])
        for ext in self._riders():
            try:
                await ext.link_up(link)
            except Exception:                              # noqa: BLE001
                log.debug("relay link_up", exc_info=True)

    async def shut(self, peer: str, tell: bool = True) -> None:
        link = self.links.pop(peer, None)
        if link is None:
            return
        if tell:
            try:
                await self.send(peer, {"t": "rl-shut"})
            except Exception:                              # noqa: BLE001
                pass
        for ext in self._riders():
            try:
                await ext.link_down(peer)
            except Exception:                              # noqa: BLE001
                pass

    async def tidy(self) -> None:
        """Shut relayed links idle for RELAY_IDLE, and ones the two arcades now link directly instead."""
        now = time.monotonic()
        for peer, link in list(self.links.items()):
            if peer in self.node.links or now - link.used > RELAY_IDLE:
                await self.shut(peer, tell=peer not in self.node.links)

    # ---------------------------------------------------------------- the wire
    def _next(self, to: str):
        node = self.node
        return node.links.get(to) or node.links.get(node._via.get(to, ""))

    async def send(self, to: str, inner: dict, blob: bytes = b"") -> None:
        node = self.node
        if to in node.links:                               # linked directly meanwhile: no need to carry it
            await node.links[to].send(inner, blob)
            return
        nxt = self._next(to)
        if nxt is None:
            raise ConnectionError(f"no way to {to[:12]}")
        header = node._sign({"t": "rl", "o": node.node_id, "to": to, "q": node._next_seq(),
                             "bh": hashlib.sha256(blob).hexdigest()[:32], "in": inner, "hops": 0})
        node._seen[f"{node.node_id}:{header['q']}"] = None
        if to in self.links:
            self.links[to].used = time.monotonic()
        await nxt.send(header, blob)

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        node = self.node
        origin, to, hops, inner = header.get("o"), header.get("to"), header.get("hops"), header.get("in")
        if not (isinstance(origin, str) and isinstance(to, str) and isinstance(hops, int) and isinstance(inner, dict)):
            return
        if origin == node.node_id or hops >= RELAY_HOPS or not node._fresh(header):
            return
        if header.get("bh") != hashlib.sha256(blob).hexdigest()[:32] or not node._verify(header):
            return
        node._mark(header)
        if to != node.node_id:
            await self._carry(link, header, blob)
            return
        kind = inner.get("t")
        if kind == "rl-open":
            # The yes first: the extensions' first frames (an offer) must not overtake it, or the opener has no
            # relayed link yet to take them on.
            await self.send(origin, {"t": "rl-yes"})
            await self._up(origin)
            return
        if kind == "rl-yes":
            await self._up(origin)
            fut = self.opening.get(origin)
            if fut is not None and not fut.done():
                fut.set_result(True)
            return
        if kind == "rl-shut":
            await self.shut(origin, tell=False)
            return
        relayed = self.links.get(origin)
        if relayed is None or kind not in CARRIED:
            return
        relayed.used = time.monotonic()
        for ext in self._riders():
            if kind in getattr(ext, "kinds", ()):
                await ext.on_frame(relayed, inner, blob)

    async def _carry(self, came, header: dict, blob: bytes) -> None:
        """Pass a frame on towards its arcade, within this arcade's limits."""
        size = len(blob) + 512
        bucket = self._by_origin.setdefault(header["o"], _Bytes(RELAY_RATE))
        if not bucket.take(size) or not self._all.take(size):
            return
        nxt = self._next(header["to"])
        if nxt is None or nxt is came:
            return
        self.carried += size
        try:
            await nxt.send(dict(header, hops=header["hops"] + 1), blob)
        except Exception:                                  # noqa: BLE001 -- the sender asks again
            pass


def reach(node, peer: str):
    """The link to `peer`, direct or relayed (a node without relays, as in a test: direct only)."""
    if node is None:
        return None
    return node.reach(peer) if hasattr(node, "reach") else node.links.get(peer)


def reachable(node) -> set:
    """Every peer `node` has a link to, direct or relayed."""
    if node is None:
        return set()
    return node.reachable() if hasattr(node, "reachable") else set(node.links)


def every_link(node) -> list:
    return [l for l in (reach(node, p) for p in reachable(node)) if l is not None]
