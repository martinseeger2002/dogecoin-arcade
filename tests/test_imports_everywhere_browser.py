"""An imported wallet follows its account to another arcade, in a real browser (2026-10-21: "an imported
wallet needs to be automatically broadcast to other arcades that you are signed in with. So all of your wallet
balances persist over all arcades that you sign into").

Two arcades in this process, "a.example.com" and "b.example.com" (their calls to each other routed to their local
ports). A key is imported on A; the account signs in on B, which brings the account and its signed record from A;
B's Wallet page registers the wallet with its key's proof and gets its coins from A on a second proof by the same
key, each coin checked against B's own chain; the wallet shows on B with its balance.
"""

import hashlib
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
from test_wallet_imports_browser import _js, _until, _wif        # noqa: E402

from selenium.webdriver.common.by import By                      # noqa: E402

from arcade import script, wallet_imports as W                   # noqa: E402

TAG = "roamer"
PASSWORD = "a long password nobody needs to guess for this test"
SECRET = hashlib.sha256(b"imported on a").digest()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(state):
    import uvicorn
    from arcade.web.app import create_app
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
    return port, server, thread


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    yield driver
    driver.quit()


@pytest.fixture(scope="module")
def two(tmp_path_factory):
    from pathlib import Path
    from arcade.web import app as appmod
    from arcade.web.state import AppState, ChainContext
    import requests as real_requests

    def state(name):
        return AppState(home=tmp_path_factory.mktemp(name),
                        messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                                               datadir=Path("/nonexistent")),
                        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                                            datadir=Path("/nonexistent")))
    a, b = state("a"), state("b")
    for st, other in ((a, "b.example.com"), (b, "a.example.com")):
        with st.store() as store:                       # each knows the other by its mesh announcement
            store.add_mesh_announcement(f"t-{other}", st.messaging.network, "nX", f"wss://{other}/mesh", 443,
                                        "cd" * 32, 0, 0)
    b.set_setting("public_hosts", ["b.example.com"])
    (pa, sa, ta), (pb, sb, tb) = _serve(a), _serve(b)
    ports = {"a.example.com": pa, "b.example.com": pb}

    def route(url):
        host = url.split("/")[2]
        if host not in ports:
            raise real_requests.ConnectionError(f"{host}: not in this test")
        return url.replace(f"https://{host}", f"http://127.0.0.1:{ports[host]}", 1)
    saved = (appmod.requests.get, appmod.requests.post, appmod._host_is_public)
    appmod.requests.get = lambda url, **kw: real_requests.request("GET", route(url), **kw)
    appmod.requests.post = lambda url, **kw: real_requests.request("POST", route(url), **kw)
    appmod._host_is_public = lambda host: True
    yield {"a": (f"http://127.0.0.1:{pa}", a), "b": (f"http://127.0.0.1:{pb}", b)}
    appmod.requests.get, appmod.requests.post, appmod._host_is_public = saved
    for server, thread in ((sa, ta), (sb, tb)):
        server.should_exit = True
        thread.join(timeout=5)


def test_a_wallet_imported_on_one_arcade_shows_on_another_with_its_balance(browser, two, monkeypatch):
    base_a, a = two["a"]
    base_b, b = two["b"]
    browser.set_script_timeout(180)
    # --- on A: an account, and a key imported into it.
    browser.get(f"{base_a}/join")
    browser.delete_all_cookies()
    made = _js(browser, "return (await w.signUp(args[0], args[1], {network: 'regtest', version: 111})).tag;",
               TAG, PASSWORD)
    assert made["ok"], made
    imported = _js(browser, """
        return await w.importWallet({network: "regtest", version: 111},
                                    {kind: "wif", secret: args[0], password: args[1]});""",
                   _wif(SECRET, 0xf1), PASSWORD)
    assert imported["ok"], imported
    address = imported["value"]["address"]
    pubkey = a.vault().get(TAG)["pubkey"]
    record = a.imported_wallets().get(pubkey)
    assert record["signed"] and record["version"] == 1, "saved signed, as version 1, by the browser"
    W.check_record(pubkey, record["version"], record["blob"], record["sig"])
    # A has this wallet's whole past (watched from the start); a coin arrives.
    with a.token_index(a.messaging).open() as db:
        db.conn.execute("INSERT INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                        ("ab" * 32, 0, address, 5 * 100_000_000, 1))
    # B's own chain: that coin is real and unspent.
    from arcade.web.state import ChainContext

    class Rpc:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close(self):
            pass

        def call(self, method, *args):
            if method == "gettxout" and args[0] == "ab" * 32:
                return {"confirmations": 1, "value": 5, "scriptPubKey": {"addresses": [address]}}
            if method == "gettxout":
                return None
            raise RuntimeError("no node (test)")

        def batch(self, calls):
            return [self.call(method, *params) for method, params in calls]

        def get_block_count(self):
            return 1
    real_rpc = ChainContext.rpc
    monkeypatch.setattr(ChainContext, "rpc", lambda self: Rpc() if self is b.messaging else real_rpc(self))

    # --- on B: sign in by name only. B finds the account on A, brings it and its signed record over.
    browser.get(f"{base_b}/join")
    browser.delete_all_cookies()
    # The sign-in first says what bringing a wallet here means (2026-10-23, wallet_js warnElsewhere). No: nothing is
    # opened or kept here. Yes: it comes over.
    browser.execute_script("window.arcadeAsk = function () { return Promise.resolve(false); };")
    refused = _js(browser, "return (await w.signIn(args[0], args[1], {network: 'regtest', version: 111})).tag;",
                  TAG, PASSWORD)
    assert not refused["ok"] and "cancelled" in refused["error"], refused
    assert b.imported_wallets().get(pubkey) is None, "a no kept nothing on B"
    browser.execute_script("window.__asked = []; window.arcadeAsk = function (t, d) { __asked.push([t, d]); return Promise.resolve(true); };")
    signed = _js(browser, "return (await w.signIn(args[0], args[1], {network: 'regtest', version: 111})).tag;",
                 TAG, PASSWORD)
    assert signed["ok"], signed
    asked = browser.execute_script("return window.__asked")
    assert asked and asked[0][0].startswith("Sign in on ") and "keeps its own copy of your encrypted wallet" in asked[0][1], asked
    on_b = b.imported_wallets().get(pubkey)
    assert on_b and on_b["blob"] == record["blob"] and on_b["version"] == 1, "the record came over, as it was"
    # B's Wallet page registers the wallet by its key and brings its coins over from A.
    # (One load of the page: a second load straight after the first can catch the first mid-way through opening the
    # wallet, and that locks it -- wallet.js `opened` forgets the open wallet on any failure.)
    browser.execute_script("localStorage.setItem(arguments[0], JSON.stringify('regtest'))", f"arcade.wallet.net.{TAG}")
    browser.get(f"{base_b}/me/wallet")
    _until(lambda: any(e["a"] == address for e in W.entries(b.setting(W.setting_key("regtest", pubkey), []))),
           "B watches the imported wallet for the account", seconds=60)
    _until(lambda: b.setting(f"backfilled:regtest:{address}") == 1, "its coin came from A, checked on B", seconds=60)
    _until(lambda: browser.find_element(By.ID, "wpicker").is_displayed(), "the wallet picker shows")

    def shown():
        browser.find_element(By.ID, "wpick").click()
        rows = [o.text for o in browser.find_elements(By.CSS_SELECTOR, "#wmenu .wl-opt")]
        browser.find_element(By.ID, "wpick").click()
        return rows
    _until(lambda: any(r.startswith("Wallet 1") and "5" in r for r in shown()),
           "the imported wallet shows on B by name, with its balance", seconds=60)
    # Nothing about it is in B's public by-tag coins answer.
    import requests
    listed = requests.request("GET", f"{base_b}/signin/{TAG}/coins", timeout=10).json()
    assert address not in json.dumps(listed)
