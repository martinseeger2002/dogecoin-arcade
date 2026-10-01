"""A node's place in the mesh: its peers, the rooms its players are in, and the forwarding.

**Finding peers.** A node starts from whatever addresses it is handed (its own
config, and later the chain) and learns more from the peers it reaches: each
link swaps the addresses its side has actually connected to. It keeps a few
outbound links up at all times and accepts inbound ones if it listens. A node
behind a router never listens and still takes part fully -- it dials out, and
everything it needs arrives over the links it made.

**Rooms without a server.** A node whose players are in a room says so to the
whole mesh every few seconds: a signed SUB naming its rooms and who is in each.
SUBs flood (each node passes on what it has not seen), and the path a node's
SUB arrived by is the path that node's room traffic is sent back along. So a
message goes only towards nodes that want it, nodes in between forward without
being in the room, and when a node goes quiet its SUBs stop and its routes and
members expire on their own. Nothing has to be torn down by anyone.

**Nobody can speak for another node.** Every SUB and every room message is
signed by the node it came from, over everything but the hop count. A node that
forwards can drop or delay, never forge or alter; and the receiver checks the
signature, not the neighbour it came from.

**Who a member is.** A member id is whatever the layer above says it is, and
each one may carry a credential (an opaque dict) that its node hands round with
it. Before a node shows a member to its own players or delivers their messages,
it asks `vouch` -- the layer above -- whether that credential proves that id.
The mesh never decides what proves what; the arcade decides that (a signature by
the member's chain address, bound to the node it is on). Nodes that only
forward never ask, so a busy crossroads costs nothing per member.

**What the mesh promises a game, and what it does not.** Delivery is
best-effort and unordered across senders, which is what a position update
wants: the next one replaces it. A message is opaque bytes, at most MAX_PAYLOAD,
and each member may send about RATE per second; the origin node refuses more,
and every forwarder drops a sender that runs far over (a node that ignores its
own limits gets its traffic dropped one hop later). Anything that has to be
true later goes on the chain.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import nacl.exceptions
import nacl.signing

from .link import Link, LinkError, handshake

log = logging.getLogger("arcade.mesh")

MAX_PAYLOAD = 512           # bytes in one room message
MAX_ROOM = 128              # characters in a room name
MAX_MEMBER = 64             # characters in a member id
MAX_ROOMS_PER_NODE = 64
MAX_MEMBERS_PER_ROOM = 256  # per node, in one SUB
RATE = 5.0                  # messages per second per member, sustained
BURST = 10                  # messages a member may send at once
FORWARD_SLACK = 2.0         # forwarders allow this multiple before dropping
IDLE = 10.0                 # seconds before a silent member or node is gone
SUB_EVERY = 3.0             # how often a node re-announces its rooms
SKEW = 60.0                 # seconds of clock difference tolerated on a message
MAX_HOPS = 16
OUTBOUND_TARGET = 4
DIAL_EVERY = 1.0            # how often a node tops up its outbound links
MAX_INBOUND = 32
PING_EVERY = 15.0
DEAD_AFTER = 45.0
SEEN_MAX = 50_000


class MeshError(ValueError):
    """A request the mesh refuses: too big, too fast, not joined, malformed."""


def _canon(header: dict) -> bytes:
    return json.dumps(header, separators=(",", ":"), sort_keys=True).encode()


def _signed_part(header: dict) -> dict:
    return {k: v for k, v in header.items() if k not in ("sig", "hops")}


class _Bucket:
    """A token bucket: `rate` per second, holding at most `burst`."""

    def __init__(self, rate: float, burst: float):
        self.rate, self.burst = rate, burst
        self.tokens, self.at = burst, time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
        self.at = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


@dataclass
class Event:
    """What a local member hears: a message, or somebody joining or leaving."""
    type: str                   # "message" | "join" | "leave"
    room: str
    member: str
    node: str                   # the mesh key (hex) of the node the member is on
    data: bytes = b""
    cred: dict | None = None    # what the member's node vouched for it with


@dataclass
class _Local:
    """A member on THIS node: a player whose page is open here."""
    room: str
    member: str
    queue: asyncio.Queue
    bucket: _Bucket
    cred: dict | None = None
    touched: float = field(default_factory=time.monotonic)


def check_room(room: Any) -> str:
    room = str(room or "")
    if not room or len(room) > MAX_ROOM or any(ord(c) < 32 for c in room):
        raise MeshError(f"a room is a name of 1 to {MAX_ROOM} printable characters")
    return room


def check_member(member: Any) -> str:
    member = str(member or "")
    if not member or len(member) > MAX_MEMBER or any(ord(c) < 32 for c in member):
        raise MeshError(f"a member id is 1 to {MAX_MEMBER} printable characters")
    return member


class MeshNode:
    """One node in the mesh. Runs on an asyncio loop: `await start()`, `await stop()`."""

    def __init__(self, signing_key: nacl.signing.SigningKey, network: str, *,
                 listen_host: str = "0.0.0.0", listen_port: int | None = None,
                 peers: list[tuple[str, int]] | None = None,
                 outbound_target: int = OUTBOUND_TARGET,
                 vouch: Callable[[str, str, dict | None], Awaitable[bool]] | None = None):
        self.key = signing_key
        self.node_id = bytes(signing_key.verify_key).hex()
        self.network = network
        self.listen_host, self.listen_port = listen_host, listen_port
        self.outbound_target = outbound_target
        # Asked before a remote member is shown or heard; None trusts every node.
        self.vouch = vouch
        self._vetted: OrderedDict[tuple, tuple[bool, float]] = OrderedDict()
        # node id -> link, for every live link
        self.links: dict[str, Link] = {}
        # (host, port) -> {"id": node id or None, "ok": last success, "fail": failures, "next": retry at}
        self.addrs: dict[tuple[str, int], dict] = {}
        for host, port in peers or []:
            self.add_address(host, port)
        self.local: dict[tuple[str, str], _Local] = {}
        # room -> origin node -> {"via": peer id or None, "members": {member: cred}, "until": expiry}
        self.remote: dict[str, dict[str, dict]] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        # room -> {(origin, member)} this node's members have been told are there
        self._present: dict[str, set[tuple[str, str]]] = {}
        self._fwd_buckets: dict[tuple[str, str], _Bucket] = {}
        self._sub_buckets: dict[str, _Bucket] = {}
        self._dialbacks: dict[str, asyncio.Future] = {}
        self._dialed_back: dict[str, float] = {}
        self._seq = 0
        self._server: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self._stopping = False

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self.listen_port is not None:
            self._server = await asyncio.start_server(self._accept, self.listen_host,
                                                      self.listen_port)
            if self.listen_port == 0:   # tests ask for any free port
                self.listen_port = self._server.sockets[0].getsockname()[1]
        self._spawn(self._dialer())
        self._spawn(self._announcer())

    async def stop(self) -> None:
        self._stopping = True
        if self._server is not None:
            self._server.close()
        # Links first: since Python 3.12 the server's wait_closed() waits for every
        # connection it accepted, and those end only when their links do.
        for link in list(self.links.values()):
            await link.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._server is not None:
            await self._server.wait_closed()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---------------------------------------------------------------- peers

    def add_address(self, host: str, port: int, node_id: str | None = None) -> None:
        if not host or not isinstance(port, int) or not 0 < port < 65536:
            return
        entry = self.addrs.setdefault((host, port), {"id": None, "ok": 0.0, "fail": 0, "next": 0.0})
        if node_id and not entry["id"]:
            entry["id"] = node_id

    def _outbound(self) -> int:
        return sum(1 for link in self.links.values() if link.outbound)

    async def _dialer(self) -> None:
        while not self._stopping:
            try:
                if self._outbound() < self.outbound_target:
                    now = time.monotonic()
                    choices = [a for a, e in self.addrs.items()
                               if e["next"] <= now and e["id"] != self.node_id
                               and e["id"] not in self.links]
                    random.shuffle(choices)
                    for addr in choices[: self.outbound_target - self._outbound()]:
                        self._spawn(self._dial(addr))
            except Exception:                      # noqa: BLE001 -- the loop must not die
                log.exception("mesh dialer")
            await asyncio.sleep(DIAL_EVERY)

    async def _dial(self, addr: tuple[str, int]) -> None:
        entry = self.addrs[addr]
        entry["next"] = time.monotonic() + 30           # not again while this one runs
        writer = None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(*addr), 5.0)
            link = await handshake(reader, writer, self.key, self.network, True, self.listen_port)
        except (LinkError, OSError, asyncio.TimeoutError) as exc:
            entry["fail"] += 1
            entry["next"] = time.monotonic() + min(600, 5 * 2 ** min(entry["fail"], 7))
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
            log.debug("mesh dial %s failed: %s", addr, exc)
            return
        entry.update(id=link.peer_id, ok=time.time(), fail=0, next=time.monotonic() + 30)
        await self._run_link(link)

    async def _accept(self, reader, writer) -> None:
        inbound = sum(1 for link in self.links.values() if not link.outbound)
        if inbound >= MAX_INBOUND or self._stopping:
            writer.close()
            await writer.wait_closed()
            return
        try:
            link = await handshake(reader, writer, self.key, self.network, False, self.listen_port)
        except LinkError as exc:
            log.debug("mesh inbound refused: %s", exc)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            return
        if link.peer_listen:
            # Where it says it can be reached. Kept as a lead, passed on to
            # others only once this node has reached it itself (_dial sets ok).
            self.add_address(link.host, link.peer_listen, link.peer_id)
        await self._run_link(link)

    async def _run_link(self, link: Link) -> None:
        if link.peer_id in self.links or self._stopping:
            await link.close()                 # already linked to that node
            return
        self.links[link.peer_id] = link
        log.info("mesh: linked %s %s", "to" if link.outbound else "from", link.peer_id[:12])
        pinger = self._spawn(self._pinger(link))
        try:
            await self._send_addrs(link)
            await self._send_own_sub(only=link)
            while True:
                header, blob = await asyncio.wait_for(link.recv(), DEAD_AFTER)
                await self._handle(link, header, blob)
        except (LinkError, asyncio.IncompleteReadError, asyncio.TimeoutError,
                ConnectionError, OSError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:                      # noqa: BLE001 -- one bad peer, not the node
            log.exception("mesh link %s", link.peer_id[:12])
        finally:
            pinger.cancel()
            if self.links.get(link.peer_id) is link:
                del self.links[link.peer_id]
            await link.close()
            self._drop_routes_via(link.peer_id)

    async def _pinger(self, link: Link) -> None:
        try:
            while True:
                await asyncio.sleep(PING_EVERY)
                await link.send({"t": "ping"})
        except (LinkError, ConnectionError, OSError):
            pass

    async def _send_addrs(self, link: Link) -> None:
        good = [{"h": h, "p": p, "id": e["id"]} for (h, p), e in self.addrs.items()
                if e["ok"] and e["id"] and e["id"] != link.peer_id][:50]
        if good:
            await link.send({"t": "addrs", "a": good})

    # ---------------------------------------------------------------- rooms: local side

    async def join(self, room: str, member: str, cred: dict | None = None) -> asyncio.Queue:
        """Put a member of this node into a room; returns the queue of its Events.

        `cred` goes round with the member so other nodes can check who it is;
        the caller has checked it already, since it is this node's own player."""
        room, member = check_room(room), check_member(member)
        if (room, member) in self.local:
            return self.local[(room, member)].queue
        if len({r for r, _ in self.local}) >= MAX_ROOMS_PER_NODE and \
                room not in {r for r, _ in self.local}:
            raise MeshError("this node is in as many rooms as it may be")
        if sum(1 for r, _ in self.local if r == room) >= MAX_MEMBERS_PER_ROOM:
            raise MeshError("this room is full on this node")
        if cred is not None and (not isinstance(cred, dict) or len(_canon(cred)) > 400):
            raise MeshError("a credential is a small object")
        entry = _Local(room, member, asyncio.Queue(maxsize=1000), _Bucket(RATE, BURST), cred)
        self.local[(room, member)] = entry
        self._announce_local(room, member, "join")
        await self._publish(room, member, "join", b"", cred=cred)
        await self._send_own_sub()
        return entry.queue

    async def leave(self, room: str, member: str) -> None:
        if self.local.pop((room, member), None) is None:
            return
        self._announce_local(room, member, "leave")
        await self._publish(room, member, "leave", b"")
        await self._send_own_sub()

    async def send(self, room: str, member: str, data: bytes) -> None:
        entry = self.local.get((room, member))
        if entry is None:
            raise MeshError("join the room first")
        if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_PAYLOAD:
            raise MeshError(f"a message is at most {MAX_PAYLOAD} bytes")
        if not entry.bucket.take():
            raise MeshError(f"too fast: about {RATE:g} messages a second")
        entry.touched = time.monotonic()
        for other in self._locals_in(room):
            if other.member != member:
                self._put(other, Event("message", room, member, self.node_id, bytes(data),
                                       entry.cred))
        await self._publish(room, member, "msg", bytes(data))

    def touch(self, room: str, member: str) -> None:
        """The member is still there (its page is still connected), sending or not."""
        entry = self.local.get((room, member))
        if entry is not None:
            entry.touched = time.monotonic()

    def members(self, room: str) -> list[dict]:
        """Who is in a room: this node's own, and every remote member that has
        been vouched for (only those ever reach a game)."""
        out = [{"member": m.member, "node": self.node_id, "cred": m.cred}
               for m in self._locals_in(room)]
        now = time.monotonic()
        for origin, info in self.remote.get(room, {}).items():
            if info["until"] > now:
                out += [{"member": m, "node": origin, "cred": c}
                        for m, c in sorted(info["members"].items())
                        if self._vetted_already(origin, m, c)]
        return out

    def _locals_in(self, room: str) -> list[_Local]:
        return [m for (r, _), m in self.local.items() if r == room]

    def _put(self, local: _Local, event: Event) -> None:
        try:
            local.queue.put_nowait(event)
        except asyncio.QueueFull:              # a page that stopped reading loses the oldest
            try:
                local.queue.get_nowait()
                local.queue.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def _announce_local(self, room: str, member: str, kind: str) -> None:
        cred = (self.local.get((room, member)) or _Local(room, member, None, None)).cred
        for other in self._locals_in(room):
            if other.member != member:
                self._put(other, Event(kind, room, member, self.node_id, b"", cred))

    # ---------------------------------------------------------------- vouching

    @staticmethod
    def _cred_key(origin: str, member: str, cred: dict | None) -> tuple:
        return (origin, member, _canon(cred) if cred is not None else b"")

    def _vetted_already(self, origin: str, member: str, cred: dict | None) -> bool:
        if self.vouch is None:
            return True
        hit = self._vetted.get(self._cred_key(origin, member, cred))
        return bool(hit and hit[0] and hit[1] > time.monotonic())

    async def _vet(self, origin: str, member: str, cred: dict | None) -> bool:
        """Does `cred` prove `member` on `origin`? Asked once, remembered a while."""
        if self.vouch is None:
            return True
        key = self._cred_key(origin, member, cred)
        hit = self._vetted.get(key)
        if hit and hit[1] > time.monotonic():
            return hit[0]
        try:
            ok = bool(await self.vouch(origin, member, cred))
        except Exception:                          # noqa: BLE001 -- unproven is refused
            log.exception("mesh vouch")
            ok = False
        self._vetted[key] = (ok, time.monotonic() + (600 if ok else 60))
        while len(self._vetted) > 20_000:
            self._vetted.popitem(last=False)
        return ok

    # ---------------------------------------------------------------- rooms: the wire

    def _next_seq(self) -> int:
        # Microseconds since the epoch, kept strictly increasing: a restarted node
        # never reuses a number it used before, so nothing it sends looks like a replay.
        self._seq = max(self._seq + 1, int(time.time() * 1_000_000))
        return self._seq

    def _sign(self, header: dict) -> dict:
        header["sig"] = self.key.sign(_canon(_signed_part(header))).signature.hex()
        return header

    @staticmethod
    def _verify(header: dict, blob: bytes = b"") -> bool:
        try:
            key = nacl.signing.VerifyKey(bytes.fromhex(str(header.get("o", ""))))
            body = _canon(_signed_part(header)) + blob
            key.verify(body, bytes.fromhex(str(header.get("sig", ""))))
            return True
        except (ValueError, nacl.exceptions.BadSignatureError, nacl.exceptions.ValueError,
                nacl.exceptions.TypeError):
            return False

    def _fresh(self, header: dict) -> bool:
        """Not seen before, and stamped within SKEW of now (an old message replayed is not)."""
        seq = header.get("q")
        if not isinstance(seq, int) or abs(seq / 1_000_000 - time.time()) > SKEW:
            return False
        return f"{header.get('o')}:{seq}" not in self._seen

    def _mark(self, header: dict) -> None:
        """Remembered only once its signature has checked out, so a forged copy
        arriving first cannot get the real one thrown away as a duplicate."""
        self._seen[f"{header.get('o')}:{header.get('q')}"] = None
        while len(self._seen) > SEEN_MAX:
            self._seen.popitem(last=False)

    async def _publish(self, room: str, member: str, kind: str, data: bytes,
                       cred: dict | None = None) -> None:
        header = {"t": "pub", "r": room, "o": self.node_id, "q": self._next_seq(),
                  "m": member, "k": kind, "hops": 0}
        if cred is not None:
            header["c"] = cred
        header["sig"] = self.key.sign(_canon(_signed_part(header)) + data).signature.hex()
        self._seen[f"{self.node_id}:{header['q']}"] = None
        await self._forward_pub(header, data, came_from=None)

    async def _forward_pub(self, header: dict, data: bytes, came_from: str | None) -> None:
        room = header["r"]
        targets = {info["via"] for origin, info in self.remote.get(room, {}).items()
                   if info["via"] and info["until"] > time.monotonic() and origin != header["o"]}
        targets.discard(came_from)
        for peer_id in targets:
            link = self.links.get(peer_id)
            if link is not None:
                try:
                    await link.send(header, data)
                except (LinkError, ConnectionError, OSError):
                    pass

    async def _send_own_sub(self, only: Link | None = None) -> None:
        rooms: dict[str, dict[str, dict | None]] = {}
        now = time.monotonic()
        for (room, member), entry in list(self.local.items()):
            if now - entry.touched > IDLE:          # its page went away without saying
                del self.local[(room, member)]
                self._announce_local(room, member, "leave")
                await self._publish(room, member, "leave", b"")
                continue
            rooms.setdefault(room, {})[member] = entry.cred
        header = self._sign({"t": "sub", "o": self.node_id, "q": self._next_seq(),
                             "rooms": rooms, "hops": 0})
        self._seen[f"{self.node_id}:{header['q']}"] = None
        links = [only] if only is not None else list(self.links.values())
        for link in links:
            try:
                await link.send(header)
            except (LinkError, ConnectionError, OSError):
                pass

    async def _announcer(self) -> None:
        while not self._stopping:
            await asyncio.sleep(SUB_EVERY)
            try:
                await self._send_own_sub()
                self._expire()
            except Exception:                      # noqa: BLE001
                log.exception("mesh announcer")

    def _expire(self) -> None:
        now = time.monotonic()
        for room in list(self.remote):
            for origin, info in list(self.remote[room].items()):
                if info["until"] <= now:
                    del self.remote[room][origin]
                    for member in info["members"]:
                        self._here(room, origin, member, False)
            if not self.remote[room]:
                del self.remote[room]

    def _drop_routes_via(self, peer_id: str) -> None:
        """A link went down: routes through it are wrong now. The next SUBs, by
        whatever path still exists, put them back within SUB_EVERY seconds."""
        for room in self.remote.values():
            for info in room.values():
                if info["via"] == peer_id:
                    info["via"] = None

    def _tell_locals(self, room: str, event: Event) -> None:
        for local in self._locals_in(room):
            self._put(local, event)

    def _here(self, room: str, origin: str, member: str, present: bool,
              cred: dict | None = None) -> None:
        """Say "join" or "leave" once per change, whichever of a SUB, a join
        message or an expiry noticed it first. Only vouched members are ever
        said to be here, so only they are ever said to have left."""
        seen = self._present.setdefault(room, set())
        key = (origin, member)
        if present and key not in seen:
            seen.add(key)
            self._tell_locals(room, Event("join", room, member, origin, b"", cred))
        elif not present and key in seen:
            seen.discard(key)
            self._tell_locals(room, Event("leave", room, member, origin))
        if not seen:
            self._present.pop(room, None)

    # ---------------------------------------------------------------- incoming

    async def _handle(self, link: Link, header: dict, blob: bytes) -> None:
        kind = header.get("t")
        if kind == "ping":
            return
        if kind == "addrs":
            for a in header.get("a", [])[:50] if isinstance(header.get("a"), list) else []:
                if isinstance(a, dict) and isinstance(a.get("h"), str) and isinstance(a.get("p"), int):
                    self.add_address(a["h"][:255], a["p"], str(a.get("id") or "")[:64] or None)
            return
        if kind == "sub":
            await self._on_sub(link, header)
            return
        if kind == "pub":
            await self._on_pub(link, header, blob)
            return
        if kind == "dialback":
            self._spawn(self._dial_back(link, header))
            return
        if kind == "dialback-said":
            waiter = self._dialbacks.pop(str(header.get("n", "")), None)
            if waiter is not None and not waiter.done():
                waiter.set_result(header)

    # ---------------------------------------------------------------- can I be reached

    async def reaches_itself(self, host: str, port: int) -> bool:
        """Dial host:port and see whether this node answers. If it does, the
        address really leads here (a router forwards the port, and lets a
        machine reach its own public address); anything else proves nothing."""
        writer = None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5.0)
            await handshake(reader, writer, self.key, self.network, True, None)
            return False                      # somebody answered, and it was not us
        except LinkError as exc:
            return "itself" in str(exc)
        except (OSError, asyncio.TimeoutError):
            return False
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass

    async def ask_dial_back(self, port: int, timeout: float = 15.0) -> dict:
        """Ask up to three peers to dial this node back on `port`, at the address
        each of them sees it coming from. Returns {"reached": bool, "seen": [hosts]}:
        whether any of them got through, and where they see this node."""
        asks = []
        for link in list(self.links.values())[:3]:
            nonce = secrets.token_hex(8)
            waiter = asyncio.get_running_loop().create_future()
            self._dialbacks[nonce] = waiter
            try:
                await link.send({"t": "dialback", "port": int(port), "n": nonce})
                asks.append(waiter)
            except (LinkError, ConnectionError, OSError):
                self._dialbacks.pop(nonce, None)
        reached, seen = False, []
        for waiter in asks:
            try:
                said = await asyncio.wait_for(waiter, timeout)
            except asyncio.TimeoutError:
                continue
            reached = reached or said.get("ok") is True
            if isinstance(said.get("host"), str):
                seen.append(said["host"])
        return {"reached": reached, "seen": seen}

    async def _dial_back(self, link: Link, header: dict) -> None:
        """A peer asks whether it can be reached. Dial it -- only ever at the
        address its own connection comes from, so nobody can aim this node at a
        third machine -- and say what happened, and where it was seen from."""
        port = header.get("port")
        now = time.monotonic()
        if not isinstance(port, int) or not 0 < port < 65536:
            return
        if now - self._dialed_back.get(link.peer_id, 0) < 60:
            return                                # once a minute per peer is plenty
        self._dialed_back[link.peer_id] = now
        host, ok, writer = link.host, False, None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5.0)
            back = await handshake(reader, writer, self.key, self.network, True, None)
            ok = back.peer_id == link.peer_id     # the node that asked, not just anybody
        except (LinkError, OSError, asyncio.TimeoutError):
            ok = False
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
        try:
            await link.send({"t": "dialback-said", "n": str(header.get("n", ""))[:32],
                             "ok": ok, "host": host})
        except (LinkError, ConnectionError, OSError):
            pass

    async def _on_sub(self, link: Link, header: dict) -> None:
        origin = str(header.get("o", ""))
        rooms = header.get("rooms")
        hops = header.get("hops")
        if origin == self.node_id or not isinstance(rooms, dict) or not isinstance(hops, int):
            return
        if hops >= MAX_HOPS or not self._fresh(header) or not self._verify(header):
            return
        self._mark(header)
        now = time.monotonic()
        # A join or leave sends a SUB at once, on top of the regular ones, so a
        # burst is normal; a node announcing in a loop is not.
        bucket = self._sub_buckets.setdefault(origin, _Bucket(4.0, 10))
        if not bucket.take():
            return
        if len(self._sub_buckets) > 10_000:
            self._sub_buckets.clear()
        clean: dict[str, dict[str, dict | None]] = {}
        for room, members in list(rooms.items())[:MAX_ROOMS_PER_NODE]:
            try:
                room = check_room(room)
                if not isinstance(members, dict):
                    continue
                clean[room] = {check_member(m): (c if isinstance(c, dict) else None)
                               for m, c in list(members.items())[:MAX_MEMBERS_PER_ROOM]}
            except (MeshError, TypeError):
                continue
        # Rooms this origin has left since its last SUB.
        for room in list(self.remote):
            if origin in self.remote[room] and room not in clean:
                for member in self.remote[room].pop(origin)["members"]:
                    self._here(room, origin, member, False)
        for room, members in clean.items():
            info = self.remote.setdefault(room, {}).get(origin)
            before = info["members"] if info else {}
            via = link.peer_id
            if info and info["via"] and info["until"] > now and info["via"] in self.links:
                via = info["via"]                       # keep the path that is working
            self.remote[room][origin] = {"via": via, "members": members, "until": now + IDLE}
            for member in set(before) - set(members):
                self._here(room, origin, member, False)
            if self._locals_in(room):
                # Only a node with players in the room asks who these are; one
                # that merely forwards never pays for the check.
                for member, cred in members.items():
                    if (origin, member) not in self._present.get(room, ()) and \
                            await self._vet(origin, member, cred):
                        self._here(room, origin, member, True, cred)
        header["hops"] = hops + 1
        for peer_id, other in list(self.links.items()):
            if peer_id != link.peer_id:
                try:
                    await other.send(header)
                except (LinkError, ConnectionError, OSError):
                    pass

    async def _on_pub(self, link: Link, header: dict, data: bytes) -> None:
        origin = str(header.get("o", ""))
        hops = header.get("hops")
        try:
            room, member = check_room(header.get("r")), check_member(header.get("m"))
        except MeshError:
            return
        if origin == self.node_id or not isinstance(hops, int) or hops >= MAX_HOPS:
            return
        if len(data) > MAX_PAYLOAD or header.get("k") not in ("msg", "join", "leave"):
            return
        if not self._fresh(header) or not self._verify(header, data):
            return
        self._mark(header)
        if header["k"] == "msg":
            bucket = self._fwd_buckets.setdefault(
                (origin, member), _Bucket(RATE * FORWARD_SLACK, BURST * FORWARD_SLACK))
            if not bucket.take():
                return                                  # its node is not keeping its own limits
            if len(self._fwd_buckets) > 10_000:
                self._fwd_buckets.clear()
        known = self.remote.get(room, {}).get(origin)
        if header["k"] == "join":
            cred = header.get("c") if isinstance(header.get("c"), dict) else None
            if known is not None:
                known["members"][member] = cred
            else:
                # Heard of before its node's SUB: it still has to expire if that
                # node goes quiet, or a vanished player would stay forever.
                self.remote.setdefault(room, {})[origin] = {
                    "via": link.peer_id, "members": {member: cred},
                    "until": time.monotonic() + IDLE}
            if self._locals_in(room) and await self._vet(origin, member, cred):
                self._here(room, origin, member, True, cred)
        elif header["k"] == "leave":
            if known is not None:
                known["members"].pop(member, None)
            self._here(room, origin, member, False)
        elif self._locals_in(room):
            # A message is heard only from a member already vouched for: one
            # whose node never said who it is (no SUB, no join) is not heard.
            cred = known["members"].get(member) if known else None
            if known is not None and member in known["members"] and \
                    await self._vet(origin, member, cred):
                self._tell_locals(room, Event("message", room, member, origin, data, cred))
        header["hops"] = hops + 1
        await self._forward_pub(header, data, came_from=link.peer_id)
