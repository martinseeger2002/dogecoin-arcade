"""DigiDollar as a coin of its own in the Wallet, in a real browser (2026-11-05: "add the digi dollar to the
main app ... it should be its own wallet coin option", "just like any other coin or chain is added or borrowed ... not
dependent on the installation of a module"): a DigiDollar tile right after DigiByte's on any arcade with DigiByte, its
balance in dollars and its DigiDollar address from the arcade's own index (/account/digidollar), and a send the arcade
builds (/account/digidollar/send) that the wallet reads from the transaction's own bytes before the review. The
DigiByte Core is stood in for (gettxout, scantxoutset); the coins are written into the index as a block would."""

import importlib
import pathlib
import socket
import sys
import threading
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                  # noqa: E402
from test_module_host import p2pkh_script                        # noqa: E402

from selenium.webdriver.common.by import By                      # noqa: E402

PILOT = pathlib.Path(__file__).resolve().parent.parent / "examples" / "packages" / "digidollar"
SHOTS = pathlib.Path(__file__).resolve().parent.parent / "build" / "screens"

def _pilot():
    sys.path.insert(0, str(PILOT))
    try:
        return importlib.import_module("lib.taproot"), importlib.import_module("module")
    finally:
        sys.path.remove(str(PILOT))

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    driver.set_window_size(1100, 950)
    yield driver
    driver.quit()

class _Core:
    """A DigiByte testnet Core that answers what a DigiDollar send asks of it."""
    def __init__(self):
        self.outs = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def call(self, method, *params):
        if method == "gettxout" and (params[0], params[1]) in self.outs:
            value, script = self.outs[params[0], params[1]]
            return {"value": value / 100_000_000, "scriptPubKey": {"hex": script.hex()}}
        if method == "scantxoutset":
            return {"unspents": []}
        raise ValueError(f"{method} is not served here")

def _serve(home, with_dgb: bool):
    import uvicorn
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    (home / "settings.json").write_text("{}")
    state = AppState(
        home=home,
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet", datadir=pathlib.Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=pathlib.Path("/nonexistent")),
        more={"dgbtest": ChainContext(network="dgbtest", role="addon", label="DigiByte testnet",
                                      conf=pathlib.Path("/nonexistent/digibyte.conf"))} if with_dgb else {},
    )
    if with_dgb:
        state._sync["dgbtest"] = (time.time() + 86400, {"ready": True, "progress": 1.0, "online": True,
                                                        "blocks": 100, "headers": 100})
        core = _Core()
        object.__setattr__(state.more["dgbtest"], "rpc", lambda: core)
        state.dd_core = core
    app = create_app(state)
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
        pytest.skip("test server did not start")
    return f"http://127.0.0.1:{port}", state, server, thread

@pytest.fixture(scope="module")
def served(tmp_path_factory):
    base, state, server, thread = _serve(tmp_path_factory.mktemp("ddwallet"), True)
    yield base, state
    server.should_exit = True
    thread.join(timeout=5)

def _sign_up(browser, base, name):
    browser.get(f"{base}/join")
    browser.delete_all_cookies()
    got = browser.execute_async_script("""
        const done = arguments[2];
        import("/wallet.js").then(async (w) => {
          await w.signUp(arguments[0], arguments[1], {network: "regtest", version: 111});
          const me = await w.opened({network: "regtest", version: 111});
          await w.tellTheNode(me);
          let k = null;
          try { k = w.keysOn(me, "dgbtest"); } catch (e) { /* an arcade without DigiByte */ }
          const hex = (u) => Array.from(u, (b) => b.toString(16).padStart(2, "0")).join("");
          done({ok: true, pubkey: k ? hex(k.pubkey) : "", address: k ? k.address : ""});
        }).catch((e) => done({ok: false, error: String(e.message || e)}));
        """, name, "a password nobody needs to guess for this test")
    assert got.get("ok"), got
    return got

def _wait(browser, script, seconds=20):
    for _ in range(seconds * 4):
        got = browser.execute_script(script)
        if got:
            return got
        time.sleep(0.25)
    raise AssertionError(f"never: {script}\n{browser.find_element(By.TAG_NAME, 'body').text[:2000]}")

def _shot(browser, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    browser.save_screenshot(str(SHOTS / name))

def test_digidollar_is_a_coin_of_its_own_with_balance_receive_and_a_checked_send(browser, served):
    base, state = served
    me = _sign_up(browser, base, "ddwallettester")
    tr, pilot = _pilot()
    pub = bytes.fromhex(me["pubkey"])
    own = pilot.dd_address("dgbtest", pub[1:])
    payee = pilot.dd_address("dgbtest", bytes.fromhex("79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"))
    # $60 of DigiDollars and 1.5 DGB on this account, as the index files them from a block.
    chain = state.more["dgbtest"]
    with state.token_index(chain).open() as db:
        db.conn.execute("INSERT INTO dd_watched (address, since, seeded) VALUES (?, 1, 1)", (own,))
        db.conn.execute("INSERT INTO dd_coin VALUES (?,?,?,?,?)", ("dd" * 32, 1, own, 6000, 5))
        db.conn.execute("INSERT INTO watched (address, since) VALUES (?, 1) ON CONFLICT DO NOTHING", (me["address"],))
        db.conn.execute("INSERT INTO utxo VALUES (?,?,?,?,?)", ("aa" * 32, 0, me["address"], 150_000_000, 5))
        db.conn.commit()
    state.dd_core.outs = {("dd" * 32, 1): (0, tr.p2tr(tr.tweak(pub[1:]))),
                          ("aa" * 32, 0): (150_000_000, p2pkh_script(me["address"]))}

    browser.execute_script("localStorage.clear()")
    browser.get(f"{base}/me/wallet?coin=dgbtest")
    _wait(browser, "return document.body.dataset.walletReady === 'yes'")

    # The tile: beside DigiByte testnet's, with DigiDollar's own icon.
    browser.find_element(By.ID, "coinpick").click()
    tiles = browser.execute_script("""return [...document.querySelectorAll('#nets button')].map(b => ({
        net: b.dataset.net, dd: b.dataset.dd || "", text: b.textContent, icon: (b.querySelector('img') || {}).src || ""}))""")
    dd = [t for t in tiles if t["dd"]]
    assert len(dd) == 1 and dd[0]["net"] == "dgbtest" and "DigiDollar testnet" in dd[0]["text"], tiles
    assert dd[0]["icon"].endswith("/coin-ddtest.png"), dd
    at = [t["net"] + t["dd"] for t in tiles]
    assert at.index("dgbtest1") == at.index("dgbtest") + 1, "right after DigiByte testnet"
    _shot(browser, "dd-1-picker.png")
    browser.find_element(By.CSS_SELECTOR, '#nets button[data-dd="1"]').click()

    # Its balance in dollars and its DigiDollar address, from the arcade's own index.
    _wait(browser, "return document.getElementById('bal').textContent.includes('$60.00')")
    assert browser.find_element(By.ID, "unit").text == "DD TEST"
    assert "DigiDollar testnet" in browser.find_element(By.ID, "coinface").text
    _shot(browser, "dd-2-balance.png")

    # Receive: the DigiDollar address, not the DGB one.
    browser.find_element(By.ID, "do-recv").click()
    big = _wait(browser, "return document.querySelector('.wl-addrbig')")
    assert big.text == own and own.startswith("TD")
    _shot(browser, "dd-3-receive.png")
    browser.execute_script("document.querySelector('.wl-sheet [aria-label=Close]').click()")

    # Send: a DigiDollar address only; dollars and cents; then the review read from the transaction's bytes.
    browser.find_element(By.ID, "do-send").click()
    to = _wait(browser, "return document.getElementById('send-to')")
    to.send_keys(me["address"])
    note = browser.execute_script("return document.querySelector('#send-to').closest('.wl-sheet')"
                                  ".querySelector('.wl-note.bad').textContent")
    assert "DigiDollar address" in note
    to.clear()
    to.send_keys(payee)
    amount = browser.find_element(By.ID, "send-amount")
    amount.send_keys("70")
    assert "more than you have" in browser.execute_script(
        "return [...document.querySelectorAll('.wl-sheet .wl-note.bad')].map(e => e.textContent).join(' ')")
    amount.clear()
    amount.send_keys("40")
    _shot(browser, "dd-4-send.png")
    [b for b in browser.find_elements(By.CSS_SELECTOR, ".wl-sheet button.wl-go") if b.text == "Review"][0].click()
    big = _wait(browser, "return document.querySelector('.wl-review .big')")
    assert big.text == "$40.00 in DigiDollars"
    sheet = browser.find_element(By.CSS_SELECTOR, ".wl-sheet").text
    assert payee in sheet and "DGB" in sheet, sheet
    _shot(browser, "dd-5-review.png")

    # The arcade says why it will not build a send: DigiDollar change under its $1 least.
    browser.execute_script("document.querySelector('.wl-sheet [aria-label=Back]').click()")
    amount = _wait(browser, "return document.getElementById('send-amount')")
    amount.clear()
    amount.send_keys("59.50")
    [b for b in browser.find_elements(By.CSS_SELECTOR, ".wl-sheet button.wl-go") if b.text == "Review"][0].click()
    err = _wait(browser, "return [...document.querySelectorAll('.wl-sheet .msg.err')].find(e => !e.hidden)")
    assert "under DigiDollar's $1 least" in err.text, err.text

    # An @name on the test chain: its DigiDollar address is looked up (/lite/api/ddaddress, arcade/ddaddress.py: the key
    # its own claim was signed with), stood in for here, and the review names them and that address.
    browser.execute_script("""
        const real = window.fetch;
        window.fetch = (url, opts) => String(url).startsWith("/lite/api/ddaddress")
          ? Promise.resolve(new Response(JSON.stringify({address: arguments[0], tag: "apple"}),
                                         {headers: {"Content-Type": "application/json"}}))
          : real(url, opts);""", payee)
    to = browser.find_element(By.ID, "send-to")
    to.clear()
    to.send_keys("@apple")
    amount = browser.find_element(By.ID, "send-amount")
    amount.clear()
    amount.send_keys("40")
    assert "looked up when you review" in browser.execute_script(
        "return document.querySelector('#send-to').closest('.wl-sheet').textContent")
    [b for b in browser.find_elements(By.CSS_SELECTOR, ".wl-sheet button.wl-go") if b.text == "Review"][0].click()
    big = _wait(browser, "return document.querySelector('.wl-review .big')")
    sheet = browser.find_element(By.CSS_SELECTOR, ".wl-sheet").text
    assert big.text == "$40.00 in DigiDollars" and "@apple" in sheet and payee in sheet, sheet
    _shot(browser, "dd-6-review-name.png")

def test_no_digidollar_tile_on_an_arcade_without_digibyte(browser, tmp_path):
    (tmp_path / "off").mkdir()
    base, state, server, thread = _serve(tmp_path / "off", False)
    try:
        _sign_up(browser, base, "ddofftester")
        browser.execute_script("localStorage.clear()")
        browser.get(f"{base}/me/wallet")
        _wait(browser, "return document.body.dataset.walletReady === 'yes'")
        nets = browser.execute_script("return [...document.querySelectorAll('#nets button')].map(b => b.dataset.net + (b.dataset.dd || ''))")
        assert nets and not any(n.endswith("1") for n in nets), nets
    finally:
        server.should_exit = True
        thread.join(timeout=5)
