"""The pop-up under a running inscription, in a real browser.

The inscribed page asks from inside its sandbox; the wallet's approval opens
in front of it, outside the sandbox; a press on Refuse inside the pop-up
closes it and answers the page. Every step is checked as an event in the
browser, not as a string in a template.
"""

import json
import threading
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

from selenium.webdriver.common.by import By                          # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait              # noqa: E402

from test_browser import _free_port, browser  # noqa: F401, E402  (fixture re-export)

from arcade.config import NETWORKS                                   # noqa: E402
from arcade.script import b58check_encode                            # noqa: E402

TXID = "ab" * 32
RECIPIENT = b58check_encode(NETWORKS["main"].pubkeyhash_version, bytes([5]) * 20)

#: The inscription: a page that, once it runs, asks the wallet for a coin
#: and a half, the way a shop or a game would.
PAGE = ("<!doctype html><title>shop</title><p>a shop</p><script>"
        "fetch('/r/send', {method: 'POST', headers: {'Content-Type': 'application/json'},"
        " body: JSON.stringify({kind: 'coins', to: %s, amount: '1.5',"
        " label: 'Hat Shop', note: 'one red hat'})})"
        ".then(r => r.json()).then(j => { document.title = 'asked ' + j.id; });"
        "</script>" % json.dumps(RECIPIENT)).encode()


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """The application on a real port, no node, holding one HTML inscription."""
    from pathlib import Path

    import uvicorn

    from arcade.db import Database
    from arcade.state import install_schema
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    home = tmp_path_factory.mktemp("popup")
    db = Database(home / "main-ledger.sqlite")
    install_schema(db)
    db.conn.execute(
        "INSERT INTO inscription(txid,number,creator,owner,block_height,position,"
        "content_type,content_len,sha256,json,chunks,content) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (TXID, 1, "nMe", "nMe", 100, 0, "text/html", len(PAGE), "cd" * 32, "", 1, PAGE))
    db.conn.commit()
    db.close()

    state = AppState(
        home=home,
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                               datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=Path("/nonexistent")),
    )
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1",
                                           port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("test server did not start")
    yield f"http://127.0.0.1:{port}", state
    server.should_exit = True
    thread.join(timeout=5)


def test_an_inscription_that_asks_gets_a_pop_up_and_the_answer(browser, served):
    base, state = served
    browser.get(f"{base}/inscriptions/{TXID}/view")
    dialog = browser.find_element(By.ID, "ask")
    assert not dialog.is_displayed(), "nothing has been asked yet"

    # The page in the sandbox asks; the pop-up opens in front of it.
    WebDriverWait(browser, 10).until(lambda b: b.execute_script(
        "return document.getElementById('ask').open"))
    assert dialog.is_displayed()
    frame = browser.find_element(By.ID, "ask-frame")
    assert "?embed=1" in frame.get_attribute("src")
    rid = frame.get_attribute("src").rsplit("/", 1)[1].split("?")[0]
    assert state.approvals.get(rid)["status"] == "pending"

    # Inside it is the wallet's own approval page, without the wallet's chrome
    # and with the request the page filed -- built or, with no node, refusable.
    browser.switch_to.frame(frame)
    WebDriverWait(browser, 10).until(lambda b: "Hat Shop" in b.page_source)
    assert "1.5 coins" in browser.page_source
    assert not browser.find_elements(By.CSS_SELECTOR, "header nav"), "no chrome in the pop-up"
    assert "could not be built" in browser.page_source, "no node: nothing to approve"
    refuse = browser.find_element(By.XPATH, "//button[normalize-space()='Refuse']")
    refuse.click()

    # Refused inside the frame: the request is answered, the pop-up says so
    # and closes itself.
    browser.switch_to.default_content()
    WebDriverWait(browser, 10).until(lambda b: b.execute_script(
        "return !document.getElementById('ask').open"))
    assert state.approvals.get(rid)["status"] == "denied"
    assert not dialog.is_displayed()

    # And once closed by a decision it does not come back for the same request.
    time.sleep(3.5)
    assert not browser.execute_script("return document.getElementById('ask').open")


def test_the_pop_up_is_not_reachable_from_inside_the_sandbox(browser, served):
    """The page that asked cannot press Approve: it cannot even see the
    dialog. And it cannot frame the approval page itself."""
    base, state = served
    browser.get(f"{base}/inscriptions/{TXID}/view")
    WebDriverWait(browser, 10).until(lambda b: b.execute_script(
        "return document.getElementById('ask').open"))
    frame = browser.find_element(By.ID, "ask-frame")
    rid = frame.get_attribute("src").rsplit("/", 1)[1].split("?")[0]

    sandbox = browser.find_element(By.CSS_SELECTOR, ".inscription-frame")
    browser.switch_to.frame(sandbox)
    reach = browser.execute_script("""
        try { return window.parent.document.getElementById('ask') ? 'seen' : 'missing'; }
        catch (e) { return 'blocked'; }""")
    assert reach == "blocked", "the sandbox must not see the page around it"
    # Framing the approval page from inside the sandbox is refused by the
    # browser on the page's own say-so (frame-ancestors 'self'); the header
    # itself is checked in test_approvals, since what a refused frame shows
    # cannot be read from here.
    browser.switch_to.default_content()
    browser.execute_script("document.getElementById('ask-close').click()")
    state.approvals.decide(rid, "denied")


def test_what_was_waiting_before_the_page_opened_does_not_pop_up_over_it(browser, served):
    """A request filed an hour ago by something else is not this page asking.
    On the live wallet a stale demo request opened over every inscription
    one looked at, and its modal swallowed the first click on the page. It
    is listed, with a link; only a fresh request opens."""
    base, state = served
    stale = state.approvals.file("main", "coins", "page", RECIPIENT, units=1,
                                 amount="0.00000001", label="Earlier")
    with state.approvals._open() as conn:
        conn.execute("UPDATE request SET created = created - 600 WHERE id = ?", (stale,))
    _add_counter_page(state)
    browser.get(f"{base}/inscriptions/{STORING}/view")   # a page that asks nothing
    WebDriverWait(browser, 10).until(lambda b: not b.find_element(By.ID, "asked").get_attribute("hidden"))
    notice = browser.find_element(By.ID, "asked")
    assert "Earlier" in notice.text and not browser.execute_script(
        "return document.getElementById('ask').open")
    time.sleep(3.5)
    assert not browser.execute_script("return document.getElementById('ask').open")
    # Fresh, while the page is open: it pops up.
    fresh = state.approvals.file("main", "coins", "page", RECIPIENT, units=1,
                                 amount="0.00000001", label="Now")
    WebDriverWait(browser, 10).until(lambda b: b.execute_script(
        "return document.getElementById('ask').open"))
    assert fresh in browser.find_element(By.ID, "ask-frame").get_attribute("src")
    browser.execute_script("document.getElementById('ask-close').click()")
    state.approvals.decide(stale, "denied"); state.approvals.decide(fresh, "denied")


# --- storage ------------------------------------------------------------------

STORING = "cd" * 32
#: A page that counts how often it has been opened, through the wallet.
COUNTER = (b"<!doctype html><title>counter</title><script src='/r/storage.js'></script><script>"
           b"arcade.storage.ready.then(function(s){"
           b"  var n = Number(s.getItem('visits') || 0) + 1;"
           b"  return s.setItem('visits', String(n)).then(function(){ document.title = 'visits ' + n; });"
           b"}).catch(function(e){ document.title = 'error ' + e.message; });"
           b"</script>")


def _add_counter_page(state):
    from arcade.db import Database
    db = Database(state.home / "main-ledger.sqlite")
    db.conn.execute(
        "INSERT OR IGNORE INTO inscription(txid,number,creator,owner,block_height,position,"
        "content_type,content_len,sha256,json,chunks,content) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (STORING, 2, "nMe", "nMe", 101, 0, "text/html", len(COUNTER), "ef" * 32, "", 1, COUNTER))
    db.conn.commit()
    db.close()


def test_a_page_remembers_through_the_wallet(browser, served):
    """The sandbox has no localStorage. arcade.storage is the same shape,
    kept by the wallet under this inscription's id, and still there when the
    page is opened again."""
    base, state = served
    _add_counter_page(state)
    state.pagestore.clear(STORING)      # the module shares one wallet

    def title():
        browser.switch_to.frame(browser.find_element(By.CSS_SELECTOR, ".inscription-frame"))
        try:
            # document.title, not browser.title: the driver's title is always
            # the top document's, whichever frame is switched to.
            WebDriverWait(browser, 10).until(lambda b: b.execute_script(
                "return document.title").startswith(("visits", "error")))
            return browser.execute_script("return document.title")
        finally:
            browser.switch_to.default_content()

    browser.get(f"{base}/inscriptions/{STORING}/view")
    assert title() == "visits 1"
    assert state.pagestore.items(STORING) == {"visits": "1"}
    browser.get(f"{base}/inscriptions/{STORING}/view")
    assert title() == "visits 2"
    assert state.pagestore.items(STORING) == {"visits": "2"}
    assert state.pagestore.items(TXID) == {}, "the other page has nothing of it"

    # The browser's own localStorage really is out of reach in there, and a
    # value too big for the wallet is refused and the page told.
    browser.switch_to.frame(browser.find_element(By.CSS_SELECTOR, ".inscription-frame"))
    assert browser.execute_script(
        "try { return typeof localStorage.getItem } catch (e) { return e.name }") == "SecurityError"
    outcome = browser.execute_async_script("""
        var done = arguments[0];
        arcade.storage.setItem('big', 'x'.repeat(70000))
          .then(function(){ done('kept') }, function(e){ done(e.message) });""")
    assert "at most" in outcome
    assert browser.execute_script("return arcade.storage.getItem('big')") is None, "rolled back"
    assert browser.execute_script("return arcade.storage.length") == 1
    browser.switch_to.default_content()
    assert state.pagestore.items(STORING) == {"visits": "2"}
