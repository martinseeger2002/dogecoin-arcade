"""Where a mesh node can be reached, said on the chain.

A node that accepts mesh connections says so in a public announcement on the
messaging chain: its IP address, its port and its mesh key, paid for by its fee
address like every other arcade announcement. Every node's index reads every
one, so every node starts from the same list of peers, and no website, domain
or directory is involved anywhere (2026-09-30: "No URLs. It all needs
to be peer to peer.").

* **An address, never a name.** A literal IPv4 or IPv6 address and a port.
  Hostnames are refused: a name is a thing somebody else resolves.
* **Only addresses the internet can reach.** Private, loopback, link-local
  and reserved ranges are refused when announcing and ignored when read; an
  announcement that only works inside its author's house helps nobody.
* **The key is the identity.** The link handshake proves it, so an announcement
  naming somebody else's key just fails to connect; it cannot impersonate.
* **Latest wins, per key.** A node whose address changes announces again.
"""

from __future__ import annotations

import ipaddress
import re

PREFIX = "arcade-mesh"
_KEY = re.compile(r"^[0-9a-f]{64}$")


def clean_host(host: str) -> str:
    """A reachable IP address as text, or ValueError saying why not."""
    try:
        ip = ipaddress.ip_address(str(host or "").strip().strip("[]"))
    except ValueError:
        raise ValueError("an IP address like 203.0.113.7 -- not a name, not a URL") from None
    if not ip.is_global:
        raise ValueError(f"{ip} cannot be reached from the internet")
    return str(ip)


def clean_port(port) -> int:
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("a port is a number") from None
    if not 0 < port < 65536:
        raise ValueError("a port is 1 to 65535")
    return port


def build(host: str, port: int, key_hex: str) -> bytes:
    """The payload: the arcade header, then `arcade-mesh <ip> <port> <key>`."""
    from ..messaging.envelope import Header, TYPE_MESH

    key = str(key_hex or "").strip().lower()
    if not _KEY.match(key):
        raise ValueError("a mesh key is 64 hex characters")
    text = f"{PREFIX} {clean_host(host)} {clean_port(port)} {key}"
    return Header(type=TYPE_MESH).encode() + text.encode()


def parse(payload: bytes) -> dict | None:
    """{host, port, key} from an announcement payload, or None if it is not a usable one."""
    from ..messaging.envelope import EnvelopeError, Header, MAGIC, VERSION, TYPE_MESH

    if len(payload) < 7 or payload[:4] != MAGIC or payload[4] != VERSION:
        return None
    if payload[5] != TYPE_MESH:
        return None
    try:
        head = Header.decode(payload)
    except EnvelopeError:
        return None
    words = payload[head.length:].rstrip(b"\x00").decode("utf-8", "replace").split()
    if len(words) != 4 or words[0] != PREFIX or not _KEY.match(words[3]):
        return None
    try:
        return {"host": clean_host(words[1]), "port": clean_port(words[2]), "key": words[3]}
    except ValueError:
        return None
