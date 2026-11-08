"""ArcadeCore's Account tab holds the account's page, never another window (2026-11-08, SadFrog: "clicking the account
tab made a recursive call it seemed"). The server serves the window at /me unless the request is a frame's, and the
service worker's fetch for a navigation (sw.js) arrives with Sec-Fetch-Dest "empty": with it installed, the tab held
the window, which opened its own Account tab, and so on. In a real browser, with the service worker controlling."""

import pathlib
import sys

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                  # noqa: E402
from test_digidollar_wallet_browser import _serve, _sign_up, _wait  # noqa: E402

from fastapi.testclient import TestClient                        # noqa: E402


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    driver.set_window_size(1100, 900)
    yield driver
    driver.quit()


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    base, state, server, thread = _serve(tmp_path_factory.mktemp("accountframe"), False)
    state.set_setting("look", "core")
    yield base, state
    server.should_exit = True
    thread.join(timeout=5)


def test_a_frame_that_says_so_gets_the_page_not_the_window(served):
    from arcade.web.app import create_app
    base, state = served
    app = TestClient(create_app(state))
    assert 'id="tab-more"' in app.get("/join").text, "a tab's own visit: the window"
    assert 'id="tab-more"' not in app.get("/join?framed=1").text, "a frame: the page"
    assert 'id="tab-more"' not in app.get("/join", headers={"sec-fetch-dest": "iframe"}).text


def test_the_account_tab_holds_the_account_page_with_the_service_worker_on(browser, served):
    base, state = served
    _sign_up(browser, base, "framed")
    browser.get(f"{base}/lite")
    browser.execute_script("localStorage.setItem('arcade.deskview', 'core')")
    # the service worker installed and controlling this page, as on any arcade visited before
    browser.execute_async_script("""const done = arguments[0];
        navigator.serviceWorker.register('/sw.js', {scope: '/'}).then(() => navigator.serviceWorker.ready).then(() => done(1), () => done(0));""")
    browser.get(f"{base}/lite")
    assert browser.execute_script("return !!navigator.serviceWorker.controller"), "the service worker answers here"
    _wait(browser, "return document.querySelector('.toolbar [data-tab=\"account\"]')")
    browser.execute_script("document.querySelector('.toolbar [data-tab=\"account\"]').click()")
    _wait(browser, """const f = document.querySelector('section[data-panel="account"] iframe');
        const d = f && f.contentDocument;
        return d && d.readyState === 'complete' && d.location.pathname === '/me' && !!d.getElementById('hello')""")
    inner = browser.execute_script("""const d = document.querySelector('section[data-panel="account"] iframe').contentDocument;
        return {window: !!d.querySelector('.toolbar'), frames: d.querySelectorAll('iframe').length}""")
    assert not inner["window"], "the account's page, not another window"
