"""Public group posts: a shared channel anyone can read.

How this differs from everything else here
------------------------------------------
Every other message in this application is sealed to one recipient and is
indistinguishable from noise to anyone else. A group post is the opposite by
design: **it is not encrypted**. Anyone with a node can read it, now and forever,
and there is no way to take it back. That is the point of a public channel, and
it is stated plainly wherever one can be written, because the rest of the
application trains the opposite expectation.

There is no member list, no invitation and no moderation. A channel is a name,
and posting to it is writing that name onto the chain. Two people who use the
same name are in the same channel; that is the whole mechanism.

Why both chains, when messaging is testnet-only
-----------------------------------------------
D-010 makes the *Messenger* testnet-only, permanently, and that has not changed:
nothing encrypted ever touches mainnet. A public post is a different thing with a
different risk -- it carries no key material and reveals nothing that posting it
does not already reveal -- so it can run on either chain. On mainnet it spends
real coins, which is the reason the interface treats the two differently rather
than presenting one feature that quietly costs money.

Carriage
--------
A text-only post is Class C: a single OP_RETURN. One output, no multisig, and
therefore **no dust** -- it costs a fee and nothing else. That caps it at about
60 characters, which is a real constraint and the right trade for talk.

A post carrying a file has to be Class B, because an OP_RETURN cannot hold one.
Class B carries up to 7.5 KB per transaction but pays roughly one unspendable
output per 30 bytes, so a 100 KB image costs several thousand dust outputs. That
is a different order of expense from a sentence, so the interface prices it
before sending rather than after, and the two cases are not presented as one
button that sometimes costs a hundred times more.
"""

from __future__ import annotations

import secrets

from dataclasses import dataclass

from ..encoding import MAX_CLASS_B_PAYLOAD, max_class_c_payload
from ..payload import AnyData
from .envelope import (
    GROUP_CHUNK_HEADER_LEN, MAGIC, TYPE_GROUP, TYPE_GROUP_CHUNK, VERSION,
    EnvelopeError, Header,
)

#: The channel used when nobody chooses one.
DEFAULT_CHANNEL = "main"

#: Bounded so the length prefixes stay one byte, and because a channel name and
#: a nickname are paid for in the same bytes as the message itself.
MAX_CHANNEL = 24
MAX_NICKNAME = 24
MAX_FILENAME = 120
MAX_TYPE = 60

#: A hard ceiling on a public attachment. Not a carriage limit -- posts chunk --
#: but a cost one: at roughly one unspendable output per 30 bytes, a megabyte is
#: already about 35,000 outputs.
MAX_FILE_BYTES = 1024 * 1024

#: 4 bytes are lost to the AnyData wrapper that every Arcade payload carries.
ANYDATA_OVERHEAD = 4

#: Distinguishes a post carrying a file from one that is only text. A reader
#: that predates attachments sees it as the start of the text and shows
#: something odd rather than crashing; readers that know it skip the framing.
FILE_MARKER = b"\x00ARCF"


class GroupError(Exception):
    """The post could not be built or understood."""


@dataclass
class GroupPost:
    channel: str = DEFAULT_CHANNEL
    nickname: str = ""
    text: str = ""
    #: An optional file. Present means Class B carriage and a much larger bill;
    #: see the module docstring.
    file_name: str = ""
    file_type: str = ""
    file_data: bytes = b""

    @property
    def has_file(self) -> bool:
        return bool(self.file_data)


def max_text_bytes(channel: str = DEFAULT_CHANNEL, nickname: str = "") -> int:
    """How much text still fits, given what the channel and nickname cost.

    Worth showing to the user as they type: on mainnet every byte here is paid
    for, and the limit is low enough to hit constantly.
    """
    used = (ANYDATA_OVERHEAD + Header(type=TYPE_GROUP).length
            + 1 + len(channel.encode()) + 1 + len(nickname.encode()))
    return max(0, max_class_c_payload() - used)


def single_transaction_file_bytes(channel: str = DEFAULT_CHANNEL,
                                  nickname: str = "", text: str = "") -> int:
    """The largest file that still fits in ONE transaction.

    Not a limit any more -- larger posts chunk -- but worth knowing, because the
    step from one transaction to two is the step where the cost doubles.
    """
    used = (ANYDATA_OVERHEAD + Header(type=TYPE_GROUP).length
            + 1 + len(channel.encode()) + 1 + len(nickname.encode())
            + len(FILE_MARKER) + 2 + len(text.encode())
            + 1 + MAX_FILENAME + 1 + MAX_TYPE)
    return max(0, MAX_CLASS_B_PAYLOAD - used)


@dataclass
class GroupPlan:
    """How a post will be carried, before anything is built."""

    payloads: list[bytes]
    transactions: int
    class_c: bool
    body_bytes: int
    msg_id: bytes | None = None


def plan(post: GroupPost) -> GroupPlan:
    """Decide the carriage for a post and produce its payload(s).

    Three cases, cheapest first:

    - text only, and short: one OP_RETURN, no dust;
    - anything that fits one Class B transaction: one transaction;
    - larger: a chain of Class B transactions, linked by change outputs exactly
      as a private message is, with a countdown ending at zero on the last.

    The chunks are NOT encrypted, so reassembly needs no key and no identity --
    a node with no wallet at all can rejoin them. That is the whole difference
    from the sealed chunk path, and the reason this needed its own type.
    """
    body = build(post)

    if not post.has_file and len(AnyData(data=body).encode()) <= max_class_c_payload():
        return GroupPlan(payloads=[body], transactions=1, class_c=True,
                         body_bytes=len(body))

    if len(body) <= MAX_CLASS_B_PAYLOAD - ANYDATA_OVERHEAD:
        return GroupPlan(payloads=[body], transactions=1, class_c=False,
                         body_bytes=len(body))

    msg_id = secrets.token_bytes(8)
    capacity = MAX_CLASS_B_PAYLOAD - ANYDATA_OVERHEAD - GROUP_CHUNK_HEADER_LEN
    # Even split, for the same reason private messages use one: a greedy split
    # makes the first transaction the largest possible, and that is the one most
    # likely to meet a relay limit.
    count = -(-len(body) // capacity)
    even = -(-len(body) // count)
    pieces = [body[i:i + even] for i in range(0, len(body), even)]

    payloads = []
    for index, piece in enumerate(pieces):
        header = Header(type=TYPE_GROUP_CHUNK, msg_id=msg_id,
                        countdown=len(pieces) - index - 1, clen=len(piece))
        payloads.append(header.encode() + piece)
    return GroupPlan(payloads=payloads, transactions=len(pieces), class_c=False,
                     body_bytes=len(body), msg_id=msg_id)


def parse_chunk(payload: bytes) -> tuple[bytes, int, bytes]:
    """Return (msg_id, countdown, data) for one public chunk."""
    header = Header.decode(payload)
    if header.type != TYPE_GROUP_CHUNK:
        raise EnvelopeError("not a public chunk")
    start = header.length
    return header.msg_id, header.countdown, payload[start:start + header.clen]


def is_group_chunk_payload(payload: bytes) -> bool:
    return (len(payload) >= 6 and payload[:4] == MAGIC
            and payload[4] == VERSION and payload[5] == TYPE_GROUP_CHUNK)


def build(post: GroupPost) -> bytes:
    """Encode a post for a single OP_RETURN output."""
    # Strip first, then fall back: a channel of spaces is not a channel, and
    # should land in the default room rather than an unnameable one.
    channel = (post.channel or "").strip() or DEFAULT_CHANNEL
    nickname = (post.nickname or "").strip()
    text = post.text or ""

    if len(channel.encode()) > MAX_CHANNEL:
        raise GroupError(f"channel names are limited to {MAX_CHANNEL} bytes.")
    if len(nickname.encode()) > MAX_NICKNAME:
        raise GroupError(f"names are limited to {MAX_NICKNAME} bytes.")
    if not text.strip():
        raise GroupError("write something to post.")

    encoded = text.encode()
    if not post.has_file:
        # Only a text-only post is constrained by the OP_RETURN. One carrying a
        # file is Class B already, where the limit is the message size rather
        # than 80 bytes.
        room = max_text_bytes(channel, nickname)
        if len(encoded) > room:
            raise GroupError(
                f"that is {len(encoded)} bytes and only {room} fit in one post. "
                f"A text post is a single OP_RETURN output, which is what makes "
                f"it cost a fee and no dust. Attaching a file lifts the limit, "
                f"but costs far more.")
    else:
        if len(encoded) > 0xFFFF:
            raise GroupError("the text of a post is limited to 65,535 bytes.")
        if len(post.file_data) > MAX_FILE_BYTES:
            raise GroupError(
                f"that file is {len(post.file_data) / 1_048_576:.1f} MB and a "
                f"public post is limited to {MAX_FILE_BYTES // 1_048_576} MB -- "
                f"lower than a private message, deliberately: a post cannot be "
                f"taken back, everyone can read it, and every byte is paid for "
                f"in outputs that can never be spent again. Send it as a private "
                f"message instead, or post a smaller version.")

    body = (Header(type=TYPE_GROUP).encode()
            + bytes([len(channel.encode())]) + channel.encode()
            + bytes([len(nickname.encode())]) + nickname.encode()
            + encoded)
    if post.has_file:
        # The file is appended after a marker and its own framing, so a reader
        # that predates attachments still gets the text and simply ignores the
        # rest -- the text length is explicit, so the tail is unambiguous.
        name = _safe_name(post.file_name)
        body = (body[:len(Header(type=TYPE_GROUP).encode())]
                + bytes([len(channel.encode())]) + channel.encode()
                + bytes([len(nickname.encode())]) + nickname.encode()
                + FILE_MARKER
                + len(encoded).to_bytes(2, "big") + encoded
                + bytes([len(name.encode())]) + name.encode()
                + bytes([len(post.file_type.encode()[:60])])
                + post.file_type.encode()[:60]
                + post.file_data)
    return body


def parse(payload: bytes) -> GroupPost:
    """Decode a post. Raises `EnvelopeError` if this is not one."""
    header = Header.decode(payload)
    if header.type != TYPE_GROUP:
        raise EnvelopeError("not a group post")

    offset = header.length
    try:
        channel_len = payload[offset]
        offset += 1
        channel = payload[offset:offset + channel_len].decode("utf-8", "replace")
        offset += channel_len
        nickname_len = payload[offset]
        offset += 1
        nickname = payload[offset:offset + nickname_len].decode("utf-8", "replace")
        offset += nickname_len
    except IndexError:
        raise EnvelopeError("truncated group post") from None

    rest = payload[offset:]
    if rest.startswith(FILE_MARKER):
        return _parse_with_file(channel, nickname, rest[len(FILE_MARKER):])

    # Class B pads with NULs; a text post is normally Class C and unpadded, but
    # strip anyway so the same parser works either way. Text is not ciphertext,
    # so trailing NULs are never meaningful here -- the reason `clen` exists for
    # sealed messages does not apply.
    text = rest.rstrip(b"\x00").decode("utf-8", "replace")
    return GroupPost(channel=channel[:MAX_CHANNEL], nickname=nickname[:MAX_NICKNAME],
                     text=text)


def _parse_with_file(channel: str, nickname: str, rest: bytes) -> GroupPost:
    """Decode the attachment framing. Never raises on a malformed tail.

    These bytes came from a stranger. A post whose file framing is damaged should
    still show its text rather than disappearing.
    """
    try:
        text_len = int.from_bytes(rest[:2], "big")
        offset = 2
        text = rest[offset:offset + text_len].decode("utf-8", "replace")
        offset += text_len
        name_len = rest[offset]
        offset += 1
        name = rest[offset:offset + name_len].decode("utf-8", "replace")
        offset += name_len
        type_len = rest[offset]
        offset += 1
        content_type = rest[offset:offset + type_len].decode("utf-8", "replace")
        offset += type_len
        data = rest[offset:]
    except (IndexError, ValueError):
        return GroupPost(channel=channel[:MAX_CHANNEL],
                         nickname=nickname[:MAX_NICKNAME],
                         text=rest.rstrip(b"\x00").decode("utf-8", "replace"))
    return GroupPost(channel=channel[:MAX_CHANNEL], nickname=nickname[:MAX_NICKNAME],
                     text=text, file_name=_safe_name(name),
                     file_type=content_type, file_data=data)


def _safe_name(name: str) -> str:
    """A filename that cannot escape a directory. The sender chooses this."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable() and c not in '<>:"|?*')
    return (name.strip().strip(".") or "attachment")[:120]


def is_group_payload(payload: bytes) -> bool:
    return (len(payload) >= 6 and payload[:4] == MAGIC
            and payload[4] == VERSION and payload[5] == TYPE_GROUP)
