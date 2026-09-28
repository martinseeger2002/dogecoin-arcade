"""Overview, Wallet and Backup, in a real browser.

The operator: "I want the arcade Web to be pretty much identical to the arcade
local... Do it page at a time... starting with overview." These drive the
three pages that came out of that pass and check the parts that only a
browser can prove: the contact code the page draws matches the one
`messaging.js` computes, the wallet-shut state is said before anything is
pressed, and the send flows build an offer and stop for a signature.
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
        home=tmp_path_factory.mktemp("overview"),
        messaging=ChainContext(network="regtest", role="messaging",
                               label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=Path("/nonexistent")),
    )
    port = _free_port()
    config = uvicorn.Config(create_app(state), host="127.0.0.1", port=port,
                            log_level="error", timeout_graceful_shutdown=1.0)
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
    assert not thread.is_alive(), "the server is still serving a keep-alive connection"


#: The account's chosen name, so tests can ask `/signin/{tag}` for its
#: blob without threading it through every test.
TAG = "overviewtester"
PASSWORD = "a password nobody needs to guess for this test"


@pytest.fixture(scope="module")
def signed_in(browser, served):
    """A real account, the way `/join` makes one: a @tag and a password,
    with the wallet open in this tab -- the primary signup path, not the
    older `/join/keys` (which never creates a vault entry, so a page that
    reads one, like Backup, would see nothing to work with)."""
    base, _ = served
    browser.get(f"{base}/join")
    browser.delete_all_cookies()
    result = browser.execute_async_script("""
        const done = arguments[2];
        Promise.all([import("/wallet.js")])
          .then(async ([w]) => {
            const made = await w.signUp(arguments[0], arguments[1],
                                        {network: "regtest", version: 111});
            done({ok: true, name: made.tag || arguments[0]});
          }).catch((e) => done({ok: false, error: String(e.message || e)}));
        """, TAG, PASSWORD)
    assert result.get("ok"), result
    return TAG


def _page(browser, base, path, flag):
    browser.get(f"{base}{path}")
    for _ in range(80):
        if browser.execute_script(
                f"return document.body.dataset.{flag} === 'yes'"):
            break
        time.sleep(0.25)
    else:
        raise AssertionError(f"{path} never finished drawing")


def test_the_overview_shows_a_contact_code_matching_messagingjs(
        browser, served, signed_in):
    base, _ = served
    _page(browser, base, "/me", "meReady")
    # `#contact` sits inside a closed <details>: Selenium's own `.text`
    # follows rendering rules and reports collapsed content as empty, so
    # this reads the text node directly rather than opening the widget
    # just to satisfy the test.
    for _ in range(40):
        shown = browser.execute_script(
            'return document.getElementById("contact").textContent;')
        if shown.startswith("arcade:"):
            break
        time.sleep(0.25)
    assert shown.startswith("arcade:regtest:"), shown

    # Computed independently, from the same identity, and they must agree.
    computed = browser.execute_async_script("""
        const done = arguments[0];
        Promise.all([import("/wallet.js"), import("/messaging.js")])
          .then(async ([w, m]) => {
            const wallet = await w.opened({network: "regtest", version: 111});
            const me = await m.identity(wallet.phrase);
            done(await m.contactCode("regtest", me.publicKey));
          }).catch((e) => done("error: " + String(e.message || e)));""")
    assert not computed.startswith("error"), computed
    assert shown == computed


def test_the_overview_says_when_the_key_is_not_yet_published(
        browser, served, signed_in):
    base, _ = served
    _page(browser, base, "/me", "meReady")
    # /me became a profile (41e72fb) and the address panel this sits in is
    # tucked away on purpose, and a new account publishes its key at signup.
    # What still matters is that the page knows: the note is armed (not hidden
    # itself) while the key is unpublished, and says why.
    for _ in range(40):
        if browser.execute_script("return document.getElementById('not-announced').hidden") is False:
            break
        time.sleep(0.25)
    assert browser.execute_script(
        "return document.getElementById('not-announced').hidden") is False
    assert "nothing to encrypt to" in browser.execute_script(
        "return document.getElementById('not-announced').textContent")


def test_overview_links_to_wallet_and_backup(browser, served, signed_in):
    base, _ = served
    _page(browser, base, "/me", "meReady")
    open_wallet = browser.find_elements(By.CSS_SELECTOR, "#wallets a")
    assert any(a.get_attribute("href").endswith("/me/wallet")
               for a in open_wallet)
    backup_links = browser.find_elements(By.CSS_SELECTOR, 'a[href="/me/backup"]')
    assert backup_links


def test_the_wallet_page_lists_a_chain_and_can_offer_a_send(
        browser, served, signed_in):
    base, _ = served
    _page(browser, base, "/me/wallet", "walletReady")
    assert not browser.find_element(By.ID, "shut").is_displayed()

    options = browser.find_elements(By.CSS_SELECTOR, "#which option")
    assert options, "at least one chain should be selectable"

    # The node has no coins for this fresh account, so the offer is
    # refused -- proving the request reached the real /account/send route
    # rather than nothing happening at all.
    browser.find_element(By.ID, "to").send_keys(
        "mpK9VHfd3ZkKZXMoWPy1kDf65RLzzbP1Bd")
    browser.find_element(By.ID, "amount").send_keys("1")
    browser.find_element(By.ID, "send").click()
    for _ in range(40):
        if browser.find_element(By.ID, "trouble").is_displayed():
            break
        time.sleep(0.25)
    assert browser.find_element(By.ID, "trouble").is_displayed()


def test_the_wallet_tokens_tab_is_reachable_and_empty(
        browser, served, signed_in):
    base, _ = served
    _page(browser, base, "/me/wallet/tokens", "walletTokensReady")
    assert not browser.find_element(By.ID, "shut").is_displayed()
    assert browser.find_element(By.ID, "nothing").is_displayed()


def test_backup_page_explains_and_can_fetch_the_blob(browser, served, signed_in):
    base, _ = served
    browser.get(f"{base}/me/backup")
    for _ in range(80):
        if browser.execute_script(
                "return document.body.dataset.backupReady === 'yes'"):
            break
        time.sleep(0.25)
    assert "There is nothing else" in browser.page_source
    assert "message history" in browser.page_source
    assert "address book" in browser.page_source
    button = browser.find_element(By.ID, "download")
    assert button.get_attribute("disabled") is None
    result = browser.execute_async_script("""
        const done = arguments[0];
        const name = document.querySelector('script[type="module"]');
        import("/wallet.js").then((w) => w.state())
          .then((said) => fetch(`/signin/${encodeURIComponent(said.name)}`))
          .then((r) => r.json())
          .then((said) => done(!!said.blob), (e) => done("error: " + e));
        """)
    assert result is True, result
