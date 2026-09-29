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

#: The post and its one comment, as the page names them: the thread div is
#: `thread-<the post's txid>`, so a test that arrives at a fragment has to
#: arrive at this one.
POST = "a" * 64
REPLY = "c" * 64
REPLY2 = "d" * 64
#: Who wrote each. The second exists so a test can block one of the two and
#: watch the count above the thread notice.
THEY = "nOtherAAAAAAAAAAAAAAAAAAAAAAAAAA"
THEM2 = "nSecondAAAAAAAAAAAAAAAAAAAAAAAAA"


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
    from arcade.messaging import feed
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

    # One post with two comments on it, because the fold is the other thing
    # this file measures, and a fold with nothing behind it is a button over an
    # empty div. Two comments rather than one so a test can block whoever wrote
    # one of them and see whether the count above the thread notices -- the
    # block lives in this browser and the count is counted on the server, which
    # is the whole of @tester S25. The composer tests look only at
    # .feedcomposer, above the posts, so this cannot move the pixels they check.
    with state.store() as store:
        store.add_group_post(state.messaging.network, "", POST, 100, 1000,
                             mine, "", "the post", mine=True)
        store.add_feed_act(state.messaging.network, REPLY, feed.REPLY, POST,
                           THEY, "a comment", 200, 2000)
        store.add_feed_act(state.messaging.network, REPLY2, feed.REPLY, POST,
                           THEM2, "another comment", 201, 2010)

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


def test_the_count_under_a_post_opens_on_the_paths_a_link_takes(feed_page):
    """A comment count is a promise, and it used to be kept for one navigation
    path only.

    The thread is folded, and the button that unfolds it is the only thing that
    ever cleared `hidden`. So the promise held for a click and broke on a
    bookmark, a pasted link, and Back: the same URL carrying
    `#thread-<txid>` painted a div that stayed shut with no control on the page
    that opened it (@tester S22, 2026-09-27). Measured here in painted pixels
    rather than in the served HTML, because `hidden` is the page's claim and
    whether the words are on screen is the reader's.

    The page answers three ways of arriving now: first paint, a fragment change
    inside the tab the feed is already open in, and a restore from the
    back/forward cache. This file can only seed the first two -- a driver cannot
    order a memory-cache restore, and a `back()` that happens to hit the cache
    one day and rebuild the document the next proves nothing -- so the third is
    carried by the platform's contract and not by a measurement made here.
    """
    browser = browsers.launch()
    try:
        browser.set_window_size(1100, 900)

        # Arrive without the fragment: folded, and the button offers to open.
        browser.get(f"{feed_page}/feed")
        folded = browser.find_element(By.ID, f"thread-{POST}")
        assert not folded.is_displayed(), "the fold is gone and the count lies"
        button = browser.find_element(By.ID, f"fold-{POST}")
        assert button.text.strip() == "2 comments", button.text

        # Arrive at the same page carrying the fragment for that thread.
        browser.get(f"{feed_page}/feed#thread-{POST}")
        opened = browser.find_element(By.ID, f"thread-{POST}")
        assert opened.is_displayed(), \
            "a thread reached by link is still a closed box with a count on it"
        assert "a comment" in opened.text, "open, but with nothing in it"
        said = browser.find_element(By.ID, f"fold-{POST}").text.strip()
        assert said == "Hide 2 comments", \
            f"the thread is on screen and the button still offers it: {said}"

        # Then the way of arriving that is not a navigation at all: the same tab
        # moving onto a fragment. Nothing reloads, so no first-paint code re-runs
        # and the page has to answer the event -- otherwise a link pasted into a
        # live tab lands on a closed thread. Setting `location.hash` from a
        # script is the only way to be certain this is that path: a URL that
        # differs only by its fragment may or may not be a reload depending on
        # the driver, and a reload here would prove nothing about the listener.
        browser.get(f"{feed_page}/feed")
        assert not browser.find_element(By.ID, f"thread-{POST}").is_displayed(), \
            "the fold is gone, which would make the rest of this test vacuous"
        browser.execute_script(f"location.hash = 'thread-{POST}';")
        time.sleep(0.3)
        moved = browser.find_element(By.ID, f"thread-{POST}")
        assert moved.is_displayed(), \
            "the fragment changed inside the document and the fold never noticed"
        assert browser.find_element(By.ID, f"fold-{POST}").text.strip() \
            == "Hide 2 comments", "open on the event, and still offering to open"
    finally:
        browser.quit()


def test_a_count_says_what_a_block_took_away(feed_page):
    """The count is counted here; the block is kept by the browser.

    Both halves are correct on their own and they disagree on the page: the
    replies from somebody the reader blocked are drawn and then hidden by this
    browser's own filter, while the number above the thread still counts them.
    So the thread opened, and there was nothing in it (@tester S25, the other
    half of @yourfirstname's report in S22 -- which is the answer to whether
    the box showed nothing at all or a tap-to-show bar: nothing at all, and no
    reason). The fold has to say what is behind it now, and it has to still say
    it after the thread is folded back up and opened again.
    """
    browser = browsers.launch()
    try:
        browser.set_window_size(1100, 900)
        browser.get(f"{feed_page}/feed#thread-{POST}")
        button = browser.find_element(By.ID, f"fold-{POST}")
        assert button.text.strip() == "Hide 2 comments", button.text

        # The reader's own list, in their own browser: base.html's Blocked,
        # which tells the page by event and never by a request.
        browser.execute_script(f"window.arcadeBlocked.add('', {THEY!r});")
        said = browser.find_element(By.ID, f"fold-{POST}").text.strip()
        assert said == "Hide 1 comment (1 hidden)", said

        # Folded back up and pressed again: the note has to survive the press,
        # which is why it goes in the button's data as well as its text.
        browser.find_element(By.ID, f"fold-{POST}").click()
        assert button.text.strip() == "1 comment (1 hidden)", button.text
        browser.find_element(By.ID, f"fold-{POST}").click()
        assert button.text.strip() == "Hide 1 comment (1 hidden)", button.text

        thread = browser.find_element(By.ID, f"thread-{POST}")
        assert thread.is_displayed()
        left = [c for c in thread.find_elements(By.CSS_SELECTOR, ".feedpost")]
        assert len(left) == 2, "the hidden card was removed rather than hidden"
        assert [c.is_displayed() for c in left].count(True) == 1, \
            "a block that hides two cards and says one"
    finally:
        browser.quit()
