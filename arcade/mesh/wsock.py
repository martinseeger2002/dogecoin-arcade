"""Mesh links over WebSocket, for nodes nobody can dial directly.

2026-10-12 (the operator: "we're not reliant on Cloudflare, but we can use Cloudflare to
strengthen the network"). A node behind a router that cannot be opened -- carrier
NAT, Starlink -- can still be reached through a tunnel it opens itself: a
Cloudflare quick tunnel (`cloudflared tunnel --url`, free, no account) gives it a
public https address and carries WebSocket, not raw TCP. So the mesh speaks
WebSocket too, on a local port the tunnel points at, and dials `wss://` addresses
it finds in the chain's announcements (announce.py, `arcade-mesh-ws`).

Nothing about the link changes: the mesh handshake and every frame run inside the
WebSocket exactly as over TCP, so the tunnel carries ciphertext between two keys it
does not have. It cannot read a room, forge a message or tell nodes apart.

Only what the mesh needs of RFC 6455, with no library: one binary message per
mesh frame, masked from the client side, ping answered, close honoured. `serve()`
and `connect()` hand back a reader and a writer shaped like asyncio's streams, so
`handshake()` and `Link` use them unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import ssl
import struct

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_LIVE: set = set()                  # connection tasks still running (see serve())
MAX_MESSAGE = 128 * 1024           # far above a mesh frame (64 KB); more is cut off
PATH = "/mesh"
URL = re.compile(r"^wss?://(?P<host>[A-Za-z0-9.-]{1,253})(?::(?P<port>\d{1,5}))?(?P<path>/[A-Za-z0-9/_.-]{0,64})?$")


class WSClosed(ConnectionError):
    pass


def _accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.encode() + _GUID).digest()).decode()


class _Socket:
    """One WebSocket over an asyncio stream pair."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, client: bool):
        self.r, self.w, self.client = reader, writer, client
        self.closed = False

    async def send(self, data: bytes, opcode: int = 0x2) -> None:
        if self.closed:
            raise WSClosed("the WebSocket is closed")
        n = len(data)
        head = bytes([0x80 | opcode])
        mask_bit = 0x80 if self.client else 0
        if n < 126:
            head += bytes([mask_bit | n])
        elif n < 1 << 16:
            head += bytes([mask_bit | 126]) + struct.pack(">H", n)
        else:
            head += bytes([mask_bit | 127]) + struct.pack(">Q", n)
        if self.client:
            mask = os.urandom(4)
            data = _xor(data, mask)
            head += mask
        self.w.write(head + data)
        await self.w.drain()

    async def recv(self) -> bytes:
        """The next whole binary message (pings answered on the way)."""
        parts: list[bytes] = []
        while True:
            b0, b1 = await self.r.readexactly(2)
            opcode, fin, masked, n = b0 & 0x0F, b0 & 0x80, b1 & 0x80, b1 & 0x7F
            if n == 126:
                (n,) = struct.unpack(">H", await self.r.readexactly(2))
            elif n == 127:
                (n,) = struct.unpack(">Q", await self.r.readexactly(8))
            if n > MAX_MESSAGE or sum(map(len, parts)) + n > MAX_MESSAGE:
                raise WSClosed("a WebSocket message over the limit")
            mask = await self.r.readexactly(4) if masked else None
            data = await self.r.readexactly(n)
            if mask:
                data = _xor(data, mask)
            if opcode == 0x8:                       # close
                self.closed = True
                raise WSClosed("closed by the other side")
            if opcode == 0x9:                       # ping: pong, keep reading
                await self.send(data, 0xA)
                continue
            if opcode == 0xA:
                continue
            parts.append(data)
            if fin:
                return b"".join(parts)

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.w.write(bytes([0x88, 0x80 if self.client else 0]) + (os.urandom(4) if self.client else b""))
                await self.w.drain()
            except (ConnectionError, OSError):
                pass
        self.w.close()


def _xor(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    whole = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(whole, "big")).to_bytes(n, "big")


class Reader:
    """`readexactly` over WebSocket messages, as handshake() and Link expect."""

    def __init__(self, sock: _Socket):
        self.sock, self.buf = sock, bytearray()

    async def readexactly(self, n: int) -> bytes:
        while len(self.buf) < n:
            try:
                self.buf += await self.sock.recv()
            except (WSClosed, asyncio.IncompleteReadError, ConnectionError) as exc:
                raise asyncio.IncompleteReadError(bytes(self.buf), n) from exc
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


class Writer:
    """`write` + `drain` send one WebSocket message; the rest of StreamWriter's face."""

    def __init__(self, sock: _Socket, peer: str):
        self.sock, self.pending, self.peer = sock, bytearray(), peer

    def write(self, data: bytes) -> None:
        self.pending += data

    async def drain(self) -> None:
        if self.pending:
            data, self.pending = bytes(self.pending), bytearray()
            await self.sock.send(data)

    def get_extra_info(self, name: str, default=None):
        # No IP to learn a peer by: it came through a tunnel. Say what it is instead.
        return ("ws", 0) if name == "peername" else default

    def is_closing(self) -> bool:
        return self.sock.closed

    def close(self) -> None:
        if not self.sock.closed:
            asyncio.get_running_loop().create_task(self.sock.close())
        else:
            self.sock.w.close()

    async def wait_closed(self) -> None:
        try:
            await self.sock.w.wait_closed()
        except (ConnectionError, OSError):
            pass


def parse_url(url: str) -> tuple[str, int, str, bool]:
    """(host, port, path, tls) of a ws:// or wss:// address, or ValueError."""
    m = URL.match(str(url or "").strip())
    if not m:
        raise ValueError("a WebSocket address like wss://name.trycloudflare.com/mesh")
    tls = url.startswith("wss://")
    port = int(m["port"] or (443 if tls else 80))
    if not 0 < port < 65536:
        raise ValueError("a port is 1 to 65535")
    return m["host"], port, m["path"] or PATH, tls


async def connect(url: str, timeout: float = 10.0) -> tuple[Reader, Writer]:
    host, port, path, tls = parse_url(url)
    ctx = ssl.create_default_context() if tls else None
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=ctx, server_hostname=host if tls else None), timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    writer.write((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: arcade-mesh\r\n\r\n").encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    lines = head.decode("latin-1").split("\r\n")
    if not lines[0].startswith("HTTP/1.1 101"):
        writer.close()
        raise ConnectionError(f"no WebSocket there: {lines[0][:60]}")
    got = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:] if ":" in l)}
    if got.get("sec-websocket-accept") != _accept_key(key):
        writer.close()
        raise ConnectionError("the WebSocket answer does not match")
    sock = _Socket(reader, writer, client=True)
    return Reader(sock), Writer(sock, url)


async def _proxy(head: bytes, reader, writer, http_port: int) -> None:
    """One /realtime/ request through the tunnel to the node's own web app, answer streamed back
    (the room stream is server-sent events and never ends by itself). Cloudflare's own headers
    ride along, so the web app's door sees a request from outside and treats it as one."""
    lines = head.decode("latin-1").split("\r\n")
    kept = [l for l in lines[1:] if l and not l.lower().startswith(("connection:", "keep-alive:"))]
    # Who really asked. Without an edge in front (a node whose mesh port is open itself), nothing else says so, and
    # the web app saw 127.0.0.1 with whatever Host the caller chose: a local request (node 3's review, 2026-10-20).
    # A forwarded request is an outside one to the door (door.EDGE_HEADERS).
    peer = writer.get_extra_info("peername")
    if peer and not any(l.lower().startswith("x-forwarded-for:") for l in kept):
        kept.append(f"X-Forwarded-For: {peer[0]}")
    length = next((int(l.split(":", 1)[1]) for l in kept if l.lower().startswith("content-length:")), 0)
    try:
        body = await asyncio.wait_for(reader.readexactly(min(length, 64 * 1024)), 15) if length else b""
        up_r, up_w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", http_port), 5)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    up_w.write(("\r\n".join([lines[0]] + kept + ["Connection: close", "", ""])).encode("latin-1") + body)
    await up_w.drain()
    try:
        while data := await up_r.read(16384):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        up_w.close()
        writer.close()


async def serve(handler, host: str, port: int, http_port: int | None = None) -> asyncio.base_events.Server:
    """A WebSocket server on host:port: each upgrade at PATH gets handler(reader, writer).
    With `http_port`, a request for /realtime/... is passed to the node's web app there -- the
    room routes only, so a player can use this node as their way in (realtime.py); anything
    else is refused, so the tunnel never exposes the wallet or the rest of the arcade."""
    async def one(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Held here until it ends: asyncio keeps only a weak hold on a connection's task, and a
        # long room stream through the tunnel was being collected mid-game ("Task was destroyed
        # but it is pending", live 2026-10-12) -- the player stopped hearing the room.
        task = asyncio.current_task()
        _LIVE.add(task)
        try:
            await _one(reader, writer)
        finally:
            _LIVE.discard(task)

    async def _one(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        got = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:] if ":" in l)}
        parts = lines[0].split(" ")
        # And an account's sealed wallet by name (/signin/<name>, GET only): another arcade signing that account in
        # asks for it (app.py _vault_elsewhere; 2026-10-19: "It should just know automatically").
        if http_port and len(parts) >= 2 and ".." not in parts[1] and (
                (parts[1].startswith("/realtime/") and parts[0] in ("GET", "POST", "OPTIONS", "HEAD"))
                or (parts[1].startswith("/signin/") and parts[0] == "GET")):
            await _proxy(head, reader, writer, http_port)
            return
        if len(parts) < 2 or parts[0] != "GET" or parts[1].split("?")[0] != PATH \
                or got.get("upgrade", "").lower() != "websocket" or "sec-websocket-key" not in got:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 12\r\nConnection: close\r\n\r\narcade mesh\n")
            await writer.drain()
            writer.close()
            return
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {_accept_key(got['sec-websocket-key'])}\r\n\r\n").encode())
        await writer.drain()
        sock = _Socket(reader, writer, client=False)
        await handler(Reader(sock), Writer(sock, "ws"))
    return await asyncio.start_server(one, host, port)
