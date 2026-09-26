"""What happened to an account, and to what it cares about: the Notifications page.

2026-09-25: one page that says if anybody made an offer, commented,
liked, shared or tipped a post of yours or a post you liked or shared, if you
sold something, if a message came -- with a red count on the tab until you look.

**Nothing new is written for it.** Every row is read, when the page asks, from
what the node already keeps: the feed's acts (`feed_act`), the posts they point
at (`group_post`), the shop's listings, and the arrivals the push watcher notes
when a message transaction pays an account's address (arcade/push.py).

**Read or unread is a marker per source**, the highest row id this account has
looked at -- not a time. A like still in the mempool has no block time yet, and a
clock-based marker would show it as new again when its block landed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .messaging import feed

#: What is worth telling somebody about, from the feed, done to THEIR post.
ON_MINE = {feed.LIKE: "liked", feed.DISLIKE: "disliked", feed.REPLY: "replied to",
           feed.SHARE: "shared", feed.TIP: "tipped"}
#: Done to a post they liked or shared: only a reply is news.
ON_FOLLOWED = {feed.REPLY: "replied to"}

LIMIT = 100


@dataclass
class Event:
    source: str                 # feed | message | sale
    seq: int                    # row id within its source: what "seen" is measured by
    kind: str                   # liked, replied to, message, sold, ...
    actor: str                  # the address that did it
    target: str = ""            # the post, message or listing it is about
    text: str = ""              # a reply's words, a post's opening, what sold
    at: int = 0                 # block time, 0 while still in the pool
    amount: int = 0             # a tip or a sale, in sats
    about_mine: bool = True     # on their own post, or on one they follow
    unread: bool = False
    extra: dict = field(default_factory=dict)


def _snippet(text: str, n: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def feed_events(conn, network: str, me: str) -> list[Event]:
    """Acts by other people on my posts, and replies on posts I liked or shared."""
    if not me:
        return []
    out: list[Event] = []
    mine = {r[0]: r[1] for r in conn.execute(
        "SELECT txid, text FROM group_post WHERE network = ? AND sender = ?", (network, me))}
    followed = {r[0] for r in conn.execute(
        "SELECT DISTINCT target FROM feed_act WHERE network = ? AND author = ? AND kind IN (?, ?)",
        (network, me, feed.LIKE, feed.SHARE))} - set(mine)
    targets = list(mine) + list(followed)
    for i in range(0, len(targets), 400):
        chunk = targets[i:i + 400]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
                f"SELECT rowid, txid, kind, target, author, text, height, block_time, amount"
                f" FROM feed_act WHERE network = ? AND target IN ({marks}) AND author != ?"
                f" ORDER BY rowid DESC LIMIT {LIMIT * 2}", (network, *chunk, me)):
            rowid, txid, kind, target, author, text, height, block_time, amount = row
            on_mine = target in mine
            names = ON_MINE if on_mine else ON_FOLLOWED
            if kind not in names:
                continue
            out.append(Event(source="feed", seq=rowid, kind=names[kind], actor=author,
                             target=target,
                             text=_snippet(text if kind == feed.REPLY else mine.get(target, "")),
                             at=block_time if (height or 0) > 0 else 0,
                             amount=int(amount or 0) if kind == feed.TIP else 0,
                             about_mine=on_mine, extra={"txid": txid}))
    return out


def message_events(arrivals: Iterable[Any]) -> list[Event]:
    """Messages that paid this account's address (push.Push.arrivals)."""
    return [Event(source="message", seq=int(r["rowid"]), kind="message", actor=r["sender"],
                  target=r["txid"], at=int(r["at"])) for r in arrivals]


def sale_events(conn, owners: set[str]) -> list[Event]:
    """Listings of mine that were bought (listings.close(..., "filled"))."""
    if not owners:
        return []
    marks = ",".join("?" * len(owners))
    return [Event(source="sale", seq=int(r[0]), kind="sold", actor="", target=r[1],
                  text=_snippet(r[2] or "a piece"), amount=int(r[3] or 0),
                  at=int(r[4] or 0), extra={"txid": r[5]})
            for r in conn.execute(
                f"SELECT rowid, id, what, price, created, spent_by FROM listing"
                f" WHERE status = 'filled' AND owner IN ({marks})"
                f" ORDER BY rowid DESC LIMIT {LIMIT}", tuple(owners))]


def merge(events: Iterable[Event], seen: dict[str, int]) -> list[Event]:
    """Newest first (anything still in the pool on top), marked read or unread."""
    out = []
    for ev in events:
        ev.unread = ev.seq > int(seen.get(ev.source, 0))
        out.append(ev)
    out.sort(key=lambda e: (e.at == 0, e.at, e.seq), reverse=True)
    return out[:LIMIT]


def unread(events: Iterable[Event]) -> int:
    return sum(1 for e in events if e.unread)


def seen_now(events: Iterable[Event], seen: dict[str, int]) -> dict[str, int]:
    """The marker after looking: the highest row of each source on the page."""
    out = dict(seen)
    for ev in events:
        out[ev.source] = max(int(out.get(ev.source, 0)), ev.seq)
    return out
