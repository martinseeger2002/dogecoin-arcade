"""UDP alongside TCP on a mesh link (2026-10-31, the operator: "Add UDP to the mesh along TCP for a backup").

The punch probe (punch.py) showed two arcades behind home NAT reach each other directly. This carries a link's frames
that way. A link is still found and proven the way it always was, over TCP, a WebSocket or a tunnel; that connection
stays open, kept alive every PING_EVERY, as the backup. Then the two nodes punch, and while the UDP path lives every
frame of the link goes over it. If it goes quiet, what it had not delivered is sent again over the backup, and new
frames follow: nothing above the link notices, in either direction.

**One socket.** A node keeps one UDP socket for all its paths, so its NAT keeps one outside port. Packets find their
path by a connection id.

**Sealed.** The keys come from the link's own keys, which the handshake proved, and a fresh nonce per attempt. Every
packet is XChaCha20-Poly1305 under a key per direction, with a packet number that is never used twice: a resent chunk
is a new packet, and a repeated packet is dropped. Nothing but the connection id and the packet number shows. Whoever
forges a packet has spent a decryption; a path only believes packets that open.

**Reliable and in order,** because extensions (Core sharing, index chunks, LLM streams) count on a link being so:
frames are cut into chunks, each with a sequence number, acknowledged cumulatively plus a 64-chunk bitmap, resent after
a round-trip-based timeout, or sooner when later chunks arrived. The window grows and halves on loss, as TCP's does,
so a path shares a Starlink dish fairly with everything else in the house.

**The hand-over is exact.** `go` is the last TCP frame before a sender moves to UDP, and the receiver holds UDP frames
until it reads `go`, so nothing overtakes. Falling back, the sender sends every chunk not known to have arrived as
`late` over TCP (their sequence numbers remove any duplicate), then `end` with the next number, and only then new
frames. The receiver finishes the path up to that number before it reads on.

Frames "ud": {"op": "try", "n", "a"} asks a linked node to open a path; {"op": "ready", "n", "a"} is its yes;
{"op": "no", "n", "why"} its no; {"op": "home", "n", "a"} the asker's home address, once the answer proved one NAT; "go", "late" ("s", "m", chunk as blob) and "end" ("n") as above. "ka" (node.py) keeps
the backup alive. An arcade without this code ignores them all, and its links stay on TCP.

The addresses in "a" are the ones STUN saw. A home-network address is added, and dialed, only when the peer is on
this NAT (or mesh_lan_dial is "on"): see punch.dialable. This is the node's one UDP socket, so a private address it
should not have aimed at is a packet to that address on the LAN here.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import struct
import time
from collections import deque

import nacl.bindings as sodium
import nacl.exceptions

from . import punch as punchlib
from .link import MAX_FRAME, LinkError, PathDown

log = logging.getLogger("arcade.mesh.udp")

MAGIC = b"AUDP1"
HEAD = struct.Struct(">5s8sQ")          # magic, connection id, packet number
CHUNK = 1120                            # bytes of frame per packet: 1200 with headers, safe on any path
INITIAL_WINDOW = 16                     # chunks in flight at the start
MAX_WINDOW = 2048                       # about 2.3 MB in flight
MAX_QUEUE = 4096                        # chunks waiting to go before a sender is made to wait
MAX_RECEIVED = 512                      # whole frames waiting to be handled before chunks are refused
KNOCK_EVERY = 0.2                       # seconds between hellos while punching
KNOCK_FOR = 10.0                        # how long a side keeps knocking
SPRAY_EACH = 8                          # guesses per knock at a NAT that picks a new port per destination
HARD_SOCKETS = 256                      # sockets the hard side opens; with 800 guesses, about 96% that one hits
KEEP_EVERY = 1.5                        # a packet at least this often keeps the NAT's door open
DEAD_WITH_DATA = 6.0                    # silent this long with data on the way: fall back now
DEAD_IDLE = 20.0                        # silent this long with nothing to send: fall back too
MAX_RESENDS = 24                        # one chunk sent this many times: the path is not working
RETRY_FIRST = 60.0                      # seconds before trying a failed peer again; doubles
RETRY_MAX = 3600.0
TRY_AFTER = 3.0                         # seconds after a link comes up
ANSWER_WAIT = 15.0                      # seconds a node has to say ready
MAP_KEEP = 60.0                         # seconds one STUN answer is trusted

_HELLO, _ACK, _DATA = b"H", b"A", b"D"
_ACK_NEXT = struct.Struct(">Q")         # next chunk wanted, then a bitmap of the MAP_BITS after it
MAP_BITS = 1024
_DATA_HEAD = struct.Struct(">QB")       # chunk number, more of this frame to come


def _derive(link_key: bytes, nonce: bytes) -> bytes:
    return hashlib.blake2b(link_key + nonce, digest_size=32, person=b"arcm-udp-path").digest()


def _cid(key: bytes) -> bytes:
    return hashlib.blake2b(key, digest_size=8, person=b"arcm-udp-cid").digest()


class _Out:
    __slots__ = ("seq", "more", "chunk", "at", "sends", "sacked")

    def __init__(self, seq: int, more: int, chunk: bytes):
        self.seq, self.more, self.chunk = seq, more, chunk
        self.at, self.sends, self.sacked = 0.0, 0, False


def _home(host) -> bool:
    return not punchlib.routable_public(host)


class Path:
    """One end of one UDP path. `sendto(bytes, addr)` puts a packet on the wire, `deliver(frame)` is awaited with each
    whole frame in order, `on_down(path, why)` is called once when it fails."""

    def __init__(self, key_out: bytes, key_in: bytes, sendto, deliver, on_down, *, on_confirmed=None, peer: str = ""):
        self.key_out, self.key_in = key_out, key_in
        self.cid_out, self.cid_in = _cid(key_out), _cid(key_in)
        self.sendto, self.deliver, self.on_down, self.on_confirmed = sendto, deliver, on_down, on_confirmed
        self.peer = peer
        self.targets: list[tuple[str, int]] = []
        self.knock_for = KNOCK_FOR
        self.fan: list = []                     # transports of a hard NAT's many sockets, while punching
        self.on_through = None
        self.spray_host: str | None = None      # the far NAT's address, to guess ports at
        self.addr: tuple[str, int] | None = None
        self.confirmed = False                  # the other side has heard this side
        self.dead = False
        self.why = ""
        self.started = self.last_heard = self.last_sent = time.monotonic()
        # sending
        self.pn = 0
        self.next_seq = 0
        self.queue: deque[_Out] = deque()
        self.flight: dict[int, _Out] = {}
        self.window = float(INITIAL_WINDOW)
        self.loss_until = 0.0
        self.srtt: float | None = None
        self.rttvar = 0.0
        self.top_sacked = -1
        # receiving
        self.seen_top = -1
        self.seen_bits = 0
        self.rx_next = 0
        self.rx_held: dict[int, tuple[int, bytes]] = {}
        self.parts: list[bytes] = []
        self.parts_size = 0
        self.frames: asyncio.Queue = asyncio.Queue()
        self.released = asyncio.Event()         # "go" read: frames may be delivered
        self.ack_due = 0
        self.ack_timer = None
        # waiting
        self.wake = asyncio.Event()
        self.room = asyncio.Event()
        self.room.set()
        self.progress = asyncio.Event()
        self.tasks: list[asyncio.Task] = []
        # counters, for Admin
        self.frames_out = self.frames_in = self.packets_out = self.packets_in = 0
        self.bytes_out = self.bytes_in = self.resends = self.bad = 0

    # ---------------------------------------------------------------- life

    def start(self, targets: list[tuple[str, int]]) -> None:
        self.targets = list(targets)
        self.started = self.last_heard = time.monotonic()
        loop = asyncio.get_running_loop()
        self.tasks = [loop.create_task(self._run()), loop.create_task(self._hand_up())]

    def aim(self, targets: list[tuple[str, int]]) -> None:
        """More addresses to knock at, for a side that learned them after it started."""
        self.targets += [t for t in targets if t not in self.targets]

    def down(self, why: str) -> None:
        if self.dead:
            return
        self.dead, self.why = True, why
        self.wake.set()
        self.room.set()
        self.progress.set()
        self.on_down(self, why)

    def close(self) -> None:
        self.dead = True
        self.wake.set()
        self.room.set()
        self.progress.set()
        if self.ack_timer is not None:
            self.ack_timer.cancel()
        for task in self.tasks:
            task.cancel()

    @property
    def usable(self) -> bool:
        return self.confirmed and not self.dead

    @property
    def rtt(self) -> float | None:
        return self.srtt

    # ---------------------------------------------------------------- packets

    def _emit(self, kind: bytes, body: bytes = b"", addr=None) -> None:
        addr = addr or self.addr
        if addr is None:
            return
        self._emit_to(kind + body, addr)

    def _emit_to(self, plain: bytes, addr, sendto=None) -> None:
        head = HEAD.pack(MAGIC, self.cid_out, self.pn)
        nonce = self.pn.to_bytes(24, "big")
        self.pn += 1
        packet = head + sodium.crypto_aead_xchacha20poly1305_ietf_encrypt(plain, head, nonce, self.key_out)
        self.last_sent = time.monotonic()
        self.packets_out += 1
        self.bytes_out += len(packet)
        try:
            (sendto or self.sendto)(packet, addr)
        except OSError:
            pass

    def received(self, data: bytes, addr, via=None) -> None:
        """A packet with this path's connection id. Only one that opens is believed."""
        if self.dead or len(data) < HEAD.size + 17:
            return
        pn = HEAD.unpack_from(data)[2]
        if not self._fresh(pn):
            return
        try:
            plain = sodium.crypto_aead_xchacha20poly1305_ietf_decrypt(
                data[HEAD.size:], data[:HEAD.size], pn.to_bytes(24, "big"), self.key_in)
        except nacl.exceptions.CryptoError:
            self.bad += 1
            return
        self._mark(pn)
        now = time.monotonic()
        if via is not None and self.fan:                # the socket that got through is the one the NAT keeps
            self.sendto = via.sendto
            keep, self.fan = via, []
            if self.on_through is not None:
                self.on_through(keep)
        # The first address that answers stays, unless it went quiet (the NAT moved) or a home-network one answers: two
        # arcades in one house meet through a router that loops back about as soon, and that road was 60 ms against 1 ms
        # on the LAN (2026-11-02, a test machine and .25). The packet opened with this path's key, so the address is believed.
        if self.addr is None or now - self.last_heard > 2.0 or (_home(addr[0]) and not _home(self.addr[0])):
            self.addr = (str(addr[0]), int(addr[1]))
        self.last_heard = now
        self.packets_in += 1
        self.bytes_in += len(data)
        kind, body = plain[:1], plain[1:]
        if kind == _DATA:
            self._on_data(body)
        elif kind == _ACK:
            self._on_ack(body, now)
        elif kind == _HELLO:
            self._send_ack()
        if kind in (_ACK, _DATA) and not self.confirmed:
            self.confirmed = True
            if self.on_confirmed is not None:
                self.on_confirmed(self)
        self.wake.set()

    def _fresh(self, pn: int) -> bool:
        if pn > self.seen_top:
            return True
        back = self.seen_top - pn
        return back < 1024 and not (self.seen_bits >> back) & 1

    def _mark(self, pn: int) -> None:
        if pn > self.seen_top:
            shift = pn - self.seen_top
            self.seen_bits = ((self.seen_bits << shift) | 1) & ((1 << 1024) - 1)
            self.seen_top = pn
        else:
            self.seen_bits |= 1 << (self.seen_top - pn)

    # ---------------------------------------------------------------- sending

    async def send_frame(self, frame: bytes) -> None:
        """Take a whole frame. From here it is this path's to deliver, or the backup's: a frame is queued whole, so a
        path that fails cannot leave half of one nowhere."""
        while len(self.queue) >= MAX_QUEUE and not self.dead:
            self.room.clear()
            await self.room.wait()
        if self.dead:
            raise PathDown(self.why)
        size = len(frame)
        pieces = max(1, (size + CHUNK - 1) // CHUNK)
        for i in range(pieces):
            self.queue.append(_Out(self.next_seq, 1 if i + 1 < pieces else 0, frame[i * CHUNK:(i + 1) * CHUNK]))
            self.next_seq += 1
        self.frames_out += 1
        self._push(time.monotonic())

    def _send_chunk(self, out: _Out, now: float) -> None:
        out.at = now
        out.sends += 1
        self._emit(_DATA, _DATA_HEAD.pack(out.seq, out.more) + out.chunk)

    def _push(self, now: float) -> None:
        while self.queue and len(self.flight) < int(self.window) and self.usable:
            out = self.queue.popleft()
            self.flight[out.seq] = out
            self._send_chunk(out, now)
        if len(self.queue) < MAX_QUEUE // 2:
            self.room.set()

    def _rto(self) -> float:
        if self.srtt is None:
            return 1.0
        return min(3.0, max(0.2, self.srtt + 4 * self.rttvar))

    def _lost(self, now: float) -> None:
        """One halving per round trip, however many chunks that trip lost."""
        if now >= self.loss_until:
            self.window = max(float(INITIAL_WINDOW), self.window / 2)
            self.loss_until = now + (self.srtt or 0.2)

    def _resend(self, now: float) -> None:
        rto = self._rto()
        for out in list(self.flight.values()):
            if out.sacked:
                continue
            # Later chunks arrived and this one did not, and it has had a whole round trip to.
            early = out.seq + 3 <= self.top_sacked and now - out.at > 1.5 * (self.srtt or 0.05)
            if now - out.at >= min(1.5, rto * 2 ** min(out.sends - 1, 4)) or early:
                if out.sends >= MAX_RESENDS:
                    self.down(f"chunk {out.seq} would not go through")
                    return
                self._lost(now)
                self.resends += 1
                self._send_chunk(out, now)

    def _on_ack(self, body: bytes, now: float) -> None:
        if len(body) != _ACK_NEXT.size + MAP_BITS // 8:
            return
        wanted = _ACK_NEXT.unpack_from(body)[0]
        bits = int.from_bytes(body[_ACK_NEXT.size:], "big")
        if wanted > self.next_seq:
            return
        done = [seq for seq in self.flight if seq < wanted]
        for seq in done:
            out = self.flight.pop(seq)
            if out.sends == 1:
                self._measure(now - out.at)
            self.window = min(MAX_WINDOW, self.window + (1.0 if self.window < 256 else 16.0 / self.window))
        i = 0
        while bits:
            step = (bits & -bits).bit_length() - 1
            i += step
            bits >>= step + 1
            out = self.flight.get(wanted + 1 + i)
            i += 1
            if out is not None:
                out.sacked = True
                self.top_sacked = max(self.top_sacked, out.seq)
        self._push(now)

    def _measure(self, sample: float) -> None:
        if self.srtt is None:
            self.srtt, self.rttvar = sample, sample / 2
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - sample)
            self.srtt = 0.875 * self.srtt + 0.125 * sample

    def unfinished(self) -> list[_Out]:
        """Every chunk the other side is not known to have, in order: for the backup."""
        return [self.flight[s] for s in sorted(self.flight)] + list(self.queue)

    # ---------------------------------------------------------------- receiving

    def _on_data(self, body: bytes) -> None:
        if len(body) < _DATA_HEAD.size:
            return
        seq, more = _DATA_HEAD.unpack_from(body)
        self.accept(seq, more, body[_DATA_HEAD.size:], late=False)
        self.ack_due += 1
        if self.ack_due >= 2:
            self._send_ack()
        elif self.ack_timer is None:
            self.ack_timer = asyncio.get_running_loop().call_later(0.005, self._send_ack)

    def accept(self, seq: int, more: int, chunk: bytes, late: bool = True) -> None:
        """A chunk, from UDP or from the backup. Late chunks are never refused: nothing would send them again."""
        if seq < self.rx_next or seq in self.rx_held:
            return
        if not late and (seq >= self.rx_next + MAX_WINDOW * 2 or self.frames.qsize() >= MAX_RECEIVED):
            return
        self.rx_held[seq] = (more, chunk)
        while self.rx_next in self.rx_held:
            more, chunk = self.rx_held.pop(self.rx_next)
            self.rx_next += 1
            self.parts.append(chunk)
            self.parts_size += len(chunk)
            if self.parts_size > MAX_FRAME + CHUNK:
                self.down("a frame over the limit")
                return
            if not more:
                self.frames.put_nowait(b"".join(self.parts))
                self.parts, self.parts_size = [], 0
                self.frames_in += 1
        self.progress.set()

    def _send_ack(self) -> None:
        if self.ack_timer is not None:
            self.ack_timer.cancel()
            self.ack_timer = None
        self.ack_due = 0
        bits = 0
        for seq in self.rx_held:
            at = seq - self.rx_next - 1
            if 0 <= at < MAP_BITS:
                bits |= 1 << at
        self._emit(_ACK, _ACK_NEXT.pack(self.rx_next) + bits.to_bytes(MAP_BITS // 8, "big"))

    async def _hand_up(self) -> None:
        await self.released.wait()
        while True:
            frame = await self.frames.get()
            try:
                await self.deliver(frame)
            except asyncio.CancelledError:
                raise
            except Exception:                  # noqa: BLE001 -- as on TCP, a bad frame ends the link
                log.exception("udp path %s", self.peer[:12])
                self.down("a frame could not be handled")
            finally:
                self.frames.task_done()

    async def finish(self, upto: int, timeout: float = 15.0) -> None:
        """The other side has fallen back: everything up to `upto` is here or on its way down the backup, which
        arrives in order, so wait until it has been handled."""
        end = time.monotonic() + timeout
        while self.rx_next < upto and time.monotonic() < end:
            self.progress.clear()
            try:
                await asyncio.wait_for(self.progress.wait(), max(0.01, end - time.monotonic()))
            except asyncio.TimeoutError:
                break
        try:
            await asyncio.wait_for(self.frames.join(), max(0.01, end - time.monotonic()))
        except asyncio.TimeoutError:
            pass

    # ---------------------------------------------------------------- the clock

    async def _run(self) -> None:
        next_knock = next_scan = next_fan = 0.0
        while not self.dead:
            now = time.monotonic()
            quiet = now - self.last_heard
            if not self.confirmed:
                if now - self.started >= self.knock_for:
                    self.down("no answer")
                    return
                if now >= next_knock:
                    for target in self.targets:
                        self._emit(_HELLO, addr=target)
                    if self.spray_host is not None:      # the other NAT's port cannot be known: guess
                        for _ in range(SPRAY_EACH):
                            self._emit(_HELLO, addr=(self.spray_host, random.randint(1024, 65535)))
                    if self.fan and now >= next_fan:     # many outside ports, one of them a guess will hit
                        for transport in self.fan:
                            for target in self.targets:
                                self._emit_to(_HELLO, target, transport.sendto)
                        next_fan = now + 1.0
                    next_knock = now + KNOCK_EVERY
            else:
                if quiet >= (DEAD_WITH_DATA if self.flight or self.queue else DEAD_IDLE):
                    self.down("silent")
                    return
                self._push(now)
                if now >= next_scan:
                    self._resend(now)
                    next_scan = now + 0.02
                if self.dead:
                    return
                if now - self.last_sent >= KEEP_EVERY:
                    self._send_ack()
                if now >= next_knock and now - self.started < self.knock_for and self.addr and not _home(self.addr[0]):
                    for target in self.targets:          # through, the long way: a home address may still answer
                        if _home(target[0]):
                            self._emit(_HELLO, addr=target)
                    next_knock = now + KNOCK_EVERY
            self.wake.clear()
            wait = 0.02 if self.flight else (KNOCK_EVERY if not self.confirmed else 1.0)
            try:
                await asyncio.wait_for(self.wake.wait(), wait)
            except asyncio.TimeoutError:
                pass


class Stream:
    """A Path shaped like an asyncio reader and writer, so `handshake()` and `Link` run on it unchanged: a mesh link with
    no TCP under it at all. One write + drain is one frame, as in wsock.py."""

    is_path = True

    def __init__(self, path: Path):
        self.path = path
        self.buffer = bytearray()
        self.pending = bytearray()
        self.more = asyncio.Event()
        self.gone = False
        path.deliver = self._arrived
        path.released.set()

    async def _arrived(self, frame: bytes) -> None:
        self.buffer += frame
        self.more.set()

    def end(self) -> None:
        self.gone = True
        self.more.set()

    async def readexactly(self, n: int) -> bytes:
        while len(self.buffer) < n:
            if self.gone:
                raise asyncio.IncompleteReadError(bytes(self.buffer), n)
            self.more.clear()
            await self.more.wait()
        out = bytes(self.buffer[:n])
        del self.buffer[:n]
        return out

    def write(self, data: bytes) -> None:
        self.pending += data

    async def drain(self) -> None:
        if self.pending:
            data, self.pending = bytes(self.pending), bytearray()
            try:
                await self.path.send_frame(data)
            except PathDown as exc:
                raise ConnectionError(str(exc)) from None

    def get_extra_info(self, name: str, default=None):
        return ("ws", 0) if name == "peername" else default        # no address to learn a peer by

    def is_closing(self) -> bool:
        return self.gone

    def close(self) -> None:
        self.end()
        if not self.path.dead:
            self.path.down("closed")
        self.path.close()

    async def wait_closed(self) -> None:
        pass


class _Socket(punchlib._Proto):
    """The node's one UDP socket: path packets by connection id, STUN answers as in punch.py."""

    def __init__(self):
        super().__init__()
        self.paths: dict[bytes, Path] = {}

    def datagram_received(self, data, addr):
        if data[:5] == MAGIC:
            path = self.paths.get(data[5:13])
            if path is not None:
                path.received(data, addr, self.transport)
            return
        super().datagram_received(data, addr)


class UdpShare:
    """A mesh extension (node.add_extension): opens a UDP path on every link that can have one. `stun_servers()` lists
    the STUN to map through. The lower node id asks, so two nodes do not ask each other at once."""

    kinds = ("ud",)

    def __init__(self, stun_servers=None, try_after: float = TRY_AFTER, public_stun=(), lan_dial=None):
        self.stun_servers = stun_servers or (lambda: [])
        self.try_after, self.public_stun = try_after, tuple(public_stun)
        self.lan_dial = lan_dial
        self.node = None
        self.sock: _Socket | None = None
        self.paths: dict[str, Path] = {}
        self.retry: dict[str, tuple[float, int]] = {}      # peer -> (not before, failures)
        self.waiting: dict[str, asyncio.Future] = {}
        self.trying: set[str] = set()
        self.mapped: tuple[float, list[list]] | None = None
        self.outside: list[str] = []            # what STUN observed for this socket
        self._lan_extra: list[list] = []        # home-network addresses, offered per peer
        self.opening = asyncio.Lock()
        self.nat = "unknown"                    # punch.nat_kind of this node's socket
        self.tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("udp", exc_info=task.exception())

    async def _open(self) -> None:
        if self.node is not None and self.node._stopping:
            raise ConnectionError("this node is stopping")
        async with self.opening:                    # two first uses at once must not each open one
            if self.sock is None:
                loop = asyncio.get_running_loop()
                _, self.sock = await loop.create_datagram_endpoint(_Socket, sock=punchlib.udp_socket())

    async def _offer(self) -> list[list]:
        """The outside addresses of the node's one socket. Home-network addresses are kept aside for offer_for."""
        await self._open()
        now = time.monotonic()
        if self.mapped is None or now - self.mapped[0] > MAP_KEEP:
            session = punchlib.Session(os.urandom(6).hex(), "u")
            session.proto = self.sock
            session.local_port = self.sock.transport.get_extra_info("sockname")[1]
            try:
                mesh = [(h, p) for h, p in (self.stun_servers() or []) if punchlib._v4(h)]
            except Exception:                              # noqa: BLE001
                mesh = []
            await session.map(mesh + [s for s in self.public_stun if s not in mesh])
            self.nat = punchlib.nat_kind(session.maps, punchlib.local_ipv4())
            self.outside = punchlib.outside_hosts(session.maps)
            full = session.offer(include_lan=True)
            safe = [a for a in full if punchlib.share_with_remote(a[0], False)]
            self._lan_extra = [a for a in full if a not in safe]
            # What every peer may be told. offer_for adds the home-network ones for a peer on this NAT.
            self.mapped = (now, safe)
        return self.mapped[1]

    def _mode(self) -> str:
        return punchlib.call_mode(self.lan_dial)

    async def offer_for(self, peer_raw=None) -> list[list]:
        """Addresses to send this peer. Home-network ones only when it is on this NAT, or mesh_lan_dial is on.

        An introduction passes no peer addresses: that frame is relayed, so a home address stays out of it unless
        the operator asked for LAN dialing."""
        offer = [list(a) for a in await self._offer()]
        peer = punchlib._addrs(peer_raw) if peer_raw is not None else []
        if punchlib.include_lan(self.outside, peer, self._mode()):
            for extra in self._lan_extra:
                if list(extra) not in offer and len(offer) < punchlib.MAX_ADDRS:
                    offer.append(list(extra))
        return offer

    def dial_targets(self, raw) -> list[tuple[str, int]]:
        parsed = punchlib._addrs(raw)
        kept = punchlib.dialable(parsed, self.outside, self._mode())
        if len(kept) != len(parsed):
            log.debug("mesh: not dialing %d home-network address(es)", len(parsed) - len(kept))
        return kept

    def _new_path(self, link, nonce: bytes) -> Path:
        peer = link.peer_id

        async def deliver(frame: bytes) -> None:
            header, blob = link.unpack(frame)
            await self.node._handle(link, header, blob)

        def confirmed(path: Path) -> None:
            self._spawn(self._attach(peer, path))

        path = Path(_derive(link._send_key, nonce), _derive(link._recv_key, nonce),
                    self.sock.transport.sendto, deliver, self._path_down, on_confirmed=confirmed, peer=peer)
        self.sock.paths[path.cid_in] = path
        self.paths[peer] = path
        return path

    async def _attach(self, peer: str, path: Path) -> None:
        link = self.node.links.get(peer) if self.node is not None else None
        if link is None or path.dead or self.paths.get(peer) is not path:
            return
        await link.attach(path)
        self.retry.pop(peer, None)
        log.info("mesh: udp path to %s at %s:%s", peer[:12], *path.addr)

    def _forget(self, path: Path) -> None:
        if self.sock is not None:
            self.sock.paths.pop(path.cid_in, None)
        if self.paths.get(path.peer) is path:
            del self.paths[path.peer]

    def _path_down(self, path: Path, why: str) -> None:
        was_up = path.confirmed
        self._later(path.peer, reset=was_up)
        self._spawn(self._fall_back(path, why))

    async def _fall_back(self, path: Path, why: str) -> None:
        link = self.node.links.get(path.peer) if self.node is not None else None
        if link is not None:
            try:
                await link.fall_back(path)
            except (LinkError, ConnectionError, OSError):
                pass
        if path.confirmed:
            log.info("mesh: udp path to %s down (%s): on the backup", path.peer[:12], why)
        if self.sock is not None:
            self.sock.paths.pop(path.cid_in, None)
        # While the other side's frames came this way it keeps taking `late` chunks until that side says `end`
        # (or the link goes); otherwise it is finished with.
        if link is None or link.incoming is not path:
            self._forget(path)
            path.close()

    def _later(self, peer: str, reset: bool = False) -> None:
        fails = 0 if reset else self.retry.get(peer, (0, 0))[1]
        wait = min(RETRY_MAX, RETRY_FIRST * 2 ** min(fails, 8))
        self.retry[peer] = (time.monotonic() + wait * random.uniform(0.8, 1.2), fails + 1)

    def _due(self, peer: str) -> bool:
        return (self.node is not None and self.node.node_id < peer and peer not in self.paths
                and peer not in self.trying and time.monotonic() >= self.retry.get(peer, (0, 0))[0])

    # ---------------------------------------------------------------- extension

    async def link_up(self, link) -> None:
        if not getattr(link.writer, "is_path", False):      # a link already on UDP has nothing to move to
            self._spawn(self._try_soon(link.peer_id))

    async def _try_soon(self, peer: str) -> None:
        await asyncio.sleep(self.try_after)
        if self._due(peer):
            await self._try(peer)

    async def link_down(self, peer_id: str) -> None:
        path = self.paths.pop(peer_id, None)
        if path is not None:
            self._forget(path)
            path.close()
        self.retry.pop(peer_id, None)

    async def rebalance(self) -> None:
        for peer in list(self.node.links):
            if self._due(peer):
                self._spawn(self._try(peer))

    async def on_stop(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        for path in list(self.paths.values()):
            path.close()
        self.paths.clear()
        if self.sock is not None and self.sock.transport is not None:
            self.sock.transport.close()
        self.sock = None

    async def _try(self, peer: str) -> None:
        link = self.node.links.get(peer)
        if link is None:
            return
        self.trying.add(peer)
        nonce = os.urandom(8).hex()
        fut = asyncio.get_running_loop().create_future()
        self.waiting[nonce] = fut
        try:
            offer = await self.offer_for(None)
            await link.send({"t": "ud", "op": "try", "n": nonce, "a": offer})
            try:
                answer = await asyncio.wait_for(fut, ANSWER_WAIT)
            except asyncio.TimeoutError:
                answer = {"op": "no", "why": "no answer (an older arcade?)"}
            if answer.get("op") != "ready" or self.node.links.get(peer) is not link:
                self._later(peer)
                return
            self._new_path(link, bytes.fromhex(nonce)).start(self.dial_targets(answer.get("a")))
            # The answer proved one NAT, so our home address may go to it, straight over this link. Both must aim: a host
            # firewall drops a LAN packet until its own side has sent to that address (a test machine's ufw, 2026-11-02).
            mine = await self.offer_for(answer.get("a"))
            if mine != offer:
                await link.send({"t": "ud", "op": "home", "n": nonce, "a": mine})
        except (LinkError, ConnectionError, OSError):
            self._later(peer)
        finally:
            self.waiting.pop(nonce, None)
            self.trying.discard(peer)

    async def _answer(self, link, header: dict) -> None:
        nonce = str(header.get("n", ""))
        peer = link.peer_id
        if len(nonce) != 16 or peer > self.node.node_id or peer in self.paths:
            await link.send({"t": "ud", "op": "no", "n": nonce[:16], "why": "not now"})
            return
        try:
            raw = bytes.fromhex(nonce)
        except ValueError:
            return
        offer = await self.offer_for(header.get("a"))
        path = self._new_path(link, raw)
        await link.send({"t": "ud", "op": "ready", "n": nonce, "a": offer})
        path.start(self.dial_targets(header.get("a")))

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        op = header.get("op")
        if op == "try":
            self._spawn(self._guarded(self._answer(link, header)))
        elif op == "home":
            path = self.paths.get(link.peer_id)
            if path is not None:
                path.aim(self.dial_targets(header.get("a")))
        elif op in ("ready", "no"):
            fut = self.waiting.get(str(header.get("n", "")))
            if fut is not None and not fut.done():
                fut.set_result(header)
        elif op == "go":
            path = self.paths.get(link.peer_id)
            if path is not None:
                link.incoming = path
                path.released.set()
        elif op == "late":
            path = link.incoming
            seq, more = header.get("s"), header.get("m")
            if path is not None and isinstance(seq, int) and more in (0, 1):
                path.accept(seq, more, bytes(blob))
        elif op == "end":
            path = link.incoming
            if path is not None and isinstance(header.get("n"), int):
                await path.finish(header["n"])
                link.incoming = None
                if not path.dead:
                    path.down("the other side fell back")
                self._forget(path)
                path.close()

    async def _guarded(self, coro) -> None:
        try:
            await coro
        except (LinkError, ConnectionError, OSError) as exc:
            log.debug("udp: %s", exc)

    def status(self) -> list[dict]:
        out = []
        for peer, p in self.paths.items():
            out.append({"peer": peer[:12], "up": p.usable, "at": f"{p.addr[0]}:{p.addr[1]}" if p.addr else None,
                        "rtt_ms": round(p.rtt * 1000, 1) if p.rtt is not None else None,
                        "frames_out": p.frames_out, "frames_in": p.frames_in, "resends": p.resends})
        return out
