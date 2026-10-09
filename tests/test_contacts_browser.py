"""The account's address book, in a real browser.

The book lives in IndexedDB and nowhere else, which is the whole reason it
exists: a node that held everybody's address book would know who talks to
whom without opening a single message (D-155). A test that asserted about
the server would therefore be testing the wrong machine. So these drive the
page and then look in the browser's own storage -- and reload, because a
book that is only in a variable is not a book.
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

from arcade import seed                                          # noqa: E402


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
    import uvicorn
    from pathlib import Path
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    state = AppState(
        home=tmp_path_factory.mktemp("contacts"),
        messaging=ChainContext(network="regtest", role="messaging",
                               label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=Path("/nonexistent")),
    )
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
    yield f"http://127.0.0.1:{port}", state
    server.should_exit = True
    thread.join(timeout=5)
    assert not thread.is_alive(), "the server is still serving a keep-alive connection"


#: Answers `/account/who/...` without a chain, so the book can be exercised
#: on a node that has never seen a block. Everything else goes to the server
#: as usual -- this stands in for the chain, not for the application.
PRETEND = """
// Bound, because module code is strict: called bare as `fetch(...)`
// `this` is undefined, and `fetch` refuses to run on anything but a Window.
const real = window.fetch.bind(window);
window.fetch = function (url, opts) {
  const where = String(url);
  if (where.startsWith("/account/who/")) {
    const name = decodeURIComponent(where.slice("/account/who/".length))
                   .replace(/^@/, "");
    return Promise.resolve(new Response(JSON.stringify({
      tag: name, address: "n" + name + "Address", key: "aa".repeat(32),
      fingerprint: "ffff"}), {status: 200,
      headers: {"Content-Type": "application/json"}}));
  }
  if (where.startsWith("/account/find")) {
    const wanted = new URL(where, location.origin).searchParams.get("q") || "";
    const all = ["robin", "gx1", "postmaster"];
    const hit = all.filter((t) => t.includes(wanted.replace(/^@/, "")));
    return Promise.resolve(new Response(JSON.stringify({
      matches: hit.map((t) => ({tag: t, address: "n" + t + "Address"}))}),
      {status: 200, headers: {"Content-Type": "application/json"}}));
  }
  return real.apply(null, arguments);
};
"""


def _signed_in(browser, base):
    """A seat, taken the way the page takes one."""
    browser.get(f"{base}/join/keys")
    browser.delete_all_cookies()
    for _ in range(80):
        if browser.execute_script(
                "return document.body.dataset.joinReady === 'yes'"):
            break
        time.sleep(0.25)
    phrase = seed.generate()
    # Signed in AND the wallet open in this tab, which is what somebody who
    # just made an account has: signing in opens it, and it stays open from
    # page to page until the tab is closed.
    answer = browser.execute_async_script("""
        const done = arguments[1];
        Promise.all([import("/signin.js"), import("/wallet.js")])
          .then(([s, w]) => s.signIn(arguments[0], {join: true})
                             .then(() => { w.remember(arguments[0]); }))
          .then(() => done(true), e => done(String(e.message || e)));""",
        phrase)
    assert answer is True, answer
    return phrase


def _book_page(browser, base):
    browser.get(f"{base}/me/contacts")
    browser.execute_script(PRETEND)
    for _ in range(80):
        if browser.execute_script(
                "return document.body.dataset.contactsReady === 'yes'"):
            break
        time.sleep(0.25)
    else:
        raise AssertionError("the address book never finished drawing")


@pytest.fixture(scope="module", autouse=True)
def seated(browser, served):
    """A seat, once, for every test here.

    Not left to whichever test happens to run first: these run in a random
    order, and a test that depended on an earlier one for its session
    passed or failed by luck of the draw.
    """
    base, _ = served
    return _signed_in(browser, base)


def _wipe_book(browser):
    return browser.execute_async_script("""
        const done = arguments[0];
        const open = indexedDB.open("arcade-messages", 3);
        open.onupgradeneeded = () => {
          const db = open.result;
          if (!db.objectStoreNames.contains("mail"))
            db.createObjectStore("mail", {keyPath: "txid"});
          if (!db.objectStoreNames.contains("marks"))
            db.createObjectStore("marks");
          if (!db.objectStoreNames.contains("book"))
            db.createObjectStore("book", {keyPath: "tag"});
          if (!db.objectStoreNames.contains("parts"))
            db.createObjectStore("parts", {keyPath: "txid"});
        };
        open.onerror = () => done("open: " + open.error);
        open.onsuccess = () => {
          const db = open.result;
          const tx = db.transaction(["book"], "readwrite");
          tx.objectStore("book").clear();
          tx.oncomplete = () => done("ok");
          tx.onerror = () => done("clear: " + tx.error);
        };""")


def _add_robin(browser, base):
    """The add flow, by hand, for the tests that need somebody in the book
    without being about the adding."""
    _book_page(browser, base)
    if browser.find_elements(By.CSS_SELECTOR, "#book .person"):
        return
    browser.find_element(By.ID, "add").click()
    box = browser.find_element(By.ID, "find")
    box.clear()
    box.send_keys("robin")
    browser.find_element(By.ID, "search").click()
    for _ in range(40):
        buttons = browser.find_elements(By.CSS_SELECTOR, "#matches button")
        if buttons:
            break
        time.sleep(0.25)
    buttons[0].click()
    for _ in range(40):
        if browser.find_elements(By.CSS_SELECTOR, "#book .person"):
            return
        time.sleep(0.25)
    raise AssertionError("could not put anybody in the book")


def test_the_book_starts_empty_and_says_so(browser, served):
    base, _ = served
    _book_page(browser, base)
    _wipe_book(browser)
    _book_page(browser, base)
    assert browser.find_element(By.ID, "nobody").is_displayed()
    assert "empty" in browser.find_element(By.ID, "nobody").text


def test_a_name_nobody_claimed_is_not_offered(browser, served):
    base, _ = served
    _book_page(browser, base)
    _wipe_book(browser)
    _book_page(browser, base)
    browser.find_element(By.ID, "add").click()
    box = browser.find_element(By.ID, "find")
    box.clear()
    box.send_keys("nobodyatall")
    browser.find_element(By.ID, "search").click()
    for _ in range(40):
        text = browser.find_element(By.ID, "matches").text
        if text:
            break
        time.sleep(0.25)
    assert "Nothing claimed matching" in text


def test_adding_somebody_keeps_them_across_a_reload(browser, served):
    """The point of the book: it is still there tomorrow, and it is here."""
    base, _ = served
    _book_page(browser, base)
    _wipe_book(browser)
    _book_page(browser, base)
    browser.find_element(By.ID, "add").click()
    box = browser.find_element(By.ID, "find")
    box.clear()
    box.send_keys("robin")
    browser.find_element(By.ID, "search").click()
    for _ in range(40):
        buttons = browser.find_elements(By.CSS_SELECTOR, "#matches button")
        if buttons:
            break
        time.sleep(0.25)
    assert buttons, "a claimed name should be offered"
    buttons[0].click()

    for _ in range(40):
        cards = browser.find_elements(By.CSS_SELECTOR, "#book .person")
        if cards:
            break
        time.sleep(0.25)
    assert cards and "@robin" in cards[0].text

    # And again from cold, which is the only version of this that matters.
    _book_page(browser, base)
    cards = browser.find_elements(By.CSS_SELECTOR, "#book .person")
    assert len(cards) == 1 and "@robin" in cards[0].text
    assert not browser.find_element(By.ID, "nobody").is_displayed()
    # What was saved is what the chain said, key included: Message is live.
    assert "off" not in cards[0].find_element(By.CSS_SELECTOR, "a.q-msg").get_attribute("class")
    # And the name opens their profile (2026-10-08).
    assert cards[0].find_element(By.CSS_SELECTOR, "a").get_attribute("href").endswith("/u/robin")


def test_the_book_is_in_the_browser_and_not_on_the_node(browser, served):
    """Nothing about who is in it ever reaches the server."""
    base, state = served
    _add_robin(browser, base)
    stored = browser.execute_async_script("""
        const done = arguments[0];
        const open = indexedDB.open("arcade-messages");
        open.onsuccess = () => {
          const db = open.result;
          const all = db.transaction(["book"], "readonly")
                        .objectStore("book").getAll();
          all.onsuccess = () => done(all.result.map(e => e.tag));
          all.onerror = () => done(["error"]);
        };
        open.onerror = () => done(["error"]);""")
    assert stored == ["robin"]
    # The node's own files say nothing about it.
    hits = [p for p in state.home.rglob("*")
            if p.is_file() and b"robin" in p.read_bytes()]
    assert not hits, f"the node wrote a contact down: {hits}"


def test_writing_to_somebody_arrives_with_them_already_chosen(browser, served):
    """Message opens the conversation with them, not a blank page with a
    name to type back in."""
    base, _ = served
    _add_robin(browser, base)
    link = browser.find_element(By.CSS_SELECTOR, "#book .person a.q-msg")
    assert link.get_attribute("href").endswith("/me/messages?to=robin")
    link.click()
    browser.execute_script(PRETEND)
    for _ in range(80):
        if browser.execute_script(
                "return document.body.dataset.messagesReady === 'yes'"):
            break
        time.sleep(0.25)
    for _ in range(40):
        if browser.find_element(By.ID, "convo-name").text == "@robin":
            break
        time.sleep(0.25)
    assert browser.find_element(By.ID, "convo-name").text == "@robin"


def test_removing_somebody_removes_them(browser, served):
    """Removed from their profile now: Saved is the button that undoes it
    (2026-10-08, the address book redesign)."""
    base, _ = served
    _add_robin(browser, base)
    browser.get(f"{base}/u/robin")
    browser.execute_script(PRETEND)
    for _ in range(80):
        save = browser.find_elements(By.ID, "save-them")
        if save and "Saved" in save[0].text:
            break
        time.sleep(0.25)
    save[0].click()
    # The page asks with its own card (arcadeAsk): its first button is the yes.
    for _ in range(40):
        yes = browser.find_elements(By.CSS_SELECTOR, ".askcard button")
        if yes:
            yes[0].click()
            break
        time.sleep(0.25)
    _book_page(browser, base)
    for _ in range(40):
        if browser.find_element(By.ID, "nobody").is_displayed():
            break
        time.sleep(0.25)
    assert browser.find_element(By.ID, "nobody").is_displayed()
    assert not browser.find_elements(By.CSS_SELECTOR, "#book .person")
