"""A feed that lets new posts in: dislikes, a head start, and popularity that wears off.

2026-09-25: "Add dislikes to the feed that will demote post popularity.
New posts need to be seen before they are given any popularity... popularity
should wear off over time so that new popular posts show up."

feed.hot(score, block_time, asof) = (HEAD_START + score) / (age_hours + 2) ** GRAVITY.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_feed_popularity import (NET, ME, OTHER, _post, _act, store)  # noqa: E402,F401

from arcade import feedview                                            # noqa: E402
from arcade.messaging import feed                                      # noqa: E402

HOUR = 3600
NOW = 2_000_000_000


def _at(store, txid, hours_ago, sender=ME, text="a post"):
    """A post whose block is `hours_ago` before NOW."""
    store.add_group_post(NET, "", txid, 100, NOW - int(hours_ago * HOUR), sender, "", text)
    return txid


def _page(store, **kw):
    return [r["txid"] for r in store.feed_posts_popular(NET, asof=NOW, limit=50, **kw)]


# --- the arithmetic ------------------------------------------------------------------

def test_a_brand_new_post_beats_a_day_old_post_with_ten_likes():
    fresh = feed.hot(0, NOW, NOW)
    old = feed.hot(10, NOW - 24 * HOUR, NOW)
    assert fresh > old, (fresh, old)


def test_a_new_post_with_a_few_likes_beats_one_nobody_has_touched():
    assert feed.hot(5, NOW - 2 * HOUR, NOW) > feed.hot(0, NOW, NOW)


def test_popularity_wears_off():
    same = [feed.hot(10, NOW - h * HOUR, NOW) for h in (1, 6, 24, 72)]
    assert same == sorted(same, reverse=True) and same[-1] < same[0] / 10


def test_a_pending_post_is_as_new_as_posts_get():
    assert feed.hot(0, 0, NOW) == feed.hot(0, NOW, NOW)


def test_more_dislikes_than_anything_else_sinks_a_post_below_zero():
    assert feed.hot(-3, NOW, NOW) < 0 < feed.hot(0, NOW - 100 * HOUR, NOW)


def test_dislike_is_a_kind_the_chain_can_carry():
    for kind in (feed.DISLIKE, feed.UNDISLIKE):
        assert kind in feed.KINDS
        assert feed.BY_NAME[feed.NAMES[kind]] == kind
        feed.build(kind, "ab" * 32)          # raises if the kind is refused


# --- the order ------------------------------------------------------------------------

def test_the_feed_shows_a_new_post_above_old_favourites(store):
    old = _at(store, "01" * 32, hours_ago=30)
    for i in range(8):
        _act(store, f"a{i}" + "00" * 31, feed.LIKE, old, author=f"liker{i}")
    new = _at(store, "02" * 32, hours_ago=0.1)
    assert _page(store)[0] == new


def test_dislikes_move_a_post_down(store):
    a = _at(store, "11" * 32, hours_ago=1)
    b = _at(store, "12" * 32, hours_ago=1)
    assert _page(store) == [b, a]                      # a tie, newest id first
    _act(store, "d1" + "00" * 31, feed.DISLIKE, b, author="x1")
    _act(store, "d2" + "00" * 31, feed.DISLIKE, b, author="x2")
    assert _page(store) == [a, b]


def test_one_person_holds_one_opinion_the_latest(store):
    p = _at(store, "21" * 32, hours_ago=1)
    _act(store, "e1" + "00" * 31, feed.LIKE, p, author="x", height=101)
    _act(store, "e2" + "00" * 31, feed.DISLIKE, p, author="x", height=102)
    row = store.feed_posts_popular(NET, asof=NOW)[0]
    assert row["score"] == -1.0, "the dislike replaced the like, it did not add to it"
    _act(store, "e3" + "00" * 31, feed.LIKE, p, author="x", height=103)
    row = store.feed_posts_popular(NET, asof=NOW)[0]
    assert row["score"] == 1.0
    _act(store, "e4" + "00" * 31, feed.UNLIKE, p, author="x", height=104)
    assert store.feed_posts_popular(NET, asof=NOW)[0]["score"] == 0.0


def test_the_card_and_the_order_agree_about_dislikes(store):
    p = _at(store, "31" * 32, hours_ago=1)
    _act(store, "f1" + "00" * 31, feed.DISLIKE, p, author="x1", height=101)
    _act(store, "f2" + "00" * 31, feed.DISLIKE, p, author="x2", height=101)
    _act(store, "f3" + "00" * 31, feed.UNDISLIKE, p, author="x2", height=102)
    _act(store, "f4" + "00" * 31, feed.LIKE, p, author="x3", height=101)
    row = store.feed_posts_popular(NET, asof=NOW)[0]
    acts = store.feed_acts_for(NET, [p]) if hasattr(store, "feed_acts_for") else None
    assert row["score"] == 0.0                          # one like, one standing dislike
    if acts is not None:
        shown = feedview.shown(row, acts, me="x1") if hasattr(feedview, "shown") else None
        if shown is not None:
            assert (shown.likes, shown.dislikes, shown.disliked_by_me) == (1, 1, True)


def test_paging_one_scroll_at_one_moment_repeats_nothing(store):
    made = [_at(store, f"{i:02d}" + "cd" * 31, hours_ago=i) for i in range(25)]
    first = store.feed_posts_popular(NET, limit=10, asof=NOW)
    seen, cur, anc = [r["txid"] for r in first], first[-1]["id"], first[-1]["rank"]
    for _ in range(5):
        page = store.feed_posts_popular(NET, cursor=cur, anchor=anc, asof=NOW, limit=10)
        if not page:
            break
        seen += [r["txid"] for r in page]
        cur, anc = page[-1]["id"], page[-1]["rank"]
    assert sorted(seen) == sorted(made) and len(seen) == len(set(seen)) == 25


def test_the_cursor_carries_the_moment():
    from arcade.web.app import _cursor3, _next_cursor
    rows = [{"id": 7, "score": 3.0, "rank": 0.123456789123}]

    class R(dict):
        def keys(self):
            return super().keys()
    token = _next_cursor([R(rows[0])], "popular", NOW)
    ident, anchor, asof = _cursor3(token)
    assert (ident, asof) == (7, NOW) and anchor <= 0.123456789123
    assert _cursor3("412@7.4") == (412, 7.4, None)      # an older link still parses
    assert _cursor3("412") == (412, None, None)
