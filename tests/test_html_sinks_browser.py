"""Words other people choose are shown as words (2026-11-09, security review of the pages' HTML).

An arcade's own origin holds what an unlocked wallet keeps for the session, so a page of it must never turn somebody
else's text into markup: a token's name, a peer's @tag, a post's tag, a file's name, a label an inscribed page gave
itself, a contact's name. Each test below puts `<img src=x onerror=...>` where that text goes and checks that no
element was made and nothing ran. A page that is built from what a stranger wrote -- the launchpad and mintpad
previews -- is served sandboxed instead, as it will be once it is inscribed.

What other arcades and the feed would say is said by stand-in routes put in front of the real ones (`FAKE`).
"""

import pathlib
import sys
import time
from urllib.parse import urlencode

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

from fastapi import Request                                          # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse            # noqa: E402
from selenium.webdriver.common.by import By                          # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                      # noqa: E402
from test_claim_browser import _free_port                          # noqa: E402
from test_collections import hashlips                               # noqa: E402
from test_realtime_rejoin_browser import Server                     # noqa: E402
from test_realtime_web import make_state                            # noqa: E402

#: Markup that runs if it is ever parsed: it sets `window.ran`.
SNEAKY = '<img src=x onerror="window.ran=1">'

#: What the stand-in routes answer, set by each test: path -> JSON, or HTML for a page of the test's own.
FAKE: dict = {}


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    import os
    from arcade.web.app import SCRIPT_POLICY_ENV, create_app

    # These tests drive the pages' own functions from outside (a stand-in page's inline script, window.eval): they
    # check escaping, not the script policy, which would rightly stop that harness. So they run with it report-only,
    # even in a suite run that enforces it (ARCADE_ENFORCE_SCRIPT_POLICY=1); tests/test_script_policy*.py cover it.
    held = os.environ.pop(SCRIPT_POLICY_ENV, None)

    home = tmp_path_factory.mktemp("sinks")
    state = make_state(home, "node")
    app = create_app(state)

    def stand_in(request: Request):
        said = FAKE.get(request.url.path)
        if isinstance(said, str):
            return HTMLResponse(said)
        return JSONResponse(said if said is not None else {"detail": "not faked"}, status_code=200 if said else 404)

    # In front of the real routes, so the page asks the arcade as it always does and hears the test.
    for path in ("/dogefeedx/network/api/status", "/dogefeedx/api/sidebar", "/approvals/waiting", "/sinks-test"):
        app.add_api_route(path, stand_in, methods=["GET"])
        app.router.routes.insert(0, app.router.routes.pop())
    port = _free_port()
    server = Server(app, port)
    yield state, f"http://127.0.0.1:{port}", home
    server.stop()
    if held is not None:
        os.environ[SCRIPT_POLICY_ENV] = held


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    driver.set_script_timeout(60)
    yield driver
    driver.quit()


@pytest.fixture(autouse=True)
def nothing_faked(browser):
    FAKE.clear()
    yield
    FAKE.clear()
    browser.delete_all_cookies()                    # an account signed in by one test is not the next one's reader


def _until(browser, script, what, seconds=20.0, *args):
    end = time.time() + seconds
    while time.time() < end:
        if browser.execute_script(script, *args):
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}: the page says "
                         f"{browser.execute_script('return document.body && document.body.innerText.slice(0, 400)')!r}")


def _text(browser, css):
    return browser.execute_script("const e = document.querySelector(arguments[0]); return e && e.textContent;", css)


def _nothing_ran(browser, where):
    """No element came out of SNEAKY inside `where`, and its handler never ran."""
    time.sleep(0.3)                                 # an <img src=x> fails at once; give its onerror a moment
    assert browser.execute_script("return window.ran") is None, "the markup ran"
    assert browser.execute_script("return document.querySelectorAll(arguments[0] + ' img[src=x]').length",
                                  where) == 0, "the words became an element"


# --- the wizards' previews: built from the query string, served sandboxed ---------------------------------------------

def _stored_by_the_preview(browser, base):
    """What the preview managed to put in the arcade's own storage: nothing, once it runs in an opaque origin."""
    browser.get(f"{base}/r/realtime.js")
    return browser.execute_script("return localStorage.getItem('pwned')")


def test_a_launchpad_preview_opened_on_its_own_cannot_reach_the_arcades_storage(served, browser):
    import httpx

    _, base, _ = served
    icon = "';localStorage.setItem('pwned','launchpad');//"
    browser.get(f"{base}/tokens/launchpad/preview?" + urlencode(
        {"name": "Goofcoin", "lot": "100", "price": "2 coins", "icon": icon}))
    # The page still works: its own script says on the button that it is a preview.
    _until(browser, "const b = document.getElementById('buy'); return b && b.textContent.includes('not on the chain')",
           "the preview's own script")
    said = httpx.get(f"{base}/tokens/launchpad/preview", params={"name": "Goofcoin"})
    assert said.headers.get("content-security-policy", "").startswith("sandbox")
    assert _stored_by_the_preview(browser, base) is None


def test_a_mintpad_preview_opened_on_its_own_cannot_reach_the_arcades_storage_and_still_draws_the_build(
        served, browser):
    _, base, home = served
    build = hashlips(home / "mintpad", count=3)
    # The wall is the build's pictures, read from the preview's own routes: an opaque origin still reaches them.
    browser.get(f"{base}/inscriptions/collection/preview?" + urlencode({"folder": str(build), "name": "Punks"}))
    _until(browser, "return document.querySelectorAll('.tile').length > 0", "the wall of the build's pictures")
    name = "Punks</title><script>localStorage.setItem('pwned','mintpad')</script>"
    browser.get(f"{base}/inscriptions/collection/preview?" + urlencode({"folder": str(build), "name": name}))
    assert _stored_by_the_preview(browser, base) is None


# --- the trade card: a token's name is its issuer's words --------------------------------------------------------------

def test_the_trade_card_shows_the_token_and_the_coin_by_name_as_words(served, browser):
    from arcade.web.app import TEMPLATES

    _, base, _ = served
    FAKE["/sinks-test"] = f"""<!doctype html><meta charset="utf-8">
{TEMPLATES.env.get_template("_exui.html").module.style()}
<div class="ex-trade" id="card">
  <div class="ex-modes"><button type="button" data-mode="limit">Set my price</button>
    <button type="button" data-mode="best">Best price now</button></div>
  <label><input type="radio" name="side" value="bid" checked>Buy</label>
  <label><input type="radio" name="side" value="ask">Sell</label>
  <input id="amount" value="5"><input id="price" value="2">
  <div class="ex-quote"></div><button type="button" id="go"></button>
</div>"""
    browser.get(f"{base}/sinks-test")
    browser.execute_script("""
        window.card = exTrade(document.getElementById('card'), {sideName: 'side', buyValue: 'bid', amount: 'amount',
            price: 'price', button: 'go', asks: [[2, 1]], bids: [], coin: arguments[0], token: arguments[0],
            feePermille: 10});
        window.card.setMode('best');      // 5 asked, 1 offered: "Only 1 <token> is offered"
    """, SNEAKY)
    assert SNEAKY in _text(browser, "#card .ex-quote"), "the names are shown, as the words they are"
    _nothing_ran(browser, "#card")
    browser.execute_script("window.card.setMode('limit')")    # the node fee's row names the coin
    assert "Node fee" in _text(browser, "#card .ex-quote")
    _nothing_ran(browser, "#card")


# --- DogeFeedX: what other arcades say about themselves ----------------------------------------------------------------

def test_the_network_app_shows_what_peers_say_about_themselves_as_words(served, browser):
    _, base, _ = served
    FAKE["/dogefeedx/network/api/status"] = {
        "nodes": [], "timestamp": 0, "trusted_nodes": [{"id": SNEAKY}],
        "mesh_peers": [{"tag": SNEAKY, "node": SNEAKY, "hosts": [SNEAKY], "linked": True}]}
    browser.get(f"{base}/dogefeedx/network")
    _until(browser, "return document.querySelectorAll('.dfx-peer-row').length === 1", "the peer's row")
    assert _text(browser, ".dfx-peer-tag") == "@" + SNEAKY
    assert _text(browser, ".dfx-node-id") == SNEAKY + "..."
    _nothing_ran(browser, "body")


# --- a picked file: its name can be anything -----------------------------------------------------------------------------

def _pick(browser, selector, files):
    """Choose files in an <input type=file> as a person does: [(name, text)], then the change event."""
    browser.execute_script("""
        const input = document.querySelector(arguments[0]), dt = new DataTransfer();
        for (const [name, text] of arguments[1])
          dt.items.add(new File([text], name, {type: name.endsWith('.png') ? 'image/png' : 'text/plain'}));
        input.files = dt.files;
        input.dispatchEvent(new Event('change', {bubbles: true}));
    """, selector, files)


def test_the_inscribe_form_names_the_picked_file_as_words(served, browser):
    _, base, _ = served
    browser.get(f"{base}/inscriptions")
    _pick(browser, "#file", [[SNEAKY + ".txt", "hello"]])
    _until(browser, "return !document.getElementById('priced').hidden", "the price of the file")
    assert _text(browser, "#priced strong") == SNEAKY + ".txt"
    _nothing_ran(browser, "#priced")


def _function(template, name):
    """One top-level function of a template's script, as written: `function name(...){` to its closing brace."""
    text = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates" / template).read_text()
    start = text.index(f"function {name}(")
    return text[start:text.index("\n}\n", start) + 2]


def test_the_messenger_names_the_attached_file_as_words(served, browser):
    _, base, _ = served
    # The note sits under a conversation's compose box, which needs a node and a conversation; the page's own two
    # functions are run instead on a page that has base.html's helpers, with the note's elements put in.
    browser.get(f"{base}/messages/more")
    browser.execute_script("""
        document.body.insertAdjacentHTML('beforeend',
          '<p id="picked" hidden><span id="picked-text"></span></p><span id="clip"></span>');
        window.eval(arguments[0] + arguments[1]);
        drawPicked(new File(['hello'], arguments[2], {type: 'text/plain'}));
    """, _function("messages.html", "estimateFile"), _function("messages.html", "drawPicked"), SNEAKY + ".txt")
    assert _text(browser, "#picked-text strong") == SNEAKY + ".txt"
    _nothing_ran(browser, "#picked")


def _signed_up(browser, state, base):
    browser.get(f"{base}/join")
    made = browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        import('/wallet.js').then((w) => w.signUp(arguments[0], 'a long enough password',
                                                   {network: 'regtest', version: arguments[1]}))
          .then((r) => done({tag: r.tag}), (e) => done({error: String(e.message || e)}));
    """, f"sinks{_free_port() % 100000}", state.messaging.params.pubkeyhash_version)
    assert "error" not in made, made
    browser.get(f"{base}/me/nfts")


def test_an_accounts_inscribe_tab_names_the_picked_file_and_a_broken_json_as_words(served, browser):
    state, base, _ = served
    _signed_up(browser, state, base)
    _pick(browser, "#file", [[SNEAKY + ".txt", "hello"]])
    _until(browser, "return !document.getElementById('picked-file').hidden", "the picked file's note")
    assert _text(browser, "#picked-text strong") == SNEAKY + ".txt"
    _nothing_ran(browser, "#picked-file")
    # A folder whose _metadata.json does not parse: some browsers quote the start of the file in their message.
    _pick(browser, "#build", [["1.png", "PNG"], ["_metadata.json", SNEAKY]])
    _until(browser, "return !document.getElementById('json-check').hidden", "the JSON check")
    assert "does not parse" in _text(browser, "#json-says")
    _nothing_ran(browser, "#json-check")


# --- what a page in the sandbox asked for, under it ----------------------------------------------------------------------

def test_an_approval_waiting_under_a_piece_is_listed_as_words(served, browser):
    from test_trade_browser import _inscribe

    state, base, _ = served
    piece = "ab" * 32
    _inscribe(state, piece, 1, "n" + "G" * 33, "text/plain", b"a piece")
    FAKE["/approvals/waiting"] = {"waiting": 1,
                                  "requests": [{"id": 'x" data-pwned="1', "summary": SNEAKY, "label": SNEAKY}]}
    browser.get(f"{base}/inscriptions/{piece}/view")
    _until(browser, "const a = document.getElementById('asked'); return a && !a.hidden", "the waiting approval")
    assert SNEAKY in _text(browser, "#asked")
    assert browser.execute_script("return document.querySelectorAll('#asked [data-pwned]').length") == 0, \
        "an id stays inside its attribute"
    _nothing_ran(browser, "#asked")


# --- the address book: a contact's name in the question before removing it ---------------------------------------------

def test_removing_a_contact_asks_about_its_name_as_words(served, browser):
    state, base, _ = served
    name = "Bob '+(window.ran=1)+' " + SNEAKY
    with state.store() as store:
        store.save_contact(name=name)
    browser.get(f"{base}/contacts/more")
    browser.find_element(By.CSS_SELECTOR, "form[action$='/delete'] button[type=submit]").click()
    alert = browser.switch_to.alert
    asked = alert.text
    alert.dismiss()
    assert name in asked, asked
    _nothing_ran(browser, "body")
