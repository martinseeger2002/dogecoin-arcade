"""Comments aimed at a name, for the characters to answer (a tester and
2026-09-28)."""

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from test_nft_market import tag                                  # noqa: E402
from arcade.messaging import feed                                # noqa: E402

SILAS = "nSilasAAAAAAAAAAAAAAAAAAAAAAAAAAA"
FAN = "nFanAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
POST, REPLY, DEEP, OTHER, MENTION = ("a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64)


def test_replies_to_a_name_and_where_to_answer(client):
    app, state = client
    now = int(time.time())
    import arcade.web.app as appmod
    with state.store() as store:
        net = state.messaging.network
        store.add_group_post(net, "", POST, 100, now - 100, SILAS, "", "the purge begins")
        store.add_feed_act(net, REPLY, feed.REPLY, POST, FAN, "who are you?", 101, now - 90)
        store.add_feed_act(net, DEEP, feed.REPLY, REPLY, SILAS, "a ghost", 102, now - 80)
        store.add_feed_act(net, OTHER, feed.REPLY, DEEP, FAN, "prove it", 103, now - 70)
        store.add_feed_act(net, MENTION, feed.REPLY, POST, SILAS, "noted", 104, now - 60)
    tag(state.home, SILAS, "silas")
    got = app.get("/r/replies/silas")
    assert got.status_code == 200, got.text
    said = got.json()["replies"]
    ids = [r["id"] for r in said]
    assert OTHER in ids and REPLY in ids, said
    assert MENTION not in ids and DEEP not in ids, "a name's own words are not replies to it"
    deep = next(r for r in said if r["id"] == OTHER)
    assert deep["root_id"] == POST and deep["root_kind"] == "post"
    assert deep["url"] == f"/feed?post={POST}"
    page = app.get(f"/feed?post={OTHER}").text
    assert "the purge begins" in page and f'id="reply-{OTHER}"' in page, \
        "a reply's own id opens its thread, with its own Reply form"

