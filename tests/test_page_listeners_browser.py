"""The controls whose inline on...= handlers moved into the pages' nonced scripts (the script policy, phase 2,
2026-11-09), pressed in a real browser that ENFORCES `script-src 'self' 'nonce-...'`: an inline handler left
behind does nothing here, so every assertion below is also a check that the control no longer needs one.

Most of these pages show their controls only with something on them -- a wallet to switch to, a contact, an
app with comments -- and the node here has nothing. So the page is drawn by its own route and template, with
its own base.html and scripts, and only the rows the controls sit on are handed in (`?as=<template>`, see
`DRAWN`). The rows are made up; the markup and the scripts are the real ones.
"""

import pathlib
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

from selenium.webdriver.common.by import By                      # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait           # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                   # noqa: E402

TX = "ab" * 32
TX2 = "cd" * 32
#: A name with everything in it that used to be pasted into an inline handler's JavaScript string.
QUOTED = "Bob's \"spare\" <wallet>"
ADDRESS = "nTestAddrAAAAAAAAAAAAAAAAAAAAAAAAA"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _item(**more):
    """A launch, an app or a mintpad as the lists draw one: reactions, no comments yet."""
    return {"txid": TX, "id": TX, "name": "Frogs", "kind": "collection", "creator": "nMe", "count": 3,
            "likes": 0, "dislikes": 0, "comments": 0, "comment_rows": [], "liked": False, "disliked": False,
            "tips": 0, "trades": 0, "mints": 3, "number": 1, **more}


def _row(**more):
    """An inscription this node holds: a picture."""
    return {"txid": TX, "number": 5, "edition": 1, "held": True, "content_type": "image/png", "json": "{}",
            "content_len": 100, "chunks": 1, "block_height": 100,
            "creator": "nMe", "owner": "nThem", "mine": False, "listing": None, "offers": 0, **more}


def _summary():
    return {"collection": "Frogs", "creator": "nMe", "count": 1, "supply": 1, "first_edition": 1, "last_edition": 1,
            "first_number": 5, "last_number": 5, "details": {}}


#: Per template, the rows its controls sit on, merged over what the route itself worked out.
DRAWN = {
    "backup.html": lambda chain: {"chains": [{
        "label": "Testnet", "is_mainnet": False, "online": True, "which": "regtest", "can_switch": True,
        "named": False, "wallet": {"balance": 1, "addresses": 1, "txcount": 0}, "wallet_file": "",
        "wallets": [{"name": "", "active": True, "size": 10}, {"name": QUOTED, "active": False, "size": 10}]}]},
    "contacts.html": lambda chain: {"people": [{"id": 7, "name": QUOTED, "testnet_address": ADDRESS,
                                                "mainnet_address": "", "hex": "", "notes": ""}],
                                    "tags": {}, "editing": None, "published": [], "matches": [], "mine": {}},
    "collection_job.html": lambda chain: {"chain": chain, "running": False, "items": [], "page": 1, "pages": 1,
                                          "job": {
        "id": 3, "name": "Frogs", "status": "done", "sender": "nMe", "chunks": 2, "sent_chunks": 2,
        "fee": 0.1, "dust": 0.01, "folder": "/tmp/frogs", "note": "", "error": "", "pending": 0, "sending": 0,
        "failed_items": 0, "items": 2, "sent": 2}},
    "games.html": lambda chain: {"viewer": "wallet", "games": [_item(
        mine=True, game={"name": "Frog Run", "opens": TX, "wallet": False})]},
    "launches.html": lambda chain: {"chain": chain, "items": [_item()], "faces": {}, "names": {}},
    "launch_thread.html": lambda chain: {
        "chain": chain, "acts": True, "root": TX, "count": 1, "mine": {},
        "launch": {"kind": "collection", "name": "Frogs", "creator": "nMe", "id": TX},
        "tree": {TX: [{"txid": TX2, "author": "nThem", "when": 0, "pending": True, "text": "hello",
                       "likes": 0, "liked": False}]}},
    # One page, three tabs: a mintpad to comment on, an offer whose piece has no picture, and two markets to search.
    "exchange.html": lambda chain: {
        "pads": [_item(mainnet=False)], "acts": True,
        "offers_in": [{"txid": TX2, "inscription": TX, "buyer": "nThem", "price": "1 coins", "number": 5,
                       "settled": False, "busy": False, "accepted": False, "held_for": None}],
        "pairs": [{"href": f"/exchange/pair/{name.lower()}", "name": name, "property_id": number,
                   "chain_net": "regtest", "mainnet": False, "coins": 0, "orders": 0, "trades": 0, "last": None,
                   "bid": None, "ask": None, "change": None, "new": False}
                  for number, name in ((9, "FROG"), (10, "TOAD"))]},
    "collection.html": lambda chain: {"chain": chain, "node": chain.status(), "inscriptions": [_row()],
                                      "for_sale": [], "owned": [], "summary": _summary(), "page": 1, "pages": 1},
    "inscriptions.html": lambda chain: {"inscriptions": [_row()], "tags": {}, "total": 1, "page": 1, "pages": 1},
    "inscription_view.html": lambda chain: {
        "chain": chain, "node": chain.status(), "row": _row(), "mine": False, "mine_account": False,
        "viewer": "wallet", "renders": False, "coins": 2.5,
        "tokens": [{"property_id": 9, "name": "FROG", "display": "1"}]},
    "collection_wizard.html": lambda chain: {
        "chain": chain, "node": chain.status(), "sender": "nMe", "pad_on": False, "pad_kind": "coins",
        "pad_html": "<p>CREATOR COLLECTION</p>", "pad_swaps": "[]", "pad_note": "\"\"",
        "tokens": [{"property_id": 9, "name": "FROG"}],
        "cost": {"items": 1, "bytes": 100, "fee": 0.1, "dust": 0.01, "total": 0.11, "chunks": 1},
        "build": SimpleNamespace(collection="Frogs", folder="/tmp/frogs", problems=[],
                                 items=[{"name": "Frog 1", "size": 100, "edition": 1}])},
    "market_collection.html": lambda chain: {
        "chain": chain, "rows": [_row()], "account_view": False, "coin_name": "coins", "page": 1, "pages": 1,
        "path": "/exchange/collection/nMe/Frogs", "tokens": [{"property_id": 9, "name": "FROG", "display": "1"}],
        "summary": _summary(), "about": {}, "owners": 1, "for_sale": 0, "offered": 0},
    "admin_login.html": lambda chain: {"need_account": False},
}


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """The app, served for real, with the script policy enforced and `?as=` drawing a page over its rows."""
    import uvicorn

    import arcade.config
    import arcade.discovery
    import arcade.web.state
    from arcade.web import app as webapp
    from arcade.web.state import AppState, ChainContext

    patches = pytest.MonkeyPatch()

    def refuse(*args, **kwargs):
        raise RuntimeError("no node available (test)")
    # Nothing here may reach a real node (conftest.no_nodes, module-scoped).
    patches.setattr(arcade.config, "load_rpc_credentials", refuse)
    patches.setattr(arcade.web.state, "load_rpc_credentials", refuse, raising=False)
    patches.setattr(arcade.discovery, "best", lambda *a, **k: None)

    home = tmp_path_factory.mktemp("listeners")
    state = AppState(home=home,
                     messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                                            datadir=pathlib.Path("/nonexistent")),
                     ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                                         datadir=pathlib.Path("/nonexistent")))

    drawn = webapp.TEMPLATES.TemplateResponse

    def draw(request, template, context, *args, **kwargs):
        name = request.query_params.get("as")
        if name in DRAWN:
            template, context = name, {**context, **DRAWN[name](state.messaging)}
        return drawn(request, template, context, *args, **kwargs)
    patches.setattr(webapp.TEMPLATES, "TemplateResponse", draw)

    app = webapp.create_app(state)

    @app.middleware("http")
    async def enforce(request, call_next):
        # The report-only script policy, enforced as well: what it would stop, this browser stops.
        response = await call_next(request)
        policy = response.headers.get("content-security-policy-report-only")
        if policy:
            response.headers.append("content-security-policy", policy)
        return response

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                                           timeout_graceful_shutdown=1.0))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        patches.undo()
        pytest.skip("test server did not start")
    yield f"http://127.0.0.1:{port}"
    patches.undo()
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    driver.set_window_size(1100, 900)
    yield driver
    driver.quit()


#: Records what the page's script policy stopped, from the moment it is installed.
WATCH = """
window.__stopped = [];
document.addEventListener('securitypolicyviolation', function (e) {
  window.__stopped.push(e.effectiveDirective + ' ' + e.blockedURI);
});
window.__stay = 1;
"""


def _open(browser, url):
    browser.get(url)
    WebDriverWait(browser, 10).until(lambda d: d.execute_script("return document.readyState") == "complete")
    browser.execute_script(WATCH)
    return browser


def _stopped(browser):
    return browser.execute_script("return window.__stopped || []")


def _left(browser):
    """Whether the page was replaced (a form posted, a link followed): the marker set on this document is gone."""
    try:
        WebDriverWait(browser, 5).until(lambda d: not d.execute_script("return window.__stay === 1"))
        return True
    except Exception:
        return False


def _ask(browser, accept: bool) -> str:
    alert = WebDriverWait(browser, 5).until(lambda d: d.switch_to.alert)
    said = alert.text
    alert.accept() if accept else alert.dismiss()
    return said


def _shown(browser, element_id):
    return browser.execute_script("var e = document.getElementById(arguments[0]); return e && !e.hidden;",
                                  element_id)


def _click(browser, css):
    element = browser.find_element(By.CSS_SELECTOR, css)
    browser.execute_script("arguments[0].scrollIntoView({block: 'center'})", element)
    element.click()
    return element


def _stay_put(browser):
    assert not _left(browser), "the page went away"
    assert not _stopped(browser), _stopped(browser)


def _confirms(browser, url, form_css, question):
    """No keeps the page; yes posts the form. Returns nothing; asserts both."""
    _open(browser, url)
    _click(browser, form_css + " [type=submit]")
    assert _ask(browser, accept=False) == question
    _stay_put(browser)
    _click(browser, form_css + " [type=submit]")
    assert _ask(browser, accept=True) == question
    assert _left(browser), "Yes did not post the form"


def test_backup_asks_before_it_switches_removes_starts_or_replaces_a_wallet(browser, served):
    url = f"{served}/backup?as=backup.html"
    _confirms(browser, url, "form[action$='/wallet/switch']",
              f"Switch to {QUOTED}? The node restarts, and testnet will use that wallet from then on.")
    _confirms(browser, url, "form[action$='/wallet/remove']",
              f"Remove {QUOTED} from the list? The file is moved aside, not deleted.")
    _confirms(browser, url, "form[action$='/wallet/new']",
              "Start using a new empty wallet? Your current one is kept and you can switch back.")
    _open(browser, url)
    form = browser.find_element(By.CSS_SELECTOR, "form[action$='/restore']")
    browser.execute_script("arguments[0].closest('details').open = true;"
                           "arguments[0].querySelectorAll('[required]').forEach(function (e) {"
                           "  if (e.type === 'checkbox') e.checked = true; else e.value = '/tmp/x/wallet.dat'; });",
                           form)
    _click(browser, "form[action$='/restore'] [type=submit]")
    assert _ask(browser, accept=False) == "Replace the testnet wallet now? The node will restart."
    _stay_put(browser)


def test_the_address_book_copies_an_address_and_asks_before_removing(browser, served):
    url = f"{served}/backup?as=contacts.html"
    _open(browser, url)
    browser.execute_script("navigator.clipboard.writeText = function (t) { window.__copied = t;"
                           " return Promise.resolve(); };"
                           "document.querySelectorAll('details').forEach(function (d) { d.open = true; });")
    code = _click(browser, "code.addr[data-copy]")
    WebDriverWait(browser, 5).until(lambda d: d.execute_script("return window.__copied") == ADDRESS)
    assert "copied" in code.get_attribute("class")
    _stay_put(browser)
    _confirms(browser, url, "form[action='/contacts/7/delete']",
              f"Remove {QUOTED} from the address book? Messages already received are kept.")


def test_a_collection_run_asks_before_it_is_forgotten(browser, served):
    _confirms(browser, f"{served}/backup?as=collection_job.html", "form[action$='/3/delete']",
              "Forget this run? What is on the chain stays there.")


def test_apps_show_how_to_list_comment_and_tip_and_ask_before_removing(browser, served):
    url = f"{served}/apps?as=games.html"
    _open(browser, url)
    for button, panel in (("[data-toggle=howto]", "howto"), (f"[data-toggle=c-{TX}]", f"c-{TX}"),
                          (f"[data-toggle=t-{TX}]", f"t-{TX}")):
        assert not _shown(browser, panel)
        _click(browser, button)
        assert _shown(browser, panel), panel
        _click(browser, button)
        assert not _shown(browser, panel), panel
    _stay_put(browser)
    _confirms(browser, url, f"form[action='/feed/{TX}/delete']",
              "Remove this app from the Apps tab? The inscription stays on the chain and still plays from its "
              "own link, but no arcade lists this card any more.")


def test_launches_and_the_mintpads_open_their_comment_box(browser, served):
    for url in (f"{served}/backup?as=launches.html", f"{served}/exchange?tab=mintpads&as=exchange.html"):
        _open(browser, url)
        assert not _shown(browser, f"c-{TX}")
        _click(browser, f"[data-toggle=c-{TX}]")
        assert _shown(browser, f"c-{TX}"), url
        _click(browser, f"[data-toggle=c-{TX}]")
        assert not _shown(browser, f"c-{TX}"), url
        _stay_put(browser)


def test_a_launch_thread_reply_opens_with_the_cursor_in_it(browser, served):
    _open(browser, f"{served}/backup?as=launch_thread.html")
    _click(browser, f"[data-toggle=r-{TX2}]")
    assert _shown(browser, f"r-{TX2}")
    assert browser.execute_script("return document.activeElement.name") == "text"
    _click(browser, f"[data-toggle=r-{TX2}]")
    assert not _shown(browser, f"r-{TX2}")
    _stay_put(browser)


@pytest.mark.parametrize("template,css", [("collection.html", "a.media[data-view]"),
                                          ("inscriptions.html", "a.media[data-view]"),
                                          ("inscription_view.html", ".inscription-picture img[data-view]")])
def test_a_picture_opens_in_the_viewer_not_as_a_download(browser, served, template, css):
    _open(browser, f"{served}/{'nfts' if template == 'inscriptions.html' else 'backup'}?as={template}")
    _click(browser, css)
    WebDriverWait(browser, 5).until(lambda d: d.execute_script(
        "var b = document.getElementById('lightbox'); return b && b.classList.contains('on');"))
    said = browser.execute_script(
        "var b = document.getElementById('lightbox'); return [b.querySelector('.lightbox-name').textContent,"
        " b.querySelector('.lightbox-save').getAttribute('href'), b.querySelector('img').getAttribute('src')];")
    assert said == ["#5", f"/content/{TX}?download=1", f"/content/{TX}"], said
    _stay_put(browser)


def test_choosing_a_file_prices_it(browser, served, tmp_path):
    _open(browser, f"{served}/nfts?as=inscriptions.html")
    picked = tmp_path / "frog.txt"
    picked.write_text("ribbit" * 100)
    browser.find_element(By.CSS_SELECTOR, "input[data-price]").send_keys(str(picked))
    WebDriverWait(browser, 5).until(lambda d: _shown(d, "priced"))
    assert "frog.txt" in browser.find_element(By.ID, "priced").text
    _stay_put(browser)


def test_the_offer_and_mintpad_pickers_show_their_token_lists(browser, served):
    _open(browser, f"{served}/backup?as=inscription_view.html")
    assert not _shown(browser, "offer-token-wrap")
    browser.execute_script("var s = document.querySelector('select#offer-kind[name=kind]'); s.value = 'token';"
                           "s.dispatchEvent(new Event('change', {bubbles: true}));")
    assert _shown(browser, "offer-token-wrap")
    _stay_put(browser)

    _open(browser, f"{served}/backup?as=market_collection.html")
    browser.execute_script("document.querySelectorAll('details').forEach(function (d) { d.open = true; });")
    token = "select[data-offer-kind] ~ select[name=property_id]"
    assert not browser.find_element(By.CSS_SELECTOR, token).is_displayed()
    browser.execute_script("var s = document.querySelector('select[data-offer-kind]'); s.value = 'token';"
                           "s.dispatchEvent(new Event('change', {bubbles: true}));")
    assert browser.find_element(By.CSS_SELECTOR, token).is_displayed()
    _stay_put(browser)

    _open(browser, f"{served}/backup?as=collection_wizard.html")
    assert not _shown(browser, "pad-fields")
    _click(browser, "#launchpad")
    assert _shown(browser, "pad-fields")
    assert browser.execute_script("return document.getElementById('pad-amount').required")
    browser.execute_script("var s = document.getElementById('pad-kind'); s.value = 'token';"
                           "s.dispatchEvent(new Event('change', {bubbles: true}));")
    assert _shown(browser, "pad-token-wrap")
    _click(browser, "#launchpad")
    assert not _shown(browser, "pad-fields")
    assert not browser.execute_script("return document.getElementById('pad-amount').required")
    _stay_put(browser)


#: In place of the network: what the page asked for, and no answer, so the page stays as it is.
HOLD = ("window.__asked = []; window.fetch = function (url, how) {"
        " window.__asked.push([String(url), how && how.body ? JSON.parse(how.body) : null]);"
        " return new Promise(function () {}); };")


def test_the_admin_forms_send_from_the_script_and_never_submit(browser, served):
    _open(browser, f"{served}/backup?as=admin_login.html")
    browser.execute_script(HOLD)
    browser.find_element(By.ID, "adminpw").send_keys("a password here")
    _click(browser, "#adminopen")
    assert browser.execute_script("return window.__asked") == [["/admin/login", {"password": "a password here"}]]
    _stay_put(browser)

    _open(browser, f"{served}/admin")
    browser.execute_script(HOLD)
    # The send form sits in a section of the panel that is folded away; pressed from the script, as Enter would.
    browser.execute_script("document.getElementById('sendamount').value = '1';"
                           "document.getElementById('sendform').requestSubmit();")
    asked = browser.execute_script("return window.__asked")
    assert [a[0] for a in asked if "send" in a[0]] == ["/admin/api/send"], asked
    _stay_put(browser)


def test_the_exchange_searches_its_markets_and_drops_a_thumbnail_that_does_not_load(browser, served):
    _open(browser, f"{served}/exchange?tab=tokens&as=exchange.html")
    rows = "#markets [data-market]"
    WebDriverWait(browser, 5).until(lambda d: len(d.find_elements(By.CSS_SELECTOR, rows)) == 2)
    browser.find_element(By.ID, "marketfind").send_keys("toad")
    WebDriverWait(browser, 5).until(lambda d: [r.is_displayed() for r in d.find_elements(By.CSS_SELECTOR, rows)]
                                    == [False, True])
    _stay_put(browser)

    # /content/<txid> answers 404 here: the picture goes and the frame stays.
    browser.get(f"{served}/exchange?tab=offers&as=exchange.html")
    WebDriverWait(browser, 5).until(lambda d: d.find_elements(By.CSS_SELECTOR, "a.ofthumb")
                                    and not d.find_elements(By.CSS_SELECTOR, "a.ofthumb img"))
