"""Imported wallets in a real browser (plan item 18): the private-key decoder, the phrase derivation, the import
proof, and the Wallet page's picker -- import a key, see it by name, rename, and see its "Wallet N" freed.

The browser is where the key lives, so this is where it is checked: a WIF decoded wrongly would register an address
nobody can spend from, and a phrase derived differently from the account's own words would be a different wallet.
"""

import hashlib
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

from arcade import script, wallet_imports as W                   # noqa: E402
from arcade.config import NETWORKS                               # noqa: E402

TAG = "importtester"
PASSWORD = "a long password nobody needs to guess for this test"


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
        home=tmp_path_factory.mktemp("imports"),
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=Path("/nonexistent")),
    )
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1", port=port, log_level="error",
                                           timeout_graceful_shutdown=1.0))
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


@pytest.fixture(scope="module")
def signed_in(browser, served):
    base, _ = served
    browser.get(f"{base}/join")
    browser.delete_all_cookies()
    browser.set_script_timeout(120)
    result = browser.execute_async_script("""
        const done = arguments[2];
        import("/wallet.js").then(async (w) => {
          const made = await w.signUp(arguments[0], arguments[1], {network: "regtest", version: 111});
          done({ok: true, name: made.tag || arguments[0]});
        }).catch((e) => done({ok: false, error: String(e.message || e)}));
        """, TAG, PASSWORD)
    assert result.get("ok"), result
    return TAG


def _wif(key: bytes, version: int, compressed: bool = True) -> str:
    return script.b58check_encode(version, key + (b"\x01" if compressed else b""))


def _js(browser, body: str, *args):
    """Run `body` (an async function body with `c` = coins.js, `w` = wallet.js, `args`) and return its value."""
    said = browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        const args = Array.from(arguments).slice(1, -1);
        Promise.all([import("/coins.js"), import("/wallet.js")]).then(async ([c, w]) => {
          try { done({ok: true, value: await (async () => {""" + body + """})()}); }
          catch (e) { done({ok: false, error: String(e.message || e)}); }
        });""", "x", *args)
    return said


# --- the private-key decoder ----------------------------------------------------------------------------------------

def test_a_wif_round_trips_through_the_encoder(browser, served, signed_in):
    key = hashlib.sha256(b"import me").digest()
    said = _js(browser, """
        const key = c.unhex(args[0]);
        const text = await c.wif(key, c.WIF.regtest);
        const got = await c.fromWif(text, "regtest");
        return {text, key: c.hex(got.key), compressed: got.compressed, pubkey: c.hex(got.pubkey),
                want: c.hex(c.publicKey(key))};""", key.hex())
    assert said["ok"], said
    v = said["value"]
    assert v["text"] == _wif(key, 0xf1)
    assert v["key"] == key.hex() and v["compressed"] is True and v["pubkey"] == v["want"]


def test_an_uncompressed_wif_keeps_its_uncompressed_key(browser, served, signed_in):
    key = hashlib.sha256(b"uncompressed").digest()
    said = _js(browser, """
        const got = await c.fromWif(args[0], "regtest");
        return {compressed: got.compressed, length: got.pubkey.length,
                address: await c.address(got.pubkey, 111)};""", _wif(key, 0xf1, compressed=False))
    assert said["ok"], said
    assert said["value"]["compressed"] is False and said["value"]["length"] == 65
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    pub = ec.derive_private_key(int.from_bytes(key, "big"), ec.SECP256K1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    assert said["value"]["address"] == script.b58check_encode(111, script.hash160(pub))


def test_a_wif_of_another_chain_is_refused_and_says_whose(browser, served, signed_in):
    key = hashlib.sha256(b"litecoin").digest()
    said = _js(browser, "return await c.fromWif(args[0], 'regtest');", _wif(key, 0xb0))
    assert not said["ok"] and "Litecoin mainnet" in said["error"], said
    said = _js(browser, "return await c.fromWif(args[0], 'ltc');", _wif(key, 0x9e))
    assert not said["ok"] and "Dogecoin mainnet" in said["error"], said


def test_a_mistyped_wif_is_refused_by_its_checksum(browser, served, signed_in):
    text = _wif(hashlib.sha256(b"typo").digest(), 0xf1)
    broken = text[:-3] + ("a" if text[-3] != "a" else "b") + text[-2:]
    said = _js(browser, "return await c.fromWif(args[0], 'regtest');", broken)
    assert not said["ok"] and "checksum" in said["error"], said
    for junk in ("", "0OIl", "abc"):
        said = _js(browser, "return await c.fromWif(args[0], 'regtest');", junk)
        assert not said["ok"], junk


def test_the_import_proof_is_the_bytes_the_node_checks(browser, served, signed_in):
    pub = bytes.fromhex("02" + "11" * 32)
    said = _js(browser, "return c.hex(await c.importProof(args[0], 'ltctest', args[1], c.unhex(args[2])));",
               "AB" * 32, "mAddress", pub.hex())
    assert said["ok"], said
    assert said["value"] == W.proof_digest("ab" * 32, "ltctest", "mAddress", pub.hex()).hex()


# --- the phrase -----------------------------------------------------------------------------------------------------

def test_a_phrase_import_derives_what_the_main_wallet_derives_from_the_same_words(browser, served, signed_in):
    said = _js(browser, """
        const chains = await w.chains();
        const out = [];
        for (const chain of chains) {
          const main = await w.opened(chain);
          const words = main.phrase.toUpperCase().split(" ").join("   ");     // as somebody might paste them
          const got = await w.importedKeys({kind: "phrase", secret: words}, chain);
          out.push({network: chain.network, main: main.on[chain.network].address,
                    imported: got.on[chain.network].address,
                    sameKey: c.hex(got.on[chain.network].key) === c.hex(main.on[chain.network].key)});
        }
        return out;""")
    assert said["ok"], said
    assert said["value"], "the node runs chains"
    for one in said["value"]:
        assert one["imported"] == one["main"] and one["sameKey"], one


def test_a_phrase_with_a_bad_checksum_is_refused(browser, served, signed_in):
    said = _js(browser, """
        return await w.importedKeys({kind: "phrase", secret: "abandon ".repeat(12).trim()},
                                    {network: "regtest", version: 111});""")
    assert not said["ok"] and "not in an order this can be" in said["error"], said


# --- the page -------------------------------------------------------------------------------------------------------

def _wallet_page(browser, base):
    browser.get(f"{base}/me/wallet")
    browser.execute_script("localStorage.setItem(arguments[0], JSON.stringify('regtest'))", f"arcade.wallet.net.{TAG}")
    browser.get(f"{base}/me/wallet")
    for _ in range(120):
        if browser.execute_script("return document.body.dataset.walletReady === 'yes'"):
            break
        time.sleep(0.25)
    else:
        raise AssertionError("the wallet page never finished drawing")
    _until(lambda: browser.find_element(By.ID, "wpicker").is_displayed(), "the wallet picker shows")


def _until(check, what, seconds=60):
    until = time.time() + seconds
    while time.time() < until:
        try:
            if check():
                return
        except Exception:                                   # noqa: BLE001 -- not drawn yet
            pass
        time.sleep(0.25)
    raise AssertionError(what)


def _menu(browser):
    browser.find_element(By.ID, "wpick").click()
    return [o.text for o in browser.find_elements(By.CSS_SELECTOR, "#wmenu .wl-opt")]


def _picked(browser) -> str:
    return browser.find_element(By.ID, "wpickname").text


def _rename(browser, name):
    browser.find_element(By.ID, "wrename").click()
    box = browser.find_element(By.ID, "rename-to")
    box.clear()
    box.send_keys(name)
    browser.find_element(By.ID, "rename-go").click()
    _until(lambda: not browser.find_elements(By.ID, "rename-go"), f"renamed to {name}")


def _no_card(browser):
    """Nothing in front of the page (a card of base.html's would catch the click); says which if one is."""
    cards = [c.text for c in browser.find_elements(By.CSS_SELECTOR, ".askcard") if c.is_displayed()]
    assert not cards, f"a card is in front of the page: {cards}"


def _import_key(browser, wif):
    _no_card(browser)
    browser.find_element(By.ID, "wpick").click()
    browser.find_element(By.CSS_SELECTOR, "#wmenu .wl-opt.import").click()
    browser.find_element(By.ID, "imp-by-key").click()
    browser.find_element(By.ID, "imp-key").send_keys(wif)
    browser.find_element(By.ID, "imp-go").click()
    # The first change on a page asks for the password (the record's key is never kept across pages).
    def asked_or_done():
        if not browser.find_elements(By.ID, "imp-go"):
            return True
        return browser.find_element(By.ID, "imp-pass").is_displayed()
    _until(asked_or_done, "the import finished or asked for the password")
    if browser.find_elements(By.ID, "imp-go"):
        _no_card(browser)
        browser.find_element(By.ID, "imp-pass").send_keys(PASSWORD)
        browser.find_element(By.ID, "imp-go").click()
    _until(lambda: not browser.find_elements(By.ID, "imp-go"),
           "the import finished: " + " ".join(e.text for e in browser.find_elements(By.ID, "imp-problem")))


def test_import_a_key_see_it_by_name_rename_it_and_its_number_is_free_again(browser, served, signed_in):
    base, state = served
    _wallet_page(browser, base)
    # The account's own wallet takes the lowest free number, kept until renamed.
    _until(lambda: _picked(browser) == "Wallet 1", "the main wallet is Wallet 1")
    _rename(browser, "Spending")
    _until(lambda: _picked(browser) == "Spending", "the main wallet is renamed")

    first = _wif(hashlib.sha256(b"first import").digest(), 0xf1)
    _import_key(browser, first)
    _until(lambda: _picked(browser) == "Wallet 1", "the import is Wallet 1")
    shown = _menu(browser)
    assert shown[0].startswith("Spending") and shown[1].startswith("Wallet 1") and shown[-1] == "+ Import a wallet"
    browser.find_element(By.ID, "wpick").click()
    # Never by its address: the address is under Receive only.
    assert browser.find_element(By.ID, "addrchip").is_displayed() is False
    browser.find_element(By.ID, "do-recv").click()
    address = browser.find_element(By.CSS_SELECTOR, ".wl-sheet .wl-addrbig").text
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    pub = ec.derive_private_key(int.from_bytes(hashlib.sha256(b"first import").digest(), "big"),
                                ec.SECP256K1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
    assert address == script.b58check_encode(NETWORKS["regtest"].pubkeyhash_version, script.hash160(pub))
    browser.find_element(By.CSS_SELECTOR, ".wl-sheet .wl-x[aria-label='Close']").click()

    _rename(browser, "Savings")
    _until(lambda: _picked(browser) == "Savings", "the import is renamed")
    # "Wallet 1" is free again: the next import takes it.
    _import_key(browser, _wif(hashlib.sha256(b"second import").digest(), 0xf1))
    _until(lambda: _picked(browser) == "Wallet 1", "the second import is Wallet 1")
    shown = _menu(browser)
    names = [t.split("\n")[0] for t in shown]
    assert names[:3] == ["Spending", "Savings", "Wallet 1"], shown
    browser.find_element(By.ID, "wpick").click()

    # The secrets are sealed on the node, never in the clear; the node's list has the two addresses.
    import json
    from arcade import accounts
    pubkey = state.vault().get(TAG)["pubkey"]
    record = state.imported_wallets().get(pubkey)
    assert record and accounts._sealed_shape(record["blob"])
    assert first not in record["blob"]
    assert len(W.entries(state.setting(W.setting_key("regtest", pubkey)))) == 2
    # And the main vault is the blob sign-up made, whatever happened since.
    assert set(json.loads(state.vault().get(TAG)["blob"])) == {"kdf", "iterations", "salt", "nonce", "sealed"}


def test_a_wif_for_another_coin_is_refused_on_the_page(browser, served, signed_in):
    base, _ = served
    _wallet_page(browser, base)
    browser.find_element(By.ID, "wpick").click()
    browser.find_element(By.CSS_SELECTOR, "#wmenu .wl-opt.import").click()
    browser.find_element(By.ID, "imp-by-key").click()
    browser.find_element(By.ID, "imp-key").send_keys(_wif(hashlib.sha256(b"ltc").digest(), 0xb0))
    browser.find_element(By.ID, "imp-go").click()
    _until(lambda: browser.find_element(By.ID, "imp-problem").is_displayed(), "the refusal shows")
    assert "Litecoin mainnet" in browser.find_element(By.ID, "imp-problem").text
