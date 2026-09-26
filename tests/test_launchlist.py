"""The launchpad list (2026-09-25): tokens and collections ranked like the
feed, with likes, dislikes, comments and trades in the score."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_collection_web import index_with_a_collection, on_mainnet  # noqa: F401,E402

from arcade import launchlist                                    # noqa: E402
from arcade.messaging import feed                                # noqa: E402


def _act(kind, target, author, height=10, txid=None, text=""):
    return {"kind": kind, "target": target, "author": author, "height": height,
            "txid": txid or f"{kind}{author}{height}", "text": text}


def test_each_author_counts_once_and_an_unlike_takes_it_back():
    said = launchlist.tally([
        _act(feed.LIKE, "t", "a", 10), _act(feed.LIKE, "t", "a", 11),
        _act(feed.LIKE, "t", "b", 10), _act(feed.UNLIKE, "t", "b", 12),
        _act(feed.DISLIKE, "t", "c", 10), _act(feed.REPLY, "t", "d", 10, text="nice")])
    assert said["t"]["likes"] == {"a"} and said["t"]["dislikes"] == {"c"}
    assert [c["text"] for c in said["t"]["comments"]] == ["nice"]


def test_trades_count_but_are_damped():
    quiet = launchlist.endorsement(0, 0, 0, 0, 0.0)
    traded = launchlist.endorsement(0, 0, 0, 3, 10.0)
    whale = launchlist.endorsement(0, 0, 0, 1, 1_000_000.0)
    assert traded > quiet
    assert whale < 25, "one huge trade cannot bury everything else"


def test_popular_and_new_order_differently():
    now = 1_000_000
    old_liked = {"likes": 9, "dislikes": 0, "comments": 2, "trades": 4, "volume": 50.0,
                 "time": now - 3600}
    fresh = {"likes": 0, "dislikes": 0, "comments": 0, "trades": 0, "volume": 0.0,
             "time": now - 60}
    popular = launchlist.rank([dict(fresh, n="fresh"), dict(old_liked, n="old")], "popular", now)
    new = launchlist.rank([dict(old_liked, n="old"), dict(fresh, n="fresh")], "new", now)
    assert popular[0]["n"] == "old" and new[0]["n"] == "fresh"


def test_the_page_lists_a_collection_with_the_feeds_buttons(client):
    app, state = client
    index_with_a_collection(state.home)
    page = app.get("/launches")
    assert page.status_code == 200, page.text[:400]
    assert "Doge Punks" in page.text and "collection" in page.text
    assert f'action="/feed/{1:064x}/like"' in page.text, "the like is aimed at #1"
    assert 'href="/launches?sort=new"' in page.text
    assert 'href="/launches"' in app.get("/exchange").text
