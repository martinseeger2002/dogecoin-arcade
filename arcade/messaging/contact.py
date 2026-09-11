"""Contact codes: exchanging keys without the chain.

The bootstrapping problem this solves
--------------------------------------
To encrypt a message to someone you need their X25519 public key. Discovering it
from an on-chain announcement requires that they have already published one,
which requires coins, which requires mining and a four-hour maturity wait. On a
network where nobody has run this before, that leaves a messenger in which no
first message can ever be sent.

A contact code is the same public key in a form that travels by any channel --
email, chat, paper, a QR code. Two people exchange codes and can message
immediately, with nothing on chain at all. Publishing then becomes what it
should always have been: a convenience for discovery, not a precondition.

Format
------
    arcade:<network>:<base58 public key>:<check>

The network is included because Dogecoin testnet and Pepecoin testnet share
address version bytes, so a key alone cannot say which chain it belongs to. The
check is the first four bytes of the fingerprint, so a mistyped code is rejected
rather than silently producing an unreadable message.
"""

from __future__ import annotations

import hashlib

from ..script import b58decode, b58encode
from .keys import fingerprint_of

PREFIX = "arcade"


class ContactError(Exception):
    """The contact code is malformed."""


def encode(network: str, public_bytes: bytes) -> str:
    """Build a shareable contact code for a public key."""
    if len(public_bytes) != 32:
        raise ContactError(f"a public key is 32 bytes, got {len(public_bytes)}")
    check = hashlib.sha256(public_bytes).hexdigest()[:4]
    return f"{PREFIX}:{network}:{b58encode(public_bytes)}:{check}"


def decode(code: str) -> tuple[str, bytes]:
    """Return (network, public key) from a contact code.

    Tolerant of whitespace and case in the prefix, since these get copied out of
    emails and chat windows where both get mangled.
    """
    cleaned = "".join(code.split())
    parts = cleaned.split(":")
    if len(parts) != 4 or parts[0].lower() != PREFIX:
        raise ContactError(
            "that does not look like a contact code. They start with "
            f"'{PREFIX}:' and have four parts separated by colons."
        )

    _, network, encoded, check = parts
    try:
        public_bytes = b58decode(encoded)
    except Exception as exc:
        raise ContactError(f"the key part is not valid base58: {exc}") from None

    if len(public_bytes) != 32:
        raise ContactError(
            f"the key part decodes to {len(public_bytes)} bytes, not 32 -- "
            "the code is probably truncated"
        )
    if hashlib.sha256(public_bytes).hexdigest()[:4] != check.lower():
        raise ContactError(
            "the check digits do not match, so the code was mistyped or "
            "corrupted in transit. Ask for it again."
        )
    return network, public_bytes


def describe(code: str) -> str:
    """A one-line summary of a code, for confirming it with a human."""
    network, public_bytes = decode(code)
    return f"{network} - {fingerprint_of(public_bytes)}"
