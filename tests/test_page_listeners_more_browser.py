"""The controls whose inline on...= handlers moved into the pages' nonced scripts (script policy phase 2, 2026-11-09),
driven in Firefox with the script policy ENFORCED rather than report-only: a control still written as an inline
handler does nothing here, so each test below fails the moment one comes back.

The pages: the Messenger (/messages/<peer>, /messages), the operator's Overview and Your arcade, the offline page, the
printed keys, Sell, a swap market's "priced in", the Wallet's tokens and NFTs, and DogeFeedX's feed, post, profile,
inscription, wallet, collections and NFTs. Pages that need a chain's data to draw a control are drawn with that data
handed to the template (`DRAWN`), so the route, the page and its script are the real ones."""

import contextlib
import pathlib
import socket
import sys
import threading
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                  # noqa: E402

from selenium.webdriver.common.by import By                      # noqa: E402
from selenium.webdriver.common.keys import Keys                  # noqa: E402

PEER = b"\x66" * 32
ADDRESS_A, ADDRESS_B = "nTokenPieceAddressAAAAAAAAAAAAAAAAA", "nTokenPieceAddressBBBBBBBBBBBBBBBBB"
PIECE = "ab" * 32

#: What a template is drawn with on top of what its route gave it: the data a page needs to show the control under
#: test, which a node-less arcade does not have.
DRAWN = {
    "wallet_tokens.html": {
        "node_error": None,
        "purses": [{"property_id": 7, "name": "Goofcoin", "test_ecosystem": False, "display": "3",
                    "pieces": [{"address": ADDRESS_A, "display": "1"}, {"address": ADDRESS_B, "display": "2"}]}],
    },
    "wallet_nfts.html": {
        "node_error": None, "my_total": 1, "tags": {}, "listed": {},
        "mine": [{"txid": PIECE, "number": 42, "held": True, "content_type": "image/png", "contenttype": "",
                  "content_len": 68, "owner": ADDRESS_A, "block_height": 100, "collection": None}],
    },
    "sell.html": {"tokens": [{"property_id": 7, "name": "Goofcoin"}], "kind": "coins"},
    # The trade panel is an account's (an operator takes from the book).
    "swap_pair.html": {"open_here": True, "viewer": "account", "can_list_orders": True,
                       "quotes": [{"key": "doge-coin", "label": "DOGE", "on": True},
                                  {"key": "main-coin", "label": "PEP", "on": False}]},
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Enforced:
    """Every answer's report-only script policy, enforced: what phase 3 will do to the pages."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def sending(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [
                    (b"content-security-policy" if k.lower() == b"content-security-policy-report-only" else k, v)
                    for k, v in message["headers"]]}
            await send(message)
        await self.app(scope, receive, sending)


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    yield driver
    driver.quit()


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    import uvicorn
    from arcade import backup
    from arcade.messaging.keys import Identity
    from arcade.web.app import TEMPLATES, create_app
    from arcade.web.state import AppState, ChainContext

    state = AppState(
        home=tmp_path_factory.mktemp("listeners"),
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                               datadir=pathlib.Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=pathlib.Path("/nonexistent")),
    )
    state.identity = Identity.generate()
    with state.store() as store:
        store.add_message(None, "tx", "tx", 1, 0, "nThem", PEER, state.identity.fingerprint, b"a message")

    real = TEMPLATES.TemplateResponse

    def drawn(request, name, context, *args, **kwargs):
        return real(request, name, {**context, **DRAWN.get(name, {})}, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(TEMPLATES, "TemplateResponse", drawn)
        # The printed keys: a wallet of two, read from no node.
        patch.setattr(backup, "private_keys", lambda rpc: [
            backup.PrintableKey(ADDRESS_A, "cPrivateKeyNumberOne", "1"),
            backup.PrintableKey(ADDRESS_B, "cPrivateKeyNumberTwo", "2")])
        patch.setattr(state.messaging, "rpc", lambda: contextlib.nullcontext(None))
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(_Enforced(create_app(state)), host="127.0.0.1", port=port,
                                               log_level="error", timeout_graceful_shutdown=1.0))
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


def _until(browser, script, *args, timeout=10.0):
    end = time.time() + timeout
    while True:
        got = browser.execute_script(script, *args)
        if got or time.time() > end:
            return got
        time.sleep(0.1)


def _open(browser, url):
    browser.get(url)
    _until(browser, "return document.readyState === 'complete'")
    assert browser.execute_script("return !!document.querySelector('script[nonce]')"), "no page here: " + url
    return browser


def _clipboard_spy(browser):
    browser.execute_script("""
        window.__copied = [];
        Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
          writeText: function (t) { window.__copied.push(t); return Promise.resolve(); }}});""")


def test_the_policy_is_enforced_here(browser, served):
    """The check under every other test: an inline handler written into one of these pages does not run."""
    base, _ = served
    _open(browser, f"{base}/offline")
    browser.execute_script("document.body.insertAdjacentHTML('beforeend',"
                           " '<button id=\"inline\" onclick=\"window.__ran = 1\">x</button>')")
    browser.find_element(By.ID, "inline").click()
    time.sleep(0.2)
    assert browser.execute_script("return window.__ran") is None, "the script policy is not enforced in this test"


def test_the_offline_page_tries_again(browser, served):
    base, _ = served
    _open(browser, f"{base}/offline")
    browser.execute_script("window.__before = 1")
    browser.find_element(By.ID, "again").click()
    assert _until(browser, "return window.__before === undefined && document.readyState === 'complete'"), \
        "Try again reloads the page"


def test_the_messenger_s_controls(browser, served):
    """New and Cancel, the box that grows, Enter that sends once, a file picked and removed, a picture opened."""
    base, _ = served
    _open(browser, f"{base}/messages/{PEER.hex()}")
    newconv = "return document.getElementById('newconv').hidden"
    assert browser.execute_script(newconv) is True
    browser.find_element(By.ID, "newconv-open").click()
    assert browser.execute_script(newconv) is False, "New shows the form"
    browser.find_element(By.ID, "newconv-cancel").click()
    assert browser.execute_script(newconv) is True, "Cancel hides it"

    box = browser.find_element(By.ID, "composer-body")
    flat = box.size["height"]
    box.send_keys("one", Keys.SHIFT + Keys.ENTER, "two", Keys.SHIFT + Keys.ENTER, "three", Keys.SHIFT + Keys.ENTER,
                  "four")
    assert box.size["height"] > flat, "the box grows with what is written"

    # A file picked shows what it costs; Remove forgets it.
    browser.execute_script("""
        const input = document.getElementById('attachment'), dt = new DataTransfer();
        dt.items.add(new File(['x'.repeat(9000)], 'notes.txt', {type: 'text/plain'}));
        input.files = dt.files;
        input.dispatchEvent(new Event('change'));""")
    assert _until(browser, "return !document.getElementById('picked').hidden"), "a picked file is priced"
    browser.find_element(By.ID, "picked-clear").click()
    assert _until(browser, "return document.getElementById('picked').hidden"
                           " && !document.getElementById('attachment').files.length"), "Remove forgets the file"

    # A picture opens over the page: the anchor as the template writes a received one.
    browser.execute_script("""
        const row = document.createElement('div');
        row.className = 'bubble-row theirs';
        row.innerHTML = '<div class="bubble"><a class="media" id="shot" href="/x?download=1"' +
          ' data-open-image="/x" data-name="cat.jpg"><img alt="cat.jpg"' +
          ' src="data:image/gif;base64,R0lGODlhAQABAAAAACw="></a></div>';
        document.getElementById('bubbles').appendChild(row);""")
    browser.find_element(By.ID, "shot").click()
    assert _until(browser, "const b = document.getElementById('lightbox'); return b && b.classList.contains('on')"
                           " && b.querySelector('.lightbox-name').textContent === 'cat.jpg'"
                           " && b.querySelector('.lightbox-save').getAttribute('href') === '/x?download=1'")
    assert browser.execute_script("return location.pathname") == f"/messages/{PEER.hex()}", \
        "the click opened the picture, not the download"
    browser.execute_script("document.getElementById('lightbox').classList.remove('on')")

    # Enter sends, once: the form's listener paints the send and a second submission is refused.
    browser.execute_script("""
        window.__sent = [];
        window.addEventListener('submit', function (e) {
          window.__sent.push([e.target.dataset.sending || '', e.defaultPrevented]);
          e.preventDefault();
        });""")
    box = browser.find_element(By.ID, "composer-body")
    box.clear()
    box.send_keys("hello", Keys.ENTER)
    assert _until(browser, "return window.__sent.length === 1"), "Enter submits the composer"
    box.send_keys(Keys.ENTER)
    browser.execute_script("document.querySelector('form.composer').requestSubmit()")
    sent = _until(browser, "return window.__sent.length === 2 && window.__sent")
    assert sent == [["yes", False], ["yes", True]], f"one send goes, the second is refused: {sent}"
    assert browser.find_element(By.CSS_SELECTOR, "form.composer button[type=submit]").text == "Sending…"


def test_the_account_messenger_s_forms_never_post_themselves(browser, served):
    """Enter in the one field of New stays on the page; the buttons act, not the form."""
    base, _ = served
    _open(browser, f"{base}/messages")
    assert _until(browser, "return document.body.dataset.messagesReady === 'yes'", timeout=20)
    browser.execute_script("window.__before = 1; document.getElementById('newconv').hidden = false")
    browser.find_element(By.ID, "who-new").send_keys("@nobody", Keys.ENTER)
    time.sleep(0.5)
    assert browser.execute_script("return window.__before") == 1, "the form posted and the page reloaded"
    assert browser.execute_script(
        "return [...document.querySelectorAll('form[data-no-submit]')].map((f) => f.id).sort()") == \
        ["composer", "newconv", "newgroup"]


def test_the_overview_copies_the_address_and_the_contact_code(browser, served):
    base, _ = served
    _open(browser, f"{base}/overview")
    _clipboard_spy(browser)
    mine = browser.find_element(By.ID, "myaddr").text.strip()
    button = browser.find_element(By.CSS_SELECTOR, "[data-copy='myaddr']")
    button.click()
    assert _until(browser, "return arguments[0].textContent === 'Copied'", button)
    browser.execute_script("document.querySelector('#contact').closest('details').open = true")
    browser.find_element(By.CSS_SELECTOR, "[data-copy='contact']").click()
    code = browser.find_element(By.ID, "contact").text.strip()
    assert _until(browser, "return window.__copied.length === 2 && window.__copied") == [mine, code]


def test_your_arcade_s_letter_leads_to_the_picture_field(browser, served):
    base, _ = served
    _open(browser, f"{base}/")
    # The panel is folded away until the wallet is open, so where the page scrolls to is what is asked for; the first
    # run's setup card would take the click.
    browser.execute_script("const card = document.getElementById('setupwizard'); if (card) card.remove();"
                           " window.__went = [];"
                           " Element.prototype.scrollIntoView = function (how) { window.__went.push([this.id, how]); };")
    browser.find_element(By.ID, "me-letter").click()
    assert _until(browser, "return window.__went.length && window.__went") == [["profile-edit", {"behavior": "smooth"}]], \
        "the @ leads to Edit profile"


def test_the_printed_keys_page_prints(browser, served):
    base, state = served
    _open(browser, f"{base}/offline")
    browser.execute_script("""
        const f = document.createElement('form');
        f.method = 'post'; f.action = '/backup/messaging/print';
        for (const [k, v] of [['csrf_token', arguments[0]], ['understand', 'yes']]) {
          const i = document.createElement('input'); i.type = 'hidden'; i.name = k; i.value = v; f.appendChild(i);
        }
        document.body.appendChild(f); f.submit();""", state.csrf_token)
    assert _until(browser, "return !!document.getElementById('print-keys')"), browser.page_source[:400]
    assert "cPrivateKeyNumberOne" in browser.page_source
    browser.execute_script("window.print = function () { window.__printed = (window.__printed || 0) + 1; }")
    browser.find_element(By.ID, "print-keys").click()
    assert _until(browser, "return window.__printed === 1")


def test_sell_shows_the_token_picker_for_a_token_price(browser, served):
    base, state = served
    from arcade.db import Database
    from test_collection_web import index_with_a_collection
    index_with_a_collection(state.home)
    db = Database(state.home / "main-ledger.sqlite")
    piece = db.conn.execute("SELECT txid FROM inscription ORDER BY number LIMIT 1").fetchone()[0]
    db.close()
    (state.home / "tokens-chain").write_text("main\n")
    state._token_chain = None
    _open(browser, f"{base}/exchange/sell/{piece}")
    picker = "return document.getElementById('sell-token').hidden"
    assert browser.execute_script(picker) is True
    kind = browser.find_element(By.ID, "kind")
    kind.find_element(By.CSS_SELECTOR, "option[value='token']").click()
    assert browser.execute_script(picker) is False, "a token price asks which token"
    kind.find_element(By.CSS_SELECTOR, "option[value='coins']").click()
    assert browser.execute_script(picker) is True


def test_a_swap_market_opens_the_market_priced_in_another_coin(browser, served):
    base, _ = served
    _open(browser, f"{base}/exchange/swap/regtest-coin/doge-coin")
    pick = browser.find_element(By.CSS_SELECTOR, ".ex-quote-pick")
    assert pick.get_attribute("data-market") == "/exchange/swap/regtest-coin/"
    pick.find_element(By.CSS_SELECTOR, "option[value='main-coin']").click()
    assert _until(browser, "return location.pathname === '/exchange/swap/regtest-coin/main-coin'"), \
        browser.current_url


def test_the_wallet_s_token_addresses_copy_and_its_pictures_open(browser, served):
    base, _ = served
    _open(browser, f"{base}/wallet/tokens")
    _clipboard_spy(browser)
    browser.execute_script("document.querySelector('[data-copy-addr]').closest('details').open = true")
    pieces = browser.find_elements(By.CSS_SELECTOR, "code[data-copy-addr]")
    assert [p.text for p in pieces] == [ADDRESS_A, ADDRESS_B]
    pieces[1].click()
    assert _until(browser, "return window.__copied.length === 1 && window.__copied[0]") == ADDRESS_B
    assert _until(browser, "return arguments[0].classList.contains('copied')", pieces[1])

    _open(browser, f"{base}/wallet/nfts")
    link = browser.find_element(By.CSS_SELECTOR, "a[data-open-image]")
    assert link.get_attribute("data-open-image") == f"/content/{PIECE}"
    link.click()
    assert _until(browser, "const b = document.getElementById('lightbox'); return b && b.classList.contains('on')"
                           " && b.querySelector('.lightbox-name').textContent === '#42'"
                           f" && b.querySelector('.lightbox-save').getAttribute('href') === '/content/{PIECE}?download=1'")
    assert browser.current_url.endswith("/wallet/nfts")


@pytest.mark.parametrize("path", ["/dogefeedx/post?id=nope", "/dogefeedx/profile?u=nobody",
                                  "/dogefeedx/inscription?id=" + PIECE + "i0"])
def test_dogefeedx_s_back_buttons_go_back(browser, served, path):
    base, _ = served
    _open(browser, f"{base}/offline")
    _open(browser, base + path)
    browser.find_element(By.CSS_SELECTOR, "[data-go-back]").click()
    assert _until(browser, "return location.pathname === '/offline'"), browser.current_url


@pytest.mark.parametrize("path,broken,check", [
    ("/dogefeedx/collections", "hide", "img.style.display === 'none'"),
    ("/dogefeedx/nfts", "hide", "img.style.display === 'none'"),
    ("/dogefeedx/post?id=nope", "hide", "img.style.display === 'none'"),
    ("/dogefeedx/profile?u=nobody", "hide", "img.style.display === 'none'"),
    ("/dogefeedx/feed", "avatar", "img.src.indexOf('data:image/svg+xml,') === 0"),
])
def test_dogefeedx_s_broken_pictures_hide_or_fall_back(browser, served, path, broken, check):
    base, _ = served
    _open(browser, base + path)
    browser.execute_script("const img = document.createElement('img'); img.id = 'broken';"
                           " img.setAttribute('data-broken', arguments[0]); img.src = '/no-such-picture.png';"
                           " document.body.appendChild(img);", broken)
    assert _until(browser, f"const img = document.getElementById('broken'); return {check}"), path


def test_dogefeedx_s_wallet_looks_an_address_up(browser, served):
    base, _ = served
    _open(browser, f"{base}/dogefeedx/wallet")
    browser.execute_script("window.__looked = 0; window.lookupAddress = function () { window.__looked++; };")
    browser.find_element(By.ID, "lookup-btn").click()
    assert _until(browser, "return window.__looked === 1"), "View looks the typed address up"
