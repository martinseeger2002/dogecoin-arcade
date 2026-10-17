"""Favorite markets (2026-10-17, layout A): the Exchange opens on the markets a reader
starred, beside All markets and + New pair; a star on every market page and every row, saved
per account on this node like a follow."""
import pathlib
import sys

from nacl.signing import SigningKey

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_web import app_state, client                                  # noqa: F401,E402
from test_admin import EDGE, LOCAL, public                              # noqa: F401,E402
from test_auth_web import _sign_in                                      # noqa: E402
from test_asset_pair import COIN, _file, _gold, _order, _side           # noqa: E402

from arcade.web import door                                              # noqa: E402


def test_the_door_lets_an_account_read_and_set_its_own():
    assert door.public_path("/account/favorites")
    assert door.public_path("/account/favorites", "POST")


def test_an_account_stars_and_unstars_and_it_follows_the_account(public):
    app, state = public
    assert _sign_in(app, SigningKey.generate(), headers=EDGE).status_code == 200
    assert app.get("/account/favorites", headers=EDGE).json() == {"favorites": [], "saved": True}
    one, two = "/exchange/pair/14@test", "/exchange/swap/test-coin/main-coin"
    for key in (one, two):
        assert app.post("/account/favorites", headers=EDGE, json={"key": key, "on": True}).status_code == 200
    assert app.get("/account/favorites", headers=EDGE).json()["favorites"] == [two, one], "newest first"
    app.post("/account/favorites", headers=EDGE, json={"key": one, "on": False})
    assert app.get("/account/favorites", headers=EDGE).json()["favorites"] == [two]
    assert app.post("/account/favorites", headers=EDGE, json={"key": "https://elsewhere"}).status_code == 400
    assert state.setting("favorites:node") is None, "an account's stars are its own"


def test_a_stranger_keeps_them_in_the_browser_and_the_operator_on_the_node(public):
    app, state = public
    assert app.get("/account/favorites", headers=EDGE).json() == {"favorites": [], "saved": False}
    assert app.post("/account/favorites", headers=EDGE, json={"key": "/exchange/pair/1@x"}).status_code == 403
    assert app.post("/account/favorites", headers=LOCAL, json={"key": "/exchange/pair/1@x"}).status_code == 200
    assert state.setting("favorites:node") == ["/exchange/pair/1@x"]


def test_the_list_and_the_market_page_star_the_same_market(client, monkeypatch):
    app, state = client
    _gold(monkeypatch)
    net, main = state.messaging.network, state.ledger.network
    _file(state, monkeypatch, [
        _order("ask-0001", _side(net, 10 * COIN, "token", "59"), _side(main, 20 * COIN))])
    key = f"/exchange/swap/{net}-token-59/{main}-coin"
    listing = app.get("/exchange?tab=tokens").text
    assert 'id="mktmode"' in listing and "Favorites" in listing and "+ New pair" in listing
    assert f'data-fav-key="{key}"' in listing and f'data-fav="{key}"' in listing, "every row has its star"
    market = app.get(f"/exchange/swap/{net}-token-59/{main}-coin").text
    assert f'data-fav="{key}"' in market, "and the market page stars the same key"
