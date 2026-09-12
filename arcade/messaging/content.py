"""What is inside a decrypted message: text, a file, and who sent it.

Why this is a layer of its own
------------------------------
The envelope (:mod:`arcade.messaging.envelope`) decides how bytes are carried and
chained; it has no opinion about what they mean. Everything here lives *inside*
the sealed box, so it is encrypted along with the message and invisible to
anyone watching the chain -- including the filename and the sender's name.

It also means attachments and profiles needed no wire format change at all.
Chunking, scanning, reassembly and the existing tests are untouched, and a
message from a version that predates this still reads correctly: a body with no
marker is exactly what it always was, plain content.

On base64
---------
Files are carried as raw bytes, not base64. Base64 exists to survive text-only
transports, and this one is binary-clean: Class B packets carry arbitrary bytes
and the ciphertext is already indistinguishable from noise. Encoding would add a
third to the payload, and payload is what dust cost tracks -- a 30 KB photo would
pay for 40 KB of outputs to no purpose. The chunk countdown that Doginals uses is
already here, in the envelope, ending at zero on the final chunk.

On sending a profile
--------------------
A first message can carry the sender's name and public addresses, so the
recipient's address book fills itself in and they can see who is making them an
offer rather than a string of base58. That is a genuine convenience and a
genuine disclosure: it ties the messaging identity to a mainnet address, to
anyone the user writes to. So it is attached to the *first* message to a new
contact and not after, it is visible in the interface before sending, and it can
be turned off.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

#: Marks a structured body. The leading control byte cannot begin valid UTF-8
#: text that a person typed, so a plain message is never mistaken for one.
BODY_MAGIC = b"\x01ARCB"
BODY_VERSION = 1

#: A ceiling on a single attachment.
#:
#: 5 MB was an invented number. The real limits are the chain's, measured from
#: the encoder rather than reasoned about -- a multisig output carries TWO
#: 30-byte packets, so it is 60 bytes per output, not 30. An earlier version of
#: this comment said otherwise and was wrong by exactly a factor of two:
#:
#:     1 MB    138 transactions     17,911 dust outputs      179 PEP    2 MB on chain
#:     5 MB    688 transactions     89,551 dust outputs      896 PEP    8 MB
#:    14 MB  1,925 transactions    250,740 dust outputs    2,507 PEP   23 MB
#:    32 MB  4,399 transactions    573,119 dust outputs    5,731 PEP   53 MB
#:
#: Still enormous. The dust is permanently unspendable, and a Dogecoin-family
#: block is 1 MB, so a 14 MB attachment needs at least 23 blocks of a chain it
#: does not have to itself.
#:
#: Messaging is testnet-only and permanently so (D-010), where the coins are
#: mined and free -- so this is a question of patience and block space rather
#: than money, and the user is the one who should answer it. The cap is raised to
#: something the chain could plausibly carry, and the cost is shown before
#: sending rather than enforced by a number nobody can see.
MAX_FILE_BYTES = 32 * 1024 * 1024


class ContentError(Exception):
    """The body could not be built or understood."""


@dataclass
class Profile:
    """Who the sender says they are. Unverified by construction."""

    name: str = ""
    testnet_address: str = ""
    mainnet_address: str = ""

    def is_empty(self) -> bool:
        return not (self.name or self.testnet_address or self.mainnet_address)

    def as_dict(self) -> dict[str, str]:
        return {k: v for k, v in {
            "name": self.name,
            "testnet_address": self.testnet_address,
            "mainnet_address": self.mainnet_address,
        }.items() if v}


@dataclass
class Attachment:
    name: str = ""
    content_type: str = "application/octet-stream"
    data: bytes = b""


@dataclass
class Content:
    """A decoded message body."""

    text: str = ""
    attachment: Attachment | None = None
    profile: Profile | None = None
    #: True when the body carried no marker, i.e. it is from before this existed
    #: or is simply plain content.
    plain: bool = True
    raw: bytes = b""


def build(text: str = "", attachment: Attachment | None = None,
          profile: Profile | None = None) -> bytes:
    """Encode a body. Plain text stays byte-identical to what it always was."""
    if attachment is None and (profile is None or profile.is_empty()):
        return text.encode()

    if attachment is not None:
        if len(attachment.data) > MAX_FILE_BYTES:
            raise ContentError(
                f"{attachment.name or 'that file'} is "
                f"{len(attachment.data) / 1_048_576:.1f} MB, over the "
                f"{MAX_FILE_BYTES // 1_048_576} MB limit. Every 30 bytes becomes "
                f"an output that can never be spent again, and a block holds "
                f"about 70 of these transactions, so a file this size would take "
                f"the chain to itself for a long time.")
        if not attachment.data:
            raise ContentError("that file is empty.")

    header: dict[str, Any] = {}
    if text:
        header["text"] = text
    if attachment is not None:
        header["file"] = {
            "name": _safe_name(attachment.name),
            "type": attachment.content_type or "application/octet-stream",
            "size": len(attachment.data),
        }
    if profile is not None and not profile.is_empty():
        header["profile"] = profile.as_dict()

    encoded = json.dumps(header, separators=(",", ":")).encode()
    if len(encoded) > 0xFFFF:
        raise ContentError("the message description is too long.")
    body = bytearray(BODY_MAGIC)
    body.append(BODY_VERSION)
    body += len(encoded).to_bytes(2, "big")
    body += encoded
    if attachment is not None:
        body += attachment.data
    return bytes(body)


def parse(body: bytes) -> Content:
    """Decode a body. Anything unrecognised is treated as plain content.

    Never raises on malformed input: this runs on bytes that arrived from
    somebody else, and a message that cannot be parsed should still be shown as
    whatever it is rather than disappearing.
    """
    if not body.startswith(BODY_MAGIC):
        return _plain(body)
    if len(body) < len(BODY_MAGIC) + 3:
        return _plain(body)

    offset = len(BODY_MAGIC)
    version = body[offset]
    offset += 1
    if version != BODY_VERSION:
        # A newer sender. Show what can be shown rather than nothing.
        return _plain(body)

    size = int.from_bytes(body[offset:offset + 2], "big")
    offset += 2
    try:
        header = json.loads(body[offset:offset + size].decode())
        if not isinstance(header, dict):
            raise ValueError
    except Exception:
        return _plain(body)
    offset += size

    attachment = None
    info = header.get("file")
    if isinstance(info, dict):
        declared = info.get("size")
        data = body[offset:]
        # Trust the bytes present, not the declared length: a truncated message
        # should give a short file rather than an exception.
        if isinstance(declared, int) and 0 <= declared <= len(data):
            data = data[:declared]
        attachment = Attachment(
            name=_safe_name(str(info.get("name", "") or "")),
            content_type=str(info.get("type", "") or "application/octet-stream"),
            data=data,
        )

    profile = None
    sent = header.get("profile")
    if isinstance(sent, dict):
        profile = Profile(
            name=str(sent.get("name", "") or "")[:80],
            testnet_address=str(sent.get("testnet_address", "") or "")[:64],
            mainnet_address=str(sent.get("mainnet_address", "") or "")[:64],
        )
        if profile.is_empty():
            profile = None

    text = header.get("text")
    return Content(
        text=str(text) if isinstance(text, str) else "",
        attachment=attachment, profile=profile, plain=False, raw=body,
    )


def _plain(body: bytes) -> Content:
    return Content(text=body.decode("utf-8", errors="replace"), plain=True, raw=body)


def _safe_name(name: str) -> str:
    """A filename that cannot escape a directory or name a device.

    The sender chooses this string and the receiver may save it, so it is
    untrusted input on the way to a filesystem.
    """
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable() and c not in '<>:"|?*')
    name = name.strip().strip(".")
    return (name or "attachment")[:120]
