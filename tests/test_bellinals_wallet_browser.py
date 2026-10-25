"""A Bellinal in the Wallet, in a real browser (2026-10-25): found on the account's own Bellscoin coins through this
node, shown with Send and List (the Exchange's Bellinals tab) and no Swap -- a lock is not made on Bellscoin -- and the
Send goes as far as the review that names the Bellinal and the recipient. Nothing here asks a real chain."""

import pathlib
import socket
import sys
import threading
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                  # noqa: E402
from test_funding import _pubkey                                 # noqa: E402

from selenium.webdriver.common.by import By                      # noqa: E402

from arcade import doginals, funding                             # noqa: E402
from arcade.script import b58check_decode, b58check_encode, hash160   # noqa: E402

T1, T3 = "71" * 32, "73" * 32
INS = T1 + "i0"
THEM = b58check_encode(25, hash160(_pubkey(int("32" * 32, 16))))
ANSWERS = {T1: {"txid": T1, "inscriptions": [{"vout": 0, "inscription_id": INS, "sat_offset": 0}]},
           T3: {"txid": T3, "inscriptions": []}}


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
        home=tmp_path_factory.mktemp("belwallet"),
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=Path("/nonexistent")),
        more={"bel": ChainContext(network="bel", role="ledger", label="Bellscoin", conf=Path("/nonexistent/bells.conf"))},
    )
    state._sync["bel"] = (time.time() + 86400, {"ready": True, "progress": 1.0, "online": True,
                                                "blocks": 100, "headers": 100})
    saved = (funding.GUARD, funding.GUARDS, funding.GUARD_BY_NET)
    app = create_app(state)
    guard = doginals.Doginals(state.home / "bel.sqlite", fetch=lambda txid: ANSWERS.get(txid, {"txid": txid,
                                                                                              "inscriptions": []}))
    funding.GUARDS = [(lambda a: b58check_decode(a)[0] == 25, guard)]
    funding.GUARD_BY_NET = {"bel": guard}
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
    yield f"http://127.0.0.1:{port}", state
    server.should_exit = True
    thread.join(timeout=5)
    funding.GUARD, funding.GUARDS, funding.GUARD_BY_NET = saved


@pytest.fixture(scope="module")
def signed_in(browser, served):
    """An account made the way /join makes one, with two coins on its Bellscoin address: one carrying a Bellinal."""
    base, state = served
    browser.get(f"{base}/join")
    browser.delete_all_cookies()
    result = browser.execute_async_script("""
        const done = arguments[2];
        import("/wallet.js").then(async (w) => {
          await w.signUp(arguments[0], arguments[1], {network: "regtest", version: 111});
          const me = await w.opened({network: "regtest", version: 111});
          await w.tellTheNode(me);
          const row = (await w.state()).chains.find((c) => c.network === "bel");
          done({ok: true, address: row && row.address});
        }).catch((e) => done({ok: false, error: String(e.message || e)}));
        """, "belwallettester", "a password nobody needs to guess for this test")
    assert result.get("ok") and str(result.get("address", "")).startswith("B"), result
    bel = state.more["bel"]
    with state.token_index(bel).open() as db:
        for txid, value in ((T1, 100_000), (T3, 300_000_000)):
            db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                            (txid, 0, result["address"], value, 1))
        db.conn.commit()
    return result["address"]


def _wait(browser, script, seconds=20):
    for _ in range(seconds * 4):
        got = browser.execute_script(script)
        if got:
            return got
        time.sleep(0.25)
    raise AssertionError(f"never: {script}\n{browser.find_element(By.ID, 'doginals').text}")


def test_a_bellinal_has_send_and_list_and_no_swap(browser, served, signed_in):
    base, _ = served
    browser.execute_script("localStorage.clear()")
    browser.get(f"{base}/me/wallet")
    _wait(browser, "return document.body.dataset.walletReady === 'yes'")
    browser.find_element(By.ID, "tab-nfts").click()
    bel = [b for b in browser.find_elements(By.CSS_SELECTOR, "#nets button") if b.get_attribute("data-net") == "bel"]
    assert bel, "the Bellscoin button"
    bel[0].click()
    card = f'#doginals [data-doginal=\\"{INS}\\"]'
    _wait(browser, f'return document.querySelector("{card}")')
    text = browser.execute_script('return document.getElementById("doginals").textContent')
    assert "Bellinals" in text and "not ready yet" not in text, text
    links = browser.execute_script(f'return [...document.querySelectorAll("{card} .wl-piecesend")]'
                                   '.map(e => [e.textContent, e.getAttribute("href") || ""])')
    assert links == [["Send", ""], ["List", "/exchange?tab=bellinals"]], links

    browser.find_element(By.CSS_SELECTOR, f'#doginals [data-doginal="{INS}"] button').click()
    to = _wait(browser, 'return document.getElementById("send-to")')
    to.send_keys(THEM)
    review = [b for b in browser.find_elements(By.CSS_SELECTOR, ".wl-sheet button.wl-go") if b.text == "Review"]
    review[0].click()
    sheet = _wait(browser, 'const s = document.querySelector(".wl-sheet");'
                           ' return s && s.textContent.includes("You\'re sending") && s.textContent')
    assert THEM in sheet and "Bellinal" in sheet, sheet
