"""Machine talk, public: one node telling every node something, under a topic.

2026-10-17: "a machine talk public message layer". The program layer had
only sealed one-to-one messages (api.py, TYPE_API), and the public machine
messages were each built for one job -- a release notice, an instance, a mesh
address, a game's state. This is the general one: any program on any node
publishes a JSON body under a topic, and every node's scanner indexes it, so any
program on any other node can read it by topic with a cursor (bot RPC
`da_publish` / `da_broadcasts`). Content flags (arcade/flags.py, topic
"flags/v1") are its first user.

* **Public and unsealed**: anybody can read it, nobody's key is needed, and no
  feed or chat ever draws it.
* **The sender is the chain's**: whoever spent the coin. A node publishes from
  its fee address, the same address its instance and mesh announcements come
  from, so readers know which arcade said it. What a sender's word is worth is
  the reader's decision, made when it reads (flags: the operator's follow list).
* **One transaction**: a body that does not fit is refused, not chunked; split
  it into several publishes. Topics name a format and its version, so a reader
  skips what it does not understand and older nodes skip the whole type.

    payload = header || {"t": topic, "b": body}     (compact JSON)
"""
from __future__ import annotations

import json
import re
from typing import Any

#: A topic: lower-case letters, digits and . _ : / -, 1-64 long, e.g. "flags/v1".
TOPIC = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,63}$")
#: What one transaction carries comfortably (a Class B payload holds 7,650 bytes).
MAX_BYTES = 7000


class BroadcastError(ValueError):
    """Not a broadcast this node can publish or read."""


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def check_topic(topic: Any) -> str:
    if not isinstance(topic, str) or not TOPIC.match(topic):
        raise BroadcastError("a topic is 1 to 64 lower-case letters, digits and . _ : / -")
    return topic


def build(topic: str, body: Any) -> bytes:
    """The payload: the arcade header, then {"t": topic, "b": body} as compact JSON."""
    from .envelope import Header, TYPE_BROADCAST
    check_topic(topic)
    try:
        encoded = _compact({"t": topic, "b": body}).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BroadcastError(f"the body is not JSON: {exc}") from None
    payload = Header(type=TYPE_BROADCAST).encode() + encoded
    if len(payload) > MAX_BYTES:
        raise BroadcastError(f"that is {len(payload)} bytes and one broadcast holds "
                             f"{MAX_BYTES}: split it into several")
    return payload


def parse(payload: bytes) -> dict | None:
    """{"topic", "body"} from a payload, or None for anything else."""
    from .envelope import EnvelopeError, Header, MAGIC, VERSION, TYPE_BROADCAST
    if len(payload) < 7 or payload[:4] != MAGIC or payload[4] != VERSION:
        return None
    if payload[5] != TYPE_BROADCAST:
        return None
    try:
        head = Header.decode(payload)
        said = json.loads(payload[head.length:].rstrip(b"\x00").decode("utf-8"))
        return {"topic": check_topic(said["t"]), "body": said.get("b")}
    except (EnvelopeError, BroadcastError, ValueError, KeyError, TypeError,
            UnicodeDecodeError):
        return None
