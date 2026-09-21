"""The mintpad's HTML editor, in a real browser.

The claim being tested is "the preview displays the edits live". That is a
claim about a browser: a server-side test can prove the box holds the page
and that the substitutions were handed over, and prove nothing at all about
whether anything is drawn. So this drives it.
"""

import pathlib
import socket
import sys
import threading
import time

import pytest

pytest.importorskip("selenium",
                    reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                  # noqa: E402
from selenium.webdriver.common.by import By                      # noqa: E402

from test_collections import hashlips                            # noqa: E402


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
    """The application with a build on disk, and no node behind it."""
    import contextlib

    import uvicorn
    from pathlib import Path
    from arcade.web import app as webapp
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    home = tmp_path_factory.mktemp("padedit")
    build = hashlips(tmp_path_factory.mktemp("build"), count=3)
    state = AppState(
        home=home,
        messaging=ChainContext(network="regtest", role="messaging",
                               label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=Path("/nonexistent")),
    )
    (home / "tokens-chain").write_text("main\n")

    # The review needs an address it believes is this node's, and a chain it
    # can open. Neither is a node: nothing here broadcasts.
    webapp._ledger_addresses = lambda rpc: ["nMe"]
    type(state.ledger).rpc = lambda self: contextlib.nullcontext(object())

    port = _free_port()
    config = uvicorn.Config(create_app(state), host="127.0.0.1", port=port,
                            log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("test server did not start")
    yield f"http://127.0.0.1:{port}", str(build), state
    server.should_exit = True
    thread.join(timeout=5)


REVIEW = """
const done = arguments[3];
const form = document.createElement('form');
form.method = 'post';
form.action = '/inscriptions/collection/review';
for (const [name, value] of [['csrf_token', arguments[0]],
                             ['folder', arguments[1]],
                             ['fromaddress', arguments[2]]]) {
  const input = document.createElement('input');
  input.name = name; input.value = value;
  form.appendChild(input);
}
document.body.appendChild(form);
done(true);
form.submit();
"""


def _review(browser, base, folder, state):
    browser.get(f"{base}/inscriptions/collection")
    browser.set_script_timeout(60)
    browser.execute_async_script(REVIEW, state.csrf_token, folder, "nMe")
    for _ in range(80):
        if browser.find_elements(By.ID, "pad-html"):
            return
        time.sleep(0.25)
    raise AssertionError("the review never appeared")


def test_the_mintpad_is_folded_away_until_it_is_asked_for(browser, served):
    """Optional means optional: nothing about a shop is on screen until the
    box is ticked, and the box starts clear."""
    base, folder, state = served
    _review(browser, base, folder, state)
    assert not browser.find_element(By.ID, "launchpad").is_selected()
    assert not browser.find_element(By.ID, "pad-fields").is_displayed()
    assert not browser.find_element(By.ID, "pad-html").is_displayed()


def test_ticking_it_draws_the_page_and_shows_its_html(browser, served):
    base, folder, state = served
    _review(browser, base, folder, state)
    browser.find_element(By.ID, "launchpad").click()
    assert browser.find_element(By.ID, "pad-fields").is_displayed()

    box = browser.find_element(By.ID, "pad-html")
    assert box.is_displayed()
    html = box.get_attribute("value")
    assert "MINTPAD" in html.upper() and "nMe" in html

    # And the frame is drawing that page, not an empty document.
    frame = browser.find_element(By.ID, "pad-preview")
    for _ in range(40):
        if frame.get_attribute("srcdoc"):
            break
        time.sleep(0.25)
    assert "MINTPAD" in frame.get_attribute("srcdoc").upper()


def test_the_preview_follows_the_edits(browser, served):
    """The claim itself. Type into the box; the frame shows it."""
    base, folder, state = served
    _review(browser, base, folder, state)
    browser.find_element(By.ID, "launchpad").click()
    box = browser.find_element(By.ID, "pad-html")

    browser.execute_script("""
        const box = arguments[0];
        box.value = box.value.replace('</style>',
            '</style><h1 id="mine">A SHOP OF MY OWN</h1>');
        box.dispatchEvent(new Event('input'));""", box)

    frame = browser.find_element(By.ID, "pad-preview")
    for _ in range(40):
        if "A SHOP OF MY OWN" in (frame.get_attribute("srcdoc") or ""):
            break
        time.sleep(0.25)
    assert "A SHOP OF MY OWN" in frame.get_attribute("srcdoc")

    # Painted, not merely set: the frame is same-origin-less but the DOM
    # inside it can still be reached through the frame context.
    browser.switch_to.frame(frame)
    try:
        for _ in range(40):
            found = browser.find_elements(By.ID, "mine")
            if found:
                break
            time.sleep(0.25)
        assert found and found[0].text == "A SHOP OF MY OWN", \
            "the edit is in the srcdoc but was never drawn"
    finally:
        browser.switch_to.default_content()


def test_the_byte_count_follows_the_edits_too(browser, served):
    """What is in the box is what is paid for, so the size is on screen."""
    base, folder, state = served
    _review(browser, base, folder, state)
    browser.find_element(By.ID, "launchpad").click()
    size = browser.find_element(By.ID, "pad-size")
    for _ in range(40):
        if size.text:
            break
        time.sleep(0.25)
    before = int(size.text.split()[0].replace(",", ""))

    browser.execute_script("""
        const box = arguments[0];
        box.value += '<!-- ' + 'x'.repeat(5000) + ' -->';
        box.dispatchEvent(new Event('input'));""",
        browser.find_element(By.ID, "pad-html"))
    for _ in range(40):
        after = int(size.text.split()[0].replace(",", ""))
        if after > before:
            break
        time.sleep(0.25)
    assert after >= before + 5000


def test_gutting_the_page_is_allowed_but_says_what_it_costs(browser, served):
    """It is their page. Warned, not forbidden -- but a page that no longer
    names its collection cannot sell one, and that is worth saying."""
    base, folder, state = served
    _review(browser, base, folder, state)
    browser.find_element(By.ID, "launchpad").click()
    browser.execute_script("""
        const box = arguments[0];
        box.value = '<h1>nothing here</h1>';
        box.dispatchEvent(new Event('input'));""",
        browser.find_element(By.ID, "pad-html"))
    warn = browser.find_element(By.ID, "pad-warn")
    for _ in range(40):
        if warn.text:
            break
        time.sleep(0.25)
    assert "CREATOR" in warn.text and "COLLECTION" in warn.text


def test_the_standard_page_can_be_put_back(browser, served):
    base, folder, state = served
    _review(browser, base, folder, state)
    browser.find_element(By.ID, "launchpad").click()
    box = browser.find_element(By.ID, "pad-html")
    original = box.get_attribute("value")

    browser.execute_script("""
        arguments[0].value = 'wrecked';
        arguments[0].dispatchEvent(new Event('input'));""", box)
    browser.find_element(By.ID, "pad-reset").click()
    for _ in range(40):
        if box.get_attribute("value") == original:
            break
        time.sleep(0.25)
    assert box.get_attribute("value") == original
