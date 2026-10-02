"""What a name wrote itself, in one answer (S195, a tester, 2026-10-01).

"/u/<tag> lists only your posts" and /r/replies/<tag> is the opposite question,
so nothing on the node said what somebody has said lately. A post is a
`group_post` row and a comment is a `feed_act` row, which is why this is one
route reading two tables and not a flag on either of the old ones."""

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from test_nft_market import tag                                # noqa: E402
from arcade.messaging import feed                              # noqa: E402

SILAS = "nSilasAAAAAAAAAAAAAAAAAAAAAAAAAAA"
FAN = "nFanAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
POST, REPLY, MINE, LATER, LIKE = ("a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64)


def _book(state, now):
    with state.store() as store:
        net = state.messaging.network
        store.add_group_post(net, "", POST, 100, now - 100, SILAS, "", "the purge begins")
        store.add_feed_act(net, REPLY, feed.REPLY, POST, FAN, "who are you?", 101, now - 90)
        store.add_feed_act(net, MINE, feed.REPLY, POST, SILAS, "a ghost", 102, now - 80)
        store.add_group_post(net, "", LATER, 103, now - 70, SILAS, "", "second dispatch")
        store.add_feed_act(net, LIKE, feed.LIKE, POST, SILAS, "", 104, now - 60)


def test_what_a_name_wrote_posts_and_comments_together(client):
    app, state = client
    now = int(time.time())
    _book(state, now)
    tag(state.home, SILAS, "silas")
    got = app.get("/r/written/silas")
    assert got.status_code == 200, got.text
    said = got.json()["written"]
    assert [r["id"] for r in said] == [LATER, MINE, POST], "newest first, both tables"
    assert REPLY not in [r["id"] for r in said], "somebody else's comment is not their writing"
    assert LIKE not in [r["id"] for r in said], "a like is not writing"
    assert all(r["author"] == SILAS and r["author_tag"] == "silas" for r in said)


def test_each_row_says_what_it_is_and_where_it_draws(client):
    app, state = client
    now = int(time.time())
    _book(state, now)
    tag(state.home, SILAS, "silas")
    said = {r["id"]: r for r in app.get("/r/written/silas").json()["written"]}
    post = said[POST]
    assert post["kind"] == "post" and post["parent_id"] == ""
    assert post["root_id"] == POST and post["root_kind"] == "post"
    assert post["url"] == f"/feed?post={POST}"
    mine = said[MINE]
    assert mine["kind"] == "reply" and mine["parent_id"] == POST
    assert mine["root_id"] == POST and mine["root_kind"] == "post"
    assert mine["url"] == f"/feed?post={POST}"
    page = app.get(mine["url"]).text
    assert "the purge begins" in page and f'id="reply-{MINE}"' in page, \
        "the url it hands back is a page that shows that comment"


def test_since_is_the_callers_to_choose(client):
    app, state = client
    now = int(time.time())
    _book(state, now)
    tag(state.home, SILAS, "silas")
    late = app.get(f"/r/written/silas?since={now - 75}").json()["written"]
    assert [r["id"] for r in late] == [LATER]
    assert app.get(f"/r/written/silas?since={now - 75}").json()["since"] == now - 75
    assert len(app.get("/r/written/silas").json()["written"]) == 3, "two days back by default"


def test_a_name_that_claims_nothing_answers_nothing(client):
    app, state = client
    got = app.get("/r/written/nobodyhere")
    assert got.status_code == 404, got.text
