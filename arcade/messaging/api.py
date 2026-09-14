"""Node-to-node messages: one DogecoinArcade talking to another.

Why it exists
-------------
A marketplace, a bot, an exchange of offers between two installations -- all of
them need one node to say something to another node and be certain it arrived,
unaltered, from who it claims. That is the private messenger's job already; what
it lacks is a way for a PROGRAM to send and read without pretending to be a
person typing in a chat window.

So this is the same envelope, the same X25519 sealed box, the same chain, with
two differences:

* its own envelope type, so an API message never lands in a human conversation
  -- a chat full of machine chatter the reader cannot act on is worse than no
  chat at all;
* its own table and its own cursor, so a program can read a queue, remember
  exactly where it stopped, and resume there.

Why on the chain at all
-----------------------
Because a custom peer-to-peer message does not propagate. Pepecoin tolerates an
unknown command and then drops it without forwarding (`net_processing.cpp`), so
a direct message reaches only the peers you happen to be connected to. The
mempool, by contrast, already gossips to every node on the network in seconds.
That is the broadcast channel; see `docs/p2p-messaging.md`.

Testnet only, like every other encrypted message here (D-010).

One transaction
---------------
An API message is a command, not a file: it is refused if it does not fit a
single transaction. Roughly 7.4 KB of payload, which is a great deal of JSON,
and the limit keeps a bot from quietly spending a fortune in a loop.
"""

from __future__ import annotations

import hashlib
import json as jsonlib
from dataclasses import dataclass

from ..encoding import MAX_CLASS_B_PAYLOAD
from .envelope import TYPE_API, EnvelopeError, Header, overhead_for, seal_message
from .keys import Identity

#: Stamped inside the sealed plaintext of every message: "DA", the protocol
#: number, and four bytes identifying the API surface the sender is speaking.
#:
#: Two nodes that disagree about the API are the failure this exists to catch,
#: and catching it at read time is far better than the alternative -- a command
#: that means one thing here and another there, acted on in good faith. It rides
#: INSIDE the ciphertext so it is authenticated with everything else: a stamp on
#: the outside could be edited by anyone who relayed the transaction.
STAMP_MAGIC = b"DA"
PROTOCOL = 1
STAMP_LEN = 8          # magic2 + protocol1 + apihash4 + reserved1

#: What one transaction will carry, after the AnyData header, the envelope and
#: the stamp.
MAX_API_PAYLOAD = (MAX_CLASS_B_PAYLOAD - 4
                   - overhead_for(Header(type=TYPE_API)) - STAMP_LEN)


def api_fingerprint(methods: list[str] | None = None) -> bytes:
    """Four bytes over the API surface: what has to match, not who built it.

    Derived from the method names themselves rather than from a version string,
    so it changes when the API changes and NOT when something unrelated does.
    Two nodes on different commits that speak the same API are compatible, and
    saying so is more useful than insisting they run identical builds.
    """
    if methods is None:
        from ..web.rpc import OmniRpc
        methods = [name for name in dir(OmniRpc) if name.startswith("da_")]
    material = f"{PROTOCOL}:" + ",".join(sorted(methods))
    return hashlib.sha256(material.encode()).digest()[:4]


def stamp(fingerprint: bytes | None = None) -> bytes:
    fingerprint = fingerprint if fingerprint is not None else api_fingerprint()
    if len(fingerprint) != 4:
        raise ApiMessageError("an API fingerprint is 4 bytes")
    return STAMP_MAGIC + bytes([PROTOCOL]) + fingerprint + b"\x00"


def read_stamp(plain: bytes) -> tuple[int, bytes, bytes]:
    """(protocol, fingerprint, body). An unstamped message is protocol 0.

    Unstamped rather than rejected: a message from something older than the
    stamp is still readable, and telling the caller "protocol 0, decide for
    yourself" is more useful than refusing to hand over bytes that opened
    perfectly well.
    """
    if len(plain) >= STAMP_LEN and plain[:2] == STAMP_MAGIC:
        return plain[2], plain[3:7], plain[STAMP_LEN:]
    return 0, b"", plain


class ApiMessageError(Exception):
    """The message cannot be sent as asked."""


@dataclass(frozen=True)
class ApiMessage:
    """What one node said to another."""

    id: int
    txid: str
    height: int
    block_time: int
    sender_pubkey: bytes
    sender_address: str
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> object | None:
        """The body as JSON, or None if it is not JSON.

        Not an error: the channel carries bytes, and a caller that wants to send
        something else is entitled to. This is a convenience for the common case.
        """
        try:
            return jsonlib.loads(self.body)
        except ValueError:
            return None


def seal(sender: Identity, recipient_public: bytes, body: bytes) -> bytes:
    """The complete payload for one API message, ready to go in a transaction."""
    if not body:
        raise ApiMessageError("there is nothing to send.")
    if len(body) > MAX_API_PAYLOAD:
        raise ApiMessageError(
            f"an API message is one transaction: {len(body):,} bytes is over "
            f"the {MAX_API_PAYLOAD:,} that carries. Send a reference to an "
            f"inscription instead of the thing itself.")
    if len(recipient_public) != 32:
        raise ApiMessageError("a recipient is a 32-byte public key")
    return seal_message(sender, recipient_public, Header(type=TYPE_API),
                        stamp() + body)


def open_payload(recipient: Identity, payload: bytes) -> tuple[bytes, bytes]:
    """(sender public key, body) for a payload addressed to us, stamp removed."""
    sender_public, _, _, body = open_stamped(recipient, payload)
    return sender_public, body


def open_stamped(recipient: Identity,
                 payload: bytes) -> tuple[bytes, int, bytes, bytes]:
    """(sender key, protocol, api fingerprint, body).

    Raises `EnvelopeError` exactly as the private path does, and for the same
    reasons -- not ours, corrupt, or a forged sender.
    """
    from .envelope import open_message

    sender_public, plain, header = open_message(recipient, payload)
    if header.type != TYPE_API:
        raise EnvelopeError("not an API message")
    protocol, fingerprint, body = read_stamp(plain)
    return sender_public, protocol, fingerprint, body


def compatible(protocol: int, fingerprint: bytes) -> bool:
    """Does the sender speak the API this node speaks?"""
    return protocol == PROTOCOL and fingerprint == api_fingerprint()
