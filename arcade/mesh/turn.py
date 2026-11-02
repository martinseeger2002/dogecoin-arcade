"""The arcade's own media server (2026-10-31, the operator: "add a multimedia server for games, audio, and video using UDP").

Games, sound and pictures already share one road in the browser: the fast lane's WebRTC connection (`_page_doors`),
data channels for a game and media tracks for a call. WebRTC is UDP. When two browsers cannot reach each other it needs
a relay, and until now that meant renting Cloudflare's. This is that relay on the node itself: TURN (RFC 5766, UDP only,
no new dependency), on the port the node's STUN already answers on, so another node's proof that the port is open
(stun.py) counts for it too. Every app gets it without a line changed.

**It cannot read anything.** What it carries is DTLS and SRTP between two browsers; it moves ciphertext between two
addresses.

**Who may use it.** `/realtime/ice` gives a signed-in player of this arcade a login for a few hours: the username holds
its expiry, the password is an HMAC of the username under a secret only this node has, so nothing need be stored and a
stranger cannot make one.

**Behind NAT.** Each relay socket learns its outside address from arcade STUN, and both ends of a connection send to
each other at once (ICE's connectivity checks), so two relays behind home NAT punch through each other the way
punch.py showed two nodes do. A player away from a NATed node's network borrows a relay from an arcade that can be
reached (TurnShare). Nothing here needs Cloudflare.

**What it will not do.** It sends only to addresses a logged-in client named (permissions, as the RFC says), never to
this machine, and never into a private network unless the client is itself on one: a relay must not be a way in.
Allocations are limited per login and in all, sending is limited per allocation and in all, and a month's data limit
ends the service until the month turns.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import struct
import time

from . import punch as punchlib
from . import stun

log = logging.getLogger("arcade.mesh.turn")

REALM = "arcade"
LOGIN_TTL = 6 * 3600
NONCE_TTL = 600
DEFAULT_LIFETIME = 600
MAX_LIFETIME = 3600
PERMISSION_TTL = 300
CHANNEL_TTL = 600
MAX_ALLOCATIONS = 256
MAX_PER_LOGIN = 32            # a 20-seat call is 19 connections from one browser
ALLOCATION_RATE = 1_500_000   # bytes a second through one allocation: 12 Mbit/s, a good camera
TOTAL_RATE = 25_000_000       # bytes a second through the server: 200 Mbit/s
MONTH_LIMIT_GB = 100.0
SAVE_EVERY = 60.0

ALLOCATE, REFRESH, SEND, DATA, PERMISSION, BIND = 0x003, 0x004, 0x006, 0x007, 0x008, 0x009
A_CHANNEL, A_LIFETIME, A_PEER, A_DATA, A_RELAYED, A_TRANSPORT = 0x000C, 0x000D, 0x0012, 0x0013, 0x0016, 0x0019
A_USERNAME, A_INTEGRITY, A_ERROR, A_REALM, A_NONCE = 0x0006, 0x0008, 0x0009, 0x0014, 0x0015
UDP = 17


def login(secret: bytes, tag: str, ttl: int = LOGIN_TTL, now: float | None = None) -> tuple[str, str]:
    """(username, password) good for `ttl` seconds. `tag` tells logins apart for the per-login limit."""
    who = hashlib.blake2b(tag.encode(), digest_size=6).hexdigest()
    username = f"{int((now or time.time()) + ttl)}:{who}"
    return username, password_for(secret, username)


def password_for(secret: bytes, username: str) -> str:
    return base64.b64encode(hmac.new(secret, username.encode(), hashlib.sha256).digest()).decode()


def parse(packet: bytes) -> tuple[int, bytes, dict[int, bytes], int | None] | None:
    """(type, transaction, attributes, where MESSAGE-INTEGRITY starts) of a STUN-format message."""
    if len(packet) < 20:
        return None
    kind, length, magic = struct.unpack("!HHI", packet[:8])
    if magic != stun.MAGIC or kind & 0xC000 or len(packet) < 20 + length:
        return None
    attrs, at, integrity = {}, 20, None
    while at + 4 <= 20 + length:
        atype, alen = struct.unpack("!HH", packet[at:at + 4])
        if at + 4 + alen > 20 + length:
            return None
        attrs.setdefault(atype, packet[at + 4:at + 4 + alen])
        if atype == A_INTEGRITY and integrity is None:
            integrity = at
            break                                       # nothing after it is covered
        at += 4 + alen + (-alen % 4)
    return kind, packet[8:20], attrs, integrity


def build(kind: int, tid: bytes, attrs: list[tuple[int, bytes]], key: bytes | None = None) -> bytes:
    body = b"".join(struct.pack("!HH", t, len(v)) + v + b"\x00" * (-len(v) % 4) for t, v in attrs)
    if key is None:
        return struct.pack("!HHI", kind, len(body), stun.MAGIC) + tid + body
    head = struct.pack("!HHI", kind, len(body) + 24, stun.MAGIC) + tid
    mac = hmac.new(key, head + body, hashlib.sha1).digest()
    return head + body + struct.pack("!HH", A_INTEGRITY, 20) + mac


def peer_address(value: bytes) -> tuple[str, int] | None:
    """An XOR-PEER-ADDRESS; IPv4 only, as the relay's sockets are."""
    if len(value) != 8 or value[1] != 1:
        return None
    port = struct.unpack("!H", value[2:4])[0] ^ (stun.MAGIC >> 16)
    return str(ipaddress.IPv4Address(struct.unpack("!I", value[4:8])[0] ^ stun.MAGIC)), port


def _plain(host: str) -> ipaddress._BaseAddress:
    ip = ipaddress.ip_address(str(host).split("%", 1)[0])
    return ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped is not None else ip


def may_reach(peer: str, client: str, own: tuple = ()) -> bool:
    """May a client at `client` have packets sent to `peer`? Never to this machine; into a private network only for
    a client already on one."""
    try:
        ip, asker = _plain(peer), _plain(client)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_link_local or str(ip) in own:
        return False
    return ip.is_global or asker.is_private


class _Bucket:
    def __init__(self, rate: float):
        self.rate, self.level, self.at = rate, rate, time.monotonic()

    def take(self, n: int) -> bool:
        now = time.monotonic()
        self.level = min(self.rate, self.level + (now - self.at) * self.rate)
        self.at = now
        if self.level < n:
            return False
        self.level -= n
        return True


class _Relay(punchlib._Proto):
    """An allocation's socket: what peers send comes in here, on the way to the client."""

    def __init__(self, allocation: "_Allocation"):
        super().__init__()
        self.allocation = allocation

    def datagram_received(self, data, addr):
        if len(data) >= 20 and data[8:20] in self.stun_wait:
            super().datagram_received(data, addr)
            return
        self.allocation.from_peer(data, (addr[0], addr[1]))


class _Allocation:
    def __init__(self, server: "TurnServer", client: tuple, user: str, key: bytes, tid: bytes):
        self.server, self.client, self.user, self.key, self.tid = server, client, user, key, tid
        self.relay: _Relay | None = None
        self.address: tuple[str, int] | None = None
        self.expires = 0.0
        self.permissions: dict[str, float] = {}
        self.channels: dict[int, tuple[str, int]] = {}
        self.numbers: dict[tuple[str, int], tuple[int, float]] = {}
        self.bucket = _Bucket(server.allocation_rate)

    def from_peer(self, data: bytes, peer: tuple) -> None:
        if self.permissions.get(peer[0], 0) < time.monotonic() or not self.server.pass_through(self, len(data)):
            return
        number = self.numbers.get(peer)
        if number is not None and number[1] > time.monotonic():
            packet = struct.pack("!HH", number[0], len(data)) + data + b"\x00" * (-len(data) % 4)
        else:
            value = struct.pack("!BBHI", 0, 1, peer[1] ^ (stun.MAGIC >> 16),
                                int(ipaddress.IPv4Address(peer[0])) ^ stun.MAGIC)
            packet = build(0x010 | DATA, os.urandom(12), [(A_PEER, value), (A_DATA, data)])
        self.server.to_client(packet, self.client)

    def to_peer(self, data: bytes, peer: tuple) -> None:
        if self.permissions.get(peer[0], 0) < time.monotonic() or not self.server.pass_through(self, len(data)):
            return
        other = self.server.by_address.get(peer)
        if other is not None:                                  # two players on this one relay
            other.from_peer(data, self.address)
        elif self.relay is not None and self.relay.transport is not None:
            self.relay.transport.sendto(data, peer)

    def close(self) -> None:
        if self.relay is not None and self.relay.transport is not None:
            self.relay.transport.close()


class TurnServer:
    """Fed the packets the STUN port does not answer (stun._Server). `stun_servers()` lists STUN to learn a relay
    socket's outside address through; `usage_file` keeps the month's count across restarts; `limit_gb()` is the
    operator's monthly limit."""

    def __init__(self, secret: bytes, stun_servers=None, usage_file=None, limit_gb=None, own_addresses=None,
                 allocation_rate: int = ALLOCATION_RATE, total_rate: int = TOTAL_RATE):
        self.secret = secret
        self.stun_servers = stun_servers or (lambda: [])
        self.usage_file = usage_file
        self.limit_gb = limit_gb or (lambda: MONTH_LIMIT_GB)
        self.own_addresses = own_addresses or (lambda: ())
        self.allocation_rate = allocation_rate
        self.total = _Bucket(total_rate)
        self.transport = None
        self.allocations: dict[tuple, _Allocation] = {}
        self.by_address: dict[tuple, _Allocation] = {}        # outside address -> allocation
        self.month, self.bytes = self._recall()
        self.saved_at = time.monotonic()
        self.over = False

    # ---------------------------------------------------------------- the meter

    @staticmethod
    def _this_month() -> str:
        return time.strftime("%Y-%m", time.gmtime())

    def _recall(self) -> tuple[str, int]:
        try:
            with open(self.usage_file) as f:
                said = json.load(f)
            if said.get("month") == self._this_month():
                return said["month"], int(said["bytes"])
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return self._this_month(), 0

    def _save(self) -> None:
        self.saved_at = time.monotonic()
        if not self.usage_file:
            return
        try:
            temp = f"{self.usage_file}.tmp"
            with open(temp, "w") as f:
                json.dump({"month": self.month, "bytes": self.bytes}, f)
            os.replace(temp, self.usage_file)
        except OSError as exc:
            log.warning("media server usage not saved: %s", exc)

    def _over_limit(self) -> bool:
        return self.bytes >= float(self.limit_gb()) * 1e9

    def pass_through(self, allocation: _Allocation, size: int) -> bool:
        month = self._this_month()
        if month != self.month:
            self.month, self.bytes = month, 0
        if self._over_limit() or not allocation.bucket.take(size) or not self.total.take(size):
            return False
        self.bytes += size
        if time.monotonic() - self.saved_at > SAVE_EVERY:
            self._save()
        return True

    def status(self) -> dict:
        return {"allocations": len(self.allocations), "month": self.month,
                "used_gb": round(self.bytes / 1e9, 3), "limit_gb": float(self.limit_gb()),
                "over": self._over_limit()}

    def available(self) -> bool:
        return self.transport is not None and not self._over_limit()

    # ---------------------------------------------------------------- the wire

    def to_client(self, packet: bytes, client: tuple) -> None:
        if self.transport is not None:
            self.transport.sendto(packet, client)

    def close(self) -> None:
        for allocation in list(self.allocations.values()):
            allocation.close()
        self.allocations.clear()
        self.by_address.clear()
        self._save()
        self.transport = None

    def _drop(self, client: tuple) -> None:
        allocation = self.allocations.pop(client, None)
        if allocation is not None:
            self.by_address.pop(allocation.address, None)
            allocation.close()

    def _sweep(self) -> None:
        now = time.monotonic()
        for client, allocation in list(self.allocations.items()):
            if allocation.expires <= now:
                self._drop(client)

    def received(self, data: bytes, addr: tuple, transport) -> None:
        """A packet on the STUN port that is not a Binding request."""
        self.transport = transport
        client = (addr[0], addr[1])
        if len(data) >= 4 and 0x40 <= data[0] <= 0x7F:         # ChannelData: the common case, kept short
            allocation = self.allocations.get(client)
            if allocation is not None:
                number, size = struct.unpack("!HH", data[:4])
                peer = allocation.channels.get(number)
                if peer is not None and len(data) >= 4 + size:
                    allocation.to_peer(data[4:4 + size], peer)
            return
        parsed = parse(data)
        if parsed is None:
            return
        kind, tid, attrs, integrity = parsed
        if kind == 0x010 | SEND:
            allocation = self.allocations.get(client)
            peer = peer_address(attrs.get(A_PEER, b""))
            if allocation is not None and peer is not None and A_DATA in attrs:
                allocation.to_peer(attrs[A_DATA], peer)
            return
        if kind not in (ALLOCATE, REFRESH, PERMISSION, BIND):
            return
        self._sweep()
        key = self._key(data, kind, tid, attrs, integrity, client)
        if key is None:
            return
        if kind == ALLOCATE:
            asyncio.get_running_loop().create_task(self._allocate(tid, attrs, client, key))
        elif kind == REFRESH:
            self._refresh(tid, attrs, client, key)
        elif kind == PERMISSION:
            self._permission(tid, attrs, client, key)
        else:
            self._bind(tid, attrs, client, key)

    def _nonce(self, client: tuple, at: int | None = None) -> str:
        at = int(at if at is not None else time.time())
        mac = hmac.new(self.secret, f"{at}:{client[0]}".encode(), hashlib.sha256).hexdigest()[:24]
        return f"{at}.{mac}"

    def _nonce_ok(self, nonce: str, client: tuple) -> bool:
        try:
            at = int(nonce.split(".", 1)[0])
        except ValueError:
            return False
        return hmac.compare_digest(nonce, self._nonce(client, at)) and 0 <= time.time() - at < NONCE_TTL

    def _error(self, kind: int, tid: bytes, code: int, why: str, client: tuple, key: bytes | None = None,
               challenge: bool = False) -> None:
        value = struct.pack("!HBB", 0, code // 100, code % 100) + why.encode()
        attrs = [(A_ERROR, value)]
        if challenge:
            attrs += [(A_REALM, REALM.encode()), (A_NONCE, self._nonce(client).encode())]
        self.to_client(build(0x110 | kind, tid, attrs, key), client)

    def _key(self, data: bytes, kind: int, tid: bytes, attrs: dict, integrity: int | None, client: tuple) -> bytes | None:
        """The message's key if it proves a current login; otherwise answer the challenge and return None."""
        name = attrs.get(A_USERNAME, b"").decode("utf-8", "replace")
        nonce = attrs.get(A_NONCE, b"").decode("utf-8", "replace")
        if integrity is None or not name or A_INTEGRITY not in attrs:
            self._error(kind, tid, 401, "Unauthorized", client, challenge=True)
            return None
        if not self._nonce_ok(nonce, client):
            self._error(kind, tid, 438, "Stale Nonce", client, challenge=True)
            return None
        try:
            alive = int(name.split(":", 1)[0]) > time.time()
        except ValueError:
            alive = False
        key = hashlib.md5(f"{name}:{REALM}:{password_for(self.secret, name)}".encode()).digest()   # noqa: S324 -- the RFC's
        head = data[:2] + struct.pack("!H", integrity + 24 - 20) + data[4:integrity]
        if not alive or not hmac.compare_digest(hmac.new(key, head, hashlib.sha1).digest(), attrs[A_INTEGRITY]):
            self._error(kind, tid, 401, "Unauthorized", client, challenge=True)
            return None
        return key

    # ---------------------------------------------------------------- the requests

    async def _allocate(self, tid: bytes, attrs: dict, client: tuple, key: bytes) -> None:
        user = attrs[A_USERNAME].decode("utf-8", "replace")
        held = self.allocations.get(client)
        if held is not None:
            if held.tid == tid and held.address:                # a resend of the same request
                self._allocated(held, tid, key)
            elif held.tid != tid:
                self._error(ALLOCATE, tid, 437, "Allocation Mismatch", client, key)
            return
        if attrs.get(A_TRANSPORT, b"")[:1] != bytes([UDP]):
            self._error(ALLOCATE, tid, 442, "Unsupported Transport Protocol", client, key)
            return
        if (self._over_limit() or len(self.allocations) >= MAX_ALLOCATIONS
                or sum(1 for a in self.allocations.values() if a.user == user) >= MAX_PER_LOGIN):
            self._error(ALLOCATE, tid, 486, "Allocation Quota Reached", client, key)
            return
        allocation = _Allocation(self, client, user, key, tid)
        allocation.expires = time.monotonic() + self._lifetime(attrs)
        self.allocations[client] = allocation
        try:
            _, allocation.relay = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: _Relay(allocation), sock=punchlib.udp_socket())
            allocation.address = await self._outside(allocation.relay, client)
        except OSError:
            self.allocations.pop(client, None)
            allocation.close()
            self._error(ALLOCATE, tid, 508, "Insufficient Capacity", client, key)
            return
        self.by_address[allocation.address] = allocation
        self._allocated(allocation, tid, key)

    async def _outside(self, relay: _Relay, client: tuple) -> tuple[str, int]:
        """Where peers should send to reach this socket."""
        session = punchlib.Session(os.urandom(6).hex(), "t")
        session.proto = relay
        session.local_port = relay.transport.get_extra_info("sockname")[1]
        try:
            mesh = [(h, p) for h, p in (self.stun_servers() or []) if punchlib._v4(h)]
        except Exception:                                      # noqa: BLE001
            mesh = []
        await session.map(mesh[:1])
        found = next(((m["host"], m["port"]) for m in session.maps if m.get("host")), None)
        return found or (punchlib.local_ipv4() or "127.0.0.1", session.local_port)

    def _allocated(self, allocation: _Allocation, tid: bytes, key: bytes) -> None:
        lifetime = max(0, int(allocation.expires - time.monotonic()))
        relayed = stun._xor_address(allocation.address[0], allocation.address[1], tid)
        mapped = stun._xor_address(allocation.client[0], allocation.client[1], tid)
        self.to_client(build(0x100 | ALLOCATE, tid, [(A_RELAYED, relayed), (A_LIFETIME, struct.pack("!I", lifetime)),
                                                     (stun.XOR_MAPPED_ADDRESS, mapped)], key), allocation.client)

    @staticmethod
    def _lifetime(attrs: dict) -> int:
        asked = attrs.get(A_LIFETIME)
        wanted = struct.unpack("!I", asked)[0] if asked and len(asked) == 4 else DEFAULT_LIFETIME
        return max(DEFAULT_LIFETIME, min(MAX_LIFETIME, wanted))

    def _owned(self, kind: int, tid: bytes, attrs: dict, client: tuple, key: bytes) -> _Allocation | None:
        allocation = self.allocations.get(client)
        if allocation is None or allocation.user != attrs[A_USERNAME].decode("utf-8", "replace"):
            self._error(kind, tid, 437, "Allocation Mismatch", client, key)
            return None
        return allocation

    def _refresh(self, tid: bytes, attrs: dict, client: tuple, key: bytes) -> None:
        allocation = self._owned(REFRESH, tid, attrs, client, key)
        if allocation is None:
            return
        asked = attrs.get(A_LIFETIME)
        if asked == struct.pack("!I", 0):
            self._drop(client)
            lifetime = 0
        else:
            lifetime = self._lifetime(attrs)
            allocation.expires = time.monotonic() + lifetime
        self.to_client(build(0x100 | REFRESH, tid, [(A_LIFETIME, struct.pack("!I", lifetime))], key), client)

    def _allowed(self, peer: tuple | None, client: tuple) -> bool:
        if peer is not None and peer in self.by_address:
            return True
        return peer is not None and may_reach(peer[0], client[0], tuple(self.own_addresses()))

    def _permission(self, tid: bytes, attrs: dict, client: tuple, key: bytes) -> None:
        allocation = self._owned(PERMISSION, tid, attrs, client, key)
        if allocation is None:
            return
        peer = peer_address(attrs.get(A_PEER, b""))
        if not self._allowed(peer, client):
            self._error(PERMISSION, tid, 403, "Forbidden", client, key)
            return
        allocation.permissions[peer[0]] = time.monotonic() + PERMISSION_TTL
        self.to_client(build(0x100 | PERMISSION, tid, [], key), client)

    def _bind(self, tid: bytes, attrs: dict, client: tuple, key: bytes) -> None:
        allocation = self._owned(BIND, tid, attrs, client, key)
        if allocation is None:
            return
        peer = peer_address(attrs.get(A_PEER, b""))
        raw = attrs.get(A_CHANNEL, b"")
        number = struct.unpack("!H", raw[:2])[0] if len(raw) == 4 else 0
        if not 0x4000 <= number <= 0x7FFF or not self._allowed(peer, client):
            self._error(BIND, tid, 400, "Bad Request", client, key)
            return
        if allocation.channels.get(number, peer) != peer or allocation.numbers.get(peer, (number,))[0] != number:
            self._error(BIND, tid, 400, "Bad Request", client, key)
            return
        now = time.monotonic()
        allocation.channels[number] = peer
        allocation.numbers[peer] = (number, now + CHANNEL_TTL)
        allocation.permissions[peer[0]] = now + PERMISSION_TTL
        self.to_client(build(0x100 | BIND, tid, [], key), client)


class TurnShare:
    """Relays from other arcades (2026-10-31: "I don't want to have to use Cloudflare for multimedia if I'm stuck behind
    NAT"). A node behind NAT cannot be reached by a player away from its home network, so it asks the arcades whose
    STUN port another node has proved open ("nodes in the mesh that are able to be servers become servers and nodes
    that cannot be servers connect to those servers"). The address always comes from that proof (stun.py), never
    from the answer, and the serving node's own limits apply.

    Frames "tu": {"op": "ask", "n", "tag"}; {"op": "login", "n", "u", "p"} or {"op": "no", "n"}."""

    kinds = ("tu",)

    def __init__(self, server: TurnServer | None, reachable=None, wait: float = 2.0):
        self.server = server                   # this node's own relay, or None
        self.reachable = reachable or (lambda: {})   # peer id -> its proved [(host, port)...]
        self.wait = wait
        self.node = None
        self.waiting: dict[str, asyncio.Future] = {}

    async def link_up(self, link) -> None:
        pass

    async def link_down(self, peer_id: str) -> None:
        pass

    async def rebalance(self) -> None:
        pass

    async def _one(self, peer: str, tag: str, hosts: list) -> dict | None:
        link = self.node.links.get(peer)
        if link is None:
            return None
        nonce = os.urandom(6).hex()
        fut = asyncio.get_running_loop().create_future()
        self.waiting[nonce] = fut
        try:
            await link.send({"t": "tu", "op": "ask", "n": nonce, "tag": tag[:64]})
            said = await asyncio.wait_for(fut, self.wait)
        except (asyncio.TimeoutError, ConnectionError, OSError, ValueError):
            return None
        finally:
            self.waiting.pop(nonce, None)
        if said.get("op") != "login" or not isinstance(said.get("u"), str) or not isinstance(said.get("p"), str):
            return None
        return ice_server(hosts, said["u"][:128], said["p"][:128])

    async def borrow(self, tag: str, count: int = 2) -> list[dict]:
        """ICE servers on up to `count` other arcades, for the player `tag`."""
        offers = [(p, h) for p, h in dict(self.reachable()).items() if p in self.node.links and h]
        got = await asyncio.gather(*(self._one(p, f"{self.node.node_id[:16]}:{tag}", h) for p, h in offers))
        return [g for g in got if g][:count]

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        op, nonce = header.get("op"), str(header.get("n", ""))[:12]
        if op == "ask":
            if self.server is None or not self.server.available():
                await link.send({"t": "tu", "op": "no", "n": nonce})
                return
            username, password = login(self.server.secret, f"{link.peer_id}:{str(header.get('tag'))[:64]}")
            await link.send({"t": "tu", "op": "login", "n": nonce, "u": username, "p": password})
        elif op in ("login", "no"):
            fut = self.waiting.get(nonce)
            if fut is not None and not fut.done():
                fut.set_result(header)


def ice_server(hosts: list, username: str, credential: str) -> dict:
    """What a browser's RTCPeerConnection takes."""
    urls = [f"turn:[{h}]:{p}?transport=udp" if ":" in h else f"turn:{h}:{p}?transport=udp" for h, p in hosts]
    return {"urls": urls, "username": username, "credential": credential}


def player_relays(server: TurnServer | None, stun_share, port: int | None, lan: str | None, asker: str, address: str,
                  borrowed: list[dict]) -> list[dict]:
    """What a signed-in player is offered: this node's relay where the player can reach it, the proved address for
    anybody and the home-network one for a player at home; the relays borrowed from other arcades only when this
    node has no proved relay of its own (2026-11-02). A browser may pick any relay it is given, and a far
    borrowed one lost 14-20% of a call's sound (@banana from .25's network, ~300 ms away) while this node's lost none."""
    if server is None or not server.available():
        return borrowed
    proved = list(stun_share.mine()) if stun_share is not None else []
    hosts = list(proved)
    try:
        if lan and port and _plain(asker).is_private:
            hosts.insert(0, (lan, port))
    except ValueError:
        pass
    if not hosts:
        return borrowed
    username, password = login(server.secret, address)
    own = [ice_server(hosts[:4], username, password)]
    return own if proved else own + borrowed
