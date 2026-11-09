"""Words other people choose are shown as words (2026-11-09, security review of the pages' HTML).

An arcade's own origin holds what an unlocked wallet keeps for the session, so a page of it must never turn somebody
else's text into markup: a token's name, a peer's @tag, a post's tag, a file's name, a label an inscribed page gave
itself, a contact's name. Each test below puts `<img src=x onerror=...>` where that text goes and checks that no
element was made and nothing ran. A page that is built from what a stranger wrote -- the launchpad and mintpad
previews -- is served sandboxed instead, as it will be once it is inscribed.

What other arcades and the feed would say is said by stand-in routes put in front of the real ones (`FAKE`).
"""

import pathlib
import sys
import time
from urllib.parse import urlencode

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

from fastapi import Request                                          # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse            # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                      # noqa: E402
from test_claim_browser import _free_port                          # noqa: E402
from test_collections import hashlips                               # noqa: E402
from test_realtime_rejoin_browser import Server                     # noqa: E402
from test_realtime_web import make_state                            # noqa: E402

#: Markup that runs if it is ever parsed: it sets `window.ran`.
SNEAKY = '<img src=x onerror="window.ran=1">'

#: What the stand-in routes answer, set by each test: path -> JSON, or HTML for a page of the test's own.
FAKE: dict = {}


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    from arcade.web.app import create_app

    home = tmp_path_factory.mktemp("sinks")
    state = make_state(home, "node")
    app = create_app(state)

    def stand_in(request: Request):
        said = FAKE.get(request.url.path)
        if isinstance(said, str):
            return HTMLResponse(said)
        return JSONResponse(said if said is not None else {"detail": "not faked"}, status_code=200 if said else 404)

    # In front of the real routes, so the page asks the arcade as it always does and hears the test.
    for path in ("/dogefeedx/network/api/status", "/dogefeedx/api/sidebar", "/approvals/waiting", "/sinks-test"):
        app.add_api_route(path, stand_in, methods=["GET"])
        app.router.routes.insert(0, app.router.routes.pop())
    port = _free_port()
    server = Server(app, port)
    yield state, f"http://127.0.0.1:{port}", home
    server.stop()


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    driver.set_script_timeout(60)
    yield driver
    driver.quit()


@pytest.fixture(autouse=True)
def nothing_faked(browser):
    FAKE.clear()
    yield
    FAKE.clear()
    browser.delete_all_cookies()                    # an account signed in by one test is not the next one's reader


def _until(browser, script, what, seconds=20.0, *args):
    end = time.time() + seconds
    while time.time() < end:
        if browser.execute_script(script, *args):
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}: the page says "
                         f"{browser.execute_script('return document.body && document.body.innerText.slice(0, 400)')!r}")


def _text(browser, css):
    return browser.execute_script("const e = document.querySelector(arguments[0]); return e && e.textContent;", css)


def _nothing_ran(browser, where):
    """No element came out of SNEAKY inside `where`, and its handler never ran."""
    time.sleep(0.3)                                 # an <img src=x> fails at once; give its onerror a moment
    assert browser.execute_script("return window.ran") is None, "the markup ran"
    assert browser.execute_script("return document.querySelectorAll(arguments[0] + ' img[src=x]').length",
                                  where) == 0, "the words became an element"


# --- the wizards' previews: built from the query string, served sandboxed ---------------------------------------------

def _stored_by_the_preview(browser, base):
    """What the preview managed to put in the arcade's own storage: nothing, once it runs in an opaque origin."""
    browser.get(f"{base}/r/realtime.js")
    return browser.execute_script("return localStorage.getItem('pwned')")


def test_a_launchpad_preview_opened_on_its_own_cannot_reach_the_arcades_storage(served, browser):
    import httpx

    _, base, _ = served
    icon = "';localStorage.setItem('pwned','launchpad');//"
    browser.get(f"{base}/tokens/launchpad/preview?" + urlencode(
        {"name": "Goofcoin", "lot": "100", "price": "2 coins", "icon": icon}))
    # The page still works: its own script says on the button that it is a preview.
    _until(browser, "const b = document.getElementById('buy'); return b && b.textContent.includes('not on the chain')",
           "the preview's own script")
    said = httpx.get(f"{base}/tokens/launchpad/preview", params={"name": "Goofcoin"})
    assert said.headers.get("content-security-policy", "").startswith("sandbox")
    assert _stored_by_the_preview(browser, base) is None


def test_a_mintpad_preview_opened_on_its_own_cannot_reach_the_arcades_storage_and_still_draws_the_build(
        served, browser):
    _, base, home = served
    build = hashlips(home / "mintpad", count=3)
    # The wall is the build's pictures, read from the preview's own routes: an opaque origin still reaches them.
    browser.get(f"{base}/inscriptions/collection/preview?" + urlencode({"folder": str(build), "name": "Punks"}))
    _until(browser, "return document.querySelectorAll('.tile').length > 0", "the wall of the build's pictures")
    name = "Punks</title><script>localStorage.setItem('pwned','mintpad')</script>"
    browser.get(f"{base}/inscriptions/collection/preview?" + urlencode({"folder": str(build), "name": name}))
    assert _stored_by_the_preview(browser, base) is None
