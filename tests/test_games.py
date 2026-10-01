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
