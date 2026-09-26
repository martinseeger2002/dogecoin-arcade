"""The Notifications page, and the red counts on its tab and on Messages.

2026-09-25: tell an account when anybody likes, replies to, shares or tips
its posts, replies on posts it liked or shared, buys from it, or writes to it --
with a red count until it looks.
"""

import pathlib
import sys

import pytest
from nacl.signing import SigningKey

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_auth_web import _sign_in                               # noqa: E402
from test_feed_web import a_post, an_act                          # noqa: E402

from arcade import notify                                        # noqa: E402
from arcade.messaging import feed                                # noqa: E402

EDGE = {"host": "node.dogecoinarcade.com", "cf-ray": "abc-LHR"}
ME = "nMeAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
THEM = "nThemBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


@pytest.fixture
def public(client):
    app, state = client
    state.set_setting("public_hosts", ["node.dogecoinarcade.com"])
    yield app, state
    state.set_setting("public_hosts", [])


def _me(app, state):
    key = SigningKey.generate()
    assert _sign_in(app, key, headers=EDGE).status_code == 200
    pubkey = key.verify_key.encode().hex()
    state.set_setting(f"address:{pubkey}", ME)
    return pubkey


def test_what_others_do_to_my_post_is_news_and_what_i_do_is_not(public):
    app, state = public
    _me(app, state)
    a_post(state, "a" * 64, text="my post", sender=ME)
    an_act(state, "1" * 64, feed.LIKE, "a" * 64, author=THEM)
    an_act(state, "2" * 64, feed.REPLY, "a" * 64, author=THEM, text="nice one")
    an_act(state, "3" * 64, feed.LIKE, "a" * 64, author=ME)          # my own like: not news
    with state.store() as store:
        events = notify.feed_events(store.conn, state.messaging.network, ME)
    assert sorted(e.kind for e in events) == ["liked", "replied to"]


def test_a_reply_on_a_post_i_liked_is_news_a_like_on_it_is_not(public):
    app, state = public
    _me(app, state)
    a_post(state, "b" * 64, text="their post", sender=THEM)
    an_act(state, "4" * 64, feed.LIKE, "b" * 64, author=ME)
    an_act(state, "5" * 64, feed.REPLY, "b" * 64, author="nOther", text="agreed")
    an_act(state, "6" * 64, feed.LIKE, "b" * 64, author="nOther")
    with state.store() as store:
        events = notify.feed_events(store.conn, state.messaging.network, ME)
    assert [(e.kind, e.about_mine) for e in events] == [("replied to", False)]


def test_the_red_count_shows_until_the_page_is_looked_at(public):
    app, state = public
    _me(app, state)
    a_post(state, "c" * 64, text="count me", sender=ME)
    an_act(state, "7" * 64, feed.LIKE, "c" * 64, author=THEM)
    page = app.get("/me", headers=EDGE).text
    assert 'href="/me/notifications">Notifications<span class="nav-count">1</span>' in page
    seen = app.get("/me/notifications", headers=EDGE).text
    assert "liked your post" in seen and "notif new" in seen
    assert 'Notifications<span class="nav-count">' not in seen.split("</nav>")[0], \
        "its own tab is read by the time it is drawn"
    again = app.get("/me", headers=EDGE).text
    assert 'Notifications<span class="nav-count">' not in again


def test_the_tabs_go_messages_notifications_feed_address_book(public):
    app, state = public
    _me(app, state)
    nav = app.get("/me", headers=EDGE).text.split("</nav>")[0]
    order = [nav.index(f'href="{h}"') for h in
             ("/me/messages", "/me/notifications", "/feed", "/me/contacts")]
    assert order == sorted(order)


def test_a_notification_links_to_its_post_alone(public):
    app, state = public
    a_post(state, "d" * 64, text="the one", sender=THEM)
    a_post(state, "e" * 64, text="another one", sender=THEM)
    page = app.get(f"/feed?post={'d' * 64}", headers=EDGE).text
    assert "the one" in page and "another one" not in page


def test_seen_is_a_row_marker_not_a_clock():
    """A like still in the pool has no block time; a clock marker would make it
    new again when its block lands. A row id does not move."""
    ev = [notify.Event(source="feed", seq=5, kind="liked", actor="x", at=0)]
    seen = notify.seen_now(ev, {})
    ev[0].at = 12345                                 # its block lands
    assert notify.merge(ev, seen)[0].unread is False


# --- /content/<id> in messages (2026-09-25) ------------------------------------


def test_a_picture_named_in_a_message_is_drawn_as_the_feed_draws_it(client, monkeypatch):
    app, state = client
    from arcade.messaging.keys import Identity
    state.identity = Identity.generate()
    peer = b"\x42" * 32
    piece = "ab" * 32
    with state.store() as store:
        store.add_message(None, "t1", "t1", 1, 0, "nThem", peer, state.identity.fingerprint,
                          f"look at this\n/content/{piece}\n<b>not bold</b>".encode())

    class Index:
        def inscription(self, key):
            return {"content_type": "image/png"} if key == piece else None
    monkeypatch.setattr(type(state), "token_index", lambda self, chain=None: Index())
    body = app.get(f"/messages/{peer.hex()}").text
    assert f'<img class="postmedia" src="/content/{piece}"' in body
    assert "&lt;b&gt;not bold&lt;/b&gt;" in body, "the message's own markup stays text"
