"""The composer, measured in painted pixels.

Three controls with three different paddings and three different fonts do
not line up by accident, and a computed style is not evidence: it passed
three broken bubble tails once, and only the pixels caught them.
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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def feed_page(tmp_path_factory):
    """The feed, served for real, with a name so the composer is drawn."""
    import uvicorn

    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    from arcade.db import Database
    from arcade.state import install_schema

    home = tmp_path_factory.mktemp("feed")
    mine = "nMeAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    # A name claimed on the chain, because that is what the page reads: the
    # composer appears for somebody who can post, and posting needs a tag
    # (D-138). Faking the helper would test a page nobody can reach.
    db = Database(home / "regtest-ledger.sqlite")
    install_schema(db)
    db.conn.execute("INSERT INTO tag(tag,address,claimed_txid,block_height,position) "
                    "VALUES('robin',?,?,100,0)", (mine, "aa" * 32))
    db.conn.commit()
    db.close()

    state = AppState(
        home=home,
        messaging=ChainContext(network="regtest", role="messaging",
                               label="Testnet", datadir=pathlib.Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=pathlib.Path("/nonexistent")),
    )
    # Undoed by name, not left set: this fixture is module-scoped, so a bare
    # assignment here served `derived_address` to every test that ran after this
    # file, in a process that had no reason to expect a stranger's address
    # (2026-09-22 -- other files' failures, none of them this one's).
    patches = pytest.MonkeyPatch()
    patches.setattr(type(state), "derived_address", property(lambda self: mine))

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
    patches.undo()
    server.should_exit = True
    thread.join(timeout=5)
    assert not thread.is_alive(), "the server is still serving a keep-alive connection"


def boxes(browser):
    return {
        name: browser.execute_script(
            "var el = document.querySelector(arguments[0]);"
            "if (!el) return null;"
            "var r = el.getBoundingClientRect();"
            "return {top: r.top, bottom: r.bottom, height: r.height, width: r.width};",
            selector)
        for name, selector in (("box", ".feedcomposer textarea"),
                               ("clip", ".feedcomposer .attach"),
                               ("post", ".feedcomposer button"))
    }


def test_the_composer_controls_line_up(feed_page):
    browser = browsers.launch()
    try:
        browser.set_window_size(1100, 900)
        browser.get(f"{feed_page}/feed")
        found = boxes(browser)
        assert all(found.values()), f"the composer is not on the page: {found}"

        heights = {name: round(box["height"]) for name, box in found.items()}
        assert len(set(heights.values())) == 1, f"three heights: {heights}"

        bottoms = [box["bottom"] for box in found.values()]
        assert max(bottoms) - min(bottoms) < 1.0, \
            f"not on one line: {[round(b, 1) for b in bottoms]}"
    finally:
        browser.quit()


def test_the_box_grows_and_the_buttons_stay_with_it(feed_page):
    """It starts one row tall and grows as you type; the clip and the button
    stay pinned to its bottom edge rather than floating up the side."""
    browser = browsers.launch()
    try:
        browser.set_window_size(1100, 900)
        browser.get(f"{feed_page}/feed")
        before = boxes(browser)

        box = browser.find_element(By.CSS_SELECTOR, ".feedcomposer textarea")
        box.send_keys("a line\nand another\nand a third\nand a fourth")
        time.sleep(0.2)
        after = boxes(browser)

        assert after["box"]["height"] > before["box"]["height"], "it did not grow"
        assert round(after["clip"]["height"]) == round(before["clip"]["height"]), \
            "the clip grew with it"
        bottoms = [box["bottom"] for box in after.values()]
        assert max(bottoms) - min(bottoms) < 1.0, \
            f"they came apart once it grew: {[round(b, 1) for b in bottoms]}"
    finally:
        browser.quit()
