"""The encrypted envelope, and the framing around it.

Construction (docs/messaging/02-design.md §3):

    inner      = crypto_box(sender_sk, recipient_pk, header || message)
    envelope   = sender_pubkey(32) || inner
    ciphertext = crypto_box_seal(recipient_pk, envelope)

Measured overhead: **126 bytes** for a single-transaction message
= 48 (sealed box) + 32 (sender key) + 40 (crypto_box nonce+MAC) + 6 (header copy).

Three properties this buys, none of which the simpler options give together:

  * **authenticated**  -- the inner box only opens if the sender holds the secret
    half of the key it claims, so identity cannot be forged.
  * **sender-anonymous to observers** -- the sender's key is inside the sealed
    box, not on the chain in the clear.
  * **repudiable** -- the recipient is convinced but cannot convince anyone else,
    because the shared secret would let them forge the same message. For private
    correspondence that is a feature; a signature would be a liability.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass

import nacl.public
from nacl.exceptions import CryptoError

from .keys import Identity

MAGIC = b"arcm"
VERSION = 1

TYPE_SINGLE = 1       # a whole message in one transaction
TYPE_CHUNK = 2        # one link of a chained multi-transaction message
TYPE_KEY_ANNOUNCE = 3 # an X25519 public key announcement
TYPE_GROUP = 4        # a public group post -- NOT encrypted, readable by anyone
TYPE_GROUP_CHUNK = 5  # one link of a public post too large for one transaction
TYPE_API = 6          # one node talking to another: sealed exactly like a private
                      # message, but addressed to a program rather than a person
TYPE_FEED_ACT = 7     # what somebody did to a post: like, reply, share, edit,
                      # delete, tip. Public, unsealed, and one type for all of
                      # them with a kind byte (feed.py, D-138)
TYPE_RELEASE = 8      # one node telling every other that a version exists.
                      # Machine talk: nobody reads it, the updater acts on it,
                      # and it is NOT a post, so the feed never draws it
                      # (release.py, D-147)
TYPE_INSTANCE = 9     # an arcade saying who runs it: its domain and revision,
                      # paid for by its fee address (instance.py). Public.
TYPE_MESH = 10        # a mesh node saying where it can be reached: IP, port and
                      # mesh key, paid for by its fee address (mesh/announce.py).
                      # Public. Older nodes do not know the type and skip it.

# Cleartext header lengths. Both message types carry `clen`, the exact number of
# ciphertext bytes in this payload.
#
# `clen` is NOT optional bookkeeping. Class B pads its final packet with NULs to a
# 30-byte boundary and Omni does not strip that padding (omnicore.cpp:1263), so a
# payload arrives with up to 29 bytes the sender never wrote. Without an explicit
# length the sealed box sees trailing garbage and rejects the message outright.
# Stripping trailing NULs instead would be worse: ciphertext legitimately ends in
# NUL about 1 time in 256.
HEADER_SINGLE_LEN = 8          # magic4 + version1 + type1 + clen2
HEADER_CHUNK_LEN = 18          # + msg_id8 + countdown2
HEADER_BOUND_SINGLE_LEN = 6    # magic4 + version1 + type1
HEADER_BOUND_CHUNK_LEN = 14    # + msg_id8
KEY_ANNOUNCE_HEADER_LEN = 6    # announcements are fixed-size and need no clen
KEY_ANNOUNCE_LEN = 38          # header6 + pubkey32
GROUP_HEADER_LEN = 6           # public, so no clen: nothing is sealed to unpad
#: magic4 + version1 + type1 + msg_id8 + countdown2 + clen2. A public chunk DOES
#: carry `clen`: Class B pads its final packet with NULs, and while a trailing
#: NUL is harmless in text it is not harmless in the middle of a rejoined file.
GROUP_CHUNK_HEADER_LEN = 18

SEALED_OVERHEAD = 48
SENDER_KEY_LEN = 32
CRYPTO_BOX_OVERHEAD = 40   # 24-byte nonce + 16-byte MAC


class EnvelopeError(Exception):
    """The payload is not a well-formed message, or failed authentication."""


# --- framing ------------------------------------------------------------------


@dataclass(frozen=True)
class Header:
    """The cleartext framing that precedes the ciphertext.

    It has to be readable *before* decryption so messages can be recognised and
    reassembled, which means it is also tamperable. Every field here is therefore
    copied inside the authenticated plaintext and compared on open -- see
    `open_message`.
    """

    type: int
    msg_id: bytes = b""          # 8 bytes, chunked messages only
    countdown: int = 0           # 0 marks the FINAL chunk (Doginals convention)
    clen: int = 0                # exact ciphertext length carried in this payload

    def encode(self) -> bytes:
        """The full cleartext header, as it appears on chain."""
        if self.type == TYPE_GROUP_CHUNK:
            if len(self.msg_id) != 8:
                raise EnvelopeError("a public chunk header needs an 8-byte id")
            return (MAGIC + bytes([VERSION, self.type]) + self.msg_id
                    + self.countdown.to_bytes(2, "big")
                    + self.clen.to_bytes(2, "big"))
        if self.type in (TYPE_KEY_ANNOUNCE, TYPE_GROUP, TYPE_FEED_ACT,
                         TYPE_RELEASE, TYPE_INSTANCE, TYPE_MESH):
            # Neither carries `clen`. It exists to undo Class B's NUL padding
            # before opening a sealed box, and nothing here is sealed: an
            # announcement is fixed-length and a group post is plain text, where
            # a trailing NUL is never meaningful.
            return self.bound_bytes()
        # TYPE_SINGLE and TYPE_API share this shape: both are one sealed payload
        # in one transaction, and both need `clen` to undo Class B's padding
        # before the box will open.
        if self.clen > 0xFFFF:
            raise EnvelopeError(f"ciphertext length {self.clen} exceeds a uint16")
        head = self.bound_bytes() + self.clen.to_bytes(2, "big")
        if self.type == TYPE_CHUNK:
            head += self.countdown.to_bytes(2, "big")
        return head

    def bound_bytes(self) -> bytes:
        """The part of the header that is authenticated inside the ciphertext.

        The **countdown is deliberately excluded**. A chunked message is sealed
        once, as a whole, so there is exactly one authenticated header -- but each
        chunk carries a different countdown. Binding it would make reassembly
        impossible by construction.

        Excluding it is safe because the countdown is transport framing, not
        content: the ciphertext is authenticated as a whole, so chunks joined in
        the wrong order fail the MAC and the message is not surfaced. Tampering
        can prevent a message being read; it cannot change what it says.
        Everything that identifies the message -- magic, version, type and
        message id -- remains authenticated.

        This used to add that ordering was "enforced structurally by the UTXO
        chain". That was wrong, and worth recording as wrong: reassembly
        collected chunks by message id and never looked at the chain at all, so
        the guarantee was asserted rather than enforced. Chunked sends did chain
        through change outputs, but nothing verified it -- and once chunks can be
        funded independently they do not chain at all. What actually protects
        reassembly is the MAC above, plus grouping chunks by sender so an
        injected chunk cannot poison a real message.
        """
        head = MAGIC + bytes([VERSION, self.type])
        if self.type == TYPE_CHUNK:
            if len(self.msg_id) != 8:
                raise EnvelopeError("chunk header needs an 8-byte message id")
            return head + self.msg_id
        return head

    @property
    def length(self) -> int:
        """Length of the full cleartext header on chain."""
        if self.type == TYPE_KEY_ANNOUNCE:
            return KEY_ANNOUNCE_HEADER_LEN
        if self.type in (TYPE_GROUP, TYPE_FEED_ACT, TYPE_RELEASE, TYPE_INSTANCE, TYPE_MESH):
            # Public and unsealed, so neither carries `clen`: it exists to
            # undo Class B's padding before a sealed box will open.
            return GROUP_HEADER_LEN
        if self.type == TYPE_GROUP_CHUNK:
            return GROUP_CHUNK_HEADER_LEN
        return HEADER_CHUNK_LEN if self.type == TYPE_CHUNK else HEADER_SINGLE_LEN

    @property
    def bound_length(self) -> int:
        """Length of the authenticated copy carried inside the ciphertext."""
        return HEADER_BOUND_CHUNK_LEN if self.type == TYPE_CHUNK else HEADER_BOUND_SINGLE_LEN

    @classmethod
    def decode(cls, payload: bytes) -> "Header":
        if len(payload) < HEADER_BOUND_SINGLE_LEN:
            raise EnvelopeError("payload too short to contain a header")
        if payload[:4] != MAGIC:
            raise EnvelopeError("not a DogecoinArcade message (bad magic)")
        version, msg_type = payload[4], payload[5]
        if version != VERSION:
            raise EnvelopeError(f"unsupported message version {version}")
        if msg_type == TYPE_CHUNK:
            if len(payload) < HEADER_CHUNK_LEN:
                raise EnvelopeError("payload too short to contain a chunk header")
            return cls(
                type=msg_type,
                msg_id=payload[6:14],
                clen=int.from_bytes(payload[14:16], "big"),
                countdown=int.from_bytes(payload[16:18], "big"),
            )
        if msg_type in (TYPE_SINGLE, TYPE_API):
            # The same shape: one sealed payload in one transaction, with the
            # ciphertext length that undoes Class B's padding.
            if len(payload) < HEADER_SINGLE_LEN:
                raise EnvelopeError("payload too short to contain a header")
            return cls(type=msg_type, clen=int.from_bytes(payload[6:8], "big"))
        if msg_type == TYPE_GROUP_CHUNK:
            if len(payload) < GROUP_CHUNK_HEADER_LEN:
                raise EnvelopeError("payload too short for a public chunk header")
            return cls(
                type=msg_type,
                msg_id=payload[6:14],
                countdown=int.from_bytes(payload[14:16], "big"),
                clen=int.from_bytes(payload[16:18], "big"),
            )
        if msg_type not in (TYPE_KEY_ANNOUNCE, TYPE_GROUP, TYPE_FEED_ACT,
                            TYPE_RELEASE, TYPE_INSTANCE, TYPE_MESH):
            raise EnvelopeError(f"unknown message type {msg_type}")
        return cls(type=msg_type)


def is_message_payload(payload: bytes) -> bool:
    """Cheap pre-filter before attempting anything expensive."""
    return len(payload) >= HEADER_BOUND_SINGLE_LEN and payload[:4] == MAGIC


# --- key announcements --------------------------------------------------------


#: Marks the optional tail that carries who the key belongs to. A NUL here means
#: Class B padding, not a tail, which is why the tag is 0x01 rather than a length.
ANNOUNCE_TAG_IDENTITY = 0x01

#: The same person's address on the other chain, so one announcement answers
#: both "where do I message them" and "where do I pay them" (D-032). Sections
#: come in order and a reader stops at the first byte it does not know, which
#: is how an older reader still gets the key and the identity address.
ANNOUNCE_TAG_OTHER_CHAIN = 0x02

#: The @tag this address holds. Unlike a name, a reader can check it: the tag
#: index says which address holds the name, and disagreement means the
#: announcement is wrong, not the chain.
ANNOUNCE_TAG_HANDLE = 0x03

#: The inscription this address uses as a profile picture, as its 32-byte
#: txid. Published rather than stored anywhere, so one search finds the name,
#: both addresses, the key AND the face (D-138). It is honoured only while
#: the chain says that address still holds the piece: a picture of something
#: somebody has sold is a picture of somebody else's property, so the check
#: is made on every draw rather than at announcement time.
ANNOUNCE_TAG_PFP = 0x04

#: A line about yourself, and a link. Published with the name rather than
#: stored anywhere, for the same reason the picture is: one search gives
#: somebody everything a profile shows, and a node that has never spoken to
#: you can draw it (D-145). Short on purpose -- this is chain, paid for by
#: the person writing it, and a profile is not an essay.
ANNOUNCE_TAG_BIO = 0x05
ANNOUNCE_TAG_URL = 0x06

#: What each may say. A bio is a sentence or two; a link is a link.
MAX_ANNOUNCE_BIO = 160
MAX_ANNOUNCE_URL = 120

#: What is left for the name once everything else is accounted for. Computed,
#: not written down: the first attempt hardcoded 16 and was wrong, because it
#: forgot the 4-byte AnyData wrapper that every Arcade payload carries. A
#: published name is short -- 12 bytes -- and that is the price of an
#: announcement costing a flat fee instead of dust.
_ANNOUNCE_FIXED = KEY_ANNOUNCE_HEADER_LEN + 32 + 1 + 20 + 1   # header, key, tag, hash, length
_ANYDATA_OVERHEAD = 4


def _max_announce_name() -> int:
    from ..encoding import max_class_c_payload
    return max(0, max_class_c_payload() - _ANYDATA_OVERHEAD - _ANNOUNCE_FIXED)


#: What fits a single OP_RETURN, and therefore what costs a flat fee and no dust.
MAX_ANNOUNCE_NAME = _max_announce_name()

#: The hard ceiling. A name longer than MAX_ANNOUNCE_NAME is still publishable --
#: it simply goes as Class B instead, costing a couple of dust outputs rather
#: than nothing. That is a far better trade than silently cutting somebody's name
#: in half: "Big Chief Energy" went on the chain twice as "Big Chief En",
#: permanently, for a fee that cannot be taken back.
MAX_ANNOUNCE_NAME_CLASS_B = 64


def build_key_announcement(public_bytes: bytes, hash160: bytes = b"",
                           name: str = "", other_hash160: bytes = b"",
                           tag: str = "", pfp: str | bytes = b"",
                           bio: str = "", url: str = "") -> bytes:
    """Header + X25519 public key, optionally saying whose key it is.

    The bare form is 38 bytes and is what earlier versions publish. The tail adds
    two things that solve real problems:

    - **The identity address**, as its 20-byte hash160. Without it, an
      announcement is attributed to whichever address funded the transaction,
      and that address changes with coin selection -- so the address a reader
      files the key under is not the one the user was told to hand out. a test machine
      measured three different addresses in play at once.
    - **A name**, so a reader's address book can fill itself in rather than
      showing a row of base58.

    The name is unverified by construction: anyone can publish any name. It is a
    convenience for the reader, never a claim the chain can support, and the
    interface says so wherever it is shown.
    """
    if len(public_bytes) != 32:
        raise EnvelopeError(f"public key must be 32 bytes, got {len(public_bytes)}")

    body = Header(type=TYPE_KEY_ANNOUNCE).encode() + public_bytes
    if not hash160:
        return body

    if len(hash160) != 20:
        raise EnvelopeError(f"address hash must be 20 bytes, got {len(hash160)}")

    # Refused rather than trimmed. Truncating here was silent, permanent and
    # paid for; a caller that wants a shorter name can shorten it deliberately.
    encoded = (name or "").strip().encode()
    if len(encoded) > MAX_ANNOUNCE_NAME_CLASS_B:
        raise EnvelopeError(
            f"a published name is limited to {MAX_ANNOUNCE_NAME_CLASS_B} bytes; "
            f"this one is {len(encoded)}")
    out = body + bytes([ANNOUNCE_TAG_IDENTITY]) + hash160 + bytes([len(encoded)]) + encoded
    if other_hash160:
        if len(other_hash160) != 20:
            raise EnvelopeError(
                f"the other chain's address hash must be 20 bytes, got "
                f"{len(other_hash160)}")
        out += bytes([ANNOUNCE_TAG_OTHER_CHAIN]) + other_hash160
    handle = (tag or "").strip().lstrip("@").encode()
    if handle:
        if len(handle) > 255:
            raise EnvelopeError("that is not a tag")
        out += bytes([ANNOUNCE_TAG_HANDLE, len(handle)]) + handle
    if pfp:
        raw = bytes.fromhex(pfp) if isinstance(pfp, str) else pfp
        if len(raw) != 32:
            raise EnvelopeError("a profile picture is an inscription, which is "
                                "named by a 32-byte transaction id")
        out += bytes([ANNOUNCE_TAG_PFP]) + raw
    for kind, value, ceiling, what in (
        (ANNOUNCE_TAG_BIO, bio, MAX_ANNOUNCE_BIO, "a bio"),
        (ANNOUNCE_TAG_URL, url, MAX_ANNOUNCE_URL, "a link"),
    ):
        said = " ".join((value or "").split())
        if not said:
            continue
        encoded = said.encode()
        if len(encoded) > ceiling:
            # Refused rather than trimmed, like every other thing that goes
            # on the chain here: a truncation is silent, permanent and paid
            # for.
            raise EnvelopeError(
                f"{what} is limited to {ceiling} bytes; this one is "
                f"{len(encoded)}")
        out += bytes([kind, len(encoded)]) + encoded
    return out


def announcement_fits_one_output(payload: bytes) -> bool:
    """Whether this announcement can go as a single OP_RETURN.

    A name up to MAX_ANNOUNCE_NAME does; a longer one needs Class B, which costs
    a couple of unspendable outputs instead of nothing. Cheap either way, and a
    great deal better than publishing half of somebody's name.
    """
    from ..encoding import max_class_c_payload
    from ..payload import AnyData

    return len(AnyData(data=payload).encode()) <= max_class_c_payload()


def parse_key_announcement(payload: bytes) -> bytes:
    header = Header.decode(payload)
    if header.type != TYPE_KEY_ANNOUNCE:
        raise EnvelopeError("not a key announcement")
    if len(payload) < KEY_ANNOUNCE_LEN:
        raise EnvelopeError(
            f"key announcement must be at least {KEY_ANNOUNCE_LEN} bytes, got {len(payload)}"
        )
    # Trailing bytes beyond the key are tolerated: an announcement carried by
    # Class B would arrive NUL-padded to a 30-byte boundary, and a newer sender
    # may append the tail below, which an older reader should simply ignore.
    return payload[KEY_ANNOUNCE_HEADER_LEN : KEY_ANNOUNCE_HEADER_LEN + 32]


def parse_announced_identity(payload: bytes) -> tuple[bytes, str]:
    """Return (hash160, name) from an announcement's tail, or (b"", "")."""
    extras = parse_announced_extras(payload)
    return extras["hash160"], extras["name"]


def parse_announced_extras(payload: bytes) -> dict:
    """Everything an announcement says about who it belongs to.

    `{"hash160": b"", "name": "", "other_hash160": b"", "tag": ""}` for a bare
    announcement. Never raises: these bytes came from a stranger, and an
    announcement whose extras are damaged should still yield its key rather
    than being discarded. A section it cannot read ends the walk, so what was
    understood up to that point is kept.
    """
    found = {"hash160": b"", "name": "", "other_hash160": b"", "tag": "",
             "pfp": "", "bio": "", "url": ""}
    at = KEY_ANNOUNCE_HEADER_LEN + 32
    if len(payload) <= at or payload[at] != ANNOUNCE_TAG_IDENTITY:
        return found              # bare announcement, or NUL padding
    try:
        found["hash160"] = payload[at + 1 : at + 21]
        if len(found["hash160"]) != 20:
            return {**found, "hash160": b""}
        length = payload[at + 21]
        name = payload[at + 22 : at + 22 + length]
        if len(name) != length:
            return found
        # Bounded by the Class B ceiling, not the single-output one: a longer
        # name is published as Class B, and cutting it here would undo that on
        # the way in -- the reader would see the truncation the sender paid
        # extra to avoid.
        found["name"] = name.decode("utf-8", "replace").strip()[:MAX_ANNOUNCE_NAME_CLASS_B]
        at = at + 22 + length
        while at < len(payload):
            kind = payload[at]
            if kind == ANNOUNCE_TAG_OTHER_CHAIN:
                other = payload[at + 1 : at + 21]
                if len(other) != 20:
                    break
                found["other_hash160"] = other
                at += 21
            elif kind == ANNOUNCE_TAG_PFP:
                piece = payload[at + 1 : at + 33]
                if len(piece) != 32:
                    break
                found["pfp"] = piece.hex()
                at += 33
            elif kind in (ANNOUNCE_TAG_BIO, ANNOUNCE_TAG_URL):
                length = payload[at + 1]
                raw = payload[at + 2 : at + 2 + length]
                if len(raw) != length:
                    break
                said = raw.decode("utf-8", "replace").strip()
                if kind == ANNOUNCE_TAG_BIO:
                    found["bio"] = said[:MAX_ANNOUNCE_BIO]
                else:
                    # A link somebody can click, so only these: this is text
                    # anybody can publish and it lands on a page (D-097).
                    found["url"] = said[:MAX_ANNOUNCE_URL] if said.startswith(
                        ("https://", "http://")) else ""
                at += 2 + length
            elif kind == ANNOUNCE_TAG_HANDLE:
                length = payload[at + 1]
                raw = payload[at + 2 : at + 2 + length]
                if len(raw) != length:
                    break
                found["tag"] = raw.decode("utf-8", "replace").strip()
                at += 2 + length
            else:
                break             # NUL padding, or a section written by a
                                  # newer sender than this reader
    except IndexError:
        return found
    return found


# --- sealing and opening ------------------------------------------------------


def seal_ciphertext(
    sender: Identity, recipient_public: bytes, header: Header, message: bytes
) -> bytes:
    """Encrypt a whole message and return **only the ciphertext**.

    Framing is deliberately not done here. A chunked message is sealed once and
    then split, so exactly one ciphertext is produced but many cleartext headers
    are written -- mixing the two layers means the first chunk ends up carrying
    two headers, which is a bug this separation makes impossible.

    The message is encrypted **once, in full**. Chunking happens afterwards, on
    the finished ciphertext -- encrypting per chunk would multiply the 126-byte
    overhead by the chunk count and expose the chunk structure.
    """
    recipient = nacl.public.PublicKey(recipient_public)

    # crypto_box generates a fresh random 24-byte nonce internally and prepends
    # it. 192 bits of randomness needs no counter and no stored state: a
    # collision takes about 2^96 messages. Deliberately NOT derived from
    # transaction data -- an unbroadcast transaction can be rebuilt with the same
    # inputs, which would silently reuse a nonce under the same key pair.
    inner = nacl.public.Box(sender.secret, recipient).encrypt(header.bound_bytes() + message)
    envelope = sender.public_bytes + bytes(inner)
    return nacl.public.SealedBox(recipient).encrypt(envelope)


def seal_message(
    sender: Identity, recipient_public: bytes, header: Header, message: bytes
) -> bytes:
    """Produce a complete single-transaction payload: `header || ciphertext`."""
    blob = seal_ciphertext(sender, recipient_public, header, message)
    # The header records the exact ciphertext length, so a reader can discard the
    # Class B padding that will be appended beneath it.
    return dataclasses.replace(header, clen=len(blob)).encode() + blob


def open_ciphertext(
    recipient: Identity, header: Header, blob: bytes
) -> tuple[bytes, bytes]:
    """Open a ciphertext whose header is already known.

    Used when reassembling a chunked message, where the ciphertext is rejoined
    from several transactions and there is no single payload to parse.
    """
    try:
        envelope = nacl.public.SealedBox(recipient.secret).decrypt(blob)
    except CryptoError:
        raise EnvelopeError("not addressed to us, or corrupt") from None

    if len(envelope) < SENDER_KEY_LEN + CRYPTO_BOX_OVERHEAD:
        raise EnvelopeError("envelope too short")

    sender_public = envelope[:SENDER_KEY_LEN]
    try:
        plain = nacl.public.Box(
            recipient.secret, nacl.public.PublicKey(sender_public)
        ).decrypt(envelope[SENDER_KEY_LEN:])
    except CryptoError:
        raise EnvelopeError("sender authentication failed: forged sender identity") from None

    bound = header.bound_bytes()
    if len(plain) < len(bound) or plain[: len(bound)] != bound:
        raise EnvelopeError("header does not match its authenticated copy")

    return sender_public, plain[len(bound) :]


def open_message(recipient: Identity, payload: bytes) -> tuple[bytes, bytes, Header]:
    """Open a complete single-transaction payload addressed to us.

    Returns (sender_public_bytes, message, header).

    Raises EnvelopeError if the payload is not ours, is corrupt, or fails any
    authentication check. Callers scanning the chain should treat that as the
    normal case: almost every payload belongs to someone else.
    """
    header = Header.decode(payload)
    if header.type not in (TYPE_SINGLE, TYPE_CHUNK, TYPE_API):
        raise EnvelopeError(f"type {header.type} is not an encrypted message")

    blob = payload[header.length :]
    if header.clen:
        if header.clen > len(blob):
            raise EnvelopeError(
                f"header claims {header.clen} ciphertext bytes but only {len(blob)} are present"
            )
        blob = blob[: header.clen]     # discard Class B padding
    sender_public, message = open_ciphertext(recipient, header, blob)
    return sender_public, message, header


def overhead_for(header: Header) -> int:
    """Total payload bytes a message costs beyond its own length.

    = sealed box + sender key + crypto_box + the authenticated header copy
      + the cleartext header that appears on chain.
    """
    return (
        SEALED_OVERHEAD + SENDER_KEY_LEN + CRYPTO_BOX_OVERHEAD
        + header.bound_length + header.length
    )


def new_message_id() -> bytes:
    return os.urandom(8)
