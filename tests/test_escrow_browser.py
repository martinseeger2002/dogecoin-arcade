"""The escrow as a player meets it: a game page, one card, a wallet button.

In a real browser on regtest. The page asks `arcade.escrow` to open an escrow
and put an NFT and some tokens in; the player confirms ONE card in their own
viewer and their browser signs each deposit; the page asks the game's judge to
release the NFT to somebody else; and once the escrow's time is up, the
player's wallet page offers "Take back" and the tokens come home.
"""

import pathlib
import sys
import time

import pytest
import requests

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")
from selenium.webdriver.common.by import By                       # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                     # noqa: E402
from test_account_offer import _settled                             # noqa: E402
from test_account_tokens import _balance, _token                    # noqa: E402
from test_claim_browser import _catch_up, _free_port               # noqa: E402,F401
from test_trade_browser import _inscribe, _press                    # noqa: E402

COIN = 100_000_000
GAME, JUDGE, SWORD = "d1" * 32, "d2" * 32, "d3" * 32
JUDGE_JS = (b"function judge(seed, inputs, params) {"
            b" return inputs && inputs.ok === true ? {release: true}"
            b" : {release: false, why: 'not earned'}; }")
PAGE = b"""<!doctype html><meta charset="utf-8"><body>
<script src="/r/escrow.js"></script><script>window.ready = true;</script>"""


@pytest.fixture(scope="module")
def world(tmp_path_factory, regtest):
    import threading
    import uvicorn
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    class Pointed(ChainContext):
        def credentials(self):
            return regtest.rpc._creds

        @property
        def params(self):
            return regtest.params

    home = tmp_path_factory.mktemp("escrowb")
    chain = Pointed(network="regtest", role="messaging", label="Testnet", datadir=regtest.datadir)
    state = AppState(home=home, messaging=chain,
                     ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                                         datadir=pathlib.Path("/nonexistent")))
    (home / "tokens-chain").write_text("regtest\n")
    regtest.rpc.call("generate", 120)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1", port=port,
                                           log_level="error", timeout_graceful_shutdown=1.0))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    driver = browsers.launch()
    yield f"http://127.0.0.1:{port}", state, regtest, driver
    driver.quit()
    server.should_exit = True
    thread.join(timeout=5)


def _run(driver, script, *args):
    driver.set_script_timeout(300)
    got = driver.execute_async_script("""
        const done = arguments[arguments.length - 1];
        Promise.resolve((async () => { %s })())
          .then((r) => done({ok: r === undefined ? true : r}),
                (e) => done({error: String(e.message || e)}));""" % script, *args)
    assert "error" not in got, got
    return got["ok"]


def test_a_player_escrows_things_the_judge_releases_and_the_wallet_takes_back(world, monkeypatch):
    base, state, node, driver = world
    driver.get(f"{base}/join")
    who = _run(driver, """
        const w = await import("/wallet.js");
        const r = await w.signUp(arguments[0], "a long enough password",
                                 {network: "regtest", version: arguments[1]});
        w.remember(r.wallet.phrase);
        return r.address;""", "escrower", state.messaging.params.pubkeyhash_version)
    node.rpc.call("generate", 1)
    _catch_up(state)
    _run(driver, 'const w = await import("/wallet.js"); await w.waitForCoins(30);')

    referee = requests.get(f"{base}/r/referee").json()["pubkey"]
    _inscribe(state, JUDGE, 950, who, "application/javascript", JUDGE_JS)
    _inscribe(state, GAME, 951, who, "text/html; charset=utf-8", PAGE,
              {"game": {"name": "Escrow Game"},
               "escrow": {"referee": {"pubkey": referee}, "judge": JUDGE, "unlock_hours": 24}})
    _inscribe(state, SWORD, 952, who, "text/plain", b"a sword")
    pid = _token(state, property_id=170, name="Gold")
    _balance(state, who, pid, 50 * COIN)

    # The escrow opens "three hours ago" (its hour is already past), so the end
    # of the test can take it back; the game cannot tell the difference.
    from arcade.web import app as appmod
    real = time.time
    monkeypatch.setattr(appmod.time, "time", lambda: real() - 3 * 3600)
    driver.get(f"{base}/inscriptions/{GAME}/full")
    driver.switch_to.frame(driver.find_element(By.CSS_SELECTOR, ".inscription-frame"))
    opened = _run(driver, "return await arcade.escrow.open({hours: 1});")
    assert opened["escrow"] and opened["owner"] == who and opened["owner_pubkey"], opened
    driver.execute_script(
        "window.depositing = arcade.escrow.deposit(arguments[0], [{inscription: arguments[1]},"
        " {token: arguments[2], amount: '20'}]).then(r => window.deposited = r,"
        " e => window.depositError = String(e.message || e));", opened["escrow"], SWORD, pid)
    _press(driver, "Put them in")
    end = time.time() + 120
    while time.time() < end and not driver.execute_script(
            "return window.deposited || window.depositError"):
        time.sleep(0.5)
    assert not driver.execute_script("return window.depositError"), \
        driver.execute_script("return window.depositError")
    assert len(driver.execute_script("return window.deposited.txids")) == 2
    monkeypatch.setattr(appmod.time, "time", real)
    _settled(state, node.rpc)
    held = _run(driver, "return await arcade.escrow.status(arguments[0]);", opened["escrow"])
    assert held["inscriptions"] == [SWORD] and held["tokens"] == {str(pid): 20 * COIN}

    # The page asks; the game's judge decides.
    winner = node.rpc.call("getnewaddress")
    facts = {k: opened[k] for k in ("escrow", "owner", "owner_pubkey", "unlock")}
    refused = driver.execute_async_script("""
        const done = arguments[arguments.length - 1];
        arcade.escrow.release(Object.assign({}, arguments[0], {to: arguments[1],
          items: [{inscription: arguments[2]}], replay: {ok: false}}))
          .then(() => done("released"), (e) => done(String(e.message || e)));""",
        facts, winner, SWORD)
    assert "not earned" in refused
    released = _run(driver, """
        return await arcade.escrow.release(Object.assign({}, arguments[0], {to: arguments[1],
          items: [{inscription: arguments[2]}], replay: {ok: true}}));""", facts, winner, SWORD)
    assert len(released["txids"]) == 1
    _settled(state, node.rpc)
    index = state.token_index(state.messaging)
    assert index.inscription(SWORD)["owner"] == winner

    # Its time is up: the wallet offers to take the rest back, and does.
    driver.switch_to.default_content()
    driver.get(f"{base}/me/wallet")
    end = time.time() + 60
    button = None
    while time.time() < end and button is None:
        found = [b for b in driver.find_elements(By.CSS_SELECTOR, "#escrow-panel button")
                 if b.text.strip() == "Take back"]
        button = found[0] if found else None
        time.sleep(0.5)
    assert button is not None, driver.find_element(By.ID, "escrow-panel").text
    button.click()
    end = time.time() + 120
    while time.time() < end and "On its way home" not in driver.find_element(By.ID, "escrow-panel").text:
        time.sleep(0.5)
    assert "On its way home" in driver.find_element(By.ID, "escrow-panel").text, \
        driver.find_element(By.ID, "escrow-panel").text
    _settled(state, node.rpc)
    assert int(index.balance(who, pid)) == 50 * COIN, "the gold came home"
