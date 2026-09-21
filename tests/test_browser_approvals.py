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
    # The inscription below is on the MAINNET ledger, so this asks the pages
    # about that chain explicitly. A wallet with no choice recorded opens on
    # the chain its identity is on (D-134); this file inherited a default.
    (home / "tokens-chain").write_text("main\n")
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


def wait_for_title(browser, *prefixes: str, timeout: int = 10) -> str:
    """The title of the document in the frame, once it starts with one of these.

    `document.title` is None while a document is mid-navigation, and calling
    .startswith on it raised AttributeError *inside* the wait's lambda, which
    kills the wait on its first poll rather than retrying: the test then failed
    on the assertion after it, blaming the page for something the wait had not
    waited for. It failed only inside a full run, where the machine is busy
    enough for the second load to be slower than the first poll -- three times
    across two machines before it was understood. A value that is not a string
    is "not ready yet", and readyState is asked first so the usual case is a
    wait rather than a lucky read.
    """
    def ready(b):
        if b.execute_script("return document.readyState") == "loading":
            return None
        title = b.execute_script("return document.title")
        return title if isinstance(title, str) and title.startswith(prefixes) else None

    return WebDriverWait(browser, timeout).until(ready)

def test_an_inscription_that_asks_gets_a_pop_up_and_the_answer(browser, served):
    base, state = served
    browser.get(f"{base}/inscriptions/{TXID}/view")
    dialog = browser.find_element(By.ID, "ask")
    assert not dialog.is_displayed(), "nothing has been asked yet"

    # The page in the sandbox asks; the pop-up opens in front of it.
    WebDriverWait(browser, 10).until(lambda b: b.execute_script(
        "return document.getElementById('ask')?.open === true"))
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
        "return document.getElementById('ask')?.open !== true"))
    assert state.approvals.get(rid)["status"] == "denied"
    assert not dialog.is_displayed()

    # And once closed by a decision it does not come back for the same request.
    time.sleep(3.5)
    assert not browser.execute_script("return document.getElementById('ask')?.open === true")


def test_the_pop_up_is_not_reachable_from_inside_the_sandbox(browser, served):
    """The page that asked cannot press Approve: it cannot even see the
    dialog. And it cannot frame the approval page itself."""
    base, state = served
    browser.get(f"{base}/inscriptions/{TXID}/view")
    WebDriverWait(browser, 10).until(lambda b: b.execute_script(
        "return document.getElementById('ask')?.open === true"))
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
        "return document.getElementById('ask')?.open === true")
    time.sleep(3.5)
    assert not browser.execute_script("return document.getElementById('ask')?.open === true")
    # Fresh, while the page is open: it pops up.
    fresh = state.approvals.file("main", "coins", "page", RECIPIENT, units=1,
                                 amount="0.00000001", label="Now")
    WebDriverWait(browser, 10).until(lambda b: b.execute_script(
        "return document.getElementById('ask')?.open === true"))
    assert fresh in browser.find_element(By.ID, "ask-frame").get_attribute("src")
    browser.execute_script("document.getElementById('ask-close').click()")
    state.approvals.decide(stale, "denied"); state.approvals.decide(fresh, "denied")



def test_the_decision_is_on_screen_without_scrolling(browser, served):
    """Approve and Refuse stay at the bottom edge of the pop-up's frame.

    A swap lists two legs, five rows and every output of the transaction. In
    a frame 640 wide the buttons that answer it were below the fold, so the
    person deciding had to scroll inside a modal to find them -- with Close,
    the one button that does nothing, sitting in plain view underneath.

    The window is squeezed here rather than the page lengthened: what matters
    is that the buttons are on screen when the document scrolls, and a short
    viewport makes any document scroll.
    """
    base, state = served
    rid = state.approvals.file("main", "coins", "page", RECIPIENT, units=1,
                               amount="0.00000001", label="Hat Shop")
    was = browser.get_window_size()
    try:
        browser.set_window_size(640, 300)
        browser.get(f"{base}/approvals/{rid}?embed=1")
        WebDriverWait(browser, 10).until(lambda b: b.find_elements(
            By.CSS_SELECTOR, "form.decide"))
        assert browser.execute_script(
            "return document.documentElement.scrollHeight > window.innerHeight"
        ), "the page has to scroll for this to be worth testing"
        # Painted position, not the rule that produces it: a computed style of
        # sticky says nothing about where the element ended up.
        seen = browser.execute_script("""
            window.scrollTo(0, 0);
            var f = document.querySelector('form.decide');
            var b = f.querySelector('button').getBoundingClientRect();
            return [b.top >= 0 && b.bottom <= window.innerHeight,
                    window.innerHeight - b.bottom];""")
        assert seen[0], "the button is off the screen until you scroll"
        assert seen[1] < 40, f"and it sits at the bottom edge, not {seen[1]}px above it"
    finally:
        browser.set_window_size(was["width"], was["height"])
        state.approvals.decide(rid, "denied")


# --- the pages' own hostname --------------------------------------------------

def test_from_its_own_hostname_a_page_still_reaches_the_wallet(browser, served):
    """Found on the live wallet: framed from a second name, an inscribed page
    sat there saying "..." -- its opaque origin sends no cookie, so every
    fetch it made met the locked door.

    Two names for loopback stand in for the real pair: the wallet at
    wallet.localhost, the pages at pages.localhost, and the guard telling
    them apart by Host exactly as it does `dogecoinarcade.com` and whatever
    the operator points at the pages (`pages_host`).
    """
    base, state = served
    port = base.rsplit(":", 1)[1]
    _add_counter_page(state)
    state.pagestore.clear(STORING)
    pages = f"http://pages.localhost:{port}"
    state.set_setting("pages_host", pages)
    try:
        wallet = f"http://wallet.localhost:{port}"
        browser.get(f"{wallet}/inscriptions/{STORING}/view")
        frame = browser.find_element(By.CSS_SELECTOR, "iframe.inscription-frame")
        assert frame.get_attribute("src") == f"{pages}/content/{STORING}"
        browser.switch_to.frame(frame)
        assert wait_for_title(browser, "visits ") == "visits 1", (
            "storage.js loaded from the pages' door and the bridge answered")
        browser.set_script_timeout(10)
        status = browser.execute_async_script(
            "var done = arguments[0];"
            "fetch('/r/blockheight').then(function(r){ done(r.status); })"
            ".catch(function(e){ done('ERR ' + e.message); });")
        assert status == 200, "the page API, cookie-less, from inside the sandbox"
        browser.switch_to.default_content()
        # And the pages' name is good for nothing else.
        browser.get(f"{pages}/approvals")
        assert "Nothing is served" in browser.page_source
    finally:
        browser.switch_to.default_content()
        state.set_setting("pages_host", "")


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
            return wait_for_title(browser, "visits", "error")
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


# --- talking to another node ---------------------------------------------------

TALKING = "12" * 32
#: A page that tells a shop's node what it wants and waits to be answered.
TALKER = (b"<!doctype html><title>talker</title><script src='/r/node.js'></script><script>"
          b"arcade.node.identity().then(function(me){"
          b"  window.me = me.pubkey;"
          b"  return arcade.node.send('%s', {order: 'hat'});"
          b"}).then(function(r){"
          b"  document.title = 'sent ' + r.txid;"
          b"  arcade.node.listen(function(reply){ document.title = 'heard ' + reply.body; },"
          b"                     {after: 0, every: 500});"
          b"}).catch(function(e){ document.title = 'error ' + e.message; });"
          b"</script>" % (b"ef" * 32))


def test_a_page_talks_to_another_node_through_the_wallet(browser, served, monkeypatch):
    """The sandbox cannot leave this machine. arcade.node sends a node-to-node
    message as this wallet and hands back what that node says -- no approval,
    because it is testnet and moves nothing."""
    from arcade.messaging.keys import Identity, fingerprint_of

    base, state = served
    from arcade.db import Database
    db = Database(state.home / "main-ledger.sqlite")
    db.conn.execute(
        "INSERT OR IGNORE INTO inscription(txid,number,creator,owner,block_height,position,"
        "content_type,content_len,sha256,json,chunks,content) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (TALKING, 3, "nMe", "nMe", 102, 0, "text/html", len(TALKER), "13" * 32, "", 1, TALKER))
    db.conn.commit()
    db.close()

    sent = {}

    class FakeSender:
        def __init__(self, *a, **k):
            pass

        def prepare(self, address, payload):
            sent["payload"] = payload
            return type("P", (), {"fee_sats": 1000, "total_sats": 1000, "size": 300,
                                  "txid": "tx"})()

        def broadcast(self, prepared):
            return "f" * 64

    class FakeRpc:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr("arcade.web.app.MessageSender", FakeSender)
    monkeypatch.setattr("arcade.web.app.funded_address", lambda *a, **k: "nAddr")
    monkeypatch.setattr(type(state.messaging), "rpc", lambda self: FakeRpc())
    state.identity = Identity.generate()
    state.ensure_identity = lambda: state.identity

    browser.get(f"{base}/inscriptions/{TALKING}/view")
    browser.switch_to.frame(browser.find_element(By.CSS_SELECTOR, ".inscription-frame"))
    try:
        assert wait_for_title(browser, "sent", "error") == "sent " + "f" * 64
        assert browser.execute_script("return window.me") == state.identity.public_bytes.hex()
        # Sealed to the shop, and it is the page's order.
        from arcade.messaging import api
        shop_key = bytes.fromhex("ef" * 32)
        assert sent["payload"], "went through the wallet's sender"
        assert state.talk.peers(TALKING, state.messaging.network) == {shop_key.hex(): 0}

        # The shop answers; the listener hears it within a poll.
        with state.store() as store:
            store.add_api_message(state.messaging.network, "t9", 7, 7, "nShop", shop_key,
                                  fingerprint_of(state.identity.public_bytes), b"hat is on its way")
        assert wait_for_title(browser, "heard") == "heard hat is on its way"
    finally:
        browser.switch_to.default_content()
