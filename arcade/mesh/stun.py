"""A STUN server on every node, for the WebRTC fast lane (2026-10-18, docs/hosting-plan.md section 6).

Before two browsers can talk directly, each has to learn the address the internet sees it at; a browser hides
its own addresses (mDNS names), so without a STUN answer two players cannot connect even over IPv6. The operator
2026-10-18: "Nodes, then public" -- browsers ask STUN on arcade nodes first, and a public one only when none is
known (realtime.py `/realtime/ice`, the operator can turn the public one off).

Only a Binding answer (RFC 5389): the request's transaction id back with XOR-MAPPED-ADDRESS, nothing kept, no
authentication -- it tells a sender its own address and nothing else, so it cannot be used to reach anybody.
It is not a relay; a relay (TURN) is paid for by a game's builder (the plan, section 6).

A node's STUN is offered to browsers only after another node has reached it: `StunShare` asks each linked node to
send it a Binding request ("st" frames), and only the addresses that answered are offered on the mesh. A node
behind carrier NAT (Starlink, phone carriers) usually cannot be reached on IPv4 and is simply never offered there.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import struct
import time

log = logging.getLogger("arcade.mesh.stun")

PORT = 3478
MAGIC = 0x2112A442
BINDING_REQUEST = 0x0001
BINDING_SUCCESS = 0x0101
XOR_MAPPED_ADDRESS = 0x0020
SOFTWARE = 0x8022
PROBE_TIMEOUT = 2.0
RECHECK = 6 * 3600.0          # an address proved reachable is asked about again after this long
MAX_OFFERED = 4               # STUN addresses one node may offer


def binding_request(tid: bytes | None = None) -> tuple[bytes, bytes]:
    tid = tid or os.urandom(12)
    return struct.pack("!HHI", BINDING_REQUEST, 0, MAGIC) + tid, tid


def _xor_address(host: str, port: int, tid: bytes) -> bytes:
    ip = ipaddress.ip_address(host.split("%", 1)[0])
    xport = port ^ (MAGIC >> 16)
    if ip.version == 4:
        xip = int(ip) ^ MAGIC
        return struct.pack("!BBHI", 0, 1, xport, xip)
    key = int.from_bytes(struct.pack("!I", MAGIC) + tid, "big")
    return struct.pack("!BBH", 0, 2, xport) + (int(ip) ^ key).to_bytes(16, "big")


def answer(packet: bytes, addr: tuple) -> bytes | None:
    """The Binding success response to `packet` from `addr`, or None for anything that is not a Binding request."""
    if len(packet) < 20:
        return None
    kind, length, magic = struct.unpack("!HHI", packet[:8])
    if kind != BINDING_REQUEST or magic != MAGIC or kind & 0xC000 or len(packet) != 20 + length:
        return None
    tid = packet[8:20]
    value = _xor_address(addr[0], int(addr[1]), tid)
    software = b"arcade"
    attrs = struct.pack("!HH", XOR_MAPPED_ADDRESS, len(value)) + value
    attrs += struct.pack("!HH", SOFTWARE, len(software)) + software + b"\x00" * (-len(software) % 4)
    return struct.pack("!HHI", BINDING_SUCCESS, len(attrs), MAGIC) + tid + attrs


def mapped_address(packet: bytes, tid: bytes) -> tuple[str, int] | None:
    """The XOR-MAPPED-ADDRESS in a Binding success response for transaction `tid`."""
    if len(packet) < 20:
        return None
    kind, length, magic = struct.unpack("!HHI", packet[:8])
    if kind != BINDING_SUCCESS or magic != MAGIC or packet[8:20] != tid:
        return None
    at = 20
    while at + 4 <= min(len(packet), 20 + length):
        atype, alen = struct.unpack("!HH", packet[at:at + 4])
        value = packet[at + 4:at + 4 + alen]
        if atype == XOR_MAPPED_ADDRESS and len(value) >= 8:
            family, xport = value[1], struct.unpack("!H", value[2:4])[0]
            port = xport ^ (MAGIC >> 16)
            if family == 1:
                return str(ipaddress.IPv4Address(struct.unpack("!I", value[4:8])[0] ^ MAGIC)), port
            if family == 2 and len(value) >= 20:
                key = int.from_bytes(struct.pack("!I", MAGIC) + tid, "big")
                return str(ipaddress.IPv6Address(int.from_bytes(value[4:20], "big") ^ key)), port
        at += 4 + alen + (-alen % 4)
    return None


class _Server(asyncio.DatagramProtocol):
    def __init__(self, counter: dict):
        self.counter = counter

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        reply = answer(data, addr)
        if reply is not None:
            self.counter["answered"] = self.counter.get("answered", 0) + 1
            self.transport.sendto(reply, addr)


async def serve(host: str = "::", port: int = PORT, counter: dict | None = None):
    """Start answering on `host`:`port` (UDP); IPv4 too when the system maps it onto an IPv6 socket.
    Returns the transports (close them to stop)."""
    loop = asyncio.get_running_loop()
    counter = counter if counter is not None else {}
    out = []
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host if ":" in host else "::", port))
        t, _ = await loop.create_datagram_endpoint(lambda: _Server(counter), sock=sock)
        out.append(t)
    except OSError:
        t, _ = await loop.create_datagram_endpoint(lambda: _Server(counter), local_addr=("0.0.0.0", port))
        out.append(t)
    return out


async def probe(host: str, port: int = PORT, timeout: float = PROBE_TIMEOUT) -> tuple[str, int] | None:
    """Ask the STUN server at host:port for our own address; None if it does not answer in time."""
    loop = asyncio.get_running_loop()
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    got: asyncio.Future = loop.create_future()
    packet, tid = binding_request()

    class _Client(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            found = mapped_address(data, tid)
            if found and not got.done():
                got.set_result(found)

        def error_received(self, exc):
            if not got.done():
                got.set_result(None)

    try:
        transport, _ = await loop.create_datagram_endpoint(_Client, family=family, remote_addr=(str(ip), port))
    except OSError:
        return None
    try:
        for _ in range(3):                         # UDP: say it again rather than wait on one lost packet
            transport.sendto(packet)
            try:
                return await asyncio.wait_for(asyncio.shield(got), timeout / 3)
            except asyncio.TimeoutError:
                continue
        return None
    finally:
        transport.close()


def _public(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_global


class StunShare:
    """A mesh extension (node.add_extension): proves this node's STUN with its linked nodes and learns theirs.

    Frames "st": {"op": "probe", "n", "a": [[host, port]...]} asks the other node to reach those addresses;
    {"op": "probed", "n", "ok": [[host, port]...]} says which answered; {"op": "have", "s": [[host, port]...]}
    offers a node's proved STUN addresses. `hosts()` names this node's own public addresses to try (its announced
    IPv4, its IPv6); None when this node runs no STUN."""

    kinds = ("st",)

    def __init__(self, hosts, port: int = PORT, allow_private: bool = False):
        self.hosts, self.port, self.allow_private = hosts, port, allow_private
        self.node = None
        self.proved: dict[tuple[str, int], float] = {}     # own address -> when another node reached it
        self.offers: dict[str, list[tuple[str, int]]] = {}  # peer -> its proved addresses
        self._asked: dict[str, float] = {}                  # probe nonce -> when
        self._probing = 0

    def _ok(self, host: str) -> bool:
        return self.allow_private or _public(host)

    def mine(self) -> list[tuple[str, int]]:
        now = time.time()
        return [a for a, at in self.proved.items() if now - at < 2 * RECHECK][:MAX_OFFERED]

    def known(self) -> list[tuple[str, int]]:
        """Every STUN address offered on the mesh that this node knows, its own first."""
        out, seen = [], set()
        for a in self.mine() + [a for offers in self.offers.values() for a in offers]:
            if a not in seen:
                seen.add(a)
                out.append(a)
        return out

    async def link_up(self, link) -> None:
        await self._ask(link)
        if self.mine():
            await link.send({"t": "st", "op": "have", "s": [list(a) for a in self.mine()]})

    async def link_down(self, peer_id: str) -> None:
        self.offers.pop(peer_id, None)

    async def rebalance(self) -> None:
        """Every minute: an address learned since the links came up (an announcement, a new IPv6) is asked about
        too, one linked node at a time."""
        if self.node is None or not self.node.links:
            return
        links = sorted(self.node.links.values(), key=lambda l: l.peer_id)
        self._turn = (getattr(self, "_turn", -1) + 1) % len(links)
        await self._ask(links[self._turn])

    async def _ask(self, link) -> None:
        if self.hosts is None:
            return
        try:
            hosts = [h for h in (self.hosts() or []) if h and self._ok(h)]
        except Exception:                                   # noqa: BLE001 -- asked again on the next link
            return
        now = time.time()
        wanted = [(h, self.port) for h in dict.fromkeys(hosts) if now - self.proved.get((h, self.port), 0) > RECHECK]
        if not wanted:
            return
        nonce = os.urandom(6).hex()
        self._asked = {n: at for n, at in self._asked.items() if now - at < 60}
        self._asked[nonce] = now
        await link.send({"t": "st", "op": "probe", "n": nonce, "a": [list(a) for a in wanted[:MAX_OFFERED]]})

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        op = header.get("op")
        if op == "probe" and self._probing < 4:
            addrs = [(str(a[0])[:64], int(a[1])) for a in header.get("a", [])[:MAX_OFFERED]
                     if isinstance(a, list) and len(a) == 2 and isinstance(a[1], int) and 0 < a[1] < 65536]
            addrs = [a for a in addrs if self._ok(a[0])]
            self._probing += 1
            try:
                ok = [a for a, said in zip(addrs, await asyncio.gather(*(probe(h, p) for h, p in addrs))) if said]
            finally:
                self._probing -= 1
            await link.send({"t": "st", "op": "probed", "n": str(header.get("n", ""))[:16],
                             "ok": [list(a) for a in ok]})
        elif op == "probed" and self._asked.pop(str(header.get("n", "")), None):
            fresh = False
            for a in header.get("ok", [])[:MAX_OFFERED]:
                if isinstance(a, list) and len(a) == 2 and isinstance(a[1], int):
                    key = (str(a[0]), int(a[1]))
                    fresh = fresh or key not in self.proved
                    self.proved[key] = time.time()
            if fresh and self.node is not None:
                log.info("stun: reachable at %s", ", ".join(f"{h}:{p}" for h, p in self.mine()))
                for other in list(self.node.links.values()):
                    await other.send({"t": "st", "op": "have", "s": [list(a) for a in self.mine()]})
        elif op == "have":
            self.offers[link.peer_id] = [(str(a[0])[:64], int(a[1])) for a in header.get("s", [])[:MAX_OFFERED]
                                         if isinstance(a, list) and len(a) == 2 and isinstance(a[1], int)
                                         and 0 < a[1] < 65536 and self._ok(str(a[0]))]
