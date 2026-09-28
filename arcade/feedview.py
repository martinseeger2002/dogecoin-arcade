"""Turning posts and the things done to them into what a page draws.

Pure functions over rows: no node, no store, no request. The web layer reads
the two tables (`group_post` and `feed_act`) and hands them here, which is
what lets every rule below be tested without a browser or a chain.

The rules, and why each one:

* **An edit is a new version, not a correction.** Both are on the chain for
  ever; the newest one from the POST'S OWN AUTHOR wins and the page says it
  was edited. An edit by anybody else is not an edit, it is a stranger
  writing in somebody's mouth, and it is dropped (D-138).
* **A delete hides, it does not erase.** The words stay on the chain and no
  arcade shows them. Only the author's delete counts, for the same reason.
* **Replies thread by their target.** A reply's parent may be a post or
  another reply and nothing has to know which -- the tree is built by
  following targets, and a reply whose parent never arrived is kept at the
  top level rather than dropped, because somebody paid to say it.
* **Counts are counts.** Muting hides what somebody said; it does not change
  a number that everybody else can see. A like is public arithmetic.
* **A like and an unlike cancel, newest wins.** Per person, by height: the
  question "does this person like this" has one answer, and it is the last
  thing they said about it.
* **A tip is kept per chain, and not from the author.** The running total a
  card shows is a sum per chain, because the units differ and adding across
  chains would be a lie; an author's tip to their own post is not counted as
  applause; and an unconfirmed one is not counted as given (the operator,
  2026-09-23, and the same rules the ORDER BY uses -- store.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .messaging import feed


@dataclass
class Shown:
    """One post as a page draws it, with its thread under it."""

    txid: str
    author: str
    text: str
    height: int
    block_time: int
    mine: bool = False
    edited: bool = False
    deleted: bool = False
    likes: int = 0
    liked_by_me: bool = False
    dislikes: int = 0
    disliked_by_me: bool = False
    shared_by_me: bool = False
    shares: int = 0
    tips: int = 0
    tipped: dict[str, int] = field(default_factory=dict)   # per chain, sats
    # Token tips as CLAIMED: (send txid, tipper), confirmed acts only and never
    # the author's own. The page believes one only once the token index says
    # that txid sent a token from the tipper to this post's author.
    token_claims: list = field(default_factory=list)
    token_tipped: dict = field(default_factory=dict)       # token name -> amount said
    replies: list["Shown"] = field(default_factory=list)
    shared_from: str = ""            # the post this one is a share of
    is_reply: bool = False
    muted: bool = False              # their words are hidden; the row is not


def _latest_by(acts: Iterable[Any], kind: int, author: str) -> Any:
    """The last thing `author` said of this kind, by height then txid.

    Ordered by txid after height so that two actions in one block resolve the
    same way on every node -- "whichever the database returned first" is not
    an order (the lesson of D-122's tie-break).
    """
    theirs = [a for a in acts if a["kind"] == kind and a["author"] == author]
    if not theirs:
        return None
    return max(theirs, key=lambda a: (a["height"] or 0, a["txid"]))


def assemble(posts: Iterable[Any], acts: Iterable[Any], *,
             me: str = "", muted: Iterable[str] = ()) -> list[Shown]:
    """Build the page's view of these posts.

    `posts` are group_post rows, `acts` every feed_act row that targets any of
    them (or any reply to them). Order is the caller's: this does not sort,
    because a feed page, a profile page and a single thread want different
    orders and only the caller knows which.
    """
    muted = set(muted or ())
    acts = list(acts)
    by_target: dict[str, list[Any]] = {}
    for act in acts:
        by_target.setdefault(act["target"], []).append(act)

    # Replies first: they are posts in their own right as far as the page is
    # concerned, and they can be liked, shared and replied to like any other.
    replies: dict[str, list[Shown]] = {}
    for act in acts:
        if act["kind"] != feed.REPLY:
            continue
        shown = _one(act["txid"], act["author"], act["text"], act,
                     by_target, me, muted, is_reply=True)
        if shown is not None:
            replies.setdefault(act["target"], []).append(shown)

    def attach(node: Shown) -> Shown:
        node.replies = sorted(
            (attach(child) for child in replies.get(node.txid, [])),
            # Oldest first, and one still waiting for its block LAST: height 0 is
            # the newest thing in the thread, not the oldest (a tester, 2026-09-27).
            key=lambda r: (not r.height, r.height or 0, r.txid))
        return node

    out: list[Shown] = []
    for row in posts:
        shown = _one(row["txid"], row["sender"], row["text"], row,
                     by_target, me, muted)
        if shown is None:
            continue
        out.append(attach(shown))
    return out


def _one(txid: str, author: str, text: str, row: Any,
         by_target: dict[str, list[Any]], me: str, muted: set[str],
         is_reply: bool = False) -> Shown | None:
    """One post or reply, with what was done to it applied."""
    acts = by_target.get(txid, [])

    gone = _latest_by(acts, feed.DELETE, author)
    if gone is not None:
        return None                   # hidden everywhere, by its own author

    edit = _latest_by(acts, feed.EDIT, author)
    said = edit["text"] if edit is not None else text

    # One opinion per person, the latest of like/unlike/dislike/undislike: a
    # like after a dislike is a change of mind, not both (2026-09-25).
    likers: dict[str, Any] = {}
    for act in acts:
        if act["kind"] in feed.OPINIONS:
            best = likers.get(act["author"])
            if best is None or (act["height"] or 0, act["txid"]) > (best["height"] or 0, best["txid"]):
                likers[act["author"]] = act
    liked = {who for who, act in likers.items() if act["kind"] == feed.LIKE}
    disliked = {who for who, act in likers.items() if act["kind"] == feed.DISLIKE}

    # A tip from the post's own author is not applause: counted flat it was
    # merely pointless, and counted by amount it is the cheapest road to the
    # top of the feed. The order's query drops the same rows (store.py), so
    # the number on the card and the place in the page cannot disagree
    # (2026-09-23).
    tips = [a for a in acts if a["kind"] == feed.TIP and a["author"] != author]
    tipped: dict[str, int] = {}
    for act in tips:
        # An unconfirmed tip may show as pending; it is not counted as given.
        if not (act["height"] or 0) > 0 or "amount" not in act.keys():
            continue
        if not act["amount"]:
            continue
        paid = ((act["paid_on"] if "paid_on" in act.keys() else "")
                or (act["network"] if "network" in act.keys() else ""))
        tipped[paid] = tipped.get(paid, 0) + int(act["amount"])

    claims = [(str(a["text"] or "").strip().lower(), a["author"]) for a in acts
              if a["kind"] == feed.TIP_TOKEN and a["author"] != author
              and (a["height"] or 0) > 0]

    return Shown(
        token_claims=claims,
        txid=txid,
        author=author,
        # Muting hides the words and keeps the row: a gap where a post was is
        # how somebody learns they were muted, and this is nobody's business
        # but the muter's (D-138).
        text="" if author in muted else said,
        height=row["height"] if "height" in row.keys() else 0,
        block_time=row["block_time"] if "block_time" in row.keys() else 0,
        mine=bool(row["mine"]) if "mine" in row.keys() else False,
        edited=edit is not None,
        likes=len(liked),
        liked_by_me=bool(me) and me in liked,
        dislikes=len(disliked),
        disliked_by_me=bool(me) and me in disliked,
        shares=sum(1 for a in acts if a["kind"] == feed.SHARE),
        shared_by_me=bool(me) and any(a["kind"] == feed.SHARE and a["author"] == me
                                      for a in acts),
        tips=len(tips),
        tipped=tipped,
        is_reply=is_reply,
        muted=author in muted,
    )


def shares_as_posts(acts: Iterable[Any], posts_by_txid: dict[str, Any]) -> list[Any]:
    """The posts somebody shared, as rows for their own feed.

    A share puts somebody else's post on your feed. It is not a copy: the row
    is the original post, carried with a note saying whose feed it is on and
    who put it there, so an edit or a delete by the original author still
    reaches every feed it was shared to (D-138).
    """
    out = []
    for act in acts:
        if act["kind"] != feed.SHARE:
            continue
        original = posts_by_txid.get(act["target"])
        if original is None:
            continue                  # the post has not been read yet
        out.append({"post": original, "by": act["author"], "note": act["text"],
                    "height": act["height"], "txid": act["txid"]})
    return out
