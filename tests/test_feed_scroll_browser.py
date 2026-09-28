"""Comments on posts that arrive by scrolling (2026-09-27: "if I scroll
down far enough on a feed comments quit opening").

The feed adds older pages as you scroll, and it copied only each post's card:
a post's thread is the element AFTER its card, so every post loaded that way
had an "N comments" button with nothing to open.
"""

import pathlib
import socket
import sys
import threading
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

from selenium.webdriver.common.by import By                          # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                      # noqa: E402

OLDEST = "0" * 63 + "1"
THEY = "nOtherAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def long_feed(tmp_path_factory):
    import uvicorn

    from arcade.messaging import feed
    from arcade.web.app import PAGE_POSTS, create_app
    from arcade.web.state import AppState, ChainContext

    home = tmp_path_factory.mktemp("longfeed")
    state = AppState(
        home=home,
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                               datadir=pathlib.Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=pathlib.Path("/nonexistent")))
    with state.store() as store:
        net = state.messaging.network
        # The oldest post, with comments, lands on the SECOND page of "new".
        store.add_group_post(net, "", OLDEST, 100, 1000, THEY, "", "the oldest post")
        for n in range(1, 3):
            store.add_feed_act(net, f"{n:064x}".replace("0", "c", 1), feed.REPLY, OLDEST,
                               THEY, f"comment {n}", 100 + n, 1000 + n)
        for n in range(PAGE_POSTS + 5):
            store.add_group_post(net, "", f"{n + 10:064x}", 200 + n, 2000 + n,
                                 THEY, "", f"post {n}")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1",
                                           port=port, log_level="error",
                                           timeout_graceful_shutdown=1.0))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("test server did not start")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    yield driver
    driver.quit()


def test_comments_open_on_a_post_that_came_in_by_scrolling(browser, long_feed):
    browser.get(f"{long_feed}/feed?sort=new")
    button = None
    for _ in range(80):
        browser.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        found = browser.find_elements(By.ID, f"fold-{OLDEST}")
        if found:
            button = found[0]
            break
        time.sleep(0.25)
    assert button is not None, "the older page never arrived"
    browser.execute_script("arguments[0].scrollIntoView({block: 'center'})", button)
    button.click()
    thread = browser.find_element(By.ID, f"thread-{OLDEST}")
    assert thread.is_displayed(), "the comments of a scrolled-in post open"
    assert "comment 1" in thread.text
    assert button.text.startswith("Hide"), button.text
