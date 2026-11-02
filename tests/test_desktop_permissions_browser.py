"""An app's permissions after it was added, and taking it off the arcade, in both looks of the Desktop View (2026-11-01,
The operator: "once the app is installed, you can no longer pick the permission permissions you have to delete the app and then
reinstall it"; "a remove from arcade option that removes the app from both the start menu and the desktop and the dock")."""
import json
import os
import pathlib
import re
import sys

import pytest

pytest.importorskip("playwright.sync_api", reason="needs playwright")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_claim_browser import _free_port                          # noqa: E402
from test_realtime_rejoin_browser import Server                    # noqa: E402
from test_realtime_web import make_state                           # noqa: E402

from arcade.web.app import create_app                              # noqa: E402

pytestmark = pytest.mark.desk_default

PAGE, CARD = "a" * 64, "b" * 64
KEY = "app:nTest:riffs"
APP = {"key": KEY, "name": "Riffs", "page": PAGE, "card": CARD, "open": f"/inscriptions/{PAGE}/full", "cover": None, "genre": "social",
       "mp": True, "tdeck": False, "desc": "Channels", "by": "@strawberry", "address": "nTest", "version": "1.0", "players": "2-20",
       "likes": 0, "comments": 0, "perms": ["wallet", "groups", "media"], "timeout": None}
SHOTS = os.environ.get("DESK_SHOTS")


@pytest.fixture
def base(tmp_path):
    port = _free_port()
    server = Server(create_app(make_state(tmp_path, "a")), port)
    yield f"http://127.0.0.1:{port}"
    server.stop()


@pytest.fixture(params=["w98", "ubuntu"])
def page(request, base):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not os.environ.get("DISPLAY"))
        ctx = browser.new_context(viewport={"width": 1280, "height": 760})
        ctx.add_init_script(f"localStorage.setItem('arcade.deskview', {json.dumps(request.param)})")
        pg = ctx.new_page()
        pg.look = request.param
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(e.message))

        def shelf(route):
            got = route.fetch()
            body = re.sub(r"var APPS = \[.*?\];\n", lambda _: f"var APPS = {json.dumps([APP])};\n", got.text(), count=1, flags=re.S)
            route.fulfill(response=got, body=body)
        pg.route(re.compile(r"/desktop$"), shelf)
        pg.route(re.compile(r"/inscriptions/.*"), lambda r: r.fulfill(status=200, content_type="text/html", body="<p>app</p>"))
        pg.goto(base + "/desktop")
        pg.wait_for_selector("#dk-start, #dk-dock", state="attached")
        pg.wait_for_function("window.arcadeShell !== undefined || document.querySelector('.dk-icon')")
        yield pg
        browser.close()


def allowed(pg, kind, id_):
    return pg.evaluate("k => localStorage.getItem(k)", f"arcade.{kind}.allow:{id_}") == "yes"


def shot(pg, name):
    if SHOTS:
        pg.screenshot(path=f"{SHOTS}/{pg.look}-{name}.png")


def add(pg, untick=(), keep=False):
    pg.locator(".dk-icon", has_text="App Store").first.dblclick()
    pg.wait_for_selector(".st-card")
    pg.locator(f'[data-add="{KEY}"]').click()
    for p in untick:
        pg.locator(f'.st-perms [data-perm="{p}"]').uncheck()
    pg.locator(".st-go").click()
    pg.wait_for_selector(f'[data-perms="{KEY}"]')
    if not keep:
        pg.locator('.dk-win:has(.st-card) [data-a="close"]').click()
        pg.wait_for_selector(".st-card", state="detached")


def icon(pg):
    return pg.locator(".dk-icon", has_text="Riffs")


def menu(pg, label):
    icon(pg).first.click(button="right")
    pg.locator("#dk-ctx .dk-mi", has_text=label).click()


def label(pg):
    return "Properties" if pg.look == "w98" else "Permissions"


def box(pg, p):
    return pg.locator(f'.pm-list [data-perm="{p}"]')


def test_a_permission_can_be_changed_after_the_app_is_added(page):
    add(page)
    assert allowed(page, "wallet", PAGE) and allowed(page, "media", PAGE)
    icon(page).first.click(button="right")
    shot(page, "menu")
    page.locator("#dk-ctx .dk-mi", has_text=label(page)).click()
    page.wait_for_selector(".pm-list .st-perm")
    assert page.locator(".pm-list .st-perm").count() == 3, "what the card declares"
    shot(page, "window")
    box(page, "media").uncheck()
    if page.look == "w98":
        assert allowed(page, "media", PAGE), "Windows 98 waits for Apply"
        page.locator(".pm-apply").click()
    page.wait_for_function("k => localStorage.getItem(k) === null", arg=f"arcade.media.allow:{PAGE}")
    assert allowed(page, "wallet", PAGE)
    box(page, "media").check()
    if page.look == "w98":
        page.locator(".pm-ok").click()
    page.wait_for_function("k => localStorage.getItem(k) === 'yes'", arg=f"arcade.media.allow:{PAGE}")
    assert not page.errors


def test_cancel_changes_nothing_on_windows_98(page):
    if page.look != "w98":
        pytest.skip("Ubuntu saves at once")
    add(page)
    menu(page, "Properties")
    box(page, "wallet").uncheck()
    page.locator(".pm-no").click()
    assert allowed(page, "wallet", PAGE)
    page.locator(".pm").wait_for(state="detached")


def test_off_clears_the_card_too_and_what_the_card_does_not_declare_shows(page):
    add(page)
    page.evaluate("""([card, page_]) => { localStorage.setItem('arcade.wallet.allow:' + card, 'yes');
                                          localStorage.setItem('arcade.contacts.allow:' + page_, 'yes'); }""", [CARD, PAGE])
    menu(page, label(page))
    page.wait_for_selector(".pm-list .st-perm")
    assert box(page, "contacts").is_checked(), "given while it ran, not on its card"
    page.locator(".pm-all").click()
    if page.look == "w98":
        page.locator(".pm-ok").click()
    page.wait_for_function("k => localStorage.getItem(k) === null", arg=f"arcade.wallet.allow:{CARD}")
    for kind in ("wallet", "media", "groups", "contacts"):
        assert not allowed(page, kind, PAGE), kind
    assert not allowed(page, "wallet", CARD)
    assert not page.errors


def test_the_app_stores_card_opens_the_same_window(page):
    add(page, keep=True)
    page.locator(f'[data-perms="{KEY}"]').click()
    page.wait_for_selector(".pm-list .st-perm")
    assert not page.errors


def test_adding_again_with_a_box_off_takes_it_back(page):
    """Reinstalling only ever added: a box left off left what was already on."""
    add(page)
    assert allowed(page, "groups", PAGE)
    menu(page, "Remove from Start menu" if page.look == "w98" else "Remove from your apps")
    menu(page, "Remove from desktop")
    icon(page).first.wait_for(state="detached")
    add(page, untick=["groups"])
    assert not allowed(page, "groups", PAGE) and allowed(page, "wallet", PAGE)


def test_remove_from_arcade_takes_it_off_everything_and_it_can_come_back(page):
    add(page)
    icon(page).first.dblclick()
    page.wait_for_selector(".dk-win iframe")
    page.locator('.dk-win:has(iframe) [data-a="min"]').click()
    icon(page).first.click(button="right")
    shot(page, "remove")
    page.locator("#dk-ctx .dk-mi", has_text="Remove from arcade").click()
    page.wait_for_selector(".askcard")
    shot(page, "ask")
    colors = page.evaluate("""() => { const c = getComputedStyle(document.querySelector('.askcard')),
                                            b = getComputedStyle(document.querySelector('.askcard button'));
                                      return [c.backgroundColor, b.backgroundColor, b.color]; }""")
    assert colors[0] in ("rgb(192, 192, 192)", "rgb(255, 255, 255)"), colors
    assert colors[1] != "rgba(0, 0, 0, 0)" and colors[1] != colors[2], colors
    page.locator(".askcard button", has_text="Remove").click()
    icon(page).first.wait_for(state="detached")
    assert not page.locator(".dk-win:visible iframe").count(), "it closes if it is open"
    if page.look == "ubuntu":
        assert not page.locator(f'#dk-dock [data-key="{KEY}"]').count()
    assert not allowed(page, "wallet", PAGE) and not allowed(page, "media", PAGE) and not allowed(page, "groups", PAGE)
    add(page, untick=["media"])
    icon(page).first.wait_for()
    assert allowed(page, "wallet", PAGE) and not allowed(page, "media", PAGE)
    assert not page.errors
