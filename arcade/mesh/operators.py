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
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable

log = logging.getLogger("arcade.mesh")

#: A public name an arcade is published under, as a page would show it: letters, digits, dots, hyphens, an optional port.
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,98}[A-Za-z0-9])?(?::[0-9]{1,5})?$")


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
            await link.send({"t": "op", "op": "me", **said})
        except Exception:                                  # noqa: BLE001 -- said again next minute
            log.debug("operators: could not say who runs this arcade", exc_info=True)

    def status(self) -> list[dict]:
        """Every arcade heard from, linked now or not, newest first."""
        linked = set(self.node.links) if self.node else set()
        rows = [{"node": peer[:12], "linked": peer in linked, **{k: v for k, v in said.items() if k != "gone"}}
                for peer, said in self.peers.items()]
        return sorted(rows, key=lambda r: (not r["linked"], -r["at"]))
