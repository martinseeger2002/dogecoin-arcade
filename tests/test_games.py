"""The Games tab (2026-09-30): inscribed pages whose JSON says {"game": ...},
listed with the mintpads' sorting and the feed's buttons, tippable like a post."""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import games as gameslib                              # noqa: E402
from arcade.web import door as doorlib                            # noqa: E402

MAKER = "ndTq6goKXGb6JLoRRQPwks2kn6bWeGKX1G"
OTHER = "nSomebodyElseAAAAAAAAAAAAAAAAAAAAA"


def _inscribe(state, txid, number, creator, said, *, height=100, kind="text/html"):
    index = state.token_index(state.messaging)
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,block_height,"
            "position,content_type,content_len,sha256,json,chunks,content) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (txid, number, creator, creator, height, 0, kind, 2, "ab" * 32,
             json.dumps(said), 1, b"hi"))
        db.conn.commit()


# ------------------------------------------------------------------ the marker

def test_a_game_needs_a_name_and_everything_else_is_cut_to_size():
    assert gameslib.parse(json.dumps({"game": {"name": "Ashvale"}}))["name"] == "Ashvale"
    for bad in ('{"game": {}}', '{"game": "Ashvale"}', '{"game": {"name": "   "}}',
                '{"mintpad": {"name": "x"}}', "not json", "[]", '{"game": {"name": true}}'):
        assert gameslib.parse(bad) is None, bad
    g = gameslib.parse(json.dumps({"game": {
        "name": "x" * 99, "description": "d" * 999, "multiplayer": "yes",
        "cover": "not a txid", "family": "has space", "players": 8}}))
    assert len(g["name"]) == 60 and len(g["description"]) == 280
    assert g["multiplayer"] is False, "only a real true says multiplayer"
    assert g["cover"] == "" and g["family"] == "" and g["players"] == "8"
    g = gameslib.parse(json.dumps({"game": {"name": "A", "cover": "AB" * 32,
                                            "family": "ashvale", "multiplayer": True}}))
    assert g["cover"] == "ab" * 32 and g["family"] == "ashvale" and g["multiplayer"]


def test_one_card_per_creator_and_name_the_newest_wins():
    rows = [{"txid": "new", "creator": MAKER, "game": {"name": "Ashvale"}},
            {"txid": "old", "creator": MAKER, "game": {"name": "ASHVALE"}},
            {"txid": "theirs", "creator": OTHER, "game": {"name": "Ashvale"}}]
    assert [r["txid"] for r in gameslib.newest_per_game(rows)] == ["new", "theirs"]


# ------------------------------------------------------------------ the tab

def test_the_games_tab_lists_games_and_nothing_else(client):
    app, state = client
    game, old, image, pad = "a1" * 32, "a2" * 32, "a3" * 32, "a4" * 32
    _inscribe(state, old, 10, MAKER, {"game": {"name": "Ashvale", "version": "1.0"}}, height=90)
    _inscribe(state, game, 11, MAKER, {"game": {"name": "Ashvale", "version": "1.1",
                                                "multiplayer": True,
                                                "description": "A small valley RPG."}})
    _inscribe(state, image, 12, MAKER, {"game": {"name": "Not a page"}}, kind="image/png")
    _inscribe(state, pad, 13, MAKER, {"mintpad": {"creator": MAKER, "collection": "x"}})
    page = app.get("/games").text
    assert f"/inscriptions/{game}/full" in page and "A small valley RPG." in page
    assert "v1.1" in page and "multiplayer" in page
    assert f"/inscriptions/{old}/full" not in page, "a new version replaces the card"
    assert f"/inscriptions/{image}/full" not in page, "a game is a page"
    assert f"/inscriptions/{pad}/full" not in page
    assert f'href="/games/{game}"' in page, "and it can be discussed"
    latest = app.get("/games?sort=new").text
    assert f"/inscriptions/{game}/full" in latest


def test_an_empty_chain_says_how_to_list_a_game(client):
    app, _ = client
    page = app.get("/games").text
    assert "No games are on" in page and '"game"' in page


def test_games_sits_between_your_arcade_and_messages():
    from arcade.web.app import ACCOUNT_NAV, NAV
    hrefs = [entry[0] for entry in ACCOUNT_NAV]
    assert hrefs.index("/games") == hrefs.index("/me") + 1 == hrefs.index("/me/messages") - 1
    hrefs = [entry[0] for entry in NAV]
    assert hrefs.index("/games") == hrefs.index("/") + 1 == hrefs.index("/messages") - 1


def test_strangers_can_see_games_and_their_discussions():
    assert doorlib.public_path("/games")
    assert doorlib.public_path("/games/" + "ab" * 32)
    assert not doorlib.public_path("/games/" + "ab" * 32, "POST")


def test_a_game_has_a_discussion_page_and_a_non_game_does_not(client):
    app, state = client
    game, pad = "b1" * 32, "b2" * 32
    _inscribe(state, game, 21, MAKER, {"game": {"name": "Race Condition"}})
    _inscribe(state, pad, 22, MAKER, {"mintpad": {"creator": MAKER, "collection": "x"}})
    page = app.get(f"/games/{game}")
    assert page.status_code == 200 and "A game" in page.text and "play it" in page.text
    assert f"/inscriptions/{game}/full" in page.text
    assert app.get(f"/games/{pad}").status_code == 404


def test_a_game_can_be_tipped_and_the_tip_goes_to_its_maker(client):
    """Tips ask "who wrote this" of _feed_thing; a game's answer is its maker,
    on this chain. The games here live on the messaging chain, so that is the
    inscription's own creator."""
    app, state = client
    game = "c1" * 32
    _inscribe(state, game, 31, MAKER, {"game": {"name": "Tippable"}})
    page = app.get(f"/feed/{game}/tip")
    assert page.status_code == 200
    assert "Tip this game" in page.text and "Tippable" in page.text
    assert MAKER[:14] in page.text, "paid to the maker's own address"


def test_a_game_on_another_chain_is_paid_where_its_maker_said(tmp_path):
    from arcade.messaging.store import MessageStore
    store = MessageStore(tmp_path / "m.sqlite")
    assert store.address_for_other("mMainnetAddr") == ""
    store.conn.execute(
        "INSERT INTO key_announcement(txid,address,pubkey,fingerprint,height,block_time,"
        "seen_at,stated,other_address) VALUES(?,?,?,?,?,?,?,?,?)",
        ("t1", "nTestnetAddr", b"k" * 32, "fp", 5, 0, 0, 1, "mMainnetAddr"))
    store.conn.commit()
    assert store.address_for_other("mMainnetAddr") == "nTestnetAddr"


def test_play_opens_the_real_game_only_when_its_maker_made_both(client):
    """A card for a game inscribed before the tab names it in `play`; Play opens
    that inscription itself, so the viewer answers for the game (its storage,
    its bound pools, its claims) and not for the card."""
    app, state = client
    real, card, borrowed, theirs = "e1" * 32, "e2" * 32, "e3" * 32, "e4" * 32
    _inscribe(state, real, 50, MAKER, {}, height=10)                     # the old game, no JSON
    _inscribe(state, card, 51, MAKER, {"game": {"name": "Ghost Fleet", "play": real}})
    _inscribe(state, theirs, 52, OTHER, {}, height=11)
    _inscribe(state, borrowed, 53, MAKER, {"game": {"name": "Not Mine", "play": theirs}})
    page = app.get("/games").text
    assert f"/inscriptions/{real}/full" in page and f"/inscriptions/{real}/view" in page
    assert f"/inscriptions/{card}/full" not in page
    assert f"/inscriptions/{theirs}/full" not in page, "a card cannot borrow another maker's game"
    assert f"/inscriptions/{borrowed}/full" in page
    assert f'href="/games/{card}"' in page, "the discussion stays with the card"
    thread = app.get(f"/games/{card}").text
    assert f"/inscriptions/{real}/full" in thread


def _delete(state, target, author, txid):
    from arcade.messaging import feed as feedlib
    with state.store() as store:
        store.add_feed_act(state.messaging.network, txid, feedlib.DELETE, target, author,
                           height=200, block_time=1)


def test_a_maker_removes_a_game_card_like_a_post_and_nobody_else_can(client):
    """2026-10-01: game cards removable by whoever created them, the way feed
    posts are -- the feed's own DELETE aimed at the card, from its maker."""
    app, state = client
    old, card, other = "f1" * 32, "f2" * 32, "f3" * 32
    _inscribe(state, old, 60, MAKER, {"game": {"name": "Gone Game", "version": "1"}}, height=90)
    _inscribe(state, card, 61, MAKER, {"game": {"name": "Gone Game", "version": "2"}})
    _inscribe(state, other, 62, MAKER, {"game": {"name": "Still Here"}})
    _delete(state, other, OTHER, "aa" * 32)                         # a stranger's delete
    page = app.get("/games").text
    assert f"/inscriptions/{card}/full" in page and f"/inscriptions/{other}/full" in page
    _delete(state, card, MAKER, "ab" * 32)                          # the maker's
    page = app.get("/games").text
    assert f"/inscriptions/{card}/full" not in page, "removed by its maker"
    assert f"/inscriptions/{old}/full" not in page, "and no older version comes back"
    assert f"/inscriptions/{other}/full" in page, "a stranger's delete counts for nothing"
    assert app.get(f"/games/{card}").status_code == 404
    # The inscription itself is untouched: it still plays from its own link.
    assert app.get(f"/inscriptions/{card}/full").status_code == 200
    # A newer version brings the game back.
    _inscribe(state, "f4" * 32, 63, MAKER, {"game": {"name": "Gone Game", "version": "3"}},
              height=300)
    assert "/inscriptions/" + "f4" * 32 + "/full" in app.get("/games").text


def test_only_the_maker_is_offered_remove(client):
    app, state = client
    mine, theirs = "c7" * 32, "c8" * 32
    _inscribe(state, mine, 70, MAKER, {"game": {"name": "Mine"}})
    _inscribe(state, theirs, 71, OTHER, {"game": {"name": "Theirs"}})
    with state.store() as store:
        store.set_meta(f"identity_address:{state.messaging.network}", MAKER)
    page = app.get("/games").text
    assert f'action="/feed/{mine}/delete"' in page
    assert f'action="/feed/{theirs}/delete"' not in page
