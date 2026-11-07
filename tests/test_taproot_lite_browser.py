"""Lite's address-type menu takes the Taproot row by itself (2026-11-07, wallet_js ADDRESS_KINDS), in a real browser:
Receive shows the account's BIP-86 address (tltc1p..., its key at m/86'/1'/0'/0/0 from the account's own words), the
node is told to watch it, and a send from it is reviewed from the transaction's own bytes (coins.verifyOffer works out
BIP-341's digest itself and must agree with the node's) before anything is signed. Litecoin testnet stands in for a
chain with Taproot here; no Litecoin node is needed to get as far as the review."""

import contextlib
import pathlib
import sys

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")
from selenium.webdriver.common.by import By                      # noqa: E402
from selenium.webdriver.support.ui import Select, WebDriverWait  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_send_browser import browser, funded, served  # noqa: F401,E402
from test_litecoin_testnet import _with_ltc_test  # noqa: E402
from test_extension import H, _bip32, _pubkey  # noqa: E402

from arcade import taproot  # noqa: E402
from arcade.config import NETWORKS  # noqa: E402
from arcade.script import b58check_encode  # noqa: E402


def _wait(driver, seconds=60):
    return WebDriverWait(driver, seconds, poll_frequency=0.2)


def test_lites_menu_shows_the_taproot_address_and_reviews_a_send_from_it(funded, monkeypatch):
    browser, base, state, node, made = funded
    chain = _with_ltc_test(state, monkeypatch, ready=True)
    browser.get(f"{base}/lite")
    said = browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        (async () => {
          const w = await import("/wallet.js"), c = await import("/coins.js");
          const open = await w.opened({network: "regtest", version: arguments[0]});
          await w.tellTheNode(open);            // the account's L test address, as an unlocking page tells it
          return {seed: c.hex(open.seed), kinds: w.addressKinds("ltctest").map((k) => k.label)};
        })().then(done, (e) => done({error: String(e && e.message || e)}));""",
        state.messaging.params.pubkeyhash_version)
    assert "error" not in said, said
    assert said["kinds"] == ["Legacy", "P2SH (Nested SegWit)", "Native SegWit", "Taproot"]
    pub = _pubkey(_bip32(bytes.fromhex(said["seed"]), (86 | H, 1 | H, H, 0, 0)))
    want = taproot.address(NETWORKS["ltctest"], pub)
    assert want.startswith("tltc1p")

    # Receive: the menu's last row, and the address shown is that key's Taproot address.
    browser.get(f"{base}/lite")
    browser.find_element(By.CSS_SELECTOR, '[data-tab="receive"]').click()
    _wait(browser).until(lambda d: "ltctest" in [o.get_attribute("value") for o in
                                                 Select(d.find_element(By.ID, "w-chain-receive")).options])
    Select(browser.find_element(By.ID, "w-chain-receive")).select_by_value("ltctest")
    _wait(browser).until(lambda d: d.find_element(By.ID, "w-kind-receive").is_displayed())
    kinds = Select(browser.find_element(By.ID, "w-kind-receive"))
    assert [o.text for o in kinds.options][-1] == "Taproot"
    kinds.select_by_value("p2tr")
    _wait(browser).until(lambda d: d.find_element(By.ID, "w-address").text.startswith("tltc1p"))
    assert browser.find_element(By.ID, "w-address").text == want
    assert browser.find_elements(By.CSS_SELECTOR, "#w-qr svg")
    _wait(browser).until(lambda d: "Taproot (m/86'/1'/0'/0/0)" in d.find_elements(
        By.CSS_SELECTOR, "[data-kindsaid]")[-1].get_attribute("textContent"))
    account = next(k for k in state.settings() if k.startswith("segwit_watch:p2tr:"))
    assert state.setting(account) == want, "the node watches it, registered with its key"

    # A coin there, and a send from it to a legacy address: reviewed, nothing signed.
    with contextlib.closing(state.token_index(chain).open()) as db:
        db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                        ("7a" * 32, 0, want, 2 * 10 ** 8, 1))
        db.conn.commit()
    theirs = b58check_encode(111, bytes(range(20)))
    browser.find_element(By.CSS_SELECTOR, '[data-tab="send"]').click()
    Select(browser.find_element(By.ID, "w-chain-send")).select_by_value("ltctest")
    _wait(browser).until(lambda d: d.find_element(By.ID, "w-kind-send").is_displayed())
    assert Select(browser.find_element(By.ID, "w-kind-send")).first_selected_option.get_attribute("value") == "p2tr"
    browser.find_element(By.ID, "w-to").send_keys(theirs)
    browser.find_element(By.ID, "w-amount").send_keys("0.5")
    browser.find_element(By.CSS_SELECTOR, '#w-form button[type="submit"]').click()
    _wait(browser).until(lambda d: d.find_element(By.ID, "w-review").is_displayed()
                         or d.find_element(By.ID, "w-said").text)
    terms = browser.find_element(By.ID, "w-terms").text
    assert f"{want} (Taproot)" in terms and theirs in terms and "0.5" in terms, \
        (terms, browser.find_element(By.ID, "w-said").text)
    assert "Comes back to you" in terms, "the change back to the Taproot address is said as change"
    browser.find_element(By.ID, "w-cancel").click()
