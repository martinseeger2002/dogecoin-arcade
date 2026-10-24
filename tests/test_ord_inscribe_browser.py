"""An ord inscribed from the Creator tab in a real browser (2026-10-23): the page picks Dogecoin and a file, the
mainnet review names the costs once, and the tab signs the commit and every reveal itself (wallet.js inscribeOrd checks
each before it signs). The Dogecoin node is faked; every signature it is handed is checked against its own digest."""

import os
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

from arcade import doginals, funding, ord_inscribe as oi, rawchain  # noqa: E402
from arcade.doginal_market import verify_signature              # noqa: E402
from arcade.ord_rules import decode_push_datas                   # noqa: E402
from arcade.txbuild import p2pkh_script                          # noqa: E402

FUNDS = "77" * 32


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeDoge:
    """sendrawtransaction keeps what it is given; every block is mined at once, so no wait."""

    def __init__(self):
        self.sent: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def call(self, method, *args):
        if method == "decoderawtransaction":
            tx = rawchain.read_tx(bytes.fromhex(args[0]))
            return {"vin": [{"txid": i["txid"], "vout": i["vout"]} for i in tx.vin],
                    "vout": [{"n": n, "value": v / 1e8, "scriptPubKey": {"hex": s.hex()}}
                             for n, (v, s) in enumerate(tx.vout)]}
        if method == "sendrawtransaction":
            tx = rawchain.read_tx(bytes.fromhex(args[0]))
            self.sent[tx.txid] = args[0]
            return tx.txid
        if method == "getrawtransaction":
            return {"txid": args[0], "confirmations": 1}
        raise AssertionError(method)


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
        home=tmp_path_factory.mktemp("ordweb"),
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=Path("/nonexistent")),
        doge=ChainContext(network="doge", role="ledger", label="Dogecoin", conf=Path("/nonexistent/dogecoin.conf")),
    )
    state._sync["doge"] = (time.time() + 86400, {"ready": True, "progress": 1.0, "online": True,
                                                 "blocks": 100, "headers": 100})
    fake = FakeDoge()
    real_rpc = ChainContext.rpc
    ChainContext.rpc = lambda self: fake if self.network in ("doge", "regtest") else real_rpc(self)
    real_guard = funding.GUARD
    app = create_app(state)
    funding.GUARD = doginals.Doginals(state.home / "dg.sqlite",
                                      fetch=lambda txid: {"txid": txid, "inscriptions": []})
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
    yield f"http://127.0.0.1:{port}", state, fake
    server.should_exit = True
    thread.join(timeout=5)
    funding.GUARD = real_guard
    ChainContext.rpc = real_rpc


@pytest.fixture(scope="module")
def signed_in(browser, served):
    base, state, _ = served
    browser.get(f"{base}/join")
    browser.delete_all_cookies()
    result = browser.execute_async_script("""
        const done = arguments[2];
        import("/wallet.js").then(async (w) => {
          await w.signUp(arguments[0], arguments[1], {network: "regtest", version: 111});
          const me = await w.opened({network: "regtest", version: 111});
          await w.tellTheNode(me);
          const rows = (await w.state()).chains;
          const row = rows.find((c) => c.network === "doge"), tag = rows.find((c) => c.network === "regtest");
          done({ok: true, address: row && row.address, tag: tag && tag.address});
        }).catch((e) => done({ok: false, error: String(e.message || e)}));
        """, "ordtester", "a password nobody needs to guess for this test")
    assert result.get("ok") and str(result.get("address", "")).startswith("D"), result
    with state.token_index(state.doge).open() as db:
        db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                        (FUNDS, 0, result["address"], 50 * 100_000_000, 1))
        db.conn.commit()
    with state.token_index(state.messaging).open() as db:
        db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                        ("88" * 32, 0, result["tag"], 50 * 100_000_000, 1))
        db.conn.commit()
    return result["address"]


def _wait(browser, script, seconds=40):
    for _ in range(seconds * 4):
        got = browser.execute_script(script)
        if got:
            return got
        time.sleep(0.25)
    raise AssertionError(f"never: {script}\n{browser.find_element(By.TAG_NAME, 'main').text[:2000]}")


def test_an_ord_goes_on_from_the_creator_tab_signed_in_the_tab(browser, served, signed_in, tmp_path):
    base, _state, fake = served
    browser.get(f"{base}/create")
    assert "Creator" in browser.find_element(By.TAG_NAME, "h1").text
    browser.find_element(By.CSS_SELECTOR, 'a[href="/create/ord"]').click()
    _wait(browser, 'return [...document.querySelectorAll("#ord-chain option")].some((o) => o.value === "doge")')
    browser.execute_script('const s = document.getElementById("ord-chain"); s.value = "doge"; s.dispatchEvent(new Event("change"))')
    rules = browser.find_element(By.ID, "ord-rules").text
    assert "Doginal" in rules and "wonky-ord" in rules and "soft-dust" in rules, rules
    data = bytes(range(256)) * 20                     # 5,120 bytes: four reveals
    f = tmp_path / "pic.png"
    f.write_bytes(data)
    browser.find_element(By.ID, "ord-file").send_keys(str(f))
    _wait(browser, 'return !document.getElementById("ord-go").disabled')
    browser.find_element(By.ID, "ord-go").click()
    review = _wait(browser, 'const c = document.querySelector(".mainnet-review"); return c && c.textContent')
    assert "Real DOGE" in review and "pic.png" in review and "5 transactions" in review, review
    browser.find_element(By.CSS_SELECTOR, ".mainnet-review button.danger").click()
    news = _wait(browser, 'const n = document.getElementById("news"); return !n.hidden && n.textContent')
    assert "Inscribed: pic.png" in news, news + " / " + browser.find_element(By.ID, "trouble").text
    txs = [rawchain.read_tx(bytes.fromhex(raw)) for raw in fake.sent.values()]
    assert len(txs) == 5
    # The chain, in the order it went: each reveal spends the one before, and every signature is good.
    by_prev = {t.vin[0]["txid"]: t for t in txs}
    commit = next(t for t in txs if t.vin[0]["txid"] == FUNDS)
    chain, at = [commit], commit
    while at.txid in by_prev:
        at = by_prev[at.txid]
        chain.append(at)
    assert len(chain) == 5 and chain[-1].vout == [(oi.POSTAGE, p2pkh_script(signed_in))], "the ord on its own address"
    for n, tx in enumerate(chain[1:], 1):
        items = decode_push_datas(tx.vin[0]["script"], "pepecoin")
        sig, lock = items[-2], items[-1]
        prev = chain[n - 1]
        vout = next(k for k, (_v, spk) in enumerate(prev.vout) if spk == oi.p2sh(lock))
        given = [{"txid": prev.txid, "vout": vout}]
        digest = funding.sighash(given, tx.vout, 0, lock)
        assert verify_signature(digest, sig[:-1], lock[1:34]), f"reveal {n}'s signature"
    id_ = chain[1].txid + "i0"
    assert id_ in news
    runs = browser.execute_async_script(
        'fetch("/account/ord/runs").then((r) => r.json()).then(arguments[0])')["runs"]
    assert runs and runs[0]["inscription"] == id_ and runs[0]["done"]
    # The made ord fills the collection index table.
    _wait(browser, f'return document.querySelector("#ix-rows tr[data-id=\\"{id_}\\"]")')
    if os.environ.get("ARCADE_SHOT"):
        browser.set_window_size(1200, 1700)
        browser.execute_script("window.scrollTo(0, 0)")
        time.sleep(0.5)
        browser.save_screenshot(os.environ["ARCADE_SHOT"])



def test_a_hashlips_build_goes_on_one_ord_at_a_time_then_its_index(browser, served, signed_in):
    """2026-10-23: "inscribe a hashlip collection just like the current NFTs one at a time ... pep test for
    indexing". Three pictures and their _metadata.json: three ords, each its own commit and reveal, then the index (its
    part and its root) naming each one's number and traits."""
    import json
    base, state, fake = served
    browser.get(f"{base}/create/ord")
    _wait(browser, 'return [...document.querySelectorAll("#set-chain option")].some((o) => o.value === "doge")')
    before = set(fake.sent)
    browser.execute_script('window.__index = []; const f = window.fetch; window.fetch = (u, o) => {'
                           ' if (String(u) === "/account/inscribe") window.__index.push(JSON.parse(o.body)); return f(u, o); }')
    meta = [{"name": f"Tiny #{n}", "edition": n, "image": f"ipfs://x/{n}.png",
             "attributes": [{"trait_type": "Eyes", "value": ["Red", "Blue", "Red"][n - 1]}]} for n in (1, 2, 3)]
    browser.execute_script("""
        const dt = new DataTransfer();
        for (const n of [3, 1, 2]) dt.items.add(new File([new Uint8Array(300 + n).fill(n)], n + ".png", {type: "image/png"}));
        dt.items.add(new File([arguments[0]], "_metadata.json", {type: "application/json"}));
        const s = document.getElementById("set-chain"); s.value = "doge";
        const f = document.getElementById("set-folder"); f.files = dt.files; f.dispatchEvent(new Event("change"));
        """, json.dumps(meta))
    picked = _wait(browser, 'const p = document.getElementById("set-picked"); return !p.hidden && p.textContent.includes("transactions") && p.textContent')
    assert "3 pictures" in picked and "JSON for 3" in picked and "6 transactions" in picked, picked
    assert browser.find_element(By.ID, "set-name").get_attribute("value") == "Tiny"
    browser.find_element(By.ID, "set-go").click()
    review = _wait(browser, 'const c = document.querySelector(".mainnet-review"); return c && c.textContent')
    assert "Inscribe Tiny: 3 pictures" in review, review
    browser.find_element(By.CSS_SELECTOR, ".mainnet-review button.danger").click()
    news = _wait(browser, 'const n = document.getElementById("news"); return !n.hidden && n.textContent', 90)
    assert "Tiny is inscribed: 3 Doginals" in news, news + " / " + browser.find_element(By.ID, "trouble").text
    new = [rawchain.read_tx(bytes.fromhex(fake.sent[t])) for t in fake.sent if t not in before]
    ords = [t for t in new if t.vout and t.vout[0] == (oi.POSTAGE, p2pkh_script(signed_in))]
    assert len(ords) == 3
    # The index: its part and its root, inscribed after the ords as this account, naming each ord's number and trait.
    import base64
    sent = browser.execute_script("return window.__index")
    assert [x["content_type"] for x in sent] == ["application/vnd.arcade.collection+json"] * 2
    part, root = [json.loads(base64.b64decode(x["content"])) for x in sent]
    runs = browser.execute_async_script('fetch("/account/ord/runs").then((r) => r.json()).then(arguments[0])')["runs"]
    ids = {r["name"]: r["inscription"] for r in runs if r["done"]}
    assert part["p"] == "arcade-collection-items" and root["name"] == "Tiny" and root["chain"] == "doge"
    assert root["supply"] == 3 and root["traits"] == ["Eyes"] and len(root["parts"]) == 1
    assert root["cover"] == ids["1.png"]
    if os.environ.get("ARCADE_SHOT2"):
        browser.set_window_size(1200, 1300)
        browser.execute_script('document.getElementById("ord-set").scrollIntoView()')
        time.sleep(0.5)
        browser.save_screenshot(os.environ["ARCADE_SHOT2"])
    got = {row[0]: (row[1], part["values"][0][row[2][0]]) for row in part["items"]}
    assert got == {ids["1.png"]: (1, "Red"), ids["2.png"]: (2, "Blue"), ids["3.png"]: (3, "Red")}, got
    # Choosing the folder again inscribes nothing twice.
    count = len(fake.sent)
    browser.execute_script("""
        const dt = new DataTransfer();
        for (const n of [3, 1, 2]) dt.items.add(new File([new Uint8Array(300 + n).fill(n)], n + ".png", {type: "image/png"}));
        dt.items.add(new File([arguments[0]], "_metadata.json", {type: "application/json"}));
        const f = document.getElementById("set-folder"); f.files = dt.files; f.dispatchEvent(new Event("change"));
        """, json.dumps(meta))
    _wait(browser, 'return !document.getElementById("set-go").disabled')
    browser.find_element(By.ID, "set-go").click()
    browser.find_element(By.CSS_SELECTOR, ".mainnet-review button.danger").click()
    _wait(browser, 'const n = document.getElementById("set-says"); return n.textContent.includes("index")', 60)
    _wait(browser, 'return !document.body.dataset.inscribing', 60)
    again = [rawchain.read_tx(bytes.fromhex(fake.sent[t])) for t in list(fake.sent)[count:]]
    assert not [t for t in again if t.vout and t.vout[0] == (oi.POSTAGE, p2pkh_script(signed_in))], \
        "no picture inscribed twice: the pieces already in were skipped"
    assert browser.execute_script("return window.__index.length") == 2, "and no second index of the same ords"
