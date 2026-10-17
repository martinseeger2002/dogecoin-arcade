"""Core to Core through the mesh: a node's Pepecoin Core peers with another arcade's Core over their mesh link.

2026-10-12 (the operator: "Can't we tunnel the core ports through the arcade?"). Two Core nodes
behind routers that cannot be opened (carrier NAT, Starlink) can never connect to each other:
Core only dials and listens the ordinary way. Their arcades can link -- directly, by an
IPv6 meeting, or through a tunnel -- so Core rides inside that link:

* For each linked arcade that offers the same chain, this node opens a listening port on
  127.0.0.1 and tells its own Core `addnode 127.0.0.1:<port> add`. Core dials it like any
  peer it was told to keep.
* What Core sends there crosses the mesh link as "cs" frames (a stream id, then the bytes)
  to the other arcade, which connects to ITS Core's own peer port on 127.0.0.1 and passes
  the bytes on. Both ways, until either side closes.
* Only to the Core peer port of a chain both sides name ("main", "test"): a peer can never
  ask this node to connect anywhere else.
* Bulk never starves a game: the bytes go in small chunks (CHUNK), each its own frame, so a
  room message waits at most one chunk; and each link carries at most RATE bytes a second
  of Core traffic. A fresh sync through a friend is slower; a game stays quick.
* Core counts an `addnode` peer as one it asked for: it keeps it and does not weigh it
  against its other peers by address, so many arcade peers all at 127.0.0.1 are fine.
  At most PER_CHAIN of them, the arcades with the quickest links first.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

log = logging.getLogger("arcade.mesh")

CHUNK = 4096            # bytes of Core traffic per mesh frame
RATE = 256 * 1024       # bytes per second of Core traffic on one link, each way
PER_CHAIN = 4           # arcade Core peers this node keeps per chain


class _Rate:
    def __init__(self, rate: float):
        self.rate, self.tokens, self.t = rate, rate, time.monotonic()

    async def take(self, n: int) -> None:
        while True:
            now = time.monotonic()
            self.tokens = min(self.rate, self.tokens + (now - self.t) * self.rate)
            self.t = now
            if self.tokens >= n:
                self.tokens -= n
                return
            await asyncio.sleep((n - self.tokens) / self.rate)


class CoreBridge:
    """Attached to a MeshNode (node.add_extension): Core peer ports, per link, per chain.

    `chains` maps a chain name to this machine's Core peer port, e.g. {"main": 33874}.
    `addnode(chain, "127.0.0.1:port", "add" | "remove")` asks that chain's Core (blocking; run
    in a thread)."""

    def __init__(self, chains: dict[str, int], addnode: Callable[[str, str, str], object] | None,
                 per_chain: int = PER_CHAIN, rate: float = RATE):
        self.chains = dict(chains)
        self.addnode = addnode
        self.per_chain, self.rate = per_chain, rate
        self.node = None
        self.offers: dict[str, set[str]] = {}            # peer id -> chains it offers
        self.listeners: dict[tuple[str, str], tuple[asyncio.base_events.Server, int]] = {}
        # (peer, stream id) -> {"w": writer to Core or None while connecting, "buf": bytes waiting, "task": pump}
        self.streams: dict[tuple[str, int], dict] = {}
        self.rates: dict[str, _Rate] = {}
        self._next = 1

    # ---------------------------------------------------------------- the node's hooks
    async def link_up(self, link) -> None:
        await link.send({"t": "cs", "op": "offer", "chains": sorted(self.chains)})

    async def link_down(self, peer_id: str) -> None:
        self.offers.pop(peer_id, None)
        for key in [k for k in self.streams if k[0] == peer_id]:
            await self._close(key, tell=False)
        for key in [k for k in self.listeners if k[0] == peer_id]:
            await self._unlisten(key)
        self.rates.pop(peer_id, None)

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        op = header.get("op")
        peer = link.peer_id
        if op == "offer":
            offered = header.get("chains")
            self.offers[peer] = {c for c in offered if isinstance(c, str)} if isinstance(offered, list) else set()
            await self.rebalance()
            return
        sid = header.get("s")
        if not isinstance(sid, int):
            return
        key = (peer, sid)
        if op == "open":
            chain = header.get("c")
            if chain not in self.chains or key in self.streams:
                await link.send({"t": "cs", "op": "close", "s": sid})
                return
            # Connect in a task of its own: this handler runs in the link's reading loop, and a
            # game's frames on the same link must not wait for Core to answer.
            self.streams[key] = {"w": None, "buf": [], "task": self.node._spawn(self._open(link, sid, chain))}
        elif op == "data":
            got = self.streams.get(key)
            if got is None:
                return
            if got["w"] is None:
                if sum(map(len, got["buf"])) + len(blob) > 1 << 20:
                    await self._close(key)                # a peer pouring into a stream that never opened
                else:
                    got["buf"].append(blob)
                return
            try:
                got["w"].write(blob)
                await got["w"].drain()
            except (ConnectionError, OSError):
                await self._close(key)
        elif op == "close":
            await self._close(key, tell=False)

    # ---------------------------------------------------------------- choosing peers
    async def rebalance(self) -> None:
        """Keep up to per_chain arcade peers per chain, quickest links first."""
        if self.node is None:
            return
        rtts = self.node.rtts()
        for chain in self.chains:
            have = [k for k in self.listeners if k[1] == chain]
            want = sorted((p for p, cs in self.offers.items() if chain in cs and p in self.node.links),
                          key=lambda p: rtts.get(p) if rtts.get(p) is not None else 9e9)[:self.per_chain]
            for key in have:
                if key[0] not in want:
                    await self._unlisten(key)
            for peer in want:
                if (peer, chain) not in self.listeners:
                    await self._listen(peer, chain)

    async def _open(self, link, sid: int, chain: str) -> None:
        key = (link.peer_id, sid)
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.chains[chain]), 5)
        except (OSError, asyncio.TimeoutError):
            await self._close(key)
            return
        got = self.streams.get(key)
        if got is None:                                    # closed meanwhile
            writer.close()
            return
        got["w"] = writer
        for blob in got.pop("buf"):
            writer.write(blob)
        got["buf"] = []
        await self._pump(link, sid, reader)

    async def _listen(self, peer: str, chain: str) -> None:
        async def core_dialled(reader, writer):
            link = self.node.links.get(peer)
            if link is None:
                writer.close()
                return
            sid, self._next = self._next, self._next + 1
            self.streams[(peer, sid)] = {"w": writer, "buf": [], "task": self.node._spawn(self._pump(link, sid, reader))}
            try:
                await link.send({"t": "cs", "op": "open", "s": sid, "c": chain})
            except Exception:                              # noqa: BLE001
                await self._close((peer, sid), tell=False)
        server = await asyncio.start_server(core_dialled, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        self.listeners[(peer, chain)] = (server, port)
        log.info("mesh: Core (%s) peers with %s through the mesh at 127.0.0.1:%d", chain, peer[:12], port)
        await self._ask(chain, port, "add")

    async def _unlisten(self, key: tuple[str, str]) -> None:
        server, port = self.listeners.pop(key)
        server.close()
        await self._ask(key[1], port, "remove")

    async def _ask(self, chain: str, port: int, op: str) -> None:
        if self.addnode is None:
            return
        try:
            await asyncio.to_thread(self.addnode, chain, f"127.0.0.1:{port}", op)
        except Exception as exc:                           # noqa: BLE001 -- Core may be down; the link still works
            log.debug("mesh: addnode %s %s: %s", op, port, exc)

    # ---------------------------------------------------------------- the bytes
    async def _pump(self, link, sid: int, reader: asyncio.StreamReader) -> None:
        rate = self.rates.setdefault(link.peer_id, _Rate(self.rate))
        try:
            while True:
                data = await reader.read(CHUNK)
                if not data:
                    break
                await rate.take(len(data))
                await link.send({"t": "cs", "op": "data", "s": sid}, data)
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        except Exception:                                  # noqa: BLE001
            log.exception("mesh core stream")
        finally:
            if (link.peer_id, sid) in self.streams:
                await self._close((link.peer_id, sid))

    async def _close(self, key: tuple[str, int], tell: bool = True) -> None:
        got = self.streams.pop(key, None)
        if got is None:
            return
        writer, task = got["w"], got["task"]
        try:
            if writer is not None:
                writer.close()
        except Exception:                                  # noqa: BLE001
            pass
        if task is not asyncio.current_task():
            task.cancel()
        if tell and self.node is not None:
            link = self.node.links.get(key[0])
            if link is not None:
                try:
                    await link.send({"t": "cs", "op": "close", "s": key[1]})
                except Exception:                          # noqa: BLE001
                    pass

    def status(self) -> dict:
        return {chain: sorted(p for (peer, c), (_, p) in self.listeners.items() if c == chain)
                for chain in self.chains}
