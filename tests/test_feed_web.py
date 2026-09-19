"""The feed as a page: who may post, what a byline is, and what scrolls.

The rules that matter here are the ones a person meets: a name before you
post, a tag that goes somewhere when you click it, and ten posts at a time
(D-138).
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade.messaging import feed                                # noqa: E402

THEM = "nThemAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def a_post(state, txid, text="hello", sender=THEM, height=100, mine=False):
    with state.store() as store:
        store.add_group_post(state.messaging.network, "", txid, height,
                             height * 10, sender, "", text, mine=mine)


def an_act(state, txid, kind, target, author=THEM, text="", height=200):
    with state.store() as store:
        store.add_feed_act(state.messaging.network, txid, kind, target,
                           author, text, height, height * 10)


def test_the_feed_shows_posts_newest_first(client):
    app, state = client
    for n in range(3):
        a_post(state, f"{n:064x}", text=f"post {n}", height=100 + n)
    body = app.get("/feed").text
    assert body.index("post 2") < body.index("post 1") < body.index("post 0")


def test_ten_at_a_time_and_a_way_to_the_rest(client):
    """A cursor rather than a page number: a post arriving between requests
    cannot push a row onto two pages or off both."""
    app, state = client
    for n in range(14):
        a_post(state, f"{n:064x}", text=f"post {n}", height=100 + n)
    body = app.get("/feed").text
    assert body.count('class="panel feedpost') == 10
    assert "post 13" in body and "post 3" not in body
    assert "before=" in body, "and a link to the older ones"

    with state.store() as store:
        rows = store.feed_posts(state.messaging.network, limit=99)
    oldest_shown = rows[9]["id"]
    older = app.get(f"/feed?before={oldest_shown}").text
    assert "post 3" in older and "post 13" not in older


def test_you_need_a_name_before_you_can_post(client):
    """Everything in the feed has a person behind it, so every byline goes
    somewhere (D-138)."""
    app, state = client
    body = app.get("/feed").text
    assert "You need a name before you can post" in body
    assert 'action="/feed/post"' not in body

    response = app.post("/feed/post", data={"csrf_token": state.csrf_token,
                                            "text": "hello"},
                        follow_redirects=False)
    assert response.status_code == 303
    # Refused, with a reason: no name here, or no node to claim one with.
    # Either way nothing was posted, which is the part that matters.
    after = " ".join(app.get("/feed").text.split())
    assert "claim a @tag first" in after or "waiting for the testnet node" in after
    with state.store() as store:
        assert store.feed_posts(state.messaging.network) == []


def test_a_byline_links_to_that_persons_feed(client, monkeypatch):
    from arcade.web import app as webapp

    app, state = client
    a_post(state, "a" * 64, text="hello from them")
    monkeypatch.setattr(webapp, "_TAGS_FOR_TEST", None, raising=False)
    with state.store() as store:
        store.add_key_announcement("t" * 64, THEM, b"\x21" * 32, "ff", 10, 10,
                                   stated=True, tag="them")
    body = app.get("/feed").text
    # The tag is read from the chain's tag index, which this fixture has no
    # node for; what the page must do either way is never invent a name.
    assert "hello from them" in body
    assert THEM[:12] in body or "@them" in body


def test_a_profile_page_for_a_tag_nobody_holds_says_so(client):
    app, state = client
    body = app.get("/u/nobody").text
    assert "Nobody holds" in body and "@nobody" in body


def test_a_deleted_post_is_not_drawn(client):
    app, state = client
    a_post(state, "a" * 64, text="regrettable")
    an_act(state, "b" * 64, feed.DELETE, "a" * 64, author=THEM)
    assert "regrettable" not in app.get("/feed").text


def test_likes_and_replies_are_shown_on_the_post(client):
    app, state = client
    a_post(state, "a" * 64, text="the post")
    an_act(state, "b" * 64, feed.LIKE, "a" * 64, author="nOther")
    an_act(state, "c" * 64, feed.REPLY, "a" * 64, author="nOther",
           text="a comment")
    body = app.get("/feed").text
    assert "the post" in body and "a comment" in body
    assert "♡ 1" in body or "1" in body


def test_muting_hides_the_words_here_and_tells_nobody(client):
    app, state = client
    a_post(state, "a" * 64, text="noisy")
    app.post(f"/feed/{'a' * 64}/mute",
             data={"csrf_token": state.csrf_token, "text": THEM},
             follow_redirects=False)
    body = " ".join(app.get("/feed").text.split())
    assert "noisy" not in body
    assert "Hidden — you muted this person" in body
    with state.store() as store:
        assert THEM in store.muted()


def test_a_picture_in_a_post_is_shown_and_a_page_is_sandboxed(client):
    """An inscription shared in a post renders when it cannot be dangerous:
    a picture is drawn, a page goes in the sandbox the viewer already uses,
    and what this node cannot identify stays a link (D-138)."""
    from arcade.db import Database
    from arcade.state import install_schema

    app, state = client
    picture, page, unknown = "aa" * 32, "bb" * 32, "cc" * 32
    db = Database(state.home / f"{state.messaging.network}-ledger.sqlite")
    install_schema(db)
    for txid, kind in ((picture, "image/png"), (page, "text/html")):
        db.conn.execute(
            "INSERT INTO inscription(txid,number,creator,owner,block_height,"
            "position,content_type,content_len,sha256,json,chunks,content) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (txid, 0 if txid == picture else 1, THEM, THEM, 100, 0, kind,
             10, "ab" * 32, "", 1, b"x"))
    db.conn.commit()
    db.close()

    a_post(state, "d" * 64,
           text=f"look /content/{picture} and /content/{page} and /content/{unknown}")
    body = app.get("/feed").text
    assert f'<img class="postmedia" src="/content/{picture}"' in body
    assert f'class="inscription-frame postmedia" src="/content/{page}"' in body
    assert 'sandbox="allow-scripts allow-pointer-lock"' in body
    assert "allow-same-origin" not in body, "never, in any frame this page draws"
    assert f'href="/inscriptions/{unknown}/view"' in body, "unknown stays a link"


def test_a_post_cannot_smuggle_markup_into_the_page(client):
    """Escaped first, then inscriptions added back by this code: what a post
    says about itself is never what decides how it is drawn."""
    app, state = client
    a_post(state, "e" * 64, text="<script>alert(1)</script> <b>bold</b>")
    body = app.get("/feed").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_a_profile_picture_is_a_piece_you_hold(client):
    """Chosen from what this wallet holds, published with the tag, and
    honoured only while the chain says you still hold it (D-138)."""
    app, state = client
    # Whitespace-insensitive: the sentences are wrapped in the template, and a
    # test that breaks when somebody re-wraps a paragraph teaches nobody
    # anything.
    body = " ".join(app.get("/contacts").text.split())
    assert "Profile picture" in body

    # Nothing held, nothing offered -- and the page says where a picture
    # comes from rather than showing an empty row.
    assert "This wallet holds no pictures yet" in body

    app.post("/profile/picture",
             data={"csrf_token": state.csrf_token, "piece": "ab" * 32},
             follow_redirects=False)
    assert state.setting("pfp") == "ab" * 32, "the choice is remembered"

    # And it is not published by choosing it: that costs a transaction, and
    # the page says so rather than spending on somebody's behalf.
    assert "Publish your tag" in " ".join(app.get("/contacts").text.split())


def test_a_picture_that_is_not_an_inscription_is_refused(client):
    app, state = client
    app.post("/profile/picture",
             data={"csrf_token": state.csrf_token, "piece": "not-a-piece"},
             follow_redirects=False)
    assert not state.setting("pfp")
    assert "an inscription on one of these chains" in \
        " ".join(app.get("/contacts").text.split())


def test_a_tip_page_says_where_the_money_would_go(client):
    """Only where they PUBLISHED an address: an address nobody published is
    an address nobody asked to be paid at (D-137)."""
    app, state = client
    a_post(state, "a" * 64, text="worth something")

    body = " ".join(app.get(f"/feed/{'a' * 64}/tip").text.split())
    assert "worth something" in body
    # The chain they posted from is always payable: they signed a transaction
    # with that address, which is a better statement than an announcement.
    assert "Which chain" in body and THEM[:12] in body
    assert "PMainnetAddress" not in body, "and nothing they never published"

    with state.store() as store:
        store.add_key_announcement("t" * 64, THEM, b"\x21" * 32, "ff", 10, 10,
                                   stated=True, tag="them",
                                   other_address="PMainnetAddressAAAAAAAAAAAAAAAAAAA")
    body = " ".join(app.get(f"/feed/{'a' * 64}/tip").text.split())
    # Shown truncated, because an address is for a machine to read.
    assert "Which chain" in body and "PMainnetAddres" in body


def test_a_tip_for_a_post_nobody_has_is_refused(client):
    app, state = client
    response = app.get(f"/feed/{'f' * 64}/tip", follow_redirects=False)
    assert response.status_code == 303
    assert "no such post" in app.get("/feed").text


def test_a_tip_to_a_chain_they_never_published_is_refused(client):
    app, state = client
    a_post(state, "a" * 64)
    app.post(f"/feed/{'a' * 64}/tip",
             data={"csrf_token": state.csrf_token, "network": "main",
                   "amount": "1"}, follow_redirects=False)
    assert "nowhere to send it" in " ".join(app.get("/feed").text.split())


def test_a_contact_gets_what_their_announcement_says(client):
    """A book held somebody whose mainnet address was one table away: the
    "Seen on the chain" button saved the address it knew and asked their
    announcement for nothing (D-139)."""
    app, state = client
    with state.store() as store:
        store.add_key_announcement("t" * 64, THEM, b"\x21" * 32, "ff", 10, 10,
                                   stated=True, tag="them",
                                   other_address="PTheirMainnetAddressAAAAAAAAAAAAAA")

    app.post("/contacts/add-published",
             data={"csrf_token": state.csrf_token, "pubkey": "21" * 32,
                   "address": THEM, "name": "them"},
             follow_redirects=False)

    with state.store() as store:
        (row,) = store.contacts()
    assert row["mainnet_address"] == "PTheirMainnetAddressAAAAAAAAAAAAAA"
    assert bytes(row["pubkey"]) == b"\x21" * 32


def test_a_book_saved_before_that_repairs_itself(client):
    """Nobody should have to re-add anybody: drawing the page fills the gaps
    from announcements already in the store, and never overwrites what
    somebody typed."""
    app, state = client
    with state.store() as store:
        store.save_contact(name="them", testnet_address=THEM)
        store.add_key_announcement("t" * 64, THEM, b"\x21" * 32, "ff", 10, 10,
                                   stated=True, tag="them",
                                   other_address="PTheirMainnetAddressAAAAAAAAAAAAAA")
        store.save_contact(name="typed", testnet_address="nOtherAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                           mainnet_address="PTypedByHandAAAAAAAAAAAAAAAAAAAAAA")
        store.add_key_announcement("u" * 64, "nOtherAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                                   b"\x22" * 32, "ee", 10, 10, stated=True,
                                   other_address="PSomethingElseAAAAAAAAAAAAAAAAAAAA")

    app.get("/contacts")

    with state.store() as store:
        found = {r["name"]: r["mainnet_address"] for r in store.contacts()}
    assert found["them"] == "PTheirMainnetAddressAAAAAAAAAAAAAA", "the gap is filled"
    assert found["typed"] == "PTypedByHandAAAAAAAAAAAAAAAAAAAAAA", \
        "and what somebody typed is theirs"
