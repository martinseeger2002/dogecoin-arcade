"""arcade.contacts.add / remove, in a real browser (2026-10-21, asked for by ASHVALE: "able to add in
game friends", game-agnostic).

An inscribed page in its sandboxed frame asks the viewer around it; the viewer shows the reader a card
naming the game and the change, and only a tap on it writes -- into the same IndexedDB book the Address
book page keeps, through the same messaging.js calls. No chain: `/account/who/...` is answered in the
viewer by WHO below, which stands in for the chain and nothing else.
"""

import json
import pathlib
import sys
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")
from selenium.webdriver.common.by import By                          # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                       # noqa: E402
from test_claim_browser import _free_port                            # noqa: E402
from test_contacts_browser import _signed_in, _wipe_book             # noqa: E402
from test_realtime_rejoin_browser import Server                      # noqa: E402
from test_realtime_web import make_state                             # noqa: E402
from test_trade_browser import _inscribe                             # noqa: E402

PAGE_ID = "c0" * 32


def _addr(tag: str) -> str:
    """A made-up address of a real address's length (34), so the door reads it as an address."""
    return ("n" + tag + "Addr" + "x" * 34)[:34]


ROBIN = _addr("robin")
#: `/account/who/<tag or address>` as the chain would answer for a claimed name with a published key.
WHO = """
const real = window.fetch.bind(window);
const addr = (t) => ("n" + t + "Addr" + "x".repeat(34)).slice(0, 34);
window.fetch = function (url, opts) {
  const where = String(url);
  if (where.startsWith("/account/who/")) {
    const asked = decodeURIComponent(where.slice("/account/who/".length)).replace(/^@/, "");
    const tag = asked.length > 24 ? asked.slice(1, asked.indexOf("Addr")) : asked.toLowerCase();
    return Promise.resolve(new Response(JSON.stringify({
      tag, address: addr(tag), key: "aa".repeat(32), fingerprint: "ffff"}),
      {status: 200, headers: {"Content-Type": "application/json"}}));
  }
  return real.apply(null, arguments);
};
"""
#: The game: one input and one button. The test writes a job into the input and clicks; the answer is
#: written into the page's own DOM, which is the one thing a WebDriver script and the page both see.
PAGE = b"""<!doctype html><meta charset="utf-8"><title>Friends</title><body>
<script src="/r/contacts.js"></script>
<input id="job"><button id="go">go</button><div id="out"></div>
<script>
document.getElementById('go').onclick = function () {
  var job = JSON.parse(document.getElementById('job').value);
  var call = job.op === 'list' ? arcade.contacts.list()
           : job.op === 'add' ? arcade.contacts.add(job.arg) : arcade.contacts.remove(job.arg);
  call.then(function (r) { done(job.slot, r && r.why ? {value: r, why: r.why} : {value: r}); },
            function (e) { done(job.slot, {error: String(e.message || e)}); });
};
function done(slot, said) {
  var p = document.createElement('pre'); p.id = 'r-' + slot; p.textContent = JSON.stringify(said);
  document.getElementById('out').appendChild(p);
}
</script>"""


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    yield driver
    driver.quit()


@pytest.fixture
def base(tmp_path, no_nodes):
    from arcade.web.app import create_app

    state = make_state(tmp_path, "node")
    _inscribe(state, PAGE_ID, 1, "n" + "G" * 33, "text/html", PAGE)
    port = _free_port()
    server = Server(create_app(state), port)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()


def _view(browser, base):
    browsers.allow_wallet(browser, base, PAGE_ID)
    browser.get(f"{base}/inscriptions/{PAGE_ID}/view")
    browser.execute_script(WHO)
    frame = browser.find_element(By.CSS_SELECTOR, ".inscription-frame")
    browser.switch_to.frame(frame)
    for _ in range(120):
        if browser.execute_script("return !!document.getElementById('go')"):
            break
        time.sleep(0.25)
    browser.switch_to.default_content()
    return frame


def _ask(browser, frame, slot, op, arg=None):
    """Start a call in the game; the answer is read later with `_answer`."""
    browser.switch_to.frame(frame)
    browser.execute_script("document.getElementById('job').value = arguments[0];"
                           "document.getElementById('go').click();",
                           json.dumps({"slot": slot, "op": op, "arg": arg}))
    browser.switch_to.default_content()


def _answer(browser, frame, slot, timeout=30):
    browser.switch_to.frame(frame)
    try:
        end = time.time() + timeout
        while time.time() < end:
            got = browser.execute_script(
                "var p = document.getElementById('r-' + arguments[0]); return p ? p.textContent : null;", slot)
            if got is not None:
                return json.loads(got)
            time.sleep(0.2)
        raise AssertionError(f"the game never heard back about {slot}")
    finally:
        browser.switch_to.default_content()


def _card(browser, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        cards = [c for c in browser.find_elements(By.CSS_SELECTOR, ".askcard") if c.is_displayed()]
        if cards:
            return cards[0]
        time.sleep(0.2)
    raise AssertionError("no card was shown to the reader")


def _cards(browser):
    return [c for c in browser.find_elements(By.CSS_SELECTOR, ".askcard") if c.is_displayed()]


def _press(card, label):
    button = next(b for b in card.find_elements(By.TAG_NAME, "button") if b.text.strip() == label)
    for _ in range(40):                       # over a game the yes is armed after a moment (2026-10-26, issue #7)
        if button.is_enabled():
            break
        time.sleep(0.1)
    button.click()


def _book(browser):
    return browser.execute_async_script("""
        const done = arguments[0];
        import('/messaging.js').then((m) => m.book()).then(
          (all) => done(all.map((e) => ({tag: e.tag, address: e.address, note: e.note || ''}))),
          (e) => done(String(e)));""")


def test_a_game_adds_and_removes_a_contact_only_with_the_readers_yes(browser, base):
    _signed_in(browser, base)
    assert _wipe_book(browser) == "ok"
    frame = _view(browser, base)

    # Add: a card naming the change; Add writes the ordinary contact, the name as the private note.
    _ask(browser, frame, "a1", "add", {"address": "@Robin", "name": "  Robin the Bold  "})
    card = _card(browser)
    assert "Add @robin (nrobinAddr" in card.text and "to your address book as “Robin the Bold”?" in card.text, card.text
    assert "This page asks to add them" in card.text, card.text
    assert sorted(b.text.strip() for b in card.find_elements(By.TAG_NAME, "button")) == ["Add", "No"]
    _press(card, "Add")
    assert _answer(browser, frame, "a1") == {"value": {"ok": True, "status": "added"}}
    assert _book(browser) == [{"tag": "robin", "address": ROBIN, "note": "Robin the Bold"}]

    # Again, by tag or by address: already there, no card.
    _ask(browser, frame, "a2", "add", {"address": "robin"})
    assert _answer(browser, frame, "a2") == {"value": {"ok": True, "status": "already"}}
    _ask(browser, frame, "a3", "add", {"address": ROBIN})
    assert _answer(browser, frame, "a3") == {"value": {"ok": True, "status": "already"}}
    assert not _cards(browser), "nothing to ask about, so nothing asked"

    # Approving an add is not permission to read the book.
    assert browser.execute_script("return localStorage.getItem('arcade.contacts.allow:' + arguments[0])",
                                  PAGE_ID) is None

    # One card at a time: a second ask while one is open is refused "busy"; No is "declined".
    _ask(browser, frame, "b1", "add", {"address": "@gx1"})
    card = _card(browser)
    _ask(browser, frame, "b2", "add", {"address": "@postmaster"})
    assert _answer(browser, frame, "b2")["error"].startswith("busy"), "a second card stacked on the first"
    assert len(_cards(browser)) == 1
    _press(card, "No")
    assert _answer(browser, frame, "b1") == {"error": "declined"}
    assert [e["tag"] for e in _book(browser)] == ["robin"], "a no writes nothing"

    # Not an address at all: refused before anything is asked.
    _ask(browser, frame, "c1", "add", {"address": "not an address!"})
    assert _answer(browser, frame, "c1")["error"].startswith("not an address")

    # Remove: its own card, then gone; asked again, absent with no card.
    _ask(browser, frame, "d1", "remove", "@robin")
    card = _card(browser)
    assert "Remove @robin from your address book?" in card.text, card.text
    _press(card, "No")
    assert _answer(browser, frame, "d1") == {"error": "declined"}
    assert [e["tag"] for e in _book(browser)] == ["robin"]
    _ask(browser, frame, "d2", "remove", ROBIN)
    _press(_card(browser), "Remove")
    assert _answer(browser, frame, "d2") == {"value": {"ok": True, "status": "removed"}}
    assert _book(browser) == []
    _ask(browser, frame, "d3", "remove", "@robin")
    assert _answer(browser, frame, "d3") == {"value": {"ok": True, "status": "absent"}}
    assert not _cards(browser)

    # By address this time: resolved to the name it holds, as the Address book would; and the Address
    # book page shows what the game added as an ordinary contact.
    _ask(browser, frame, "e1", "add", {"address": ROBIN})
    card = _card(browser)
    assert "Add @robin (nrobinAddr\u2026) to your address book?" in card.text, card.text
    _press(card, "Add")
    assert _answer(browser, frame, "e1")["value"]["status"] == "added"
    browser.get(f"{base}/me/contacts")
    for _ in range(80):
        if browser.execute_script("return document.body.dataset.contactsReady === 'yes'"):
            break
        time.sleep(0.25)
    people = [p.text for p in browser.find_elements(By.CSS_SELECTOR, "#book .person")]
    assert any("robin" in p for p in people), people


def test_a_guest_and_a_page_on_its_own_are_refused_with_a_reason(browser, base):
    browser.get(f"{base}/")
    browser.delete_all_cookies()
    frame = _view(browser, base)
    _ask(browser, frame, "g1", "add", {"address": "@robin"})
    assert _answer(browser, frame, "g1")["error"].startswith("sign in first")
    _ask(browser, frame, "g2", "remove", "@robin")
    assert _answer(browser, frame, "g2")["error"].startswith("sign in first")
    assert not _cards(browser)

    # The page opened on its own: nobody around it to ask.
    src = frame.get_attribute("src")
    browser.get(src)
    for _ in range(80):
        if browser.execute_script("return !!document.getElementById('go')"):
            break
        time.sleep(0.25)
    browser.execute_script("document.getElementById('job').value = arguments[0];"
                           "document.getElementById('go').click();",
                           json.dumps({"slot": "n1", "op": "add", "arg": {"address": "@robin"}}))
    end = time.time() + 15
    got = None
    while time.time() < end and got is None:
        got = browser.execute_script("var p = document.getElementById('r-n1'); return p ? p.textContent : null;")
        time.sleep(0.2)
    assert got is not None and json.loads(got)["error"].startswith("no viewer"), got


@pytest.fixture
def card_base(tmp_path, no_nodes):
    """The same page, now an app with its own card that says it reads contacts."""
    from arcade.web.app import create_app

    state = make_state(tmp_path, "node")
    _inscribe(state, PAGE_ID, 1, "n" + "G" * 33, "text/html", PAGE, {"app": {"name": "Friends", "contacts": True}})
    port = _free_port()
    server = Server(create_app(state), port)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()


def test_an_apps_contacts_switch_decides_and_an_empty_list_says_why(browser, card_base):
    """2026-10-23: "Allow access to Contacts should be another box on the card". An app with a card is never
    asked in the page: off is an empty list that says 'off' (an old 'no' is only the switch off), on is the book."""
    base = card_base
    _signed_in(browser, base)
    assert _wipe_book(browser) == "ok"
    frame = _view(browser, base)
    _ask(browser, frame, "l1", "list")
    assert _answer(browser, frame, "l1") == {"value": [], "why": "off"}
    assert not _cards(browser), "nothing asked: the card's switch decides"
    browser.switch_to.default_content()
    assert browser.execute_script("return localStorage.getItem('arcade.contacts.uses:' + arguments[0])", PAGE_ID) \
        == "yes", "its card shows the switch from now on"
    browser.execute_script("localStorage.setItem('arcade.contacts.allow:' + arguments[0], 'no')", PAGE_ID)
    _ask(browser, frame, "l2", "list")
    assert _answer(browser, frame, "l2")["why"] == "off", "an old no is the switch off, not a wall"
    # On (as the card's switch keeps it): the book.
    browser.switch_to.default_content()
    browser.execute_script("localStorage.setItem('arcade.contacts.allow:' + arguments[0], 'yes')", PAGE_ID)
    _ask(browser, frame, "a1", "add", {"address": "@Robin"})
    _press(_card(browser), "Add")
    assert _answer(browser, frame, "a1")["value"]["status"] == "added"
    _ask(browser, frame, "l3", "list")
    said = _answer(browser, frame, "l3")
    assert [c["tag"] for c in said["value"]] == ["robin"] and "why" not in said, said
