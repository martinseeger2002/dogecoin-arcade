"""Inscriptions: arbitrary files written onto the chain, owned by an address.

What this is
------------
An inscription is a file put on the chain in full, uncompressed, exactly as the
creator supplied it, together with an immutable JSON field of their choosing. It
belongs to an address, and the owner can hand it to someone else. It is indexed
by the same engine as the tokens (`arcade/state.py`), so it survives reorgs the
same way and is part of the same ledger.

How it differs from ordinals
----------------------------
Ordinals bind data to a particular satoshi, so it travels whenever that coin is
spent. Here an inscription belongs to an ADDRESS and is moved by a transaction
that says so -- the Omni way, and the way every other object in this ledger
works. The practical difference: spending your coins never moves your
inscriptions by accident, and moving one is always deliberate and visible.

The carriage
------------
Chunks of type-200 `AnyData` over Class B, which carries 7,646 bytes per
transaction (`arcade/encoding.py`) -- about 7.5x what a text-field trick manages
and, more importantly, chunks that are INDEPENDENT of each other. A wallet split
into separate outputs funds them all at once, so a megabyte goes out in one
pass rather than in a chain that has to confirm link by link.

The alternative considered and rejected was the Doginals technique: 1,500-byte
pushes in a rolling commit/reveal, each transaction spending the one before. It
is cheaper per byte in fees and leaves no dust -- but every transaction depends
on its predecessor, and a node accepts only 25 unconfirmed transactions in a
chain (`validation.h: DEFAULT_ANCESTOR_LIMIT`). A megabyte is ~700 of them, so
it would confirm in 28 waves with a block between each: half an hour of waiting
against one pass. The dust it saves is recoverable here anyway -- every data
output is a 1-of-3 including the sender's own key (`encoding.py`), so it can be
swept back.

Why it hides inside type 200
----------------------------
`AnyData` is already decoded and deliberately ignored by the engine
(`state.py`). An older copy of this software therefore SKIPS an inscription
instead of halting on an unknown type, which is what it does with anything it
does not recognise. The magic below is what tells the two apart.
"""

from __future__ import annotations

import hashlib
import json as jsonlib
from dataclasses import dataclass, field

MAGIC = b"INSC"
VERSION = 1

KIND_CHUNK = 1        # a piece of an inscription's content
KIND_TRANSFER = 2     # hand an inscription to the reference address

#: magic4 + version1 + kind1 + id8 + countdown2 + clen2
CHUNK_HEADER_LEN = 18
#: magic4 + version1 + kind1 + txid32
TRANSFER_LEN = 38

#: The first chunk carries this before the content begins.
#:   total4 + sha256(32) + ctype_len1 + json_len2
MANIFEST_FIXED_LEN = 39

MAX_CONTENT_TYPE = 255
MAX_JSON = 65_535

#: A ceiling, not a judgement. Nothing in the format stops a larger file; this
#: is the point past which the cost stops being something anybody types by
#: accident. At 7,646 bytes a transaction, 5 MB is ~690 transactions.
MAX_CONTENT = 5 * 1024 * 1024


class InscriptionError(Exception):
    """The inscription is not well formed."""


@dataclass(frozen=True)
class Manifest:
    """Everything about an inscription except its bytes.

    `sha256` is over the content alone. It is what makes a reassembled file
    verifiable: chunks arrive in whatever order the miner chose, and a file that
    is one chunk short must be recognisable as short rather than shown as a
    broken picture.
    """

    total: int
    sha256: bytes
    content_type: str = "application/octet-stream"
    json: str = ""

    def encode(self) -> bytes:
        ctype = self.content_type.encode()[:MAX_CONTENT_TYPE]
        body = self.json.encode()
        if len(body) > MAX_JSON:
            raise InscriptionError(
                f"the JSON field is limited to {MAX_JSON:,} bytes.")
        if len(self.sha256) != 32:
            raise InscriptionError("sha256 must be 32 bytes")
        return (self.total.to_bytes(4, "big") + self.sha256
                + bytes([len(ctype)]) + ctype
                + len(body).to_bytes(2, "big") + body)

    @classmethod
    def decode(cls, raw: bytes) -> tuple["Manifest", bytes]:
        """Return the manifest and whatever content followed it."""
        if len(raw) < MANIFEST_FIXED_LEN:
            raise InscriptionError("truncated manifest")
        total = int.from_bytes(raw[:4], "big")
        digest = raw[4:36]
        ctype_len = raw[36]
        at = 37 + ctype_len
        if len(raw) < at + 2:
            raise InscriptionError("truncated content type")
        ctype = raw[37:at].decode("utf-8", "replace")
        json_len = int.from_bytes(raw[at:at + 2], "big")
        at += 2
        if len(raw) < at + json_len:
            raise InscriptionError("truncated JSON field")
        body = raw[at:at + json_len].decode("utf-8", "replace")
        return cls(total=total, sha256=digest, content_type=ctype, json=body), \
            raw[at + json_len:]


def validate_json(text: str) -> str:
    """Refuse anything that is not JSON, before it is paid for for ever.

    Empty means "none". Anything else has to parse, because an inscription
    cannot be corrected: a typo in the JSON field is permanent, and finding out
    at read time is finding out far too late.
    """
    text = (text or "").strip()
    if not text:
        return ""
    try:
        jsonlib.loads(text)
    except ValueError as exc:
        raise InscriptionError(f"that is not valid JSON: {exc}") from None
    if len(text.encode()) > MAX_JSON:
        raise InscriptionError(f"the JSON field is limited to {MAX_JSON:,} bytes.")
    return text


def chunk_header(inscription_id: bytes, countdown: int, clen: int,
                 kind: int = KIND_CHUNK) -> bytes:
    if len(inscription_id) != 8:
        raise InscriptionError("an inscription id is 8 bytes")
    if not 0 <= countdown <= 0xFFFF:
        raise InscriptionError("countdown out of range")
    return (MAGIC + bytes([VERSION, kind]) + inscription_id
            + countdown.to_bytes(2, "big") + clen.to_bytes(2, "big"))


@dataclass(frozen=True)
class Chunk:
    """One transaction's worth of an inscription."""

    inscription_id: bytes
    countdown: int          # 0 marks the LAST chunk, as everywhere else here
    body: bytes
    kind: int = KIND_CHUNK

    def encode(self) -> bytes:
        return chunk_header(self.inscription_id, self.countdown,
                            len(self.body), self.kind) + self.body


@dataclass(frozen=True)
class Transfer:
    """Hand an inscription to whoever the reference output names."""

    txid: bytes             # the creating transaction, 32 bytes, as on the wire

    def encode(self) -> bytes:
        if len(self.txid) != 32:
            raise InscriptionError("an inscription is named by a 32-byte txid")
        return MAGIC + bytes([VERSION, KIND_TRANSFER]) + self.txid


def is_inscription(payload: bytes) -> bool:
    """Does this AnyData body belong to us? Cheap enough to ask of everything."""
    return len(payload) >= 6 and payload[:4] == MAGIC


def parse(payload: bytes) -> Chunk | Transfer:
    """Read one inscription payload. Raises `InscriptionError` if malformed."""
    if not is_inscription(payload):
        raise InscriptionError("not an inscription payload")
    version, kind = payload[4], payload[5]
    if version != VERSION:
        raise InscriptionError(f"inscription version {version} is not readable here")

    if kind == KIND_TRANSFER:
        if len(payload) < TRANSFER_LEN:
            raise InscriptionError("truncated transfer")
        return Transfer(txid=payload[6:38])

    if kind != KIND_CHUNK:
        raise InscriptionError(f"unknown inscription kind {kind}")
    if len(payload) < CHUNK_HEADER_LEN:
        raise InscriptionError("truncated chunk header")
    inscription_id = payload[6:14]
    countdown = int.from_bytes(payload[14:16], "big")
    clen = int.from_bytes(payload[16:18], "big")
    body = payload[CHUNK_HEADER_LEN:CHUNK_HEADER_LEN + clen]
    if len(body) != clen:
        # Class B pads with NULs and never strips them, so a short body means
        # the transaction really was cut -- not that padding went missing.
        raise InscriptionError("chunk is shorter than its own length field")
    return Chunk(inscription_id=inscription_id, countdown=countdown, body=body)


def plan(content: bytes, content_type: str, json_text: str = "",
         inscription_id: bytes | None = None,
         capacity: int = 7_646) -> list[bytes]:
    """Everything that has to go on chain, in order, as AnyData bodies.

    The first chunk carries the manifest and then as much content as still
    fits; the rest are content alone. Split evenly rather than greedily, for
    the same reason a message is: a greedy split makes the first transaction
    the largest possible, and that is the one most likely to meet a limit.
    """
    import secrets

    if not content:
        raise InscriptionError("there is nothing to inscribe.")
    if len(content) > MAX_CONTENT:
        raise InscriptionError(
            f"that file is {len(content) / 1_048_576:.1f} MB and an inscription "
            f"is limited to {MAX_CONTENT // 1_048_576} MB here.")
    json_text = validate_json(json_text)
    inscription_id = inscription_id or secrets.token_bytes(8)

    manifest = Manifest(total=len(content), sha256=hashlib.sha256(content).digest(),
                        content_type=content_type or "application/octet-stream",
                        json=json_text).encode()
    room = capacity - CHUNK_HEADER_LEN
    if room <= len(manifest):
        raise InscriptionError("the manifest does not fit in one transaction")

    stream = manifest + content
    count = -(-len(stream) // room)
    even = -(-len(stream) // count)
    pieces = [stream[i:i + even] for i in range(0, len(stream), even)]
    return [Chunk(inscription_id=inscription_id, countdown=len(pieces) - 1 - n,
                  body=piece).encode()
            for n, piece in enumerate(pieces)]


@dataclass
class Assembly:
    """Chunks of one inscription, gathered until the set is complete."""

    inscription_id: bytes
    pieces: dict[int, bytes] = field(default_factory=dict)
    total_chunks: int | None = None

    def add(self, chunk: Chunk) -> None:
        self.pieces[chunk.countdown] = chunk.body
        if chunk.countdown == 0 and self.total_chunks is None:
            # The last chunk is the only one that says how many there were:
            # the first chunk's countdown IS count-1, so seeing either end is
            # enough, and seeing the end is what completion means.
            pass

    @property
    def highest(self) -> int:
        return max(self.pieces) if self.pieces else -1

    def complete(self) -> bool:
        """Every countdown from the highest seen down to 0, with no gap."""
        if 0 not in self.pieces:
            return False
        return set(self.pieces) == set(range(self.highest + 1))

    def join(self) -> tuple[Manifest, bytes]:
        """The manifest and the content, verified against the manifest's hash."""
        if not self.complete():
            raise InscriptionError("some of the inscription is missing")
        stream = b"".join(self.pieces[c] for c in
                          range(self.highest, -1, -1))
        manifest, content = Manifest.decode(stream)
        content = content[:manifest.total]
        if len(content) != manifest.total:
            raise InscriptionError("the content is shorter than the manifest says")
        if hashlib.sha256(content).digest() != manifest.sha256:
            raise InscriptionError("the content does not match its own hash")
        return manifest, content
