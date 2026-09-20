"""The feed as it is this second: posts and reactions still in the mempool.

Read fresh, never written down. The store is built from blocks and stays
that way -- a post that never confirms must leave nothing behind, and two
nodes must agree on what the chain says rather than on what their own
mempool happened to hold. This is the same distinction the marketplace
already makes for offers, asks and listings (D-117), arriving here for the
same reason it arrived there: a block is a minute or ten, and a like that
takes ten minutes to appear reads as a like that did not work.

So a feed draws blocks first and this on top: everything here is marked
pending, counted like anything else, and gone from view the moment the
block it is in is read -- or the moment it is dropped, with nothing to undo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import payload as P
from ..indexer import PrevOutCache
from ..tx import extract
from . import feed, group
from ..script import b58check_encode
from .envelope import (EnvelopeError, TYPE_KEY_ANNOUNCE, is_message_payload,
                       parse_announced_extras, parse_key_announcement)


@dataclass
class Pending:
    """What the mempool holds for the feed right now."""

    posts: list[dict[str, Any]] = field(default_factory=list)
    acts: list[dict[str, Any]] = field(default_factory=list)
    #: Announcements: who somebody says they are, what key to write to them
    #: with, and which piece is their face. Read here for the same reason as
    #: the rest -- a name claimed a minute ago and a face changed a minute
    #: ago should both be visible now, not after a block (the operator, D-144).
    said: list[dict[str, Any]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.posts or self.acts or self.said)

    def face_for(self, address: str) -> str:
        """The newest picture this address has announced in the pool."""
        for row in reversed(self.said):
            if row["address"] == address and row["pfp"]:
                return row["pfp"]
        return ""

    def tag_for(self, address: str) -> str:
        for row in reversed(self.said):
            if row["address"] == address and row["tag"]:
                return row["tag"]
        return ""


def read(rpc: Any, params: Any, network: str, mine: str = "",
         limit: int = 300) -> Pending:
    """Posts and reactions waiting for a block, in the shape the store uses.

    The rows are dicts with the same keys the tables have, so a page can
    merge them with what it read from the store and treat both alike. Height
    0 is what marks them pending -- the same convention a post broadcast from
    this machine already uses while it waits.
    """
    found = Pending()
    try:
        ids = list(rpc.call("getrawmempool") or [])[:max(0, limit)]
    except Exception:
        return found                  # no node, no mempool, no harm
    if not ids:
        return found
    cache = PrevOutCache(rpc, params)
    for txid in ids:
        try:
            raw = rpc.call("getrawtransaction", txid, True)
            atx = extract(raw, 0, 0, params, cache.lookup)
        except Exception:
            continue                  # gone between the list and the read
        if atx is None or not atx.payload:
            continue
        try:
            message = P.decode(atx.payload)
        except P.PayloadError:
            continue
        if not isinstance(message, P.AnyData):
            continue
        body = message.data
        if not is_message_payload(body):
            continue

        if feed.is_feed_act(body):
            try:
                act = feed.parse(body)
            except EnvelopeError:
                continue              # a kind this version does not know
            found.acts.append({
                "txid": atx.txid, "network": network, "kind": act.kind,
                "target": act.target_hex, "author": atx.sender,
                "text": act.text, "height": 0, "block_time": 0,
                "mine": 1 if mine and atx.sender == mine else 0,
                "pending": 1,
            })
            continue

        if len(body) >= 6 and body[5] == TYPE_KEY_ANNOUNCE:
            try:
                extras = parse_announced_extras(body)
                pubkey = parse_key_announcement(body)
            except EnvelopeError:
                continue
            # The address the announcement NAMES, not the one that funded it:
            # the same rule the scanner follows, and the reason it exists
            # (D-062).
            where = atx.sender
            if extras["hash160"]:
                where = b58check_encode(params.pubkeyhash_version,
                                        extras["hash160"])
            found.said.append({
                "txid": atx.txid, "address": where, "pubkey": pubkey,
                "tag": extras.get("tag", ""), "pfp": extras.get("pfp", ""),
                "name": extras.get("name", ""), "bio": extras.get("bio", ""),
                "url": extras.get("url", ""), "pending": 1,
            })
            continue

        if group.is_group_payload(body):
            try:
                post = group.parse(body)
            except EnvelopeError:
                continue
            found.posts.append({
                "id": 0, "txid": atx.txid, "network": network,
                "channel": post.channel, "sender": atx.sender,
                "nickname": post.nickname, "text": post.text,
                "height": 0, "block_time": 0,
                "mine": 1 if mine and atx.sender == mine else 0,
                "file_name": "", "file_type": "", "file_data": None,
                "pending": 1,
            })
    return found


class Row(dict):
    """A dict that answers `keys()` the way a sqlite3.Row does.

    The view layer asks rows what columns they have, so a pending row has to
    answer the same question a stored one does -- otherwise the page would
    need to know which kind it was holding, which is exactly what it must
    not know.
    """

    def keys(self):                   # noqa: D102 - dict already documents it
        return list(super().keys())
