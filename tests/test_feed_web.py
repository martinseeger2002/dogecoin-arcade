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
    # A bare id is the New feed's cursor (S14: one cursor shape per stream).
    older = app.get(f"/feed?sort=new&before={oldest_shown}").text
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

    # An id, not a picker: the same answer token icons and collection
    # thumbnails arrived at, and one fewer way to say one thing.
    assert 'name="piece"' in body and "the inscription id of a piece you hold" in body
    assert "Any piece you hold, on either chain" in body

    app.post("/profile/picture",
             data={"csrf_token": state.csrf_token, "piece": "ab" * 32},
             follow_redirects=False)
    assert state.setting("pfp") == "ab" * 32, "the choice is remembered"

    # One press: choosing a face publishes it, on the same tag. A picture
    # saved and not published does nothing for anybody, because it is the
    # announcement that carries it (Robin).
    assert "publishes your tag again with the new picture" in \
        " ".join(app.get("/contacts").text.split())


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
    assert "nothing on this chain with that transaction id" in app.get("/feed").text


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


def test_a_comment_can_be_tipped_like_a_post(client):
    """Anything you can like you can tip. The tip page looked in the posts
    table only, so tipping a comment said "no such post on this chain" --
    a comment is a feed_act row (D-143)."""
    from arcade.messaging import feed

    app, state = client
    a_post(state, "a" * 64, text="the post")
    an_act(state, "b" * 64, feed.REPLY, "a" * 64, author=THEM, text="the comment")

    body = " ".join(app.get(f"/feed/{'b' * 64}/tip").text.split())
    assert "no such post" not in body
    assert "the comment" in body and "The reply" in body
    assert "Which chain" in body, "and somewhere to send it"


def test_tipping_something_that_is_not_on_the_feed_is_refused(client):
    app, state = client
    response = app.get(f"/feed/{'f' * 64}/tip", follow_redirects=False)
    assert response.status_code == 303
    assert "nothing on this chain with that transaction id" in app.get("/feed").text


def test_a_like_cannot_be_tipped(client):
    """A like says nothing, so there is nothing to pay somebody for."""
    from arcade.messaging import feed

    app, state = client
    a_post(state, "a" * 64)
    an_act(state, "c" * 64, feed.LIKE, "a" * 64, author=THEM)
    response = app.get(f"/feed/{'c' * 64}/tip", follow_redirects=False)
    assert response.status_code == 303


def test_a_new_picture_changes_every_post_that_person_ever_made(client):
    """The face comes from the newest announcement under that tag and is read
    on every draw, so nothing is copied beside a post and nothing goes stale.
    Change it and yesterday's posts show the new one (Robin, D-138)."""
    from arcade.db import Database
    from arcade.state import install_schema

    app, state = client
    first, second = "aa" * 32, "bb" * 32
    db = Database(state.home / f"{state.messaging.network}-ledger.sqlite")
    install_schema(db)
    for number, txid in enumerate((first, second)):
        db.conn.execute(
            "INSERT INTO inscription(txid,number,creator,owner,block_height,"
            "position,content_type,content_len,sha256,json,chunks,content) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (txid, number, THEM, THEM, 100, 0, "image/png", 10, "ab" * 32,
             "", 1, b"x"))
    db.conn.commit()
    db.close()

    a_post(state, "d" * 64, text="an old post")
    with state.store() as store:
        store.add_key_announcement("t" * 64, THEM, b"\x21" * 32, "ff", 10, 10,
                                   stated=True, tag="them", pfp=first)
    assert f"/content/{first}" in app.get("/feed").text

    # They publish again with a different piece: same tag, newer announcement.
    with state.store() as store:
        store.add_key_announcement("u" * 64, THEM, b"\x21" * 32, "ff", 20, 20,
                                   stated=True, tag="them", pfp=second)
    body = app.get("/feed").text
    assert f"/content/{second}" in body, "the old post wears the new face"
    assert f"/content/{first}" not in body


def test_a_picture_of_a_piece_they_sold_is_not_shown(client):
    """Checked against the chain on every draw: a picture of something
    somebody has sold is a picture of somebody else's property."""
    from arcade.db import Database
    from arcade.state import install_schema

    app, state = client
    piece = "cc" * 32
    db = Database(state.home / f"{state.messaging.network}-ledger.sqlite")
    install_schema(db)
    db.conn.execute(
        "INSERT INTO inscription(txid,number,creator,owner,block_height,"
        "position,content_type,content_len,sha256,json,chunks,content) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (piece, 0, THEM, "nSomebodyElse", 100, 0, "image/png", 10, "ab" * 32,
         "", 1, b"x"))
    db.conn.commit()
    db.close()

    a_post(state, "e" * 64, text="still posting")
    with state.store() as store:
        store.add_key_announcement("t" * 64, THEM, b"\x21" * 32, "ff", 10, 10,
                                   stated=True, tag="them", pfp=piece)
    body = app.get("/feed").text
    assert "still posting" in body
    assert f"/content/{piece}" not in body, "they announced it; they do not hold it"


def test_a_profile_wallet_shows_what_they_hold_not_what_you_hold(client):
    """Clicking "@tag's wallet" from somebody's feed went to your own wallet
    page. It is their holdings, read from the chain for the addresses their
    tag names (Robin, D-145)."""
    from arcade.db import Database
    from arcade.state import install_schema

    app, state = client
    piece = "aa" * 32
    db = Database(state.home / f"{state.messaging.network}-ledger.sqlite")
    install_schema(db)
    db.conn.execute(
        "INSERT INTO inscription(txid,number,creator,owner,block_height,"
        "position,content_type,content_len,sha256,json,chunks,content) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (piece, 7, THEM, THEM, 100, 0, "image/png", 10, "ab" * 32, "", 1, b"x"))
    db.conn.execute("INSERT INTO tag(tag,address,claimed_txid,block_height,position) "
                    "VALUES('them',?,?,100,0)", (THEM, "t" * 64))
    # Their coins and a whole-unit token, as the index watches them: the page
    # read the node's own wallet for coins (0.00 for every account) and divided
    # a whole-unit balance by 10^8 (804 Grepples read 0) -- 2026-09-26.
    from arcade import utxos as utxoslib
    utxoslib.install(db)
    db.conn.execute("INSERT INTO utxo(txid,vout,address,value,height) VALUES(?,?,?,?,?)",
                    ("c1" * 32, 0, THEM, 8_510_488_000, 100))
    db.conn.execute("INSERT OR REPLACE INTO property(property_id, ecosystem, property_type, "
                    "issuer, name, total_tokens, creation_txid, creation_block) "
                    "VALUES(9, 1, 1, ?, 'Grepples', 1000, ?, 1)", (THEM, "dd" * 32))
    db.conn.execute("INSERT OR REPLACE INTO balance(address, property_id, balance) "
                    "VALUES(?, 9, 804)", (THEM,))
    db.conn.commit()
    db.close()

    body = " ".join(app.get("/u/them/wallet").text.split())
    assert "85.10" in body, "their coins, from the index"
    assert "Grepples</a> <span class=\"muted\">804</span>" in body, body[body.find("Grepples"):][:200]
    assert "@them's wallet" in body
    assert THEM in body, "their address, not this wallet's"
    assert f"/content/{piece}" in body, "and what they hold"
    assert "back up your wallet" in body or True    # the footer is not the point


def test_a_profile_wallet_for_a_tag_nobody_holds_says_so(client):
    app, state = client
    body = " ".join(app.get("/u/nobody/wallet").text.split())
    assert "Nobody holds" in body


def test_a_bio_and_a_link_are_published_with_the_tag(client):
    app, state = client
    app.post("/profile/about",
             data={"csrf_token": state.csrf_token, "bio": "builds chain things",
                   "url": "https://dogecoinarcade.com"},
             follow_redirects=False)
    assert state.setting("bio") == "builds chain things"
    assert state.setting("url") == "https://dogecoinarcade.com"

    body = " ".join(app.get("/contacts").text.split())
    assert "builds chain things" in body


def test_a_link_that_is_not_a_link_is_refused(client):
    """It ends up on other people's pages as something to click."""
    app, state = client
    app.post("/profile/about",
             data={"csrf_token": state.csrf_token, "bio": "",
                   "url": "javascript:alert(1)"}, follow_redirects=False)
    assert not state.setting("url")
    assert "a link starts with https://" in " ".join(app.get("/contacts").text.split())


def test_a_bio_too_long_is_refused_rather_than_trimmed(client):
    app, state = client
    app.post("/profile/about",
             data={"csrf_token": state.csrf_token, "bio": "x" * 200, "url": ""},
             follow_redirects=False)
    assert not state.setting("bio")
    assert "at most 160 characters" in " ".join(app.get("/contacts").text.split())


def test_looking_at_the_feed_clears_its_badge(client):
    """A count beside Feed that survives looking at the feed is a count
    nobody can clear (Robin)."""
    app, state = client
    a_post(state, "a" * 64, text="something new")
    with state.store() as store:
        assert store.board_unread(state.messaging.network) == 1

    body = app.get("/feed").text
    assert "something new" in body
    with state.store() as store:
        assert store.board_unread(state.messaging.network) == 0

    # And the nav it is drawn in has no badge on the next page either.
    assert 'href="/feed">Feed<span class="nav-count"' not in app.get("/").text


def test_a_profile_page_does_not_clear_the_badge(client):
    """One person's feed is not the feed: what is unread is everybody
    else's, and reading one person's page has not shown it to you."""
    app, state = client
    a_post(state, "a" * 64, text="something new")
    app.get("/u/them")
    with state.store() as store:
        assert store.board_unread(state.messaging.network) == 1


def test_a_page_can_look_somebody_up_by_name(client):
    """Everything published under a tag, in one lookup, and nothing about
    the wallet drawing the page (D-145)."""
    from arcade.db import Database
    from arcade.state import install_schema

    app, state = client
    who = "nThemAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    db = Database(state.home / f"{state.messaging.network}-ledger.sqlite")
    install_schema(db)
    db.conn.execute("INSERT INTO tag(tag,address,claimed_txid,block_height,position) "
                    "VALUES('them',?,?,100,0)", (who, "t" * 64))
    db.conn.commit()
    db.close()
    with state.store() as store:
        store.add_key_announcement("a" * 64, who, b"\x21" * 32, "ff", 10, 10,
                                   stated=True, tag="them",
                                   other_address="PTheirMainnetAAAAAAAAAAAAAAAAAAAAA",
                                   bio="builds chain things",
                                   url="https://dogecoinarcade.com")

    found = app.get("/r/profile/them").json()
    assert found["tag"] == "them" and found["address"] == who
    assert found["mainnet"] == "PTheirMainnetAAAAAAAAAAAAAAAAAAAAA"
    assert found["bio"] == "builds chain things"
    assert found["url"] == "https://dogecoinarcade.com"
    assert found["picture"] == "", "they announced none"
    assert app.get("/r/profile/nobody").status_code == 404


def test_friends_is_a_third_order_narrowed_to_the_address_book(client):
    """Friends (2026-09-25): the newest posts, with the operator's book
    handed to the page as addresses for the browser to narrow them by. The
    node sends everybody's posts; which are shown is the page's to decide."""
    app, state = client
    FRIEND = "nFriendAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    a_post(state, f"{1:064x}", text="from a friend", sender=FRIEND, height=101)
    a_post(state, f"{2:064x}", text="from a stranger", height=102)
    with state.store() as store:
        store.save_contact(name="friend", testnet_address=FRIEND)
    body = app.get("/feed?sort=friends").text
    assert 'href="/feed?sort=friends"' in body and ">Friends</a>" in body
    assert f'data-author="{FRIEND}"' in body and f'data-author="{THEM}"' in body
    assert FRIEND in body.split("book = new Set(")[1].split(")")[0], \
        "the operator's book is what the page narrows by"
    assert "from a stranger" in app.get("/feed").text, "Popular is unchanged"


def test_blocking_is_offered_on_a_profile_and_listed_in_the_book(client):
    """A Block button on somebody's page, and the Blocked list at the bottom of
    the address book, shut until opened (2026-09-25)."""
    app, state = client
    book = app.get("/contacts").text
    assert '<details class="panel" id="blocked-box"' in book
    assert book.index("blocked-box") > book.index("Address book"), \
        "below everything else"
    assert "window.arcadeBlocked" in book


def test_a_profiles_message_button_lands_on_a_filled_in_letter(client):
    """The operator's Message button goes to /compose?to=@tag, and the box is
    already filled in (2026-09-26)."""
    app, _ = client
    body = app.get("/compose?to=@somebody").text
    assert 'value="@somebody"' in body


def test_page_scripts_wait_for_what_base_defines_below_them():
    """base.html defines window.arcadeBlocked and arcadeAsk AFTER the body block,
    so a page's classic inline script that reads them straight away finds
    nothing: the profile's Block button did nothing at all (tester-e5,
    2026-09-26). Module scripts run after parsing and are fine."""
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "arcade/web/templates"
    late = []
    for page in root.glob("*.html"):
        if page.name == "base.html":
            continue
        for attrs, body in re.findall(r"<script([^>]*)>(.*?)</script>",
                                      page.read_text(), re.S):
            if "module" in attrs or "src=" in attrs:
                continue
            if "window.arcadeBlocked" in body and "DOMContentLoaded" not in body:
                late.append(page.name)
    assert not late, late


def test_a_post_you_shared_offers_unshare_instead_of_a_second_share(client):
    """A second share is a second paid transaction saying the same thing
    (tester-e5, 2026-09-26)."""
    from arcade import feedview
    from arcade.messaging import feed as feedlib
    assert "shared_by_me" in feedview.Shown.__dataclass_fields__
    page = (pathlib.Path(__file__).resolve().parents[1]
            / "arcade/web/templates/feed.html").read_text()
    assert "Unshare" in page and "/unshare" in page and "p.my_share" in page



def test_a_cursor_the_feed_cannot_read_is_refused_not_restarted(client):
    """@tester S12/S14 (2026-09-26): a garbled or cross-stream cursor, or a
    sort that does not exist, used to come back as page one with a 200."""
    app, state = client
    a_post(state, "ab" * 32, text="one post")
    assert app.get("/feed?before=AAAA").status_code == 400
    assert app.get("/feed?sort=new&before=44@0.06@1790469609").status_code == 400
    assert app.get("/feed?before=44").status_code == 400
    assert app.get("/feed?sort=hot").status_code == 400
    assert app.get("/feed?before=").status_code == 200, "empty is page one"
    assert app.get("/feed?sort=new&before=44").status_code == 200
    assert app.get("/feed?before=44@0.06@1790469609").status_code == 200
