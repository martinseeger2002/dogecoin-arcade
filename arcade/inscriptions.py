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
import re
from dataclasses import dataclass, field

MAGIC = b"INSC"
VERSION = 1

KIND_CHUNK = 1        # a piece of an inscription's content
KIND_TRANSFER = 2     # hand an inscription to the reference address
KIND_SWAP = 5         # two parties trade in one transaction (3 and 4 are tags)
KIND_OFFER = 6        # an offer for somebody's inscription, said out loud

#: What one side of a swap hands over.
LEG_NONE = 0
LEG_INSCRIPTION = 1   # a 32-byte txid
LEG_TOKEN = 2         # property id (4 bytes) and units (8 bytes)
LEG_COINS = 3         # satoshis (8 bytes), paid inside the same transaction

#: magic4 + version1 + kind1 + id8 + countdown2 + clen2
CHUNK_HEADER_LEN = 18
#: magic4 + version1 + kind1 + txid32
TRANSFER_LEN = 38

#: The first chunk carries this before the content begins.
#:   total4 + sha256(32) + ctype_len1 + json_len2
MANIFEST_FIXED_LEN = 39

MAX_CONTENT_TYPE = 255
MAX_JSON = 65_535

#: There is no policy ceiling on an inscription: what it costs is the creator's
#: to decide, and the interface shows that cost before anything is spent. What
#: remains is arithmetic. The countdown field is two bytes, so a set is at most
#: 65,536 chunks -- about 498 MB, which is some 99,000 coins and seven hours of
#: blocks at one chunk a transaction. Nothing anybody reaches; everything short
#: of it is allowed.
MAX_CHUNKS = 0x10000


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


#: HashLips names every item `<prefix> #<edition>`; this is how a name is
#: split back into the two.
_EDITION_NAME = re.compile(r"^(.*\S)\s*#\s*(\d+)\s*$")


def collection_of(json_text: str) -> tuple[str, int | None, str] | None:
    """Which collection an inscription's JSON says it belongs to, if any.

    Returns (collection, edition, name) or None. The rule is the one the
    HashLips Art Engine's metadata follows, because that is what people
    actually have on disk: an object with a `name` of the form `Prefix #12`
    and an integer `edition`. An explicit `collection` string wins over the
    name, so a set made some other way can say so outright. Deterministic
    from the bytes on the chain alone -- two nodes must agree on what is in
    a collection the same way they agree on what number an inscription got.
    """
    if not json_text:
        return None
    try:
        data = jsonlib.loads(json_text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    name = name.strip() if isinstance(name, str) else ""
    edition = data.get("edition")
    if isinstance(edition, bool) or not isinstance(edition, int):
        edition = None
    collection = data.get("collection")
    collection = collection.strip() if isinstance(collection, str) else ""
    if not collection:
        match = _EDITION_NAME.match(name)
        if match is None:
            return None
        collection = match.group(1).strip()
        if edition is None:
            edition = int(match.group(2))
    if not collection or len(collection) > 200:
        return None
    return collection, edition, name[:200]


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


@dataclass(frozen=True)
class Leg:
    """One side of a swap: what one party gives the other."""

    kind: int
    txid: bytes = b""       # LEG_INSCRIPTION
    property_id: int = 0    # LEG_TOKEN
    amount: int = 0         # LEG_TOKEN units, or LEG_COINS satoshis

    def encode(self) -> bytes:
        if self.kind == LEG_INSCRIPTION:
            if len(self.txid) != 32:
                raise InscriptionError("an inscription is named by a 32-byte txid")
            return bytes([self.kind]) + self.txid
        if self.kind == LEG_TOKEN:
            if not 0 < self.property_id < 2 ** 32 or not 0 < self.amount < 2 ** 64:
                raise InscriptionError("a token leg needs a property and an amount")
            return (bytes([self.kind]) + self.property_id.to_bytes(4, "big")
                    + self.amount.to_bytes(8, "big"))
        if self.kind == LEG_COINS:
            if not 0 < self.amount < 2 ** 64:
                raise InscriptionError("a coin leg needs an amount")
            return bytes([self.kind]) + self.amount.to_bytes(8, "big")
        raise InscriptionError(f"unknown swap leg {self.kind}")

    @classmethod
    def decode(cls, raw: bytes, at: int) -> tuple["Leg", int]:
        if at >= len(raw):
            raise InscriptionError("truncated swap")
        kind = raw[at]
        if kind == LEG_INSCRIPTION:
            body = raw[at + 1:at + 33]
            if len(body) != 32:
                raise InscriptionError("truncated swap")
            return cls(kind, txid=body), at + 33
        if kind == LEG_TOKEN:
            body = raw[at + 1:at + 13]
            if len(body) != 12:
                raise InscriptionError("truncated swap")
            return (cls(kind, property_id=int.from_bytes(body[:4], "big"),
                        amount=int.from_bytes(body[4:], "big")), at + 13)
        if kind == LEG_COINS:
            body = raw[at + 1:at + 9]
            if len(body) != 8:
                raise InscriptionError("truncated swap")
            return cls(kind, amount=int.from_bytes(body, "big")), at + 9
        raise InscriptionError(f"unknown swap leg {kind}")

    def describe(self) -> str:
        if self.kind == LEG_INSCRIPTION:
            return f"inscription {self.txid.hex()}"
        if self.kind == LEG_TOKEN:
            return f"{self.amount} units of property {self.property_id}"
        if self.kind == LEG_COINS:
            return f"{self.amount / 100_000_000:.8f} coins"
        return "nothing"


@dataclass(frozen=True)
class Swap:
    """Two parties trade in one transaction.

    The sender (the first input, so this is Class C only) is the SELLER and
    gives `give` to the buyer; the buyer is the first input that is not the
    seller's, and gives `take` to the seller. Both have signed the
    transaction -- an input is a signature, and SIGHASH_ALL covers every
    input and output, so neither party can change what the other agreed to.
    Both legs move or neither does. That is what makes it a swap rather than
    two sends that hope to land in the same block: there is one transaction,
    so there is no block in which one side has happened and the other has not.

    The buyer is not named in the payload, on purpose: an OP_RETURN carries
    76 bytes here (encoding.max_class_c_payload), and two inscription legs
    are 66 of them. Naming the buyer would put an NFT-for-NFT swap 21 bytes
    over. The inputs already say who the buyer is.
    """

    give: Leg               # from the seller to the buyer
    take: Leg               # from the buyer to the seller

    def encode(self) -> bytes:
        return (MAGIC + bytes([VERSION, KIND_SWAP])
                + self.give.encode() + self.take.encode())


def is_inscription(payload: bytes) -> bool:
    """Does this AnyData body belong to us? Cheap enough to ask of everything."""
    return len(payload) >= 6 and payload[:4] == MAGIC


@dataclass(frozen=True)
class Offer:
    """An offer for one inscription, made in public.

    Said on the chain rather than sent as a message, because there is no way
    to message a stranger who has not published a key -- and somebody holding
    an NFT never asked to be reachable. Every node reads it, so the wallet
    that holds the piece finds the offer by watching its own things (D-042).

    The buyer is the transaction's sender. What they will pay is one leg, the
    same kind a swap carries. Nothing is locked and nothing is promised: it
    is an offer, and the answer is a swap that both sides sign.
    """

    txid: bytes           # the inscription being offered for
    take: "Leg"           # what the buyer will pay

    def encode(self) -> bytes:
        if len(self.txid) != 32:
            raise InscriptionError("an offer names a 32-byte inscription")
        return (MAGIC + bytes([VERSION, KIND_OFFER]) + self.txid
                + self.take.encode())


def parse(payload: bytes) -> Chunk | Transfer | Swap | Offer:
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

    if kind == KIND_SWAP:
        give, at = Leg.decode(payload, 6)
        take, at = Leg.decode(payload, at)
        if at != len(payload):
            raise InscriptionError("a swap has nothing after its two legs")
        return Swap(give=give, take=take)

    if kind == KIND_OFFER:
        if len(payload) < 38:
            raise InscriptionError("truncated offer")
        take, at = Leg.decode(payload, 38)
        if at != len(payload):
            raise InscriptionError("an offer has nothing after its price")
        return Offer(txid=payload[6:38], take=take)

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
    if count > MAX_CHUNKS:
        raise InscriptionError(
            f"that file needs {count:,} transactions and the format carries at "
            f"most {MAX_CHUNKS:,}: the countdown that marks the last chunk is "
            f"two bytes. About {MAX_CHUNKS * room / 1_048_576:.0f} MB is the "
            f"ceiling, and it is arithmetic rather than policy.")
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
