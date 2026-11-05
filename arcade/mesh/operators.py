"""Who runs the arcades this one is linked to (2026-10-20: "In the admin panel, I wanna see a list of other
operators").

On the wire (frames of kind "op" on an ordinary link, never flooded): each arcade says, when a link comes up and once
a minute after,

    me  {tag, name, version, hosts, sites}
                                      its operator's @name (or its node's own, for an arcade whose operator is its node,
                                      as on .25), the arcade's name, its version, the names it is published under, and
                                      the sites its operator ticked to share with other arcades' Affiliates tabs

Its own word about itself only: the link proves which arcade is speaking. Nothing more is proved: the @name is what
that arcade SAYS its operator is called, so it is shown (Network -> Other operators, Admin -> Overview's Arcades panel)
and never trusted for anything else -- no payment, permission or vouching reads it. `tag` is optional: an arcade whose
operator has no name, or one from before it was said, leaves it out and is shown by its mesh id. On receipt it is kept
only when it has a tag's shape (arcade/tags.py: 2-24 of a-z, 0-9 and _, not a reserved name); anything else is dropped
to "" (2026-10-21, approving the Overview mockup). Hosts are kept only when they look like a host name.

`sites` (2026-10-21, the Admin Affiliates panel: "a list of shared URLs from other node operators"): optional,
[{url, name, about}], at most 20 (arcade/affiliates.py MAX_SITES). A list longer than that, or anything not a list, is
ignored whole and what was kept before stands; each entry is checked strictly (affiliates.clean_site: https only, a
named host, no userinfo, port or IP literal, plain text of bounded length) and a bad one dropped. An empty list says
"I share nothing now" and the hearer forgets that peer's sites. An arcade from before the field leaves it out and
nothing changes. What is heard goes to `heard(peer id, tag, sites)` (the web state's settings, so it survives a
restart); it is shown to the operator and reaches users only once the operator ticks a site.

`links` (2026-11-02: the phone "is not listing all the arcades seen by strawberry"): optional, [{id, tag, host, owner}],
the arcades the speaker is linked to now, at most MAX_LINKS, never the hearer itself. A small node keeps two or three
links on purpose (node.outbound_target), so what it reaches itself is a fraction of the mesh; this lets Admin ->
Overview's Arcades panel show the rest as "seen through @x". Each id must be a mesh id's shape (64 hex), each tag pass
said_tag and each host _HOST, or that part is dropped. Display only, like the rest of the frame: it is the speaker's
word, and only from a speaker linked now and heard within VIA_FRESH seconds (seen_through).

`owner` (2026-11-04, "Who runs this node"): optional, {a, x, s}, its owner's statement that it owns this node
(arcade/owner.py), said only when its owner ticked to. The words it signs name the speaker's node id, so it is kept as
it came and checked from its bytes where it is shown; a malformed one is kept as None.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable

from ..disk_sizes import clean as clean_disk
from ..owner import shaped as shaped_owner

log = logging.getLogger("arcade.mesh")

#: A public name an arcade is published under, as a page would show it: letters, digits, dots, hyphens, an optional port.
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,98}[A-Za-z0-9])?(?::[0-9]{1,5})?$")
#: A mesh id: the hex of a node's verify key.
_ID = re.compile(r"^[0-9a-f]{64}$")
#: At most this many of the speaker's links are said, and kept.
MAX_LINKS = 100
#: What a linked arcade said about its links counts this long (it says it again every minute).
VIA_FRESH = 150


def clean_links(value) -> list[dict]:
    """A peer's word for the arcades it is linked to: [{id, tag?, host?}], bounded and shape-checked, [] otherwise."""
    if not isinstance(value, list):
        return []
    out, seen = [], set()
    for entry in value[:MAX_LINKS]:
        if not isinstance(entry, dict):
            continue
        node = entry.get("id")
        if not isinstance(node, str) or not _ID.match(node) or node in seen:
            continue
        seen.add(node)
        row = {"id": node}
        tag = said_tag(entry.get("tag"))
        if tag:
            row["tag"] = tag
        host = entry.get("host")
        if isinstance(host, str) and _HOST.match(host):
            row["host"] = host
        owner = shaped_owner(entry.get("owner"))
        if owner:
            row["owner"] = owner
        out.append(row)
    return out


def owned_by(peers: dict, linked, address: str, network: str, check: Callable, me: str = "",
             now: float | None = None) -> list[dict]:
    """[{id, tag, host, linked, by_tag, at}]: the arcades whose owner's statement (arcade/owner.py) names `address`
    and checks out (`check(statement, network, node) -> address`), heard from themselves or passed on in a linked
    arcade's `links` within VIA_FRESH (2026-11-04, Admin -> Your nodes). This arcade itself is left out."""
    now = time.time() if now is None else now
    linked = set(linked)
    found: dict[str, dict] = {}
    for peer, said in peers.items():
        if peer != me and (said.get("owner") or {}).get("a") == address:
            found[peer] = {"id": peer, "tag": said.get("tag") or "", "host": (said.get("hosts") or [""])[0],
                           "linked": peer in linked, "by_tag": "", "at": float(said.get("at") or 0),
                           "owner": said["owner"]}
    for peer, said in peers.items():
        if peer not in linked or now - float(said.get("at") or 0) >= VIA_FRESH:
            continue
        for entry in said.get("links") or []:
            node = entry.get("id")
            if node and node != me and node not in found and (entry.get("owner") or {}).get("a") == address:
                # Its host and @name only from its own word (2026-11-05 review): beside a statement passed on, they are
                # the passing arcade's, unsigned, and a hostile one could put its own admin page under your node's row.
                own = peers.get(node) or {}
                found[node] = {"id": node, "tag": own.get("tag") or "", "host": (own.get("hosts") or [""])[0],
                               "linked": False, "by_tag": said.get("tag") or peer[:12],
                               "at": float(said.get("at") or 0), "owner": entry["owner"]}
    rows = [r for r in found.values() if check(r.pop("owner"), network, r["id"]) == address]
    return sorted(rows, key=lambda r: (not r["linked"], r["tag"] or r["id"]))


def seen_through(peers: dict, linked, me: str = "", now: float | None = None) -> dict[str, dict]:
    """{mesh id: {by, by_tag, tag, host}}: arcades that an arcade linked to this one now said, within VIA_FRESH, it
    is linked to (this one and the linked ones themselves left out). `by` is the arcade that said so; one that also
    said a tag or host for it is preferred, then the one heard last."""
    now = time.time() if now is None else now
    linked = set(linked)
    out: dict[str, dict] = {}
    speakers = sorted(((p, s) for p, s in peers.items() if p in linked and now - float(s.get("at") or 0) < VIA_FRESH),
                      key=lambda ps: -float(ps[1].get("at") or 0))
    for peer, said in speakers:
        for entry in said.get("links") or []:
            node = entry.get("id")
            if not node or node == me or node == peer or node in linked:
                continue
            have = out.get(node)
            if have is not None and (have["tag"] or have["host"] or not (entry.get("tag") or entry.get("host"))):
                continue
            out[node] = {"by": peer, "by_tag": said.get("tag") or "", "tag": entry.get("tag") or "",
                         "host": entry.get("host") or ""}
    return out


def said_tag(value) -> str:
    """A peer's word for its operator's @name, kept only when it is shaped like one ("" otherwise): a str, at most
    tags.MAX_LENGTH once normalised, of the tag alphabet, and not a name nobody may hold. Bounded before it is looked
    at, so a megabyte "tag" costs nothing."""
    from .. import tags as taglib
    if not isinstance(value, str) or len(value) > taglib.MAX_LENGTH + 1:
        return ""
    tag = taglib.normalise(value)
    if not (taglib.MIN_LENGTH <= len(tag) <= taglib.MAX_LENGTH) or not taglib.ALLOWED.match(tag) \
            or tag in taglib.RESERVED:
        return ""
    return tag


class OperatorsShare:
    kinds = ("op",)

    def __init__(self, about: Callable[[], dict], heard: Callable[[str, str, list], Any] | None = None):
        self.about = about                       # this arcade's own {tag, name, version, hosts, sites}
        self.heard = heard                       # (peer id, tag, sites) -> kept (affiliates.record); off thread
        self.node = None
        self.peers: dict[str, dict] = {}         # peer id -> what it said, and when

    async def link_up(self, link) -> None:
        await self._say(link)

    async def link_down(self, peer_id: str) -> None:
        if peer_id in self.peers:
            self.peers[peer_id]["gone"] = time.time()

    async def rebalance(self) -> None:
        for link in list((self.node.links if self.node else {}).values()):
            await self._say(link)

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        if header.get("op") != "me":
            return
        hosts = header.get("hosts") if isinstance(header.get("hosts"), list) else []
        self.peers[link.peer_id] = {
            "tag": said_tag(header.get("tag")), "name": str(header.get("name") or "")[:60],
            "version": str(header.get("version") or "")[:40],
            "hosts": [h for h in hosts[:4] if isinstance(h, str) and _HOST.match(h)],
            "lora": header.get("lora") is True,
            # What its chains take on its disk (arcade/disk_sizes.py, 2026-10-27): its word, shown as "about".
            "disk": clean_disk(header.get("disk")),
            # The arcades it is linked to now (an older arcade says none): Overview's "seen through" rows.
            "links": clean_links(header.get("links")),
            # Its owner's signed word that it owns this node (arcade/owner.py, 2026-11-04), kept as it came and
            # checked only where it is shown (Admin -> Your nodes); an arcade that says none has None.
            "owner": shaped_owner(header.get("owner")),
            "at": time.time()}
        if "sites" in header and self.heard is not None:
            from ..affiliates import clean_sites
            sites = clean_sites(header.get("sites"))
            if sites is not None:
                try:
                    await asyncio.to_thread(self.heard, link.peer_id, self.peers[link.peer_id]["tag"], sites)
                except Exception:                          # noqa: BLE001 -- heard again next minute
                    log.debug("operators: could not keep a peer's shared sites", exc_info=True)

    async def _say(self, link) -> None:
        try:
            said = await asyncio.to_thread(self.about)
            await link.send({"t": "op", "op": "me", **said, "links": self._links(link.peer_id)})
        except Exception:                                  # noqa: BLE001 -- said again next minute
            log.debug("operators: could not say who runs this arcade", exc_info=True)

    def _links(self, hearer: str) -> list[dict]:
        """The arcades this one is linked to now, as said in "me": each id, with the tag and first host it said."""
        out = []
        for peer in list((self.node.links if self.node else {}))[:MAX_LINKS + 1]:
            if peer == hearer or not isinstance(peer, str) or not _ID.match(peer):
                continue
            heard = self.peers.get(peer) or {}
            row = {"id": peer}
            if heard.get("tag"):
                row["tag"] = heard["tag"]
            if heard.get("hosts"):
                row["host"] = heard["hosts"][0]
            if heard.get("owner"):
                row["owner"] = heard["owner"]          # its owner's statement, passed on unchanged (arcade/owner.py)
            out.append(row)
        return out[:MAX_LINKS]

    def status(self) -> list[dict]:
        """Every arcade heard from, linked now or not, newest first."""
        linked = set(self.node.links) if self.node else set()
        rows = [{"node": peer[:12], "linked": peer in linked,
                 **{k: v for k, v in said.items() if k not in ("gone", "links", "owner")}}
                for peer, said in self.peers.items()]
        return sorted(rows, key=lambda r: (not r["linked"], -r["at"]))
