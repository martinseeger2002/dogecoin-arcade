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
    coins = json.loads(re.search(r"var COINS = (\[.*?\]);\n", page.text).group(1))
    assert coins and all(c["net"] and c["label"] for c in coins), "a wallet shortcut for every coin"
    system = json.loads(re.search(r"var SYSTEM = (\{.*?\});\n", page.text).group(1))
    assert {"version", "node", "chains"} <= set(system)
    one = [a for a in apps if a["name"] == "Call Me"][0]
    assert one["key"] == gameslib.app_key(MAKER, "Call Me") and one["open"] == f"/inscriptions/{one['page']}/full"
    assert 'id="deskview"' not in page.text and 'id="dk-toggle"' not in page.text, "the switch is Themes on Your arcade"


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


def test_themes_on_your_arcade_turn_the_desktop_on_and_pick_its_look():
    """2026-10-28: "put the toggle in 'your arcade' section near light or dark - and we can call it themes - lets try an
    ubuntu style one to add to the category"."""
    me = (TEMPLATES / "me.html").read_text()
    assert me.index('id="theme-pref"') < me.index('id="desk-theme-pref"'), "beside Light or dark"
    for want in ('data-desk-set=""', 'data-desk-set="w98"', 'data-desk-set="ubuntu"', 'data-desk-set="dogefeedx"'):
        assert want in me
    assert "Math.max(screen.width, window.innerWidth) <= 640) return;" in me, "not on a phone"
    assert "classList.contains('indesk')" in me, "a desktop window is a computer, even when the window is narrow"
    base = (TEMPLATES / "base.html").read_text()
    assert 'id="deskview"' not in base, "no switch in the website's header any more"
    # Pages opened in a tab of their own go to the desktop while a desktop look is on: never Admin, sign-in, a phone,
    # the installed app or a page already in one of its windows.
    redirect = base[base.index("THE DESKTOP VIEW (desktop.html"):]
    redirect = redirect[:redirect.index("if (!installed || window.top !== window")]
    for need in ("!installed", "window.top === window", "window.innerWidth > 640", "admin|join|restore|signin",
                 "/^(1|w98|ubuntu|dogefeedx)$/.test(localStorage.getItem('arcade.deskview') || '{{ desk_default }}')"):
        assert need in redirect
    assert "look.href = skin === 'ubuntu' ? '/deskubuntu.css' : skin === 'dogefeedx' ? '/deskdogefeedx.css' : '/desk98.css';" in base


def test_the_ubuntu_look_has_its_own_shell_and_its_own_window_style(client):
    app, _state = client
    desk = (TEMPLATES / "desktop.html").read_text()
    for need in ("'{{ desk_default }}' === 'w98' ? 'w98' :", "function paintDock()",
                 "function toggleApps(", "$('dk-sys').addEventListener('click'", "[data-skin=\"ubuntu\"] .dk-dock",
                 "setSkin('w98')", "setSkin('ubuntu')", "function openCascade(", "function themeChoices(",
                 "title: 'Switch Theme'"):
        assert need in desk, need
    css = app.get("/deskubuntu.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css") and "#e95420" in css.text
    assert door.public_path("/deskubuntu.css")


def test_the_dogefeedx_look_has_macos_style_and_five_app_windows(client):
    """2026-11-02: DogeFeedX theme with macOS-style dock and five app windows."""
    app, _state = client
    desk = (TEMPLATES / "desktop.html").read_text()
    for need in ("DFX = SKIN === 'dogefeedx'", "MAC_LIKE = UB || DFX",
                 "setSkin('dogefeedx')", "[data-skin=\"dogefeedx\"]",
                 "byKey.dfxprofile", "byKey.dfxmarket", "byKey.dfxterminal",
                 "byKey.dfxnetwork", "byKey.dfxexplorer",
                 "function openDfxProfile()", "function openDfxMarket()",
                 "function openDfxTerminal()", "function openDfxNetwork()", "function openDfxExplorer()",
                 "function magnifyDock(", "function dockCount(", "dfx-badge", "flex-wrap: nowrap",
                 "dfxhome", "dfxfiles", "dfxnotifications", "dfxmessages", "dfxinscribe",
                 "dfxlaunchpad", "dfxnfts", "dfxarcade", "dfxbookmarks", "dfxmemes",
                 "dfxleaderboard", "dfxsettings"):
        assert need in desk, need
    css = app.get("/deskdogefeedx.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert "#d9a520" in desk, "DogeFeed gold color (the desktop's own colours; the window CSS uses its paler golds)"
    assert "#fff6dd" in css.text, "the windows' pale gold"
    assert door.public_path("/deskdogefeedx.css")


def test_icons_move_and_windows_move_size_and_minimize():
    desk = (TEMPLATES / "desktop.html").read_text()
    for need in ("layout.at[p.b.dataset.key] = [", "'n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'",
                 "function minimize(w)", "function toggleMax(w)", ".dk.moving iframe { pointer-events: none; }",
                 "tell(w, 'minimized')", "tell(w, 'closing')"):
        assert need in desk, need
    # A wallpaper that is not a picture runs sandboxed and never takes a click.
    assert "f.setAttribute('sandbox', 'allow-scripts')" in desk and ".dk-bg { position: absolute; inset: 0; pointer-events: none;" in desk


def test_admin_is_on_the_desktop_and_every_page_in_a_window_wears_the_old_look(client):
    """2026-10-28: "admin should be in it as well - also styled windows 98 from this view"; "all apps internally
    should be styled windows 98 as well"."""
    app, _state = client
    desk = (TEMPLATES / "desktop.html").read_text()
    assert "href != '/apps' %}" in desk and "'/admin')" not in desk, "Admin's tab is not left off the desktop"
    css = app.get("/desk98.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert "html.indesk" in css.text and door.public_path("/desk98.css")
    base = (TEMPLATES / "base.html").read_text()
    assert "'/deskubuntu.css' : skin === 'dogefeedx' ? '/deskdogefeedx.css' : '/desk98.css'" in base and base.index("look.href") > base.index("if (desk) {")


def test_a_coin_shortcut_opens_the_wallet_on_that_coin_and_any_icon_can_go():
    wallet = (TEMPLATES / "my_wallet.html").read_text()
    assert 'new URLSearchParams(location.search).get("coin")' in wallet
    desk = (TEMPLATES / "desktop.html").read_text()
    assert "href: WALLET + '?coin=' + encodeURIComponent(c.net)" in desk
    assert "if (e.key === 'Delete') removeIcon(b.dataset.key);" in desk, "any icon, pages too"
    for need in ("function openPrograms()", "function openShortcuts()", "function openSysinfo()", "function openRun()",
                 "function logOff()", "'/explore/api/collections?net='", "layout.programs"):
        assert need in desk, need


def test_programs_start_with_the_four_that_come_with_it():
    """2026-10-28: "default programs shoulld be that come with it are: browser, video call, and Arcade Spaces and tic tac
    toe": pinned by publisher and name (gameslib.app_key), never by a name another app could copy."""
    desk = (TEMPLATES / "desktop.html").read_text()
    want = [gameslib.app_key("nWKJgSeAZSmfRCxZnwVgtYyPNowQT5X1Gk", "Browser"),
            gameslib.app_key("neKR5zDMgrAmSW94rMLYHRBUqzPtuXQ7gp", "Video Call"),
            gameslib.app_key("nWKJgSeAZSmfRCxZnwVgtYyPNowQT5X1Gk", "Arcade Spaces"),
            gameslib.app_key("nWKJgSeAZSmfRCxZnwVgtYyPNowQT5X1Gk", "Tic-Tac-Toe")]
    line = re.search(r"var DEFAULT_PROGRAMS = (\[.*?\]);", desk, re.S).group(1)
    assert json.loads(line.replace("'", '"')) == want


def test_picking_the_other_desktop_from_inside_one_loads_it():
    """Themes in a desktop window: /desktop#open=/me to /desktop#open=/me changes only the hash, which never loads the
    page, so the look stayed the old one until a reload (found filming the promo, 2026-10-29)."""
    me = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates/me.html").read_text()
    assert "if (want) shell.location.reload();" in me


def test_the_app_store_is_the_home_screens_store_in_a_window():
    """2026-10-30, the operator: "You know how the App Store works in the PWA that's how the App Store should work in the
    desktop modes": search, kinds, cards, + Add with the sheet of what the app asks for (the card's own switches), and
    an added app goes to Start -> Programs and the desktop. Driven in a real browser in both looks while it was built."""
    desk = (TEMPLATES / "desktop.html").read_text()
    assert "if (it.kind === 'store') { openStore(); return; }" in desk, "the App Store icon opens the store, not /apps"
    for need in ("function openStore()", "class=\"w98-field st-q\"", "ST_GAMES", "data-add=", "data-open=",
                 "arcadeAppPermSet(p, permIds(a), !!grants[p])", "setProgram(a.key, true);", "putOnDesk(a.key);",
                 "anybody can publish a copy"):
        assert need in desk, need
    home = (TEMPLATES / "home.html").read_text()
    for perm in ("wallet: ['Wallet access'", "contacts: ['Contacts'", "media: ['Camera & microphone'", "notify: ['Notifications'"):
        assert perm in home and perm in desk, f"the desktop asks for {perm} as the home screen does"


def test_game_access_lists_every_permission_an_app_can_hold():
    """2026-11-01: Group chats and Notifications were missing, so the list was not the whole of what an app may do."""
    access = (TEMPLATES / "my_access.html").read_text()
    for need in ("arcade.wallet.allow:", "arcade.media.allow:", "arcade.contacts.allow:", "arcade.groups.allow:", "/account/ring/apps"):
        assert need in access, need
