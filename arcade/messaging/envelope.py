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
        if self.type == TYPE_KEY_ANNOUNCE:
            return self.bound_bytes()
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
        content: tampering with it can only cause a reassembly failure, which the
        gap and ordering checks already detect, and ordering is in any case
        enforced structurally by the UTXO chain rather than by trusting this
        field. Everything that identifies the message -- magic, version, type and
        message id -- remains authenticated.
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
        if msg_type == TYPE_SINGLE:
            if len(payload) < HEADER_SINGLE_LEN:
                raise EnvelopeError("payload too short to contain a header")
            return cls(type=msg_type, clen=int.from_bytes(payload[6:8], "big"))
        if msg_type != TYPE_KEY_ANNOUNCE:
            raise EnvelopeError(f"unknown message type {msg_type}")
        return cls(type=msg_type)


def is_message_payload(payload: bytes) -> bool:
    """Cheap pre-filter before attempting anything expensive."""
    return len(payload) >= HEADER_BOUND_SINGLE_LEN and payload[:4] == MAGIC


# --- key announcements --------------------------------------------------------


def build_key_announcement(public_bytes: bytes) -> bytes:
    """38 bytes: header + X25519 public key. Fits Class C's 72-byte capacity."""
    if len(public_bytes) != 32:
        raise EnvelopeError(f"public key must be 32 bytes, got {len(public_bytes)}")
    return Header(type=TYPE_KEY_ANNOUNCE).encode() + public_bytes


def parse_key_announcement(payload: bytes) -> bytes:
    header = Header.decode(payload)
    if header.type != TYPE_KEY_ANNOUNCE:
        raise EnvelopeError("not a key announcement")
    if len(payload) < KEY_ANNOUNCE_LEN:
        raise EnvelopeError(
            f"key announcement must be at least {KEY_ANNOUNCE_LEN} bytes, got {len(payload)}"
        )
    # Trailing bytes beyond the key are tolerated: an announcement carried by
    # Class B would arrive NUL-padded to a 30-byte boundary.
    return payload[KEY_ANNOUNCE_HEADER_LEN : KEY_ANNOUNCE_HEADER_LEN + 32]


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
    if header.type not in (TYPE_SINGLE, TYPE_CHUNK):
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
