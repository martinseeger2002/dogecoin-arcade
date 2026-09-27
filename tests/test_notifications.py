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
    assert "liked your post" in seen and "nrow new" in seen
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


def test_a_link_to_a_post_that_is_not_here_says_which_it_is_not(public):
    """One hex character, and the page used to have nothing to say about it.

    Measured by the reporter, not inferred: two links to a release post, one
    character apart at position 62, both answering 200 -- one with the post on
    it and one with the plain feed (`?post=` falls through to the empty-list
    panel, whose one sentence is "Nothing here yet", which is a sentence about a
    feed and not about the id in the URL). It is the same fault as a POST to a
    route this node is older than: two different facts, one indistinguishable
    response, and the reader has to guess which (a tester, 2026-09-27).

    So the page distinguishes the three things it can actually know -- the post
    is not here, the id cannot be an id, or there is nothing posted at all --
    and prints the id it looked for, because that is the only way a person can
    compare it with the one they meant. A `post=` that is not a txid is not
    echoed: it is the one string on this page that arrived from a URL and could
    be anything.

    One shape it deliberately does NOT separate, and that is a decision rather
    than an oversight (the reporter asked it be flagged): a txid that was real
    on another node, and one this node indexed and later pruned, both arrive at
    the same sentence as a txid that never existed. The page cannot tell them
    apart -- the question is answered from the local table, and an absent row is
    one fact, not three -- and "not on this node" is the true sentence for all
    three. Splitting them would mean keeping a tombstone for every post id ever
    seen to answer a question nobody is asking, so it stays one sentence and one
    test.
    """
    app, state = public
    a_post(state, "d" * 64, text="quill-42", sender=THEM)

    mine = "d" * 63 + "e"
    page = app.get(f"/feed?post={mine}", headers=EDGE).text
    assert "That post is not on this node" in page, \
        "a wrong id was answered with the front page"
    assert mine in page, "it prints what it looked for, or the typo is found by hand"
    assert "quill-42" not in page and "Nothing here yet" not in page, \
        "one is a missing post and one is an empty feed; not both at once"

    loose = app.get("/feed?post=hello", headers=EDGE).text
    assert "not a post id" in loose, "a non-id was answered with the front page"
    assert "hello" not in loose, "the id-shaped claim was echoed back into the page"


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


# --- @mentions (2026-09-26) -------------------------------------------

def test_a_mention_is_news_whole_names_only(public):
    app, state = public
    a_post(state, "f1" * 32, text="hey @Maple look at this", sender=THEM)
    a_post(state, "f2" * 32, text="@maplesyrup is not you", sender=THEM)
    a_post(state, "f3" * 32, text="write to maple@example.com", sender=THEM)
    a_post(state, "f4" * 32, text="me naming @maple myself", sender=ME)
    an_act(state, "f5" * 32, feed.REPLY, "f2" * 32, author=THEM, text="cc @maple.")
    with state.store() as store:
        events = notify.mention_events(store.conn, state.messaging.network, ME, "maple")
    got = sorted((e.source, e.kind, e.target) for e in events)
    assert got == [("mention", "mentioned you in a post", "f1" * 32),
                   ("mention_reply", "mentioned you in a comment", "f2" * 32)]


def test_a_mention_shows_on_the_notifications_page(public):
    from arcade.db import Database
    from arcade.state import install_schema

    app, state = public
    _me(app, state)
    db = Database(state.home / f"{state.messaging.network}-ledger.sqlite")
    install_schema(db)
    db.conn.execute("INSERT OR REPLACE INTO tag(tag,address,claimed_txid,block_height,position) "
                    "VALUES('maple',?,?,100,0)", (ME, "t" * 64))
    db.conn.commit()
    db.close()
    a_post(state, "f6" * 32, text="thanks @maple!", sender=THEM)
    seen = " ".join(app.get("/me/notifications", headers=EDGE).text.split())
    assert "mentioned you in a post" in seen, seen[seen.find("Notifications"):][:600]
    assert f"/feed?post={'f6' * 32}" in seen


def test_a_mention_in_a_post_is_a_link_to_their_feed():
    from arcade.web.app import post_html
    out = post_html("hi @Maple, mail bob@example.com <b>@x</b> and @ok_name")
    assert '<a class="mention" href="/u/maple">@Maple</a>' in out
    assert 'href="/u/ok_name"' in out
    assert "bob@example.com" in out and "/u/example" not in out
    assert "&lt;b&gt;" in out, "still escaped first"
    assert 'href="/u/x"' not in out, "a tag is at least two characters"


def test_a_swap_names_the_piece_by_its_collection():
    """a tester, 2026-09-26: "You sold a piece for 7" -- which piece, to whom."""
    rows = [{"seq": 5, "txid": "aa" * 32, "inscription": "bb" * 32,
             "from_address": ME, "to_address": THEM, "block_height": 10, "time": 100,
             "number": 44, "collection": "Skull Squad", "edition": 7},
            {"seq": 6, "txid": "cc" * 32, "inscription": "dd" * 32,
             "from_address": ME, "to_address": THEM, "block_height": 11, "time": 101,
             "number": 45, "collection": None, "edition": None}]
    got = notify.swap_events(rows, {ME}, {"aa" * 32: 700000000})
    assert [(e.kind, e.text, e.actor) for e in got] == [
        ("sold", "Skull Squad #7", THEM), ("sold", "#45", THEM)]
    assert got[0].amount == 700000000


def test_a_deleted_reply_or_post_is_no_longer_news(public):
    """a tester, 2026-09-27: a reply its author deleted still showed on the
    notifications of the post it answered. Deleted is deleted everywhere."""
    app, state = public
    _me(app, state)
    a_post(state, "d1" * 32, text="my post", sender=ME)
    an_act(state, "d2" * 32, feed.REPLY, "d1" * 32, author=THEM, text="oops, wrong post @maple")
    an_act(state, "d3" * 32, feed.REPLY, "d1" * 32, author=THEM, text="this one stays")
    a_post(state, "d4" * 32, text="hey @maple", sender=THEM)
    an_act(state, "d5" * 32, feed.DELETE, "d2" * 32, author=THEM)
    an_act(state, "d6" * 32, feed.DELETE, "d4" * 32, author=THEM)
    an_act(state, "d7" * 32, feed.DELETE, "d3" * 32, author="nOther")  # not theirs to delete
    with state.store() as store:
        events = notify.feed_events(store.conn, state.messaging.network, ME)
        mentions = notify.mention_events(store.conn, state.messaging.network, ME, "maple")
    assert [e.extra["txid"] for e in events if e.kind == "replied to"] == ["d3" * 32]
    assert mentions == []
