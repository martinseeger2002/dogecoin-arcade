"""The Wallet's Doginals in a real browser: the page asks a (fake) Doginals source about the account's own Dogecoin coins,
keeps each answer, shows the Doginal with a Send button, and the Send goes as far as the review that names the Doginal
and the recipient. And when the answer cannot be had the page says so. Nothing here asks a real one."""

import json
import pathlib
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                  # noqa: E402
from test_funding import _pubkey                                 # noqa: E402

from selenium.webdriver.common.by import By                      # noqa: E402

from arcade import doginals, funding                             # noqa: E402
from arcade.script import b58check_encode, hash160               # noqa: E402

T1, T3 = "11" * 32, "33" * 32
INS = T1 + "i0"
THEM = b58check_encode(30, hash160(_pubkey(int("32" * 32, 16))))
ANSWERS = {T1: {"txid": T1, "inscriptions": [{"vout": 0, "inscription_id": INS, "sat_offset": 0}]},
           T3: {"txid": T3, "inscriptions": []}}
#: A 1x1 PNG, for the preview.
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def reads():
    """The node's chain reading, faked from ANSWERS, counting each transaction it is asked to read (2026-10-15: no
    outside index; the node reads its own chain)."""
    asked = []

    def read(txid):
        asked.append(txid)
        return ANSWERS.get(txid, {"txid": txid, "inscriptions": []})
    return read, asked


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    yield driver
    driver.quit()


@pytest.fixture(scope="module")
def served(tmp_path_factory, reads):
    import uvicorn
    from pathlib import Path
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    state = AppState(
        home=tmp_path_factory.mktemp("dgwallet"),
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=Path("/nonexistent")),
        doge=ChainContext(network="doge", role="ledger", label="Dogecoin", conf=Path("/nonexistent/dogecoin.conf")),
    )
    state._sync["doge"] = (time.time() + 86400, {"ready": True, "progress": 1.0, "online": True,
                                                 "blocks": 100, "headers": 100})
    real_guard = funding.GUARD
    app = create_app(state)
    funding.GUARD = doginals.Doginals(state.home / "dg.sqlite", fetch=reads[0])
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
    funding.GUARD = real_guard


@pytest.fixture(scope="module")
def signed_in(browser, served):
    """An account made the way /join makes one, with two coins on its Dogecoin address: one carrying a Doginal."""
    base, state = served
    browser.get(f"{base}/join")
    browser.delete_all_cookies()
    result = browser.execute_async_script("""
        const done = arguments[2];
        import("/wallet.js").then(async (w) => {
          await w.signUp(arguments[0], arguments[1], {network: "regtest", version: 111});
          const me = await w.opened({network: "regtest", version: 111});
          await w.tellTheNode(me);
          const row = (await w.state()).chains.find((c) => c.network === "doge");
          done({ok: true, address: row && row.address});
        }).catch((e) => done({ok: false, error: String(e.message || e)}));
        """, "dgwallettester", "a password nobody needs to guess for this test")
    assert result.get("ok") and str(result.get("address", "")).startswith("D"), result
    with state.token_index(state.doge).open() as db:
        for txid, value in ((T1, 100_000), (T3, 300_000_000)):
            db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                            (txid, 0, result["address"], value, 1))
        db.conn.commit()
    return result["address"]


def _open_doginals(browser, base):
    browser.get(f"{base}/me/wallet")
    for _ in range(80):
        if browser.execute_script("return document.body.dataset.walletReady === 'yes'"):
            break
        time.sleep(0.25)
    browser.find_element(By.ID, "tab-nfts").click()
    doge = [b for b in browser.find_elements(By.CSS_SELECTOR, "#nets button")
            if b.get_attribute("data-net") == "doge" or b.text.strip().startswith("D")]   # an icon since 2026-10-18
    assert doge, "the Dogecoin button"
    doge[0].click()


def _wait(browser, script, seconds=20):
    for _ in range(seconds * 4):
        got = browser.execute_script(script)
        if got:
            return got
        time.sleep(0.25)
    raise AssertionError(f"never: {script}\n{browser.find_element(By.ID, 'doginals').text}")


def test_the_wallet_finds_its_doginal_asks_once_and_sends_it_to_review(browser, served, signed_in, reads):
    base, _ = served
    _, asked = reads
    browser.execute_script("localStorage.clear()")
    _open_doginals(browser, base)
    _wait(browser, f'return document.querySelector("#doginals [data-doginal=\\"{INS}\\"]")')
    text = browser.execute_script('return document.getElementById("doginals").textContent')
    assert "Doginals" in text and INS in text and "Read from the chain" in text
    src = browser.execute_script(f'return document.querySelector("#doginals [data-doginal=\\"{INS}\\"] iframe").src')
    assert src == f"{base}/content/{INS}/fit", "this arcade's own /content, read off the chain, fitted to the frame"
    assert len(browser.find_elements(By.CSS_SELECTOR, "#doginals [data-doginal]")) == 1, "the clean coin is not one"
    assert sorted(set(asked)) == [T1], "a coin too big to be Doginal postage is never asked about"
    count = len(asked)
    # A block, an answer, a redraw: the picture frame is the same element, not built (and loaded) again.
    browser.execute_script(f'window.__dgframe = document.querySelector("#doginals [data-doginal=\\"{INS}\\"] iframe");'
                           'document.dispatchEvent(new Event("arcade:changed"))')
    time.sleep(2)
    assert browser.execute_script(f'return window.__dgframe === document.querySelector('
                                  f'"#doginals [data-doginal=\\"{INS}\\"] iframe")'), "the frame was rebuilt"

    _open_doginals(browser, base)            # again: answered from this browser (and the node), nothing read again
    _wait(browser, f'return document.querySelector("#doginals [data-doginal=\\"{INS}\\"]")')
    assert len(asked) == count

    browser.find_element(By.CSS_SELECTOR, f'#doginals [data-doginal="{INS}"] button').click()
    to = _wait(browser, 'return document.getElementById("send-to")')
    to.send_keys(THEM)
    review = [b for b in browser.find_elements(By.CSS_SELECTOR, ".wl-sheet button.wl-go") if b.text == "Review"]
    review[0].click()
    sheet = _wait(browser, 'const s = document.querySelector(".wl-sheet");'
                           ' return s && s.textContent.includes("You\'re sending") && s.textContent')
    assert THEM in sheet and "Doginal" in sheet, sheet


def test_a_chain_that_cannot_be_read_is_said_plainly(browser, served, signed_in):
    base, _ = served
    browser.execute_script("localStorage.clear()")
    real_guard = funding.GUARD

    def down(txid):
        raise OSError("the node is not answering")
    # A node that has kept no answers yet, and cannot read its chain just now.
    funding.GUARD = doginals.Doginals(served[1].home / "dg-empty.sqlite", fetch=down)
    try:
        _open_doginals(browser, base)
        _wait(browser, 'return document.getElementById("doginals").textContent'
                       '.includes("Inscriptions can\'t be checked right now")')
        assert not browser.find_elements(By.CSS_SELECTOR, "#doginals [data-doginal]"), "nothing guessed"
    finally:
        funding.GUARD = real_guard


def test_many_doginals_come_a_page_at_a_time(browser, served, signed_in, reads):
    """2026-10-21: "I have like over 1500 doginals in that wallet. Make sure they have good pagination. And lazy
    loading and caching." Thirty cards a page, a hundred transactions a call to the node, and a page's frames only."""
    base, state = served
    many = [f"{n:064x}" for n in range(0x5000, 0x5000 + 65)]
    for t in many:
        ANSWERS[t] = {"txid": t, "inscriptions": [{"vout": 0, "inscription_id": t + "i0", "sat_offset": 0}]}
    with state.token_index(state.doge).open() as db:
        for t in many:
            db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                            (t, 0, signed_in, 100_000, 2))
        db.conn.commit()
    browser.execute_script("localStorage.clear(); sessionStorage.clear()")
    _open_doginals(browser, base)
    _wait(browser, 'return document.querySelector(".wl-dgpager select")')
    _wait(browser, 'return document.querySelector(".wl-dgpager").textContent.includes("66 Doginals")')
    assert len(browser.find_elements(By.CSS_SELECTOR, "#doginals [data-doginal]")) == 30
    assert len(browser.find_elements(By.CSS_SELECTOR, "#doginals iframe")) == 30, "only this page's pictures"
    pages = browser.execute_script('return [...document.querySelectorAll(".wl-dgpager option")].map(o => o.text)')
    assert pages == ["Page 1 of 3", "Page 2 of 3", "Page 3 of 3"]
    first = browser.execute_script('return [...document.querySelectorAll("#doginals [data-doginal]")]'
                                   '.map(c => c.dataset.doginal)')
    [b for b in browser.find_elements(By.CSS_SELECTOR, ".wl-dgpager button") if "Next" in b.text][0].click()
    _wait(browser, 'return document.querySelector(".wl-dgpager select").value === "1"')
    second = browser.execute_script('return [...document.querySelectorAll("#doginals [data-doginal]")]'
                                    '.map(c => c.dataset.doginal)')
    assert len(second) == 30 and not set(first) & set(second)
    browser.execute_script('const s = document.querySelector(".wl-dgpager select"); s.value = "2";'
                           ' s.dispatchEvent(new Event("change"))')
    _wait(browser, 'return document.querySelectorAll("#doginals [data-doginal]").length === 6')
    # Back again: the page is remembered and every answer comes from this browser, none from the node.
    count = len(reads[1])
    _open_doginals(browser, base)
    _wait(browser, 'return document.querySelectorAll("#doginals [data-doginal]").length === 6')
    assert len(reads[1]) == count
