"""Identity by key, not by name: an arcade says on the chain who runs it.

docs/multi-user.md, "Running your own instance" (and 2026-09-25): an
instance's real identity is its operator, its fee address and its source
revision -- never a name shape like "dogecoinarcade7". So an instance announces
its DOMAIN and its REVISION in a transaction paid for by its FEE ADDRESS.

**Why no separate signature.** The announcement is a transaction whose input is
spent by the fee address's own key, so the chain has already verified that
whoever holds that key published it; the scanner records that funding address
as the announcer (the same attribution every public arcade message uses). A
second signature over the same words would prove nothing the first does not.

**Why the chain alone is not enough.** Anybody can announce "dogecoinarcade.com":
a domain is owned off the chain. So an instance also publishes its fee address
at https://<its domain>/.well-known/dogecoinarcade.json, and the directory
(/instances) checks, from the VISITOR's browser, that the domain and the chain
name the same fee address. Only when both agree is an instance shown as
verified. The node never fetches arbitrary domains itself (no server-side
requests to names somebody typed onto the chain).

**Latest wins, per fee address and domain.** Announcing again with a new
revision updates the entry; an operator moving domains announces the new one.
"""

from __future__ import annotations

import re

#: What the payload says, after the arcade header. Readable on purpose, like the
#: release notice: anybody looking at the chain can see what it claims.
PREFIX = "arcade-instance"

#: A hostname: letters, digits, hyphens and dots, no scheme, no port, no path.
_HOST = re.compile(r"^(?=.{3,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_REV = re.compile(r"^[0-9a-f]{4,40}$")


def clean_domain(domain: str) -> str:
    """A domain as the announcement carries it, or ValueError saying why not."""
    said = str(domain or "").strip().lower().rstrip(".")
    for scheme in ("https://", "http://"):
        if said.startswith(scheme):
            said = said[len(scheme):]
    said = said.split("/")[0]
    if not _HOST.match(said):
        raise ValueError("a domain like arcade.example.com: no scheme, no port, no path")
    return said


def build(domain: str, revision: str) -> bytes:
    """The payload: the arcade header, then `arcade-instance <domain> <revision>`."""
    from .messaging.envelope import Header, TYPE_INSTANCE

    rev = str(revision or "").strip().lower()[:40]
    if not _REV.match(rev):
        raise ValueError("a source revision is the hex of a commit")
    text = f"{PREFIX} {clean_domain(domain)} {rev}"
    return Header(type=TYPE_INSTANCE).encode() + text.encode()


def parse(payload: bytes) -> dict | None:
    """{domain, revision} from an announcement payload, or None if it is not one."""
    from .messaging.envelope import (EnvelopeError, Header, MAGIC, VERSION,
                                     TYPE_INSTANCE)

    if len(payload) < 7 or payload[:4] != MAGIC or payload[4] != VERSION:
        return None
    if payload[5] != TYPE_INSTANCE:
        return None
    try:
        head = Header.decode(payload)
    except EnvelopeError:
        return None
    words = payload[head.length:].rstrip(b"\x00").decode("utf-8", "replace").split()
    if len(words) != 3 or words[0] != PREFIX:
        return None
    try:
        domain = clean_domain(words[1])
    except ValueError:
        return None
    if not _REV.match(words[2]):
        return None
    return {"domain": domain, "revision": words[2]}
