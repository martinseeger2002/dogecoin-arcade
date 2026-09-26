"""The launchpad list: every token and collection, ranked like the feed.

2026-09-25: "The launchpad list should be sorted by popularity or recent ...
like, dislike, comment ... based on transaction history for the assets." So a launch
is scored the way a post is -- feed.hot(), the same head start and the same gravity --
and its endorsement is what people said about it plus what they did with it:

    likes - dislikes                    what people said, one each
    + 0.5 x comments                    talked about, but a comment is not a vote
    + 2 x log2(1 + trades)              traded at all matters more than how often
    + log2(1 + coins traded)            and money through it, damped the same way

Logarithms because a launch with a thousand trades is not a thousand times more
interesting than one with one, and a whale's single trade must not bury everything
else. The reactions are the feed's own acts aimed at the launch's own txid (a token's
creation, a collection's #1), so nothing new goes on the chain.
"""

from __future__ import annotations

import math
from typing import Iterable

from .messaging import feed as feedlib

WEIGHT_COMMENT = 0.5
WEIGHT_TRADED = 2.0
WEIGHT_VOLUME = 1.0


def tally(acts: Iterable) -> dict[str, dict]:
    """Likes, dislikes and comments per target, and who did which.

    A like and its unlike cancel, as on the feed: each author's LAST word on a
    target is what counts.
    """
    last: dict[tuple[str, str, str], int] = {}
    comments: dict[str, list] = {}
    for act in sorted(acts, key=lambda a: (a["height"] or 1 << 60, a["txid"])):
        kind, target, author = int(act["kind"]), act["target"], act["author"]
        if kind in (feedlib.LIKE, feedlib.UNLIKE):
            last[(target, author, "like")] = kind
        elif kind in (feedlib.DISLIKE, feedlib.UNDISLIKE):
            last[(target, author, "dislike")] = kind
        elif kind == feedlib.REPLY:
            comments.setdefault(target, []).append(act)
    out: dict[str, dict] = {}
    for (target, author, what), kind in last.items():
        entry = out.setdefault(target, {"likes": set(), "dislikes": set(), "comments": []})
        if what == "like" and kind == feedlib.LIKE:
            entry["likes"].add(author)
        if what == "dislike" and kind == feedlib.DISLIKE:
            entry["dislikes"].add(author)
    for target, rows in comments.items():
        out.setdefault(target, {"likes": set(), "dislikes": set(), "comments": []})[
            "comments"] = rows
    return out


def endorsement(likes: int, dislikes: int, comments: int, trades: int,
                volume: float) -> float:
    return (likes - dislikes + WEIGHT_COMMENT * comments
            + WEIGHT_TRADED * math.log2(1 + max(0, trades))
            + WEIGHT_VOLUME * math.log2(1 + max(0.0, volume)))


def rank(items: list[dict], sort: str, asof: int) -> list[dict]:
    """Popular (feed.hot over the endorsement) or New (newest block first)."""
    for item in items:
        item["score"] = endorsement(item["likes"], item["dislikes"], item["comments"],
                                    item["trades"], item["volume"])
        item["hot"] = feedlib.hot(item["score"], item["time"], asof)
    if sort == "new":
        # Still in the pool (time 0) is newer than anything in a block.
        return sorted(items, key=lambda i: (i["time"] == 0, i["time"]), reverse=True)
    return sorted(items, key=lambda i: i["hot"], reverse=True)
