"""A payment made from a profile, in a real browser on regtest: the coin picked on the page, the amount typed into the
Wallet's own Send sheet, reviewed, signed in the tab, and the coins arriving (2026-11-02)."""

import pathlib
import sys
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")
from selenium.webdriver.common.by import By                      # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait          # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_send_browser import _catch_up, _keys, browser, funded, served  # noqa: F401,E402


def _wait(driver, seconds=30):
    return WebDriverWait(driver, seconds, poll_frequency=0.2)


def _signed_up(browser, state, name):
    version = state.messaging.params.pubkeyhash_version
    made = browser.execute_async_script("""
        const done = arguments[3];
        window.w.signUp(arguments[0], arguments[1], {network: "regtest", version: arguments[2]})
          .then((r) => { window.wallet = r.wallet; done({tag: r.tag, address: r.address}); },
                (e) => done({error: String(e.message || e)}));""", name, "a long enough password", version)
    assert "error" not in made, made
    return made


@pytest.fixture
def payee(funded):
    """@name, claimed on the chain by one account, then a second account, with coins, at the keyboard."""
    browser, base, state, node, first = funded
    claimed = browser.execute_async_script("""
        const done = arguments[0];
        window.w.claim(window.wallet).then(done, (e) => done({error: String(e.message || e)}));""")
    assert "error" not in claimed, claimed
    node.rpc.call("generate", 1)
    _catch_up(state)
    browser.delete_all_cookies()
    browser.get(f"{base}/join")
    browser.execute_script("sessionStorage.clear(); localStorage.clear();")
    assert browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        import("/wallet.js").then((m) => { window.w = m; done(true); }, (e) => done(String(e)));""") is True
    payer = _signed_up(browser, state, f"payer{int(time.time() * 1000) % 100000}")
    node.rpc.call("sendtoaddress", payer["address"], 50.0)
    node.rpc.call("generate", 1)
    _catch_up(state)
    _keys(browser, {"network": "regtest", "version": state.messaging.params.pubkeyhash_version})
    return browser, base, state, node, first, payer


def _open_coin(browser, base, tag, network):
    browser.get(f"{base}/u/{tag}")
    _wait(browser).until(lambda d: d.find_element(By.ID, "pay-them").is_displayed())
    assert not browser.find_element(By.ID, "ppay").is_displayed()
    browser.find_element(By.ID, "pay-them").click()
    row = browser.find_element(By.CSS_SELECTOR, f'#ppay a[data-network="{network}"]')
    assert row.is_displayed()
    row.click()


def test_a_coin_picked_on_a_profile_pays_them_through_the_send_sheet(payee):
    browser, base, state, node, them, payer = payee
    _open_coin(browser, base, them["tag"], "regtest")
    _wait(browser, 60).until(lambda d: d.execute_script(
        "const t = document.getElementById('send-to'); return t && t.value"))
    assert browser.find_element(By.ID, "send-to").get_attribute("value") == "@" + them["tag"]
    assert "?" not in browser.current_url, "a reload does not open it again"

    amount = browser.find_element(By.ID, "send-amount")
    assert amount.get_attribute("value") == "", "the amount is theirs to type"
    amount.send_keys("2.5")
    review = browser.find_element(By.CSS_SELECTOR, ".wl-go")
    _wait(browser).until(lambda d: review.is_enabled())
    review.click()
    _wait(browser, 60).until(lambda d: them["address"] in d.find_element(By.TAG_NAME, "body").text)
    assert _paying(node, them["address"]) == [], "nothing has gone out before they press Send"

    send = _wait(browser, 30).until(lambda d: next(
        (b for b in d.find_elements(By.CSS_SELECTOR, ".wl-go") if b.text.startswith("Send ")), None))
    send.click()
    _wait(browser, 60).until(lambda d: _paying(node, them["address"]))
    assert _paying(node, them["address"]) == [2.5], "one payment, of what they typed, to the address the name holds"


def _paying(node, address):
    """What the waiting transactions pay this address. A new account's first Wallet visit puts its own name and key on
    the chain, so the mempool is not empty, and the node's wallet does not watch an account's address: each output is
    read."""
    out = []
    for txid in node.rpc.call("getrawmempool"):
        for vout in node.rpc.call("getrawtransaction", txid, 1)["vout"]:
            if address in vout["scriptPubKey"].get("addresses", []):
                out.append(float(vout["value"]))
    return out


def test_a_coin_whose_wallet_is_hidden_never_opens_a_send_on_another_coin(payee):
    browser, base, state, node, them, payer = payee
    pubkey = browser.execute_async_script("""
        const done = arguments[0];
        fetch("/account").then((r) => r.json()).then((said) => done(said.pubkey), (e) => done(""));""")
    assert pubkey
    state.set_setting(f"wallets_hidden:{pubkey}", ["main"])
    browser.get(f"{base}/me/wallet?to=@{them['tag']}&coin=main")
    _wait(browser, 60).until(lambda d: d.execute_script("return document.body.dataset.walletReady") == "yes")
    assert browser.find_elements(By.ID, "send-to") == [], "the Send sheet stayed shut"


def test_the_operators_pay_opens_the_nodes_wallet_on_the_address_shown(payee):
    """The node's own send is another path: the link hands it the address, so what Review shows is what the sheet showed."""
    browser, base, state, node, them, payer = payee
    browser.delete_all_cookies()
    browser.execute_script("sessionStorage.clear();")
    _open_coin(browser, base, them["tag"], "regtest")
    _wait(browser, 60).until(lambda d: d.execute_script(
        "const t = document.getElementById('send-to'); return t && t.value"))
    assert browser.find_element(By.ID, "send-to").get_attribute("value") == them["address"]
    assert browser.current_url.endswith("/wallet"), browser.current_url
