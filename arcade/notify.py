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


#: A reply or share its own author deleted drops out of notifications, the
#: way it drops out of the feed (feedview._one; a tester, 2026-09-27).
_NOT_DELETED = ("NOT EXISTS (SELECT 1 FROM feed_act d WHERE d.network = {t}.network "
                "AND d.target = {t}.txid AND d.author = {t}.author AND d.kind = ?)")


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
                f" FROM feed_act a WHERE network = ? AND target IN ({marks}) AND author != ?"
                f" AND {_NOT_DELETED.format(t='a')}"
                f" ORDER BY rowid DESC LIMIT {LIMIT * 2}", (network, *chunk, me, feed.DELETE)):
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


def mention_events(conn, network: str, me: str, tag: str) -> list[Event]:
    """Posts and comments by other people that name @tag (2026-09-26).

    Two sources with their own read markers, because their row ids come from two
    tables: `mention` (a post, group_post.id) and `mention_reply` (a comment or
    a share's words, feed_act.rowid). The word has to be the whole name --
    @robinez does not name @robin.
    """
    import re as _re
    if not (me and tag):
        return []
    word = _re.compile(rf"(?<![\w@/.])@{_re.escape(tag)}(?![\w@])", _re.I)
    like = "%@" + tag.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    out: list[Event] = []
    for row in conn.execute(
            "SELECT id, txid, sender, text, height, block_time FROM group_post g "
            "WHERE network = ? AND sender != ? AND text LIKE ? ESCAPE '\\' "
            "AND NOT EXISTS (SELECT 1 FROM feed_act d WHERE d.network = g.network "
            "AND d.target = g.txid AND d.author = g.sender AND d.kind = ?) "
            f"ORDER BY id DESC LIMIT {LIMIT}", (network, me, like, feed.DELETE)):
        rowid, txid, sender, text, height, block_time = row
        if word.search(text or ""):
            out.append(Event(source="mention", seq=int(rowid), kind="mentioned you in a post",
                             actor=sender, target=txid, text=_snippet(text),
                             at=block_time if (height or 0) > 0 else 0,
                             extra={"txid": txid}))
    for row in conn.execute(
            "SELECT rowid, txid, kind, target, author, text, height, block_time FROM feed_act a "
            "WHERE network = ? AND author != ? AND kind IN (?, ?) AND text LIKE ? ESCAPE '\\' "
            f"AND {_NOT_DELETED.format(t='a')} "
            f"ORDER BY rowid DESC LIMIT {LIMIT}",
            (network, me, feed.REPLY, feed.SHARE, like, feed.DELETE)):
        rowid, txid, kind, target, author, text, height, block_time = row
        if word.search(text or ""):
            out.append(Event(source="mention_reply", seq=int(rowid),
                             kind="mentioned you in a comment" if kind == feed.REPLY
                             else "mentioned you sharing a post",
                             actor=author, target=target, text=_snippet(text),
                             at=block_time if (height or 0) > 0 else 0,
                             extra={"txid": txid}))
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


def swap_events(rows: Iterable[dict], owners: set[str],
                paid: dict | None = None) -> list[Event]:
    """Pieces sold or bought by swap (ledger.swaps_of), whatever route made them.

    The listing book only knows the sales it filed; an answered offer finished by
    its buyer never touches it, so a seller was told nothing (filming, 2026-09-26).
    `paid` maps a swap txid to the coins it paid, in sats, where known.
    """
    out = []
    for r in rows:
        sold = r["from_address"] in owners
        out.append(Event(source="swap", seq=int(r["seq"]),
                         kind="sold" if sold else "bought",
                         actor=r["to_address"] if sold else r["from_address"],
                         target=r["inscription"],
                         # By its name in its collection where it has one: "Skull
                         # Squad #7" rather than "#44" (a tester, 2026-09-26).
                         text=(f"{r['collection']} #{r['edition']}"
                               if r.get("collection") and r.get("edition") is not None
                               else f"#{r['number']:,}"),
                         at=int(r["time"] or 0), amount=int((paid or {}).get(r["txid"], 0)),
                         extra={"txid": r["txid"]}))
    return out


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


@dataclass
class Group:
    """What the page draws as one row: "@a and 2 others liked your post"."""
    kind: str
    source: str
    target: str
    actors: list[str]
    text: str = ""
    at: int = 0
    amount: int = 0
    about_mine: bool = True
    unread: bool = False
    count: int = 1


#: Kinds that fold into one row per post; a reply is words, so each is its own.
FOLDS = {"liked", "disliked", "shared", "tipped", "message"}


def grouped(events: Iterable[Event]) -> list[Group]:
    """Rows for the page, newest first: likes, dislikes, shares and tips on one
    post fold into one row, and several messages from one person into one; new
    and already-seen ones are never folded together, so "New" means new."""
    rows: list[Group] = []
    index: dict[tuple, Group] = {}
    for ev in events:                                  # already newest first
        key = None
        if ev.kind in FOLDS:
            key = (ev.kind, ev.unread, ev.target if ev.source != "message" else ev.actor)
        row = index.get(key) if key else None
        if row is None:
            row = Group(kind=ev.kind, source=ev.source, target=ev.target, actors=[ev.actor],
                        text=ev.text, at=ev.at, amount=ev.amount,
                        about_mine=ev.about_mine, unread=ev.unread)
            rows.append(row)
            if key:
                index[key] = row
            continue
        row.count += 1
        row.amount += ev.amount
        if ev.actor not in row.actors:
            row.actors.append(ev.actor)
        if ev.at == 0 or (row.at and ev.at > row.at):
            row.at = ev.at
    return rows


def ago(at: int, now: int) -> str:
    """"just now", "5m", "3h", "2d", then the date."""
    if not at:
        return "pending"
    gone = max(0, now - int(at))
    if gone < 60:
        return "just now"
    if gone < 3600:
        return f"{gone // 60}m"
    if gone < 86400:
        return f"{gone // 3600}h"
    if gone < 7 * 86400:
        return f"{gone // 86400}d"
    import datetime as _dt
    return _dt.datetime.fromtimestamp(at).strftime("%b %-d")
