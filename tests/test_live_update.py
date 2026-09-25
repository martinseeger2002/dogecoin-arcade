"""The page shows what arrived without being thrown away and rebuilt.

A new block used to mean `location.reload()`: scroll position gone, every script
re-run, every image re-fetched, the page blinking white -- for one new line in
one list. Anything half typed suppressed it entirely, so the fix for that bug
was to stop showing new messages to anybody who was writing one.

These tests pin the replacement. The decisive check is not "the new message is
on the page" -- a reload would pass that too -- it is that a value set on
`window` before the update is still there afterwards. Only a document that was
never discarded can do both.

Skipped when selenium or a browser is unavailable, like the other browser tests.
"""

import socket
import threading
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

import browsers                                                      # noqa: E402
from selenium.webdriver.common.by import By                          # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    yield driver
    driver.quit()


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """The real application, plus the state object, so a test can make something
    happen on the chain's behalf and bump the generation the page watches."""
    import uvicorn
    from pathlib import Path
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext
    from arcade.messaging.keys import Identity

    home = tmp_path_factory.mktemp("live")
    state = AppState(
        home=home,
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                               datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=Path("/nonexistent")),
    )
    state.identity = Identity.generate()
    peer = b"\x77" * 32
    with state.store() as store:
        store.add_message(None, "tx", "tx", 1, 0, "nThem", peer,
                          state.identity.fingerprint, b"the first message")

    port = _free_port()
    config = uvicorn.Config(create_app(state), host="127.0.0.1", port=port,
                            log_level="error", timeout_graceful_shutdown=1.0)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("test server did not start")

    yield f"http://127.0.0.1:{port}", peer.hex(), peer, state
    server.should_exit = True
    server.force_exit = True        # a keep-alive connection otherwise outlives the join
    thread.join(timeout=5)


def _arrives(state, peer, text: str) -> None:
    """What a block bringing a message looks like from the page's point of view."""
    with state.store() as store:
        store.add_message(None, text[:8], text[:8], 2, 0, "nThem", peer,
                          state.identity.fingerprint, text.encode())
    state.bump_generation()


def _settled(browser) -> None:
    """Let the page take its baseline: the server's copy it compares every update to.

    It is fetched a moment after the page opens, while nothing is happening. A change that
    lands inside that first moment has no baseline to be measured against, so the page
    offers a refresh button for it instead of guessing -- correct, but not what these
    tests are about."""
    time.sleep(1.5)


def _until(check, seconds: float = 12.0):
    """Wait for something the browser will do on its own schedule."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if check():
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def test_a_new_message_appears_without_the_page_being_reloaded(browser, served):
    """The whole point: new content, same document.

    The marker is the proof. `location.reload()` would satisfy every assertion
    about the message and fail this one, which is exactly the bug being fixed.
    """
    base, peer_hex, peer, state = served
    browser.get(f"{base}/messages/{peer_hex}")
    browser.execute_script("window.__survived = 'yes'")
    _settled(browser)

    _arrives(state, peer, "a message that arrived while you were reading")

    assert _until(lambda: "while you were reading" in browser.page_source), (
        "the poller never brought the new message in"
    )
    assert browser.execute_script("return window.__survived") == "yes", (
        "the document was replaced -- this is a reload wearing a merge's clothes"
    )


def test_a_half_written_draft_survives_an_update(browser, served):
    """The old code refused to update at all rather than risk a draft.

    Now the draft is protected field by field, so both things can be true at
    once: the message arrives, and what you were typing is still there.
    """
    base, peer_hex, peer, state = served
    browser.get(f"{base}/messages/{peer_hex}")

    _settled(browser)
    box = browser.find_element(By.CSS_SELECTOR, "form.composer textarea")
    box.send_keys("half a sentence I am still")

    _arrives(state, peer, "something landing mid-sentence")

    assert _until(lambda: "landing mid-sentence" in browser.page_source), (
        "a draft in progress must no longer hold back what arrived"
    )
    assert browser.find_element(
        By.CSS_SELECTOR, "form.composer textarea"
    ).get_attribute("value") == "half a sentence I am still"


def test_an_unchanged_page_is_left_completely_alone(browser, served):
    """No generation change, no fetch of the page, nothing touched.

    Worth pinning because the cheap path is what makes this affordable to run
    every five seconds on a page somebody left open overnight.
    """
    base, peer_hex, _peer, _state = served
    browser.get(f"{base}/messages/{peer_hex}")
    browser.execute_script("window.__survived = 'yes'")
    before = browser.page_source

    time.sleep(7)                      # more than one poll of the five-second loop

    assert browser.execute_script("return window.__survived") == "yes"
    assert browser.page_source == before


# --- three-way: only what the SERVER changed is applied (2026-09-25) ------------------


def test_what_the_page_drew_itself_survives_an_update(browser, served):
    """Balances, "on its way", anything filled in by the page's own script.

    A two-way merge put the server's placeholder back over them at every block.
    Here the page draws over its own heading; the server never changes that
    heading, so an update must leave the page's version alone.
    """
    base, peer_hex, peer, state = served
    browser.get(f"{base}/messages/{peer_hex}")
    browser.execute_script("window.__survived = 'yes'")
    _settled(browser)
    browser.execute_script(
        "var t = document.createElement('span'); t.id = 'drawn';"
        "document.querySelector('footer.version').appendChild(t);"
        "document.querySelector('footer.version').firstChild.nodeValue = 'drawn by the page '")

    _arrives(state, peer, "arrived beside something the page drew")

    assert _until(lambda: "beside something the page drew" in browser.page_source)
    assert "drawn by the page" in browser.page_source, (
        "an update overwrote text the page had drawn for itself"
    )
    assert browser.execute_script("return window.__survived") == "yes"


def test_something_the_reader_hid_or_opened_stays_that_way(browser, served):
    """A reply box opened, a section closed: the reader's doing, not the server's."""
    base, peer_hex, peer, state = served
    browser.get(f"{base}/messages/{peer_hex}")
    _settled(browser)
    browser.execute_script("document.querySelector('footer.version').hidden = true")

    _arrives(state, peer, "tucked away: the footer, while this arrived")

    assert _until(lambda: "tucked away: the footer" in browser.page_source)
    assert browser.execute_script(
        "return document.querySelector('footer.version').hidden") is True


def test_new_site_code_is_offered_not_forced(browser, served):
    """When the site itself was updated, the open page cannot take it in place.

    It says so with a button. It does not reload under the reader.
    """
    base, peer_hex, peer, state = served
    browser.get(f"{base}/messages/{peer_hex}")
    browser.execute_script("window.__survived = 'yes'")
    _settled(browser)
    cls = type(state)
    old = cls.__dict__["running_version"]
    cls.running_version = property(lambda self: "0000000-new")
    try:
        _arrives(state, peer, "newsite: arrived with a new version")
        assert _until(lambda: browser.execute_script(
            "return !!document.getElementById('newposts')")), "no refresh was offered"
        assert browser.execute_script("return window.__survived") == "yes", (
            "the page reloaded itself instead of offering"
        )
    finally:
        cls.running_version = old
