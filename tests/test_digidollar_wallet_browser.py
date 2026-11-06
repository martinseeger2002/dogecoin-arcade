"""DigiDollar as a coin of its own in the Wallet, in a real browser (2026-11-05: "add the digi dollar to the
main app ... it should be its own wallet coin option"): a DigiDollar tile beside DigiByte's while the DigiDollar module
is on, its balance in dollars and its DigiDollar address from the module's `ddreceive`, and a send the module prepares
(`ddsend`) that the wallet reads from the transaction's own bytes before it shows the review. The module's answers are
stood in for in the page (no DigiByte Core here); the send is a real one the pilot builds for this account's key."""

import importlib
import json
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

from arcade import modhost, packages                             # noqa: E402

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


def _serve(home, digidollar_on: bool):
    import uvicorn
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    settings = {}
    if digidollar_on:
        pid, row = packages.install(home, PILOT)
        settings = {"packages": {pid: row}, "modules": {"digidollar": True}}
    (home / "settings.json").write_text(json.dumps(settings))
    state = AppState(
        home=home,
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet", datadir=pathlib.Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=pathlib.Path("/nonexistent")),
        more={"dgbtest": ChainContext(network="dgbtest", role="addon", label="DigiByte testnet",
                                      conf=pathlib.Path("/nonexistent/digibyte.conf"))},
    )
    state._sync["dgbtest"] = (time.time() + 86400, {"ready": True, "progress": 1.0, "online": True,
                                                    "blocks": 100, "headers": 100})
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
          const k = w.keysOn(me, "dgbtest");
          const hex = (u) => Array.from(u, (b) => b.toString(16).padStart(2, "0")).join("");
          done({ok: true, pubkey: hex(k.pubkey), address: k.address});
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


# The module's answers, stood in for: `ddreceive` and `ddsend` as the console gives them (lite/web.py answer_line).
STAND_IN = """
window.__ddLines = [];
const real = window.fetch;
window.fetch = async (url, opts) => {
  if (String(url).includes("/lite/api/console")) {
    const asked = JSON.parse(opts.body);
    window.__ddLines.push({line: asked.line, pubkeys: asked.pubkeys, lite: opts.headers["X-Arcade-Lite"]});
    const word = asked.line.split(" ")[0];
    const result = word === "ddreceive" ? arguments[0] : word === "ddsend" ? {sign: arguments[1]} : null;
    return new Response(JSON.stringify(result ? {ok: true, result} : {ok: false, error: "no"}),
                        {headers: {"Content-Type": "application/json"}});
  }
  return real(url, opts);
};
"""


def _shot(browser, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    browser.save_screenshot(str(SHOTS / name))


def test_digidollar_is_a_coin_of_its_own_with_balance_receive_and_a_checked_send(browser, served, tmp_path):
    base, state = served
    me = _sign_up(browser, base, "ddwallettester")
    tr, pilot = _pilot()
    pub = bytes.fromhex(me["pubkey"])
    own = pilot.dd_address("dgbtest", pub[1:])
    payee = pilot.dd_address("dgbtest", bytes.fromhex("79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"))

    # The send the pilot prepares for this account's key: $40 of its $60 to the payee, $20 back, the fee in DGB.
    class Chain:
        outs = {("dd" * 32, 1): (0, tr.p2tr(tr.tweak(pub[1:]))),
                ("aa" * 32, 0): (150_000_000, p2pkh_script(me["address"]))}

        def __call__(self, network):
            from arcade.config import NETWORKS
            return NETWORKS["dgbtest"], self.call

        def call(self, method, params):
            if method == "getblockcount":
                return 812
            if method == "gettxout" and (params[0], params[1]) in self.outs:
                value, script = self.outs[params[0], params[1]]
                return {"value": value / 100_000_000, "scriptPubKey": {"hex": script.hex()}}
            raise ValueError(f"{method} is not served here")

    home = tmp_path / "modhome"
    home.mkdir()
    pid, row = packages.install(home, PILOT)
    host = modhost.Host(home, Chain())
    prepared = {**pilot.send(pub[1:], [{"txid": "dd" * 32, "vout": 1, "cents": 6000}],
                             [{"txid": "aa" * 32, "vout": 0, "value": 150_000_000}], p2pkh_script(me["address"]),
                             pilot.dd_key("dgbtest", payee), 4000, "dgbtest", payee), "chain": "dgbtest"}
    _, unsigned = host.prepare({"packages": {pid: row}}, "digidollar", prepared, me["address"], pub)
    offer = {**unsigned.as_json(), "chain": "dgbtest", "offer": "stood-in"}
    receive = {"chain": "test", "address": own, "digidollars": 60.0,
               "coins": [{"txid": "dd" * 32, "vout": 1, "dollars": 60.0, "from": "a send"}]}

    browser.execute_script("localStorage.clear()")
    browser.get(f"{base}/me/wallet?coin=dgbtest")
    _wait(browser, "return document.body.dataset.walletReady === 'yes'")
    browser.execute_script(STAND_IN, receive, offer)

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

    # Its balance in dollars, its DigiDollar address, from the module, as this account.
    _wait(browser, "return document.getElementById('bal').textContent.includes('$60.00')")
    assert browser.find_element(By.ID, "unit").text == "DD TEST"
    lines = browser.execute_script("return window.__ddLines")
    assert lines[0]["line"] == "ddreceive test" and lines[0]["lite"] == "1"
    assert lines[0]["pubkeys"]["dgbtest"] == me["pubkey"], "the module is told this wallet's key, as Lite tells it"
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
    assert browser.execute_script("return window.__ddLines.map(l => l.line)")[-1] == f"ddsend {payee} 40 test"
    _shot(browser, "dd-5-review.png")

    # The same offer for another amount than typed is refused before any review.
    browser.execute_script("document.querySelector('.wl-sheet [aria-label=Back]').click()")
    amount = _wait(browser, "return document.getElementById('send-amount')")
    amount.clear()
    amount.send_keys("30")
    [b for b in browser.find_elements(By.CSS_SELECTOR, ".wl-sheet button.wl-go") if b.text == "Review"][0].click()
    err = _wait(browser, "return [...document.querySelectorAll('.wl-sheet .msg.err')].find(e => !e.hidden)")
    assert "not the send you typed" in err.text and "Nothing was signed" in err.text


def test_no_digidollar_tile_while_the_module_is_off(browser, tmp_path):
    (tmp_path / "off").mkdir()
    base, state, server, thread = _serve(tmp_path / "off", False)
    try:
        _sign_up(browser, base, "ddofftester")
        browser.execute_script("localStorage.clear()")
        browser.get(f"{base}/me/wallet?coin=dgbtest")
        _wait(browser, "return document.body.dataset.walletReady === 'yes'")
        nets = browser.execute_script("return [...document.querySelectorAll('#nets button')].map(b => b.dataset.net + (b.dataset.dd || ''))")
        assert "dgbtest" in nets and "dgbtest1" not in nets, nets
    finally:
        server.should_exit = True
        thread.join(timeout=5)
