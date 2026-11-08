"""What the ArcadeCore window does, reachable from the website's look too, by the window's own code (2026-11-08, SadFrog:
"there shouldnt be a differentiation between wallets now it should all be the same thing - when you change the theme,
thats it everything is styled differently but the availability and capability doesnt change", and "if things are
getting chosen chose the lites version of things"): API keys under the account (/me/keys), the theme and the tabs
(/look), the console (/console) and Build (/build) as website pages drawn from the window's partials; the theme worn by
every website page; Your tabs honoured by the website's bar; and the website's pages reachable from the window (Go).
The interactive parts are in test_core_in_every_look_browser.py."""
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state  # noqa: F401,E402
from test_auth_web import _sign_in  # noqa: E402

from arcade import packages  # noqa: E402
from arcade import modules as modulelib  # noqa: E402
from arcade.lite import commands as cmdlib  # noqa: E402
from arcade.web import door as doorlib  # noqa: E402
from arcade.web.app import create_app  # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}
OUTSIDE = {"host": "arcade.example.com", "cf-connecting-ip": "198.51.100.9"}
WINDOW = Path(__file__).resolve().parent.parent / "arcade" / "lite" / "templates"


@pytest.fixture
def app(app_state):
    app_state.set_setting("public_hosts", ["arcade.example.com"])
    yield TestClient(create_app(app_state))
    app_state.set_setting("public_hosts", [])
    app_state.set_setting(modulelib.TABS_SETTING, None)


def _nav(page: str) -> list[str]:
    bar = page.split("<header>", 1)[1].split("</header>", 1)[0]
    return re.findall(r'<a class="[^"]*"(?: data-module="[^"]*")? href="([^"]+)"', bar)


def test_the_pages_are_every_callers_and_claimed_and_open_at_the_door(app):
    for path in ("/console", "/build", "/look", "/me/keys"):
        assert modulelib.module_of(path) == "node", f"{path} is the core's"
        assert doorlib.public_path(path), f"{path} is reached from outside, by an account"
    for path, mark in (("/console", 'id="line"'), ("/build", 'id="build"'), ("/look", 'id="yourtabs"')):
        page = app.get(path, headers=OUTSIDE)
        assert page.status_code == 200 and mark in page.text, path
    assert "/join" in str(app.get("/me/keys", headers=OUTSIDE, follow_redirects=False).headers.get("location")), \
        "a visitor is asked to sign in for keys"


def test_api_keys_under_the_account_are_builds_own(app):
    """The same markup and code as Window -> Build -> API keys (_build.html), opened on its keys, over the same routes."""
    assert _sign_in(app, SigningKey.generate(), headers=OUTSIDE).status_code == 200
    page = app.get("/me/keys", headers=OUTSIDE).text
    assert 'id="k-list"' in page and 'id="k-make"' in page
    assert "var KEYS = OPERATOR ? '/admin/api/keys' : '/account/keys'" in page, "the window's own code"
    assert 'var h = location.hash.slice(1) || "keys"' in page, "opened on API keys"
    assert 'id="k-users"' not in page, "the say over users' keys is the operator's"
    assert app.get("/account/keys", headers=OUTSIDE).status_code == 200
    assert 'href="/me/keys"' in app.get("/me", headers=OUTSIDE).text, "linked from the account's own page"


def test_the_website_wears_the_callers_theme(app, app_state, tmp_path):
    """A theme is the window's values, which name the website's own variables too (packages.COLOURS): every website
    page carries the caller's theme in itself, empty while they wear the arcade's own look."""
    for path in ("/docs", "/console", "/look"):
        assert '<style id="arcade-theme"></style>' in app.get(path, headers=OUTSIDE).text, path
    assert app.get("/lite/theme.css", headers=OUTSIDE).text == ""
    op = cmdlib.Caller(app_state, "operator", local=True)
    folder = packages.template(tmp_path, "sunset")
    cmdlib.run_line(op, f"installtheme {folder}")
    cmdlib.run_line(op, "settheme sunset")
    try:
        css = app.get("/lite/theme.css", headers=OUTSIDE).text
        assert "html:root" in css and "--bg:" in css and "--fg:" in css and "--accent:" in css, \
            "the website's own colours are the theme's"
        assert f'<style id="arcade-theme">{css}</style>' in app.get("/docs", headers=OUTSIDE).text
    finally:
        cmdlib.run_line(op, "removetheme sunset")


def test_look_is_the_windows_theme_and_tabs_and_the_operators_themes(app):
    visitor = app.get("/look", headers=OUTSIDE).text
    assert 'id="theme"' in visitor and 'id="yourtabs"' in visitor and 'id="o-themes"' not in visitor
    assert "localStorage.getItem(LOCAL)" in visitor, "a visitor's tabs stay in their browser"
    assert visitor.count("pick();") >= 2, "drawn at once on the page"
    assert _sign_in(app, SigningKey.generate(), headers=OUTSIDE).status_code == 200
    user = app.get("/look", headers=OUTSIDE).text
    assert 'id="t-pick"' in user and 'id="o-themes"' not in user
    operator = app.get("/look", headers=LOCAL).text
    if 'id="o-themes"' in operator:                    # the operator's machine (the test client may be "outside")
        assert operator.index('id="o-themes"') < operator.index("$('o-tcheck').onclick"), \
            "the operator's themes are in the page before the code that finds them"


def test_your_tabs_are_honoured_by_the_websites_bar(app, app_state):
    """One choice for every look: a module an account hid under Your tabs is gone from its ArcadeCore tabs and from
    the website's bar; a visitor's choice is kept in their browser, and the bar marks each module's tab to hide it."""
    visitor = app.get("/docs", headers=OUTSIDE).text
    assert 'data-module="feed" href="/feed"' in visitor, "a module's tab says which module it is"
    assert "lite_tabs_hidden" in visitor, "and a visitor's own choice hides it"
    assert _sign_in(app, SigningKey.generate(), headers=OUTSIDE).status_code == 200
    assert "/feed" in _nav(app.get("/me", headers=OUTSIDE).text)
    hid = app.post("/lite/api/tabs", headers={**OUTSIDE, "x-arcade-lite": "1"}, json={"hidden": ["feed"]})
    assert hid.status_code == 200, hid.text
    assert "/feed" not in _nav(app.get("/me", headers=OUTSIDE).text), "hidden from the website's bar"
    assert 'data-module="feed"' not in app.get("/lite", headers=OUTSIDE).text, "and from the window's tabs"
    assert "lite_tabs_hidden') || '[]')" not in app.get("/me", headers=OUTSIDE).text.split("</header>")[0], \
        "an account's choice is kept with it, not in the browser"


def test_a_node_module_is_a_tab_of_the_website_as_of_the_window(app, app_state):
    pilot = Path(__file__).resolve().parent.parent / "examples" / "packages" / "digidollar"
    op = cmdlib.Caller(app_state, "operator", local=True)
    cmdlib.run_line(op, f"installmodule {pilot}")
    cmdlib.run_line(op, "install digidollar")
    assert _sign_in(app, SigningKey.generate(), headers=OUTSIDE).status_code == 200
    bar = _nav(app.get("/me", headers=OUTSIDE).text)
    assert "/module/digidollar" in bar, bar
    assert bar.index("/module/digidollar") < bar.index("/docs"), "before Docs, as the window's are after its own"
    assert 'data-module="digidollar"' in app.get("/lite", headers=OUTSIDE).text
    assert app.post("/lite/api/tabs", headers={**OUTSIDE, "x-arcade-lite": "1"},
                    json={"hidden": ["digidollar"]}).status_code == 200
    assert "/module/digidollar" not in _nav(app.get("/me", headers=OUTSIDE).text)


def test_the_console_page_runs_the_windows_console(app):
    page = app.get("/console", headers=OUTSIDE).text
    assert 'import * as modforms from "/lite/modforms.js"' in page and "modforms.startConsole(" in page
    window = app.get("/lite", headers=OUTSIDE).text
    assert "m.startConsole($('out'), line" in window, "the window's console is the same copy"
    assert "function startConsole" in (WINDOW / "modforms.js").read_text()
    said = app.post("/lite/api/console", headers={**OUTSIDE, "x-arcade-lite": "1"}, json={"line": "help"}).json()
    assert said["ok"], said


def test_the_websites_pages_are_reached_from_the_window(app):
    """Window -> Go lists the website's own pages this caller has, each opened inside the window (framed, wearing its
    look); what the window has of its own (Send to Transactions, the Address book, ...) stays the window's."""
    assert _sign_in(app, SigningKey.generate(), headers=OUTSIDE).status_code == 200
    window = app.get("/lite", headers=OUTSIDE).text
    gone = set(re.findall(r'data-go="([^"]+)"', window))
    own = {"/", "/me/messages", "/apps", "/me/wallet", "/me/contacts", "/me", "/messages", "/admin"}
    for href in _nav(app.get("/me", headers=OUTSIDE).text):
        assert href in gone or href in own or href.startswith("/#"), f"{href} is not reached from the window"
    assert '<section data-panel="page" class="frame"' in window
    assert "dialog: function (path, hash)" in window, "a framed link to /console opens the window's console"
    assert "win.dialog(u.pathname" in app.get("/docs", headers=OUTSIDE).text
