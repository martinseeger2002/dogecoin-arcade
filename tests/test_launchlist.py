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


PAD = "d" * 63 + "1"


def _a_pad(home):
    """A collection's mintpad, inscribed: a page whose JSON names it."""
    import json
    from arcade.db import Database
    db = Database(home / "main-ledger.sqlite")
    db.conn.execute(
        "INSERT INTO inscription(txid,number,creator,owner,block_height,position,"
        "content_type,content_len,sha256,json,chunks,content) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (PAD, 90, "nMe", "nMe", 450, 0, "text/html", 10, "ee" * 32,
         json.dumps({"mintpad": {"creator": "nMe", "collection": "Doge Punks"}}), 1,
         b"<p>pad</p>"))
    db.conn.commit()
    db.close()


def test_launches_is_folded_into_the_mintpads_tab(client):
    """2026-09-28: Exchange opens on Mintpads, which lists every pad drawn
    as the feed draws it and ranked like Launches was; Launches goes."""
    app, state = client
    index_with_a_collection(state.home)
    _a_pad(state.home)
    moved = app.get("/launches?sort=new", follow_redirects=False)
    assert moved.status_code == 303
    assert moved.headers["location"] == "/exchange?tab=mintpads&sort=new"
    page = app.get("/exchange").text
    assert 'href="/launches"' not in page, "no Launches button"
    assert f'class="inscription-frame postmedia padframe" src="/content/{PAD}"' in page
    assert "NFT mintpad" in page and "Doge Punks" in page
    assert f'action="/feed/{PAD}/like"' in page, "the like is aimed at the pad"
    assert 'href="/exchange?tab=mintpads&amp;sort=new"' in page


def test_a_visitor_sees_the_counts_and_how_to_join_in(client):
    """Signed out, the reactions are counts and a way in, not nothing (filming,
    2026-09-26: a signed-out phone saw no likes, dislikes or comments at all)."""
    app, state = client
    index_with_a_collection(state.home)
    _a_pad(state.home)
    state.public = True
    try:
        page = app.get("/exchange?tab=mintpads").text
    finally:
        state.public = False
    if "Doge Punks" in page:
        assert "Sign in to react" in page and "/like\"" not in page


def test_the_launch_wizard_is_gone_and_its_address_goes_to_the_mintpad_wizard(client):
    """2026-09-26: no wizard for making tokens or collections (that is
    easy already); a mintpad wizard instead."""
    app, _ = client
    ex = app.get("/exchange").text
    assert 'href="/launch"' not in ex
    moved = app.get("/launch", follow_redirects=False)
    assert moved.status_code == 303 and moved.headers["location"] == "/mintpad/new"
    assert 'href="/mintpad/new"' in app.get("/exchange?tab=mintpads").text
