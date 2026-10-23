"""A game's own front door, /play/<game> (SadFrog, 2026-10-17): the sign-up in the
game's colours for somebody who came from the game's site, or the game itself."""

import pathlib
import re
import sys

from nacl.signing import SigningKey

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_games import MAKER, _inscribe                          # noqa: E402
from test_auth_web import LOCAL, TUNNEL, _sign_in                # noqa: E402

from arcade import games as gameslib                              # noqa: E402
from arcade.web import door as doorlib                            # noqa: E402

REAL, CARD = "f1" * 32, "f2" * 32
#: a visitor from the internet, through the tunnel or an edge
OUTSIDE = dict(TUNNEL, **{"x-forwarded-for": "203.0.113.5"})


def _ashvale(state):
    _inscribe(state, REAL, 60, MAKER, {}, height=10)
    _inscribe(state, CARD, 61, MAKER, {"game": {"name": "ASHVALE", "family": "ashvale",
                                                "play": REAL}})


def test_a_stranger_gets_the_games_own_sign_up(client):
    app, state = client
    _ashvale(state)
    page = app.get("/play/ashvale", headers=OUTSIDE)
    assert page.status_code == 200
    body = page.text
    assert "<title>Play ASHVALE</title>" in body
    assert f'const NEXT = "/inscriptions/{REAL}/full";' in body, "signing up lands in the game"
    assert gameslib.DOOR_SKINS["ashvale"]["bg"] in body, "in the game's colours"
    assert re.search(r'import \* as wallet from "/wallet\.js(\?v=[0-9a-f]{12})?"', body), \
        "the same account /join makes (wallet.js by its fingerprinted address since 2026-10-20)"
    assert "nobody can reset it" in body and "recovery words" in body


def test_the_door_is_found_by_family_name_or_card(client):
    app, state = client
    _ashvale(state)
    _inscribe(state, "f3" * 32, 62, MAKER, {"game": {"name": "Ghost Fleet"}})
    for which in ("ashvale", "ASHVALE", CARD, "ghost-fleet"):
        assert app.get(f"/play/{which}", headers=OUTSIDE).status_code == 200, which
    assert app.get("/play/nothing-here", headers=OUTSIDE).status_code == 404
    assert app.get("/play/" + "f9" * 32, headers=OUTSIDE).status_code == 404


def test_somebody_signed_in_goes_straight_to_the_game(client):
    app, state = client
    _ashvale(state)
    assert app.get("/play/ashvale", headers=LOCAL, follow_redirects=False).status_code == 200, \
        "the node's own machine, signed out, gets the sign-up like anybody"
    joined = _sign_in(app, SigningKey.generate(), headers=OUTSIDE)
    assert joined.status_code == 200
    # the session cookie is https-only, and the test client speaks http: hand it back
    session = dict(OUTSIDE, cookie=joined.headers["set-cookie"].split(";")[0])
    went = app.get("/play/ashvale", headers=session, follow_redirects=False)
    assert went.status_code == 303 and went.headers["location"] == f"/inscriptions/{REAL}/full"


def test_the_door_is_public_and_only_for_reading():
    assert doorlib.public_path("/play/ashvale")
    assert doorlib.public_path("/play/" + "ab" * 32)
    assert not doorlib.public_path("/play/ashvale", "POST")
    assert not doorlib.public_path("/play/a/b")


def test_ashvale_says_its_assets_are_test_assets_before_anybody_signs_up(client):
    app, state = client
    _ashvale(state)
    _inscribe(state, "f3" * 32, 62, MAKER, {"game": {"name": "Ghost Fleet"}})
    body = app.get("/play/ashvale", headers=OUTSIDE).text
    assert "Please read before you play" in body
    assert "test network" in body and "no monetary value" in body
    assert "take no responsibility" in body and "glitches" in body
    assert "Please read before you play" not in app.get("/play/ghost-fleet", headers=OUTSIDE).text


def test_a_game_moving_here_says_what_signing_in_here_means(client):
    """2026-10-23 (the 'access' door's move): a player sent from another arcade (?from=) is told, before typing a
    password, that this arcade keeps its own copy of the encrypted wallet; the notice is drawn in the browser from the
    query, never echoed by the server."""
    app, state = client
    _ashvale(state)
    body = app.get(f"/play/{CARD}?from=elsewhere.example", headers=OUTSIDE).text
    assert 'id="moved"' in body and "keeps its own copy of your encrypted wallet" in body
    assert "elsewhere.example" not in body, "the place it came from is not written into the page by the server"


def test_the_access_shim_is_served_to_any_page(client):
    """/r/access.js (2026-10-23): arcade.access for an inscribed page, served with CORS like the other shims."""
    app, _ = client
    got = app.get("/r/access.js", headers=OUTSIDE)
    assert got.status_code == 200 and "javascript" in got.headers["content-type"]
    assert got.headers.get("access-control-allow-origin") == "*"
    for op in ("status", "wallet", "nodes", "move"):
        assert f"{op}: function" in got.text, op
