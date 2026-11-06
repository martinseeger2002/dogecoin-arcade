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
    browser.find_element(By.ID, "coinpick").click()                  # the coins are tiles in a menu (PR #38)
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
    # A tile shows its number (once read) and collection, not its id (2026-11-05); the id is on its card.
    assert "Doginals" in text and "Read from the chain" in text
    assert browser.find_elements(By.CSS_SELECTOR, f'#doginals .dg-tile[data-doginal="{INS}"]')
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
    # A coin arrives meanwhile, so the arcade builds the list again (2026-11-06: it is built from the guard, once per
    # change of the address's coins).
    fresh_coin = "ee" * 32
    with served[1].token_index(served[1].doge).open() as db:
        db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                        (fresh_coin, 0, signed_in, 100_000, 2))
        db.conn.commit()
    try:
        _open_doginals(browser, base)
        _wait(browser, 'return document.getElementById("doginals").textContent'
                       '.includes("Inscriptions can\'t be checked right now")')
        assert not browser.find_elements(By.CSS_SELECTOR, "#doginals [data-doginal]"), "nothing guessed"
    finally:
        funding.GUARD = real_guard
        with served[1].token_index(served[1].doge).open() as db:
            db.conn.execute("DELETE FROM utxo WHERE txid=?", (fresh_coin,))
            db.conn.commit()


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


def test_doginals_come_in_inscription_order_with_their_collections_and_a_card(browser, served, signed_in, reads):
    """2026-11-05, the operator: "they should be ordered by inscription number earliest first", "if they are a part of a
    collection, they should have the collection metadata with them", "Each one should be able to be clicked, and
    their card should open with all of the information and a content link"; Send and one List on the card."""
    import random
    base, state = served
    txs = [f"{n:064x}" for n in range(0x7000, 0x7000 + 40)]
    rng = random.Random(5)
    numbers = {t + "i0": n for t, n in zip(txs, rng.sample(range(1000, 900000), len(txs)))}
    texts = set(txs[::5])
    for t in txs:
        ANSWERS[t] = {"txid": t, "inscriptions": [{"vout": 0, "inscription_id": t + "i0", "sat_offset": 0}]}
    with state.token_index(state.doge).open() as db:
        for t in txs:
            db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                            (t, 0, signed_in, 100_000, 3))
        db.conn.commit()
    funding.GUARD.ctype_of = lambda iid: "text/plain" if iid[:64] in texts else "image/png"
    asked = []

    class Index:
        tip = 6_000_000

        def lookup(self, q):
            asked.append(q.get("what"))
            if q["what"] == "numbers":
                return {"numbers": {i: {"number": numbers[i], "height": 4_700_000, "content_type": "image/png",
                                        "size": 900} for i in q["ids"] if i in numbers}, "walked_to": 6_000_000}
            if q["what"] == "number":
                i = q["id"]
                return {"number": {"inscription": {"id": i, "number": numbers.get(i, 0), "height": 4_700_000,
                                                   "content_type": "image/png", "size": 900, "output": f"{i[:64]}:0",
                                                   "offset": 0, "owner": signed_in}}}
            raise ValueError("no such question")

    class Known:
        def in_collection(self, chain, iid):
            if iid in list(numbers)[:12]:
                return {"slug": "dogepunks", "collection": "DogePunks", "position": list(numbers).index(iid) + 1,
                        "name": f"DogePunk #{list(numbers).index(iid) + 1}", "source": "nintondo"}
            return None

        def collection(self, chain, slug):
            return {"supply": 10000, "owners": 2210, "created": 1677000000}
    real = (state.ord_indexes, getattr(state, "known_collections", None))
    state.ord_indexes = lambda: {"doge": Index()}
    state.known_collections = lambda: Known()
    try:
        browser.execute_script("localStorage.clear(); sessionStorage.clear()")
        browser.set_window_size(1100, 1300)
        _open_doginals(browser, base)
        # Once the numbers are in, the first page is the earliest of them all.
        _wait(browser, 'return !document.getElementById("doginals").textContent.includes("Reading numbers")'
                       ' && document.querySelectorAll("#doginals .dg-num").length >= 30', 40)
        shown = browser.execute_script('return [...document.querySelectorAll("#doginals .dg-tile")].map(c => c.dataset.doginal)')
        mine = sorted((n, i) for i, n in numbers.items())
        assert shown[:10] == [i for _, i in mine[:10]], "earliest number first"
        assert "numbers" in asked
        tiles = browser.execute_script('return document.getElementById("doginals").textContent')
        assert "DogePunks" not in tiles or "DogePunk #" in tiles
        assert "Pictures & pages" in tiles and "Text" in tiles
        browser.execute_script('document.getElementById("doginals").scrollIntoView()')
        browser.save_screenshot(str(pathlib.Path.home() / "ordmock" / "real-grid.png"))
        # Newest first, then back.
        browser.execute_script('const s = [...document.querySelectorAll("#doginals .dg-pick")][1]; s.value = "new";'
                               ' s.dispatchEvent(new Event("change"))')
        _wait(browser, f'return document.querySelector("#doginals .dg-tile").dataset.doginal === {json.dumps(mine[-1][1])}')
        browser.execute_script('const s = [...document.querySelectorAll("#doginals .dg-pick")][1]; s.value = "old";'
                               ' s.dispatchEvent(new Event("change"))')
        # A collection piece's card: its number, collection and the content link, Send and one List.
        punk = next(i for _, i in mine if i in list(numbers)[:12] and i[:64] not in texts)
        browser.execute_script(f'document.getElementById("doginals").querySelector(".dg-go").value = "#{numbers[punk]}";'
                               'document.getElementById("doginals").querySelector(".dg-go").dispatchEvent('
                               'new KeyboardEvent("keydown", {key: "Enter"}))')
        card = _wait(browser, 'const m = document.querySelector(".dg-modal");'
                              ' return m && m.textContent.includes("Open content") && m.textContent')
        assert f"#{numbers[punk]:,}" in card and "DogePunks" in card and "10,000 pieces" in card, card
        assert "Send" in card and card.count("List") == 2, "one List button (and the sentence about it)"
        link = browser.execute_script('return [...document.querySelectorAll(".dg-modal a")].find(a => '
                                      'a.textContent.startsWith("Open content")).getAttribute("href")')
        assert link == f"/content/{punk}"
        browser.save_screenshot(str(pathlib.Path.home() / "ordmock" / "real-card.png"))
        browser.execute_script('document.querySelector(".dg-x").click()')
        assert not browser.find_elements(By.CSS_SELECTOR, ".dg-modal")
    finally:
        state.ord_indexes = real[0]
        if real[1] is not None:
            state.known_collections = real[1]


def test_drc20_mints_are_left_out_of_the_doginals_grid(browser, served, signed_in, reads):
    """2026-11-05, the operator: "it is showing a bunch of DRC 20 transactions along with my doginals" -- "The DRC 20 mints
    should be [out] from the ORD grid but we shouldn't count them towards tokens". The index says which are this
    chain's token operations (meta.token): they leave the grid and its counts; a plain text one stays."""
    base, state = served
    txs = [f"{n:064x}" for n in range(0x7400, 0x7400 + 12)]
    mints, note = set(txs[:8]), txs[8]
    for t in txs:
        ANSWERS[t] = {"txid": t, "inscriptions": [{"vout": 0, "inscription_id": t + "i0", "sat_offset": 0}]}
    with state.token_index(state.doge).open() as db:
        for t in txs:
            db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                            (t, 0, signed_in, 100_000, 3))
        db.conn.commit()
    funding.GUARD.ctype_of = lambda iid: "text/plain" if iid[:64] in mints or iid[:64] == note else "image/png"

    class Index:
        tip = 6_000_000

        def lookup(self, q):
            if q["what"] == "numbers":
                return {"numbers": {i: {"number": 500 + txs.index(i[:64]), "height": 4_700_000,
                                        "content_type": "text/plain" if i[:64] in mints or i[:64] == note else "image/png",
                                        "size": 60, "protocol": "drc-20" if i[:64] in mints else None}
                                    for i in q["ids"] if i[:64] in txs}, "walked_to": 6_000_000}
            raise ValueError("no such question")
    real = state.ord_indexes
    state.ord_indexes = lambda: {"doge": Index()}
    try:
        browser.execute_script("localStorage.clear(); sessionStorage.clear()")
        browser.set_window_size(1100, 1300)
        _open_doginals(browser, base)
        _wait(browser, 'return document.getElementById("doginals").textContent.includes("token inscriptions are")', 40)
        shown = browser.execute_script('return [...document.querySelectorAll("#doginals .dg-tile")].map(c => c.dataset.doginal)')
        mine = [i for i in shown if i[:64] in txs]                  # (the earlier tests' coins are here too)
        assert sorted(mine) == sorted(t + "i0" for t in txs if t not in mints), mine
        text = browser.execute_script('return document.getElementById("doginals").textContent')
        assert "8 token inscriptions are not shown here" in text and "Tokens tab" in text
        browser.execute_script('document.getElementById("doginals").scrollIntoView()')
        browser.save_screenshot(str(pathlib.Path.home() / "ordmock" / "no-mints.png"))
    finally:
        state.ord_indexes = real


def test_drc20_tokens_are_in_the_tokens_tab_and_send_like_any_token(browser, served, signed_in):
    """2026-11-05, the operator: "make sure we have DRC 20s in the Wallet tokens tab they should be able to be sent just like
    any other of our tokens like on Pepe": the balance from the ord index, tapped, and Send is for that token."""
    base, state = served

    class Index:
        tip = 6_000_000

        def lookup(self, q):
            if q["what"] == "balances":
                return {"balances": [{"tick": "dogi", "units": "150000", "transferable_units": "0", "decimals": 2}],
                        "walked_to": 6_000_000}
            raise ValueError("no such question")
    real = state.ord_indexes
    state.ord_indexes = lambda: {"doge": Index()}
    try:
        _open_doginals(browser, base)
        browser.find_element(By.ID, "tab-tokens").click()
        row = _wait(browser, 'return [...document.querySelectorAll(".wl-tok")].find(r => r.textContent.includes("dogi"))')
        assert "DRC-20" in row.text and "1,500" in row.text, row.text
        row.click()
        _wait(browser, 'return document.getElementById("unit") && document.body.textContent.includes("Send is for it now")')
        browser.save_screenshot(str(pathlib.Path.home() / "ordmock" / "real-drc20.png"))
        browser.find_element(By.ID, "do-send").click()
        sheet = _wait(browser, 'const s = document.querySelector(".wl-sheet"); return s && s.textContent')
        assert "dogi" in sheet, sheet
        browser.save_screenshot(str(pathlib.Path.home() / "ordmock" / "real-drc20-send.png"))
    finally:
        state.ord_indexes = real
