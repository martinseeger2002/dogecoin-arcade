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
    yield f"http://127.0.0.1:{port}", state
    server.should_exit = True
    thread.join(timeout=5)


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
    answer = browser.execute_async_script("""
        const done = arguments[1];
        import("/signin.js").then(m => m.signIn(arguments[0], {join: true}))
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


def test_the_book_starts_empty_and_says_so(browser, served):
    base, _ = served
    _signed_in(browser, base)
    _book_page(browser, base)
    assert browser.find_element(By.ID, "nobody").is_displayed()
    assert "empty" in browser.find_element(By.ID, "nobody").text


def test_a_name_nobody_claimed_is_not_offered(browser, served):
    base, _ = served
    _book_page(browser, base)
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
        cards = browser.find_elements(By.CSS_SELECTOR, "#book .card")
        if cards:
            break
        time.sleep(0.25)
    assert cards and "@robin" in cards[0].text

    # And again from cold, which is the only version of this that matters.
    _book_page(browser, base)
    cards = browser.find_elements(By.CSS_SELECTOR, "#book .card")
    assert len(cards) == 1 and "@robin" in cards[0].text
    assert not browser.find_element(By.ID, "nobody").is_displayed()
    # What was saved is what the chain said, address and key both.
    assert "messaging" in cards[0].text


def test_the_book_is_in_the_browser_and_not_on_the_node(browser, served):
    """Nothing about who is in it ever reaches the server."""
    base, state = served
    _book_page(browser, base)
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
    base, _ = served
    _book_page(browser, base)
    link = browser.find_element(By.CSS_SELECTOR, "#book .card a.btn")
    assert link.get_attribute("href").endswith("/me/messages?to=robin")
    link.click()
    for _ in range(80):
        if browser.execute_script(
                "return document.body.dataset.messagesReady === 'yes'"):
            break
        time.sleep(0.25)
    assert browser.find_element(By.ID, "to").get_attribute("value") == "@robin"


def test_removing_somebody_removes_them(browser, served):
    base, _ = served
    _book_page(browser, base)
    browser.execute_script("window.confirm = () => true;")
    buttons = [b for b in browser.find_elements(By.CSS_SELECTOR, "#book button")
               if b.text == "Remove"]
    assert buttons
    buttons[0].click()
    for _ in range(40):
        if browser.find_element(By.ID, "nobody").is_displayed():
            break
        time.sleep(0.25)
    assert browser.find_element(By.ID, "nobody").is_displayed()
    assert not browser.find_elements(By.CSS_SELECTOR, "#book .card")
