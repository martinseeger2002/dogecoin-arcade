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
Class C, a single OP_RETURN. One output, no multisig, and therefore no dust: a
post costs a fee and nothing else. That caps a post at about 60 characters, which
is a real constraint and the right trade -- Class B would carry far more but pays
roughly one unspendable output per 30 bytes, and on mainnet that is somebody's
money burnt to say something in public.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..encoding import max_class_c_payload
from .envelope import MAGIC, TYPE_GROUP, VERSION, EnvelopeError, Header

#: The channel used when nobody chooses one.
DEFAULT_CHANNEL = "main"

#: Bounded so the length prefixes stay one byte, and because a channel name and
#: a nickname are paid for in the same bytes as the message itself.
MAX_CHANNEL = 24
MAX_NICKNAME = 24

#: 4 bytes are lost to the AnyData wrapper that every Arcade payload carries.
ANYDATA_OVERHEAD = 4


class GroupError(Exception):
    """The post could not be built or understood."""


@dataclass
class GroupPost:
    channel: str = DEFAULT_CHANNEL
    nickname: str = ""
    text: str = ""


def max_text_bytes(channel: str = DEFAULT_CHANNEL, nickname: str = "") -> int:
    """How much text still fits, given what the channel and nickname cost.

    Worth showing to the user as they type: on mainnet every byte here is paid
    for, and the limit is low enough to hit constantly.
    """
    used = (ANYDATA_OVERHEAD + Header(type=TYPE_GROUP).length
            + 1 + len(channel.encode()) + 1 + len(nickname.encode()))
    return max(0, max_class_c_payload() - used)


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
    room = max_text_bytes(channel, nickname)
    if len(encoded) > room:
        raise GroupError(
            f"that is {len(encoded)} bytes and only {room} fit in one post. A "
            f"public post is a single OP_RETURN output, which is what makes it "
            f"cost a fee and no dust.")

    return (Header(type=TYPE_GROUP).encode()
            + bytes([len(channel.encode())]) + channel.encode()
            + bytes([len(nickname.encode())]) + nickname.encode()
            + encoded)


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

    # Class B pads with NULs; a public post is normally Class C and unpadded, but
    # strip anyway so the same parser works either way. Text is not ciphertext,
    # so trailing NULs are never meaningful here -- the reason `clen` exists for
    # sealed messages does not apply.
    text = payload[offset:].rstrip(b"\x00").decode("utf-8", "replace")
    return GroupPost(channel=channel[:MAX_CHANNEL], nickname=nickname[:MAX_NICKNAME],
                     text=text)


def is_group_payload(payload: bytes) -> bool:
    return (len(payload) >= 6 and payload[:4] == MAGIC
            and payload[4] == VERSION and payload[5] == TYPE_GROUP)
