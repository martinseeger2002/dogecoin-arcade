"""The Desktop View (desktop.html; 2026-10-28: "a toggle at the top left of the user's desktop view (not the admin side
and leave mobile view alone) that turns the normal website into a 'Desktop View' ... like old windows 98"; "move apps
around the screen like normal desktop apps"; "select our own background image of inscriptions")."""
import json
import pathlib
import re
import sys

from nacl.signing import SigningKey

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_admin import EDGE, LOCAL, public                      # noqa: F401,E402
from test_auth_web import _sign_in                              # noqa: E402
from test_games import MAKER, _inscribe                         # noqa: E402

from arcade import games as gameslib                              # noqa: E402
from arcade.web import door                                       # noqa: E402

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates"


def test_the_door_lets_anybody_open_the_desktop_and_an_account_keep_its_wallpaper():
    assert door.public_path("/desktop")
    assert door.public_path("/account/deskbg") and door.public_path("/account/deskbg", "POST")


def test_the_desktop_has_every_tab_but_admin_as_an_icon_and_the_apps_for_its_programs(client):
    app, state = client
    _inscribe(state, "a1" * 32, 51, MAKER, {"app": {"name": "Call Me", "genre": "calls"}}, height=100)
    page = app.get("/desktop")
    assert page.status_code == 200
    tabs = json.loads(re.search(r"var TABS = (\[.*?\]);\n", page.text).group(1))
    apps = json.loads(re.search(r"var APPS = (\[.*?\]);\n", page.text).group(1))
    hrefs = [t["href"] for t in tabs]
    assert "/feed" in hrefs and "/exchange" in hrefs
    assert "/apps" not in hrefs, "the App Store is its own icon"
    assert not [h for h in hrefs if h.startswith("/admin")], "not the admin side"
    one = [a for a in apps if a["name"] == "Call Me"][0]
    assert one["key"] == gameslib.app_key(MAKER, "Call Me") and one["open"] == f"/inscriptions/{one['page']}/full"
    assert 'id="deskview"' not in page.text, "the desktop draws its own switch, not the page header's"
    assert 'id="dk-toggle"' in page.text


def test_a_wallpaper_is_any_inscription_with_how_it_is_laid_and_kept_for_the_account(public):
    app, _state = public
    assert app.get("/account/deskbg", headers=EDGE).json() == {"id": "", "fit": "fill"}
    assert app.post("/account/deskbg", headers=EDGE, json={"id": "ab" * 32 + "i0"}).status_code == 403, "sign in first"
    assert _sign_in(app, SigningKey.generate(), headers=EDGE).status_code == 200
    iid = "AB" * 32 + "i3"
    assert app.post("/account/deskbg", headers=EDGE, json={"id": iid, "fit": "tile"}).json() == \
        {"id": iid.lower(), "fit": "tile"}
    assert app.get("/account/deskbg", headers=EDGE).json() == {"id": iid.lower(), "fit": "tile"}
    assert app.get("/account/homebg", headers=EDGE).json() == {"id": ""}, "the phone's home screen keeps its own"
    assert app.post("/account/deskbg", headers=EDGE, json={"id": "javascript:alert(1)"}).status_code == 400
    assert app.post("/account/deskbg", headers=EDGE, json={"id": iid, "fit": "url(x)"}).status_code == 400
    assert app.post("/account/deskbg", headers=EDGE, json={"id": ""}).json() == {"id": "", "fit": "fill"}


def test_the_switch_is_on_every_page_but_admin_and_never_on_a_phone(client):
    app, _state = client
    feed = app.get("/feed").text
    assert 'id="deskview"' in feed
    admin = app.get("/admin")
    assert admin.status_code != 200 or 'id="deskview"' not in admin.text
    base = (TEMPLATES / "base.html").read_text()
    assert "@media(max-width:640px){header .deskview{display:none !important}}" in base, "leave mobile view alone"
    assert "not path.startswith('/admin')" in base
    # Pages opened in a tab of their own go to the desktop while the switch is on: never Admin, sign-in, a phone,
    # the installed app or a page already in one of its windows.
    redirect = base[base.index("THE DESKTOP VIEW (desktop.html"):]
    redirect = redirect[:redirect.index("if (!installed || window.top !== window")]
    for need in ("!installed", "window.top === window", "window.innerWidth > 640", "admin|join|restore|signin",
                 "localStorage.getItem('arcade.deskview') === '1'"):
        assert need in redirect


def test_icons_move_and_windows_move_size_and_minimize():
    desk = (TEMPLATES / "desktop.html").read_text()
    for need in ("layout.at[p.b.dataset.key] = [", "'n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'",
                 "function minimize(w)", "function toggleMax(w)", ".dk.moving iframe { pointer-events: none; }",
                 "tell(w, 'minimized')", "tell(w, 'closing')"):
        assert need in desk, need
    # A wallpaper that is not a picture runs sandboxed and never takes a click.
    assert "f.setAttribute('sandbox', 'allow-scripts')" in desk and ".dk-bg { position: absolute; inset: 0; pointer-events: none;" in desk
