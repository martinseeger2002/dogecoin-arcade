"""The Desktop View in a browser (2026-10-29): a computer opens the website in the Ubuntu desktop until its reader picks
another look ("The website should by default open to Ubuntu view"), and the helper is a window like the rest ("the AI
robot helper opened in the desktop OS UI is not able to be moved around the desktop like the rest of the apps")."""
import pathlib
import sys

import pytest

pytest.importorskip("playwright.sync_api", reason="needs playwright")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_claim_browser import _free_port                          # noqa: E402
from test_realtime_rejoin_browser import Server                    # noqa: E402
from test_realtime_web import make_state                           # noqa: E402

from arcade.web.app import create_app                              # noqa: E402

pytestmark = pytest.mark.desk_default


@pytest.fixture
def base(tmp_path):
    port = _free_port()
    server = Server(create_app(make_state(tmp_path, "a")), port)
    yield f"http://127.0.0.1:{port}"
    server.stop()


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1400, "height": 900})
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(e.message))
        yield pg
        browser.close()


def test_a_computer_that_never_picked_a_look_opens_in_the_ubuntu_desktop(base, page):
    page.goto(base + "/exchange")
    page.wait_for_url(base + "/desktop")
    assert page.evaluate("document.documentElement.getAttribute('data-skin')") == "ubuntu"
    page.wait_for_function("document.querySelector('.dk-win iframe') && "
                           "document.querySelector('.dk-win iframe').contentWindow.location.pathname === '/exchange'")
    assert page.evaluate("localStorage.getItem('arcade.deskview')") == "ubuntu", "kept as if picked"
    assert page.errors == []


def test_the_website_once_picked_stays_the_website(base, page):
    page.goto(base + "/desktop")
    page.evaluate("localStorage.setItem('arcade.deskview', 'web')")
    page.goto(base + "/exchange")
    page.wait_for_timeout(500)
    assert page.url == base + "/exchange"


def test_windows_98_once_picked_stays_windows_98(base, page):
    page.goto(base + "/desktop")
    page.evaluate("localStorage.setItem('arcade.deskview', 'w98')")
    page.goto(base + "/feed")
    page.wait_for_url(base + "/desktop")
    assert page.evaluate("document.documentElement.getAttribute('data-skin')") == "w98"


def test_a_phone_keeps_the_website(base, page):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base + "/exchange")
    page.wait_for_timeout(500)
    assert page.url == base + "/exchange"


def test_the_helper_is_a_window_that_moves_sizes_and_closes(base, page):
    page.route("**/account/llm/helper", lambda r: r.fulfill(json={"available": True, "model": "test-model"}))
    page.goto(base + "/desktop")
    page.wait_for_function("document.getElementById('arc-bot') && document.getElementById('arc-bot').style.display === 'flex'")
    page.dblclick('.dk-icon[data-key="helper"]')
    page.wait_for_selector('.dk-win[aria-label="Helper"]')
    win = '.dk-win[aria-label="Helper"]'
    assert page.evaluate(f"!!document.querySelector('{win} .dk-body > #arc-bot-box')"), "the chat is inside the window"
    assert page.evaluate(f"document.querySelectorAll('{win} .dk-rz').length") == 8, "it sizes like the rest"
    assert page.evaluate(f"!!document.querySelector('{win} [data-a=min]')"), "and minimizes"
    before = page.eval_on_selector(win, "e => [e.offsetLeft, e.offsetTop]")
    title = page.locator(f"{win} .dk-title")
    box = title.bounding_box()
    page.mouse.move(box["x"] + 60, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + 260, box["y"] + 150, steps=8)
    page.mouse.up()
    after = page.eval_on_selector(win, "e => [e.offsetLeft, e.offsetTop]")
    assert after[0] - before[0] > 150 and after[1] - before[1] > 100, (before, after)
    page.click(f'{win} [data-a="close"]')
    page.wait_for_function(f"!document.querySelector('{win}')")
    assert page.evaluate("document.getElementById('arc-bot-box').parentElement === document.body")
    assert page.evaluate("document.getElementById('arc-bot-box').style.display") == "none"
    page.dblclick('.dk-icon[data-key="helper"]')
    page.wait_for_selector(win, state="visible")
    assert page.errors == []


@pytest.mark.parametrize("skin", ["ubuntu", "w98"])
def test_a_window_refreshes_on_its_own(base, page, skin):
    """2026-11-02: "if you make a post you'll have to normally refresh whole arcade - we should just be able to refresh
    every window". The button, the title bar's menu and F5 inside a window reload that window's page, nothing else."""
    page.goto(base + "/desktop")
    page.evaluate(f"localStorage.setItem('arcade.deskview', '{skin}')")
    page.goto(base + "/exchange")
    page.wait_for_url(base + "/desktop")
    page.wait_for_function("document.querySelector('.dk-win iframe') && "
                           "document.querySelector('.dk-win iframe').contentDocument.readyState === 'complete'")
    page.evaluate("window.deskMark = 1")
    win = page.locator(".dk-win").first
    frame = "document.querySelector('.dk-win iframe').contentWindow"

    def mark():
        page.evaluate(f"{frame}.refreshMark = 1")

    def reloaded():
        page.wait_for_function(f"{frame}.refreshMark === undefined && {frame}.document.readyState === 'complete'")
        assert page.evaluate("window.deskMark") == 1, "the desktop itself did not reload"

    mark()
    win.locator('.dk-tbtn[data-a="reload"]').click()
    reloaded()

    page.wait_for_timeout(300)
    mark()
    win.locator(".dk-tname").click(button="right")
    page.locator('#dk-ctx [data-k="reload"]').click()
    reloaded()

    page.wait_for_timeout(300)
    mark()
    page.evaluate(f"{frame}.focus()")                               # as after a click inside the window
    page.keyboard.press("F5")
    reloaded()
    assert page.errors == []
