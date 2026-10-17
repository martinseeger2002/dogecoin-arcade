"""One connection between two arcade nodes: encrypted, and each side proven by key.

**Who is on the other end.** Every node has a mesh key, an Ed25519 key of its
own (kept apart from wallet and messaging keys, for the reasons those keep
apart from each other). A node's mesh identity is that public key; an address
is only where it happened to be found. The handshake proves the key, so a
peer cannot claim to be another node, and a node that changes address is
still the same node.

**The handshake.** Each side sends a fresh X25519 key in the clear (HELLO).
Both derive the same secret from the pair, and from it one key per direction.
Everything after that is encrypted, including the AUTH frame, in which each
side names its mesh key and signs the transcript of both HELLOs with it, so
the proof cannot be replayed into another connection and an onlooker never
learns which nodes are talking.

**Frames.** A 4-byte length, then the sealed body. Bodies are capped at
MAX_FRAME, far above what a room message may carry, so a peer that sends a
huge length is cut off rather than buffered.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import struct
from dataclasses import dataclass

import nacl.bindings as sodium
import nacl.exceptions
import nacl.public
import nacl.signing

#: Bumped when the wire format changes; nodes on different versions do not link.
VERSION = 2   # 2026-10-12: mesh v2 -- rooms as fingerprints, members in the room (node.py)
MAGIC = b"ARCM"
MAX_FRAME = 64 * 1024
HANDSHAKE_TIMEOUT = 10.0
_AUTH_CONTEXT = b"arcade-mesh-auth-v1"


class LinkError(Exception):
    """The other side is not a node we can talk to: wrong network, bad proof, or gone."""


@dataclass(frozen=True)
class Hello:
    network: str        # e.g. "pepecoin-main": nodes of different chains never link
    ephemeral: bytes    # X25519 public key, 32 bytes, fresh per connection

    def encode(self) -> bytes:
        net = self.network.encode()
        return MAGIC + bytes([VERSION, len(net)]) + net + self.ephemeral

    @classmethod
    def decode(cls, raw: bytes) -> "Hello":
        if len(raw) < 6 or raw[:4] != MAGIC:
            raise LinkError("not an arcade mesh node")
        if raw[4] != VERSION:
            raise LinkError(f"mesh version {raw[4]}, this node speaks {VERSION}")
        n = raw[5]
        net, eph = raw[6:6 + n], raw[6 + n:]
        if len(net) != n or len(eph) != 32:
            raise LinkError("malformed hello")
        return cls(network=net.decode("utf-8", "replace"), ephemeral=eph)


async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    head = await reader.readexactly(4)
    (size,) = struct.unpack(">I", head)
    if size > MAX_FRAME:
        raise LinkError(f"frame of {size} bytes is over the limit")
    return await reader.readexactly(size)


def _frame(body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + body


class Link:
    """An established, authenticated connection. Send and receive JSON-able dicts
    plus an optional opaque binary blob (the room payload, never parsed)."""

    def __init__(self, reader, writer, peer_key: bytes, outbound: bool,
                 send_key: bytes, recv_key: bytes, peer_listen: int | None):
        self.reader, self.writer = reader, writer
        self.peer_key = peer_key                  # the other node's Ed25519 public key
        self.peer_id = peer_key.hex()
        self.outbound = outbound
        self.peer_listen = peer_listen            # the port it accepts on, if it does
        self._send_key, self._recv_key = send_key, recv_key
        self._send_n = 0
        self._recv_n = 0
        self._lock = asyncio.Lock()
        self.closed = False

    @property
    def host(self) -> str:
        peer = self.writer.get_extra_info("peername") or ("?", 0)
        return str(peer[0])

    @staticmethod
    def _nonce(n: int) -> bytes:
        return n.to_bytes(24, "big")

    def _seal(self, plain: bytes) -> bytes:
        sealed = sodium.crypto_aead_xchacha20poly1305_ietf_encrypt(
            plain, None, self._nonce(self._send_n), self._send_key)
        self._send_n += 1
        return sealed

    def _open(self, sealed: bytes) -> bytes:
        try:
            plain = sodium.crypto_aead_xchacha20poly1305_ietf_decrypt(
                sealed, None, self._nonce(self._recv_n), self._recv_key)
        except nacl.exceptions.CryptoError:
            raise LinkError("a frame failed authentication") from None
        self._recv_n += 1
        return plain

    @staticmethod
    def pack(header: dict, blob: bytes = b"") -> bytes:
        if header.get("t") == "pub":
            try:
                return _pack_pub(header, blob)
            except (KeyError, ValueError, TypeError, OverflowError, struct.error):
                pass                                     # anything unusual goes as JSON, as before
        head = json.dumps(header, separators=(",", ":")).encode()
        return struct.pack(">H", len(head)) + head + blob

    @staticmethod
    def unpack(plain: bytes) -> tuple[dict, bytes]:
        if len(plain) < 2:
            raise LinkError("empty frame")
        if plain[:2] == _BIN:
            return _unpack_pub(plain)
        (n,) = struct.unpack(">H", plain[:2])
        try:
            header = json.loads(plain[2:2 + n])
        except ValueError:
            raise LinkError("unreadable frame header") from None
        if not isinstance(header, dict):
            raise LinkError("frame header is not an object")
        return header, plain[2 + n:]

    async def send(self, header: dict, blob: bytes = b"") -> None:
        if self.closed:
            raise LinkError("link is closed")
        async with self._lock:                    # nonces must go out in order
            self.writer.write(_frame(self._seal(self.pack(header, blob))))
            await self.writer.drain()

    async def recv(self) -> tuple[dict, bytes]:
        return self.unpack(self._open(await _read_frame(self.reader)))

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


# ---------------------------------------------------------------- room messages, in binary
# (2026-10-12, "very low latency" for tens of thousands of players.) A room message ("pub") is
# by far the commonest frame, and as JSON it spent ~300 bytes on its header: the sender's key
# and signature as hex, field names, quotes. In binary the same header is ~120 bytes and costs
# no JSON parse per hop. The dict that comes out is exactly the dict that went in, so the
# signature (over canonical JSON of the signed fields, node.py) checks the same either way.
#
#   FF FF | 01 | origin 32 | sig 64 | q u64 | hops u8 | kind u8 | room u8+bytes | member u8+bytes
#         | cred u16+canonical JSON (0 = none) | d u16 count + 4 bytes each | the payload
_BIN = b"\xff\xff"          # a JSON header of 65535 bytes cannot fit a frame, so this never collides
_KINDS = ("msg", "join", "leave", "here")


def _pack_pub(h: dict, blob: bytes) -> bytes:
    room, member = str(h["r"]).encode(), str(h["m"]).encode()
    cred = json.dumps(h["c"], separators=(",", ":"), sort_keys=True).encode() if "c" in h else b""
    d = h.get("d")
    shorts = b"".join(bytes.fromhex(x) for x in d) if isinstance(d, list) else b""
    if isinstance(d, list) and any(len(x) != 8 for x in d):
        raise ValueError("short ids are 8 hex characters")
    return (_BIN + b"\x01" + bytes.fromhex(h["o"]) + bytes.fromhex(h["sig"])
            + struct.pack(">QBB", int(h["q"]), int(h["hops"]), _KINDS.index(h["k"]))
            + struct.pack(">B", len(room)) + room + struct.pack(">B", len(member)) + member
            + struct.pack(">H", len(cred)) + cred
            + struct.pack(">H", len(d) if isinstance(d, list) else 0xFFFF) + shorts + blob)


def _unpack_pub(b: bytes) -> tuple[dict, bytes]:
    try:
        if b[2] != 1 or len(b) < 3 + 32 + 64 + 10:
            raise LinkError("unknown binary frame")
        i = 3
        origin, sig = b[i:i + 32].hex(), b[i + 32:i + 96].hex(); i += 96
        q, hops, kind = struct.unpack(">QBB", b[i:i + 10]); i += 10
        n = b[i]; room = b[i + 1:i + 1 + n].decode(); i += 1 + n
        n = b[i]; member = b[i + 1:i + 1 + n].decode(); i += 1 + n
        (n,) = struct.unpack(">H", b[i:i + 2]); cred = b[i + 2:i + 2 + n]; i += 2 + n
        (n,) = struct.unpack(">H", b[i:i + 2]); i += 2
        h = {"t": "pub", "r": room, "o": origin, "q": q, "m": member, "k": _KINDS[kind], "hops": hops, "sig": sig}
        if cred:
            h["c"] = json.loads(cred)
        if n != 0xFFFF:
            h["d"] = [b[i + 4 * k:i + 4 * k + 4].hex() for k in range(n)]; i += 4 * n
        if i > len(b):
            raise LinkError("a binary frame ended early")
        return h, b[i:]
    except (IndexError, ValueError, struct.error, UnicodeDecodeError) as exc:
        raise LinkError(f"unreadable binary frame: {type(exc).__name__}") from None


def _derive(shared: bytes, transcript: bytes, label: bytes) -> bytes:
    return hashlib.blake2b(shared + transcript, digest_size=32, person=label.ljust(16, b"\0")[:16]).digest()


async def handshake(reader, writer, signing_key: nacl.signing.SigningKey, network: str,
                    outbound: bool, listen_port: int | None, *, initiator: bool | None = None) -> Link:
    """Run the handshake on a fresh connection; return a Link or raise LinkError.

    `initiator` is who plays the dialer's part in the key schedule, when that is
    not simply who connected: on a connection both nodes dialled at once (a
    "meet", node.py) each side believes it dialled, so both pass the same rule
    -- the lower node id is the dialer -- and agree without a word on the wire."""
    dialer = outbound if initiator is None else initiator
    eph = nacl.public.PrivateKey.generate()
    mine = Hello(network=network, ephemeral=bytes(eph.public_key))
    try:
        writer.write(_frame(mine.encode()))
        await writer.drain()
        theirs = Hello.decode(await asyncio.wait_for(_read_frame(reader), HANDSHAKE_TIMEOUT))
        if theirs.network != network:
            raise LinkError(f"that node is on {theirs.network!r}, this one on {network!r}")
        shared = sodium.crypto_scalarmult(bytes(eph), theirs.ephemeral)
        first, second = (mine, theirs) if dialer else (theirs, mine)
        transcript = hashlib.blake2b(first.encode() + second.encode(), digest_size=32).digest()
        k_out = _derive(shared, transcript, b"arcm-dialer")
        k_in = _derive(shared, transcript, b"arcm-listener")
        send_key, recv_key = (k_out, k_in) if dialer else (k_in, k_out)

        link = Link(reader, writer, b"", outbound, send_key, recv_key, None)
        role = b"D" if dialer else b"L"
        proof = signing_key.sign(_AUTH_CONTEXT + role + transcript).signature
        await link.send({"t": "auth", "key": bytes(signing_key.verify_key).hex(),
                         "sig": proof.hex(), "listen": listen_port})
        header, _ = await asyncio.wait_for(link.recv(), HANDSHAKE_TIMEOUT)
        if header.get("t") != "auth":
            raise LinkError("expected auth")
        try:
            key = bytes.fromhex(str(header.get("key", "")))
            sig = bytes.fromhex(str(header.get("sig", "")))
            their_role = b"L" if dialer else b"D"
            nacl.signing.VerifyKey(key).verify(_AUTH_CONTEXT + their_role + transcript, sig)
        except (ValueError, nacl.exceptions.BadSignatureError, nacl.exceptions.ValueError,
                nacl.exceptions.TypeError):
            raise LinkError("the other node could not prove its key") from None
        if key == bytes(signing_key.verify_key):
            raise LinkError("connected to itself")
        listen = header.get("listen")
        link.peer_key, link.peer_id = key, key.hex()
        link.peer_listen = listen if isinstance(listen, int) and 0 < listen < 65536 else None
        return link
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError) as exc:
        raise LinkError(f"handshake failed: {type(exc).__name__}") from None
