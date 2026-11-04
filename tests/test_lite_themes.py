"""Themes in ARCADE Lite: the commands that install, try, set, allow and pick them, and /lite/theme.css, which each
caller sees -- a visitor, a user, the operator acting as one, and the node's own view (arcade/packages.py)."""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state  # noqa: F401,E402
from test_auth_web import _sign_in  # noqa: E402

from arcade import packages  # noqa: E402
from arcade.lite import commands as cmdlib  # noqa: E402
from arcade.web.app import create_app  # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}
OUTSIDE = {"host": "arcade.example.com", "cf-connecting-ip": "198.51.100.9"}


def _edit(folder: Path, change) -> Path:
    manifest = json.loads((folder / "manifest.json").read_text())
    change(manifest)
    (folder / "manifest.json").write_text(json.dumps(manifest))
    return folder


@pytest.fixture
def app(app_state):
    app_state.set_setting("public_hosts", ["arcade.example.com"])
    yield TestClient(create_app(app_state))
    app_state.set_setting("public_hosts", [])


def _op(state, local=True):
    return cmdlib.Caller(state, "operator", local=local)


def test_installed_chosen_allowed_and_picked_from_three_sides(app, app_state, tmp_path):
    """The operator installs and sets the arcade's theme; a visitor wears it; a user picks an allowed one; the operator
    acting as a user is a user like any other; and the node's own view may wear any installed one."""
    sunset = packages.template(tmp_path, "sunset")
    night = _edit(packages.template(tmp_path, "night"), lambda m: m["look"].update(radius=0))
    with pytest.raises(cmdlib.CommandError, match="arcade's own machine"):
        cmdlib.run_line(_op(app_state, local=False), f"installtheme {sunset}")
    sun = cmdlib.run_line(_op(app_state), f"installtheme {sunset}")["id"]
    moon = cmdlib.run_line(_op(app_state), f"installtheme {night}")["id"]
    assert (app_state.home / "packages" / sun / "manifest.json").is_file()
    assert app.get("/lite/theme.css", headers=OUTSIDE).text == "", "installed is not worn"

    cmdlib.run_line(_op(app_state), "settheme sunset")
    assert "--radius:3px" in app.get("/lite/theme.css", headers=OUTSIDE).text, "a visitor wears the arcade's"
    assert cmdlib.run_line(_op(app_state), "listthemes")["yours"] is None, "the arcade's is nobody's own pick"

    user = SigningKey.generate()
    pubkey = user.verify_key.encode().hex()
    as_user = cmdlib.Caller(app_state, "user", pubkey)
    with pytest.raises(cmdlib.CommandError, match="has not let people pick"):
        cmdlib.run_line(as_user, "usetheme night")
    cmdlib.run_line(_op(app_state), "allowtheme night")
    assert [t["name"] for t in cmdlib.run_line(as_user, "listthemes")["pick"]] == ["night"]
    assert cmdlib.run_line(as_user, "usetheme night") == "you see night"
    assert _sign_in(app, user, headers=OUTSIDE).status_code == 200
    assert "--radius:0px" in app.get("/lite/theme.css", headers=OUTSIDE).text, "the user's own pick"
    assert "installed" not in cmdlib.run_line(as_user, "listthemes"), "the operator's list is the operator's"

    cmdlib.run_line(_op(app_state), "allowtheme night off")
    assert "--radius:3px" in app.get("/lite/theme.css", headers=OUTSIDE).text, "no longer allowed: the arcade's"
    assert packages.chosen(app_state.settings(), "node") == sun
    app_state.set_setting(f"{packages.THEME}:node", moon)
    assert packages.chosen(app_state.settings(), "node") == moon, "the node's own view wears any installed one"

    cmdlib.run_line(_op(app_state), "removetheme sunset")
    assert not (app_state.home / "packages" / sun).exists() and app_state.setting(packages.THEME) is None
    assert app.get("/lite/theme.css", headers=OUTSIDE).text == ""


def test_a_tried_folder_shows_at_the_machine_only_and_says_what_is_wrong(app, app_state, tmp_path):
    folder = _edit(packages.template(tmp_path, "draft"), lambda m: m["look"].update(radius=7))
    cmdlib.run_line(_op(app_state), f"trytheme {folder}")
    here = app.get("/lite/theme.css", headers=LOCAL).text
    assert "--radius:7px" in here, here
    assert app.get("/lite/theme.css", headers=OUTSIDE).text == "", "nobody else sees a theme being tried"
    _edit(folder, lambda m: m["look"]["light"].update(text="#eeeeee"))
    assert "trytheme: look.light: text on panel is too hard to read" in app.get("/lite/theme.css", headers=LOCAL).text
    cmdlib.run_line(_op(app_state), "trytheme off")
    assert app.get("/lite/theme.css", headers=LOCAL).text == ""


def test_a_themes_font_is_served_only_from_an_installed_theme(app, app_state, tmp_path):
    folder = _edit(packages.template(tmp_path, "sunset"), lambda m: m["look"].update(font="files/sun.woff2"))
    (folder / "files" / "sun.woff2").write_bytes(b"wOF2font")
    pid = cmdlib.run_line(_op(app_state), f"installtheme {folder}")["id"]
    got = app.get(f"/lite/packages/{pid}/files/sun.woff2", headers=OUTSIDE)
    assert got.status_code == 200 and got.content == b"wOF2font" and got.headers["content-type"] == "font/woff2"
    assert app.get(f"/lite/packages/{pid}/files/../manifest.json", headers=OUTSIDE).status_code == 404
    assert app.get(f"/lite/packages/{'0' * 64}/files/sun.woff2", headers=OUTSIDE).status_code == 404
    assert app.get("/lite/packages/try/files/sun.woff2", headers=OUTSIDE).status_code == 404


def test_a_theme_changed_on_disk_is_not_worn(app, app_state, tmp_path):
    pid = cmdlib.run_line(_op(app_state), f"installtheme {packages.template(tmp_path, 'sunset')}")["id"]
    cmdlib.run_line(_op(app_state), "settheme sunset")
    _edit(app_state.home / "packages" / pid, lambda m: m["look"].update(radius=9))
    assert app.get("/lite/theme.css", headers=OUTSIDE).text.startswith("/* theme: the theme kept as")


def test_the_window_and_the_full_pages_in_lite_link_the_theme(app, app_state):
    assert '<link rel="stylesheet" href="/lite/theme.css">' in app.get("/lite", headers=OUTSIDE).text
    assert "/lite/theme.css" not in app.get("/docs", headers=LOCAL).text, "the full edition is unchanged"
    app_state.set_setting("edition", "lite")
    try:
        assert '<link rel="stylesheet" href="/lite/theme.css">' in app.get("/docs", headers=LOCAL).text
    finally:
        app_state.set_setting("edition", None)
