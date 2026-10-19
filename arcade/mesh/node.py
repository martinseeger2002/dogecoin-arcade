"""A node's place in the mesh: its peers, the rooms its players are in, and the forwarding.

**Finding peers.** A node starts from whatever addresses it is handed (its own
config, and later the chain) and learns more from the peers it reaches: each
link swaps the addresses its side has actually connected to. It keeps a few
outbound links up at all times and accepts inbound ones if it listens. A node
behind a router never listens and still takes part fully -- it dials out, and
everything it needs arrives over the links it made.

**Meeting halfway, over IPv6.** Two nodes that are both behind routers cannot
dial each other the ordinary way: each router drops the other's first packet.
Over IPv6 there is no address translation, only the router's firewall, and a
firewall lets in the answer to a connection its own side opened. So nodes meet:
every MEET_EVERY seconds, on the same wall-clock beat everywhere, each node dials
every IPv6 peer it is not linked to, from its own mesh port to theirs. Both
routers see an outgoing connection, both let the other's packet in, and the two
dials become one TCP connection (a simultaneous open), or one of them lands on
the other's listener if that router lets it in anyway. Either way both sides
take the handshake's dialer part by one rule -- the lower node id dials -- so
nothing new crosses the wire and an IPv4 dial is untouched. No server, no
relay, no router setting (2026-10-12, Starlink: the router cannot be changed).

**Rooms without a server.** A node whose players are in a room says so to the
whole mesh: a signed SUB naming its rooms -- as 8-byte fingerprints, not names, and
nothing about who is in them -- sent when that set changes and again every
SUB_EVERY seconds. SUBs flood (each node passes on what it has not seen), and the
path a node's SUB arrived by is the path that node's room traffic is sent back
along. So a message goes only towards nodes that want it, nodes in between forward
without being in the room, and when a node goes quiet its SUBs stop and its routes
expire on their own. Nothing has to be torn down by anyone.

**Who is in a room travels in the room** (mesh v2, 2026-10-12: "tens of thousands of
players and hundreds of nodes"). Version 1 flooded every member and certificate of
every room to every node every 3 s: 155 KB/s per node with 20 nodes and nobody
playing, and it fell over at 40. Now each node with players in a room sends a HERE
into that room every HERE_EVERY seconds -- the names of its players there, and
their certificates only every HERE_FULL beats or when a node new to the room
appears -- routed like any room message, so only nodes in the room carry it. A
member whose node stops saying HERE is gone after IDLE seconds.

**The fastest links win.** Every ping carries a number and comes back as a pong, so
each link knows its round-trip time. Every TUNE_EVERY seconds a node that already
has its outbound links tries one more peer; once the trial has a time, the slowest
outbound link is dropped if the newcomer beat it clearly. Rooms do not notice: their
routes are found again over the links that remain (2026-10-12, the operator: "choose the
one with the fastest ping time").

**Each copy says whom it is for.** A room message goes towards every node with players
in the room, each along the path that node's SUB came by. Sent blindly, the paths
overlap and a node hears the same message from several neighbours (84% of arrivals
were copies, measured). So every copy carries "d": the short ids of the nodes it is
for, split by next hop, and a node passes each on only towards those -- one copy per
path. "d" is outside the signature, like "hops": it steers, it cannot change a word.

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
import hashlib
import ipaddress
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

from . import wsock
from .link import Link, LinkError, handshake

log = logging.getLogger("arcade.mesh")
GUESTLIKE = lambda m: str(m).startswith("guest-")   # noqa: E731 -- a guest has no certificate to wait for


def room_id(room: str) -> str:
    """A room's fingerprint in a SUB: 8 bytes of BLAKE2b, as hex."""
    return hashlib.blake2b(room.encode("utf-8"), digest_size=8).hexdigest()


def is_ws(host: str) -> bool:
    return str(host).startswith(("wss://", "ws://"))


def is_v6(host: str) -> bool:
    try:
        return ipaddress.ip_address(str(host).strip("[]")).version == 6
    except ValueError:
        return False

MAX_PAYLOAD = 512           # bytes in one room message
MAX_ROOM = 128              # characters in a room name
MAX_MEMBER = 64             # characters in a member id
MAX_ROOMS_PER_NODE = 2048      # rooms one node's players may be in (fingerprints in one SUB)
MAX_MEMBERS_PER_ROOM = 256  # per node, in one SUB
RATE = 5.0                  # messages per second per member, sustained
BURST = 10                  # messages a member may send at once
FORWARD_SLACK = 2.0         # forwarders allow this multiple before dropping
IDLE = 10.0                 # seconds before a silent member or node is gone
#: How long one of THIS node's own members keeps its seat with its page's stream gone quiet.
#: A phone or Wi-Fi drops every stream at once for a few seconds and the browser reconnects
#: on the same token; at IDLE (10 s) that cost the seat -- everyone saw the player leave and
#: come back, and its game had to start over (2026-10-13, @gx1/@apple). A page that really
#: went away says leave; one that vanished without a word is gone after this.
LOCAL_GRACE = 30.0
SUB_EVERY = 30.0            # how often a node re-announces its rooms (it also does on every change)
HERE_EVERY = 8.0            # how often a node says, in each of its rooms, who of its players is there
HERE_FULL = 6               # every so many HEREs carry the certificates (or at once, for a newcomer)
MAX_HERE = 60 * 1024        # bytes of one HERE (MAX_MEMBERS_PER_ROOM certificates fit)
SHORT = 8                   # hex characters of a node id in a copy's "d" list
SKEW = 60.0                 # seconds of clock difference tolerated on a message
MAX_HOPS = 16
OUTBOUND_TARGET = 6          # 2026-10-12: 4 -> 6, more paths for hundreds of nodes
DIAL_EVERY = 1.0            # how often a node tops up its outbound links
MEET_EVERY = 15.0           # the wall-clock beat IPv6 peers meet on (seconds)
MEET_WAIT = 9.0             # how long one meeting dial keeps knocking (SYN retries at 1, 3, 7 s)
MEET_MAX = 32               # IPv6 peers met per beat
TUNE_EVERY = 300.0          # how often a node tries one more peer to see if it is faster
TUNE_SETTLE = 40.0          # how long the trial link gets to show its round-trip time
TUNE_MARGIN = 0.8           # a newcomer must be this much faster than the slowest link to replace it
MAX_INBOUND = 32             # the default; a server takes more (MeshNode(max_inbound=...), --mesh-inbound)
PING_EVERY = 15.0
DEAD_AFTER = 45.0
SEEN_MAX = 50_000


class MeshError(ValueError):
    """A request the mesh refuses: too big, too fast, not joined, malformed."""


def _canon(header: dict) -> bytes:
    return json.dumps(header, separators=(",", ":"), sort_keys=True).encode()


def _signed_part(header: dict) -> dict:
    # "hops" and "d" (whom a copy is for) change on the way; everything else is signed.
    return {k: v for k, v in header.items() if k not in ("sig", "hops", "d")}


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
                 listen_host: str | None = None, listen_port: int | None = None,
                 peers: list[tuple[str, int]] | None = None,
                 outbound_target: int = OUTBOUND_TARGET,
                 max_inbound: int = MAX_INBOUND,
                 ws_port: int | None = None,
                 web_port: int | None = None,
                 vouch: Callable[[str, str, dict | None], Awaitable[bool]] | None = None):
        self.key = signing_key
        self.node_id = bytes(signing_key.verify_key).hex()
        self.network = network
        self.listen_host, self.listen_port = listen_host, listen_port
        self.outbound_target = outbound_target
        self.max_inbound = max(1, int(max_inbound))
        # A local WebSocket port for a tunnel to point at (wsock.py); None: no WebSocket side.
        self.ws_port = ws_port
        self.web_port = web_port                  # the node's web app: the tunnel carries its /realtime/ routes
        self._ws_server = None
        self._accepting = 0                       # inbound handshakes in progress
        self.extensions: list = []                # e.g. corelink.CoreBridge: told of links, given its frames
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
        # room id -> origin node -> {"via": peer id or None, "until": expiry}: where to send a room's traffic
        self.routes: dict[str, dict[str, dict]] = {}
        # room -> origin node -> {"members": {member: cred}, "until": expiry}: who is where, from HEREs
        self.remote: dict[str, dict[str, dict]] = {}
        self._here_due: set[str] = set()           # rooms whose next HERE carries certificates
        self._here_soon: asyncio.Task | None = None
        self._here_beat = 0
        self._sub_sent: frozenset = frozenset()     # the room ids last announced
        self._last_sub: OrderedDict[str, dict] = OrderedDict()   # origin -> its latest SUB, for a new neighbour
        #: origin -> when (this machine's clock) a fresh, verified SUB from it last arrived.
        #: Every node floods one every SUB_EVERY seconds, rooms or none, so this is the
        #: mesh-wide heartbeat (2026-10-13: orders show while their lister is online).
        self._heard: dict[str, float] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        # room -> {(origin, member)} this node's members have been told are there
        self._present: dict[str, set[tuple[str, str]]] = {}
        self._fwd_buckets: dict[tuple[str, str], _Bucket] = {}
        self._sub_buckets: dict[str, _Bucket] = {}
        self._dialbacks: dict[str, asyncio.Future] = {}
        self._dialed_back: dict[str, float] = {}
        self._seq = 0
        # This node's own IPv6 address, the one it announces: meeting dials leave
        # from it, so the far router sees the address it was told about.
        self.meet_from: str | None = None
        self._server: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self._stopping = False
        self._probing = False                  # one outbound link over target while a trial runs

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self.listen_port is not None:
            self._server = await asyncio.start_server(self._accept, self.listen_host,
                                                      self.listen_port, reuse_port=True)
            if self.listen_port == 0:   # tests ask for any free port
                self.listen_port = self._server.sockets[0].getsockname()[1]
        if self.ws_port is not None:
            self._ws_server = await wsock.serve(self._accept, "127.0.0.1", self.ws_port, http_port=self.web_port)
            if self.ws_port == 0:
                self.ws_port = self._ws_server.sockets[0].getsockname()[1]
        self._spawn(self._dialer())
        self._spawn(self._announcer())
        if self.listen_port is not None:
            self._spawn(self._meeter())
        self._spawn(self._tuner())
        self._spawn(self._rebalancer())

    async def stop(self) -> None:
        self._stopping = True
        if self._server is not None:
            self._server.close()
        if self._ws_server is not None:
            self._ws_server.close()
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

    def add_extension(self, ext) -> None:
        """Something riding on the links (corelink.CoreBridge, coreshare.CoreShare): link_up(link),
        link_down(peer_id), on_frame(link, header, blob) for frames of its `kinds` ("cs" when it names
        none), rebalance() every minute."""
        ext.node = self
        self.extensions.append(ext)

    async def _rebalancer(self) -> None:
        while not self._stopping:
            await asyncio.sleep(60)
            for ext in list(self.extensions):
                try:
                    await ext.rebalance()
                except Exception:                  # noqa: BLE001
                    log.exception("mesh extension")

    def _outbound(self) -> int:
        return sum(1 for link in self.links.values() if link.outbound)

    async def _dialer(self) -> None:
        while not self._stopping:
            try:
                if self._outbound() < self.outbound_target + (1 if self._probing else 0):
                    now = time.monotonic()
                    choices = [a for a, e in self.addrs.items()
                               if e["next"] <= now and e["id"] != self.node_id
                               and e["id"] not in self.links
                               and (not self._meets(a, e) or e.get("plain"))]
                    random.shuffle(choices)
                    # Direct addresses first: a tunnel (wss://) is for when nothing else reaches a node.
                    have = {e["id"] for a, e in self.addrs.items() if not is_ws(a[0]) and e["id"] and e["fail"] < 3}
                    choices = [a for a in choices if not is_ws(a[0])] + \
                              [a for a in choices if is_ws(a[0]) and self.addrs[a]["id"] not in have]
                    for addr in choices[: self.outbound_target + (1 if self._probing else 0) - self._outbound()]:
                        self._spawn(self._dial(addr))
            except Exception:                      # noqa: BLE001 -- the loop must not die
                log.exception("mesh dialer")
            await asyncio.sleep(DIAL_EVERY)

    async def _dial(self, addr: tuple[str, int]) -> None:
        entry = self.addrs[addr]
        entry["next"] = time.monotonic() + 30           # not again while this one runs
        writer = None
        try:
            if is_ws(addr[0]):                                 # a node behind a tunnel (wsock.py)
                reader, writer = await asyncio.wait_for(wsock.connect(addr[0]), 12.0)
            else:
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
        fresh = not entry["ok"]
        entry.update(id=link.peer_id, ok=time.time(), fail=0, next=time.monotonic() + 30)
        if fresh:
            self._tell_addr(addr, link.peer_id)
        await self._run_link(link)

    def _meets(self, addr: tuple[str, int], entry: dict) -> bool:
        """Is this a peer this node meets halfway rather than simply dials?"""
        return self.listen_port is not None and is_v6(addr[0]) and not entry.get("plain")

    async def _meeter(self) -> None:
        """On every beat of the wall clock, dial each IPv6 peer not linked yet,
        from this node's own mesh port (see the module notes)."""
        while not self._stopping:
            await asyncio.sleep(MEET_EVERY - time.time() % MEET_EVERY)
            try:
                due = [a for a, e in self.addrs.items()
                       if e["id"] and e["id"] != self.node_id and e["id"] not in self.links
                       and self._meets(a, e)]
                random.shuffle(due)
                for addr in due[:MEET_MAX]:
                    self._spawn(self._meet(addr))
            except Exception:                      # noqa: BLE001 -- the loop must not die
                log.exception("mesh meeter")

    async def _meet(self, addr: tuple[str, int]) -> None:
        import socket
        entry = self.addrs[addr]
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        writer = None
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind((self.meet_from or "::", self.listen_port))
            sock.setblocking(False)
            await asyncio.wait_for(loop.sock_connect(sock, (addr[0], addr[1], 0, 0)), MEET_WAIT)
        except (OSError, asyncio.TimeoutError) as exc:
            sock.close()
            # A meeting nobody answers is a failed direct address. The dialer turns to a node's
            # tunnel (wss://) only once its direct addresses have failed a few times, and without
            # this count an IPv6 address behind a closed firewall kept the tunnel from ever being
            # tried (2026-10-19: node 3 never linked to a node announcing both).
            entry["fail"] += 1
            log.debug("mesh meet %s: no answer (%s)", addr, exc)
            return
        try:
            reader, writer = await asyncio.open_connection(sock=sock)
            link = await handshake(reader, writer, self.key, self.network, True, self.listen_port,
                                   initiator=self.node_id < entry["id"])
        except (LinkError, OSError) as exc:
            # It answered and the handshake failed: most likely a node from before
            # meetings, which always takes the listener's part. Dial it plainly from now on.
            entry["met_bad"] = entry.get("met_bad", 0) + 1
            if entry["met_bad"] >= 2:
                entry["plain"] = True
            if writer is not None:
                writer.close()
            else:
                sock.close()
            log.debug("mesh meet %s refused: %s", addr, exc)
            return
        fresh = not entry["ok"]
        entry.update(ok=time.time(), fail=0, met_bad=0)
        if fresh:
            self._tell_addr(addr, link.peer_id)
        log.info("mesh: met %s over IPv6", entry["id"][:12])
        await self._run_link(link)

    async def _accept(self, reader, writer) -> None:
        # Handshakes in progress count too: nodes connecting at once all passed a check
        # that counted only finished links (a hub set to 2 took 4).
        inbound = sum(1 for link in self.links.values() if not link.outbound) + self._accepting
        if inbound >= self.max_inbound or self._stopping:
            writer.close()
            await writer.wait_closed()
            return
        self._accepting += 1
        try:
            await self._accept_one(reader, writer)
        finally:
            self._accepting = max(0, self._accepting - 1)

    async def _accept_one(self, reader, writer) -> None:
        initiator = None
        peer = writer.get_extra_info("peername") or ()
        if len(peer) >= 2:
            met = self.addrs.get((str(peer[0]), int(peer[1])))
            if met and met.get("id") and self._meets((str(peer[0]), int(peer[1])), met):
                initiator = self.node_id < met["id"]     # a meeting: the lower id dials
        try:
            link = await handshake(reader, writer, self.key, self.network, False, self.listen_port,
                                   initiator=initiator)
        except LinkError as exc:
            log.debug("mesh inbound refused: %s", exc)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            return
        if link.peer_listen and link.host not in ("ws", "?"):
            # Where it says it can be reached. Kept as a lead, passed on to
            # others only once this node has reached it itself (_dial sets ok).
            # (Not for a link through a tunnel: there is no address to learn there.)
            self.add_address(link.host, link.peer_listen, link.peer_id)
        self._accepting = max(0, self._accepting - 1)        # a link now: counted among the links
        try:
            await self._run_link(link)
        finally:
            self._accepting += 1                                  # the caller takes it off again

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
            await self._send_table(link)
            for ext in list(self.extensions):
                await ext.link_up(link)
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
            for ext in list(self.extensions):
                try:
                    await ext.link_down(link.peer_id)
                    await ext.rebalance()
                except Exception:                  # noqa: BLE001
                    log.exception("mesh extension")

    async def _pinger(self, link: Link) -> None:
        link.rtt, link.pings = None, {}       # seconds, smoothed; ping number -> sent at
        first = True
        try:
            while True:
                await asyncio.sleep(1.0 if first else PING_EVERY)
                first = False
                n = secrets.token_hex(4)
                link.pings[n] = time.monotonic()
                if len(link.pings) > 8:
                    link.pings.pop(next(iter(link.pings)))
                await link.send({"t": "ping", "n": n})
        except (LinkError, ConnectionError, OSError):
            pass

    def _pong(self, link: Link, n: str) -> None:
        sent = getattr(link, "pings", {}).pop(n, None)
        if sent is None:
            return
        rtt = time.monotonic() - sent
        link.rtt = rtt if link.rtt is None else 0.7 * link.rtt + 0.3 * rtt

    def last_seen(self, node_id: str) -> float | None:
        """When `node_id` was last heard from (unix time): now if linked directly, else
        its last heartbeat SUB; None if never. A node is online if this is within
        3.5 x SUB_EVERY -- the same window a room route lives (`until`)."""
        if node_id == self.node_id or node_id in self.links:
            return time.time()
        return self._heard.get(node_id)

    def room_live(self, room: str) -> bool:
        """Is anybody in `room` right now: here, or on a node whose SUB still stands?"""
        if any(r == room for (r, _m) in list(self.local)):
            return True
        now = time.monotonic()
        return any(v.get("until", 0) > now for v in list(self.routes.get(room_id(room), {}).values()))

    def online(self, node_id: str, now: float | None = None) -> bool:
        seen = self.last_seen(node_id)
        return seen is not None and (now or time.time()) - seen <= 3.5 * SUB_EVERY

    def rtts(self) -> dict[str, float | None]:
        """Each linked node's round-trip time in milliseconds (None until measured)."""
        return {pid: (round(l.rtt * 1000, 1) if getattr(l, "rtt", None) is not None else None)
                for pid, l in self.links.items()}

    async def _tuner(self) -> None:
        while not self._stopping:
            await asyncio.sleep(TUNE_EVERY)
            try:
                await self.tune_once()
            except Exception:                      # noqa: BLE001 -- the loop must not die
                log.exception("mesh tuner")

    async def tune_once(self, settle: float | None = None) -> str | None:
        """Try one more peer; drop the slowest outbound link if the trial beat it.
        Returns the id of the link dropped, if any."""
        if self._outbound() < self.outbound_target:
            return None
        spare = [a for a, e in self.addrs.items()
                 if e["id"] != self.node_id and e["id"] not in self.links and e["next"] <= time.monotonic()]
        if not spare:
            return None
        before = {pid for pid, l in self.links.items() if l.outbound}
        self._probing = True
        try:
            await asyncio.sleep(TUNE_SETTLE if settle is None else settle)
        finally:
            self._probing = False
        out = [l for l in self.links.values() if l.outbound]
        if len(out) <= self.outbound_target or any(getattr(l, "rtt", None) is None for l in out):
            return None
        new = [l for l in out if l.peer_id not in before]
        slowest = max(out, key=lambda l: l.rtt)
        if not new or slowest in new or min(l.rtt for l in new) > slowest.rtt * TUNE_MARGIN:
            # The trial was no better: let it go again instead.
            drop = max(new, key=lambda l: l.rtt) if new else slowest
        else:
            drop = slowest
        log.info("mesh: dropping the slower link to %s (%.0f ms)", drop.peer_id[:12], drop.rtt * 1000)
        await drop.close()
        if self.links.get(drop.peer_id) is drop:     # gone now, not when its reader next wakes
            del self.links[drop.peer_id]
        return drop.peer_id

    async def _send_addrs(self, link: Link) -> None:
        good = [{"h": h, "p": p, "id": e["id"]} for (h, p), e in self.addrs.items()
                if e["ok"] and e["id"] and e["id"] != link.peer_id][:50]
        if good:
            await link.send({"t": "addrs", "a": good})

    def _tell_addr(self, addr: tuple[str, int], node_id: str) -> None:
        """An address just reached for the first time, passed on to the links already up:
        _send_addrs runs only as a link starts, and a link that came up while this dial
        was still under way would never hear of it."""
        async def tell(link: Link) -> None:
            try:
                await link.send({"t": "addrs", "a": [{"h": addr[0], "p": addr[1], "id": node_id}]})
            except (LinkError, ConnectionError, OSError):
                pass
        for link in list(self.links.values()):
            if link.peer_id != node_id:
                self._spawn(tell(link))

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
        await self._send_own_sub(if_changed=True)        # first player here: say so before speaking
        await self._publish(room, member, "join", b"", cred=cred)
        self._due(room)
        return entry.queue

    async def leave(self, room: str, member: str) -> None:
        if self.local.pop((room, member), None) is None:
            return
        self._announce_local(room, member, "leave")
        await self._publish(room, member, "leave", b"")
        await self._send_own_sub(if_changed=True)

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

    async def _on_here(self, room: str, origin: str, data: bytes, known: dict | None) -> None:
        try:
            body = json.loads(data)
        except ValueError:
            return
        if not isinstance(body, list):
            return
        names: dict[str, dict | None] = {}
        full = False
        for item in body[:MAX_MEMBERS_PER_ROOM]:
            if not isinstance(item, list) or not item:
                continue
            try:
                m = check_member(item[0])
            except MeshError:
                continue
            if len(item) > 1:
                full = True
                names[m] = item[1] if isinstance(item[1], dict) else None
            else:
                names[m] = (known or {}).get("members", {}).get(m) if known else None
        if known is None:
            known = self.remote.setdefault(room, {})[origin] = {"members": {}, "until": 0.0}
            if self._locals_in(room):
                self._here_due.add(room)             # a node new to us: tell it who we are, in full
        before = known["members"]
        # A names-only HERE keeps the certificates already known; a member never seen with one waits for it.
        known["members"] = {m: c for m, c in names.items() if full or m in before or GUESTLIKE(m)}
        known["until"] = time.monotonic() + max(IDLE, 3 * HERE_EVERY)
        for m in set(before) - set(known["members"]):
            self._here(room, origin, m, False)
        if self._locals_in(room):
            for m, c in known["members"].items():
                if (origin, m) not in self._present.get(room, ()) and await self._vet(origin, m, c):
                    self._here(room, origin, m, True, c)

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
                        if c is not None or self.vouch is None or GUESTLIKE(m)
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
        wanted = header.get("d")
        wanted = {str(x) for x in wanted[:4096]} if isinstance(wanted, list) else None
        now = time.monotonic()
        groups: dict[str, list[str]] = {}
        for origin, info in self.routes.get(room_id(room), {}).items():
            if origin == header["o"] or origin == self.node_id or not info["via"] or info["until"] <= now:
                continue
            short = origin[:SHORT]
            if wanted is not None and short not in wanted:
                continue                                 # another copy is taking it there
            if info["via"] == came_from:
                # Our path to it runs back the way this copy came: the tables disagree for a moment
                # (a SUB wave half through). Never send it back, and never spray it round either --
                # a copy re-sent on every link multiplies (a 40-node run choked on exactly that).
                # The next HERE, after the wave, takes the right path.
                continue
            groups.setdefault(info["via"], []).append(short)
        for peer_id, shorts in groups.items():
            link = self.links.get(peer_id)
            if link is not None:
                try:
                    await link.send(dict(header, d=shorts), data)
                except (LinkError, ConnectionError, OSError):
                    pass

    def _own_rooms(self) -> set[str]:
        return {r for r, _ in self.local}

    async def _expire_locals(self) -> None:
        now = time.monotonic()
        for (room, member), entry in list(self.local.items()):
            if now - entry.touched > max(IDLE, LOCAL_GRACE):   # its page went away without saying
                del self.local[(room, member)]
                self._announce_local(room, member, "leave")
                await self._publish(room, member, "leave", b"")

    async def _send_own_sub(self, only: Link | None = None, if_changed: bool = False) -> None:
        ids = frozenset(room_id(r) for r in self._own_rooms())
        if if_changed and ids == self._sub_sent and only is None:
            return
        self._sub_sent = ids
        header = self._sign({"t": "sub", "o": self.node_id, "q": self._next_seq(),
                             "rooms": sorted(ids), "hops": 0})
        self._seen[f"{self.node_id}:{header['q']}"] = None
        links = [only] if only is not None else list(self.links.values())
        for link in links:
            try:
                await link.send(header)
            except (LinkError, ConnectionError, OSError):
                pass

    def _due(self, room: str) -> None:
        """A full HERE for this room, within a quarter of a second (a newcomer should not wait a beat)."""
        self._here_due.add(room)
        if self._here_soon is None or self._here_soon.done():
            async def soon():
                await asyncio.sleep(0.25)
                await self._send_heres(only_due=True)
            self._here_soon = self._spawn(soon())

    async def _send_heres(self, only_due: bool = False) -> None:
        """In each room this node has players in: who they are (see the module notes)."""
        if not only_due:
            self._here_beat += 1
        rooms: dict[str, dict[str, dict | None]] = {}
        for (room, member), entry in list(self.local.items()):
            rooms.setdefault(room, {})[member] = entry.cred
        due, self._here_due = self._here_due, set()
        if only_due:
            rooms = {r: m for r, m in rooms.items() if r in due}
        for room, members in rooms.items():
            full = room in due or self._here_beat % HERE_FULL == 0
            body = [[m, c] if full else [m] for m, c in sorted(members.items())]
            data = json.dumps(body, separators=(",", ":")).encode()
            if len(data) > MAX_HERE:                  # cannot happen within the limits; never send a broken one
                continue
            await self._publish(room, "-", "here", data)

    async def _announcer(self) -> None:
        last_sub = 0.0
        while not self._stopping:
            await asyncio.sleep(HERE_EVERY)
            try:
                await self._expire_locals()
                if time.monotonic() - last_sub >= SUB_EVERY:
                    await self._send_own_sub()
                    last_sub = time.monotonic()
                else:
                    await self._send_own_sub(if_changed=True)
                await self._send_heres()
                self._expire()
            except Exception:                      # noqa: BLE001
                log.exception("mesh announcer")

    def _expire(self) -> None:
        now = time.monotonic()
        for rid in list(self.routes):
            for origin, info in list(self.routes[rid].items()):
                if info["until"] <= now:
                    del self.routes[rid][origin]
            if not self.routes[rid]:
                del self.routes[rid]
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
        for room in self.routes.values():
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
            if isinstance(header.get("n"), str):
                await link.send({"t": "pong", "n": header["n"][:16]})
            return
        if kind == "pong":
            self._pong(link, str(header.get("n", "")))
            return
        # Extensions name the frame kinds they ride on: Core peering "cs" (corelink), shared Cores
        # "cr" (coreshare).
        riders = [e for e in self.extensions if kind in getattr(e, "kinds", ("cs",)) and hasattr(e, "on_frame")]
        if riders:
            for ext in riders:
                await ext.on_frame(link, header, blob)
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
        if origin == self.node_id or not isinstance(rooms, list) or not isinstance(hops, int):
            return
        if hops >= MAX_HOPS or not self._fresh(header) or not self._verify(header):
            return
        self._mark(header)
        # A join or leave sends a SUB at once, on top of the regular ones, so a
        # burst is normal; a node announcing in a loop is not.
        bucket = self._sub_buckets.setdefault(origin, _Bucket(4.0, 10))
        if not bucket.take():
            return
        if len(self._sub_buckets) > 10_000:
            self._sub_buckets.clear()
        now = time.monotonic()
        ids = {r for r in rooms[:MAX_ROOMS_PER_NODE] if isinstance(r, str) and len(r) == 16}
        mine = {room_id(r): r for r in self._own_rooms()}
        for rid in ids & set(mine):                         # it is newly in a room we are in: tell it who we are
            if origin not in self.routes.get(rid, {}):
                self._due(mine[rid])
        for rid in list(self.routes):                       # rooms this origin has left
            if origin in self.routes[rid] and rid not in ids:
                del self.routes[rid][origin]
                if not self.routes[rid]:
                    del self.routes[rid]
        until = now + max(3.5 * SUB_EVERY, IDLE)
        for rid in ids:
            # The path this SUB came by FIRST is the path back (only a first arrival gets here).
            # Every node takes it from the same wave, so the paths to `origin` form one tree;
            # keeping an older path instead let two nodes each point at the other.
            self.routes.setdefault(rid, {})[origin] = {"via": link.peer_id, "until": until}
        self._heard[origin] = time.time()
        if len(self._heard) > 20_000:
            for old in sorted(self._heard, key=self._heard.get)[:10_000]:
                del self._heard[old]
        header["hops"] = hops + 1
        self._last_sub[origin] = dict(header)
        self._last_sub.move_to_end(origin)
        while len(self._last_sub) > 5000:
            self._last_sub.popitem(last=False)
        for peer_id, other in list(self.links.items()):
            if peer_id != link.peer_id:
                try:
                    await other.send(header)
                except (LinkError, ConnectionError, OSError):
                    pass

    async def _send_table(self, link: Link) -> None:
        """A new neighbour hears every node's latest SUB at once, not on the next refresh.
        Only those still within SKEW: an older one would be refused as a replay anyway."""
        now = time.time()
        for origin, header in list(self._last_sub.items()):
            if origin == link.peer_id or abs(header.get("q", 0) / 1_000_000 - now) > SKEW - 5:
                continue
            try:
                await link.send(header)
            except (LinkError, ConnectionError, OSError):
                return

    async def _on_pub(self, link: Link, header: dict, data: bytes) -> None:
        origin = str(header.get("o", ""))
        hops = header.get("hops")
        try:
            room = check_room(header.get("r"))
            member = "-" if header.get("k") == "here" else check_member(header.get("m"))
        except MeshError:
            return
        if origin == self.node_id or not isinstance(hops, int) or hops >= MAX_HOPS:
            return
        kind = header.get("k")
        if kind not in ("msg", "join", "leave", "here") or len(data) > (MAX_HERE if kind == "here" else MAX_PAYLOAD):
            return
        if f"{origin}:{header.get('q')}" in self._seen:
            # Heard already, by another path: it may still be on its way to nodes this copy
            # is for and the first was not, so pass it on towards those -- and only those.
            if isinstance(header.get("d"), list):
                header["hops"] = hops + 1
                await self._forward_pub(header, data, came_from=link.peer_id)
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
        if header["k"] == "here":
            await self._on_here(room, origin, data, known)
        elif header["k"] == "join":
            cred = header.get("c") if isinstance(header.get("c"), dict) else None
            if known is not None:
                known["members"][member] = cred
            else:
                # A node new to this room: everybody here tells it who they are, in full,
                # on their next beat. It expires like any other if it goes quiet.
                self.remote.setdefault(room, {})[origin] = {
                    "members": {member: cred}, "until": time.monotonic() + IDLE}
                if self._locals_in(room):
                    self._due(room)
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
