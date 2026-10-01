"""Two players trade inside a game page, in two real browsers, on regtest.

Everything a trade touches, end to end and nothing stubbed: two accounts that
signed up in their own browsers, one game page both opened full screen, the
realtime room they meet in, `arcade.swap.trade` from one page and
`arcade.swap.answer` from the other, the consent card each player presses in
their OWN viewer, the leg signed in one browser and sealed to the other, the
other browser finishing it, the block, and the ledger afterwards. The pages
only ever see statuses.
"""

import json
import pathlib
import sys
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")
from selenium.webdriver.common.by import By                       # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                     # noqa: E402
from test_claim_browser import _catch_up, _free_port               # noqa: E402,F401
from test_account_tokens import _balance, _token                    # noqa: E402
from test_account_offer import _settled                             # noqa: E402

COIN = 100_000_000
GAME = "ee" * 32
PIECE = "ef" * 32
PAGE = b"""<!doctype html><meta charset="utf-8"><body>
<script src="/r/realtime.js"></script><script src="/r/swap.js"></script>
<script>
window.log = []; window.room = null;
arcade.swap.onTrade(function (e) { window.log.push(e); });
arcade.realtime.join('plaza', {game: 'tradetest'}).then(function (r) { window.room = r; });
</script>"""


@pytest.fixture(scope="module")
def world(tmp_path_factory, regtest):
    """The app on regtest, with the realtime mesh running and two browsers."""
    import threading
    import uvicorn
    from arcade.mesh import node as meshlib
    from arcade.mesh.service import for_state
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    meshlib.SUB_EVERY = 0.5

    class Pointed(ChainContext):
        def credentials(self):
            return regtest.rpc._creds

        @property
        def params(self):
            return regtest.params

    home = tmp_path_factory.mktemp("trade")
    chain = Pointed(network="regtest", role="messaging", label="Testnet",
                    datadir=regtest.datadir)
    state = AppState(home=home, messaging=chain,
                     ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                                         datadir=pathlib.Path("/nonexistent")))
    (home / "tokens-chain").write_text("regtest\n")
    regtest.rpc.call("generate", 120)
    state.mesh = for_state(state, listen_port=None).start()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1", port=port,
                                           log_level="error", timeout_graceful_shutdown=1.0))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    a, b = browsers.launch(), browsers.launch()
    yield f"http://127.0.0.1:{port}", state, regtest, a, b
    a.quit()
    b.quit()
    server.should_exit = True
    thread.join(timeout=5)
    state.mesh.stop()


def _signed_up(driver, base, state, node, name):
    driver.get(f"{base}/join")
    driver.set_script_timeout(180)
    made = driver.execute_async_script("""
        const done = arguments[3];
        import("/wallet.js").then((w) => w.signUp(arguments[0], arguments[1],
                                   {network: "regtest", version: arguments[2]})
          .then((r) => { w.remember(r.wallet.phrase); window.wallet = r.wallet;
                         done({address: r.address}); }))
          .catch((e) => done({error: String(e.message || e)}));""",
        name, "a long enough password", state.messaging.params.pubkeyhash_version)
    assert "error" not in made, made
    node.rpc.call("generate", 1)
    _catch_up(state)
    # What a real sign-up does next (wallet.setUp): the name, then -- once it is
    # in a block -- the messaging key that lets a sealed leg reach this account.
    claimed = driver.execute_async_script("""
        const done = arguments[0];
        import("/wallet.js").then((w) => w.waitForCoins(30).then(() => w.claim(window.wallet)))
          .then(done, (e) => done({error: String(e.message || e)}));""")
    assert "error" not in claimed, claimed
    node.rpc.call("generate", 1)
    _catch_up(state)
    told = driver.execute_async_script("""
        const done = arguments[1];
        import("/messaging.js").then((mail) => mail.identity(window.wallet.phrase)
            .then((me) => mail.announce(window.wallet, me, arguments[0])))
          .then((r) => done({txid: r.txid}), (e) => done({error: String(e.message || e)}));""", name)
    assert "error" not in told, told
    return made["address"]


def _inscribe(state, txid, number, owner, content_type, content, said=None):
    index = state.token_index(state.messaging)
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,block_height,"
            "position,content_type,content_len,sha256,json,chunks,content) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (txid, number, owner, owner, 1, 0, content_type, len(content), "ab" * 32,
             json.dumps(said or {}), 1, content))
        db.conn.commit()


def _in_game(driver, base):
    driver.get(f"{base}/inscriptions/{GAME}/full")
    frame = driver.find_element(By.CSS_SELECTOR, ".inscription-frame")
    driver.switch_to.frame(frame)


def _wait(driver, script, what, timeout=60, node=None, state=None):
    end = time.time() + timeout
    while time.time() < end:
        got = driver.execute_script(script)
        if got:
            return got
        if node is not None:
            node.rpc.call("generate", 1)
            _catch_up(state)
        time.sleep(1)
    raise AssertionError(f"timed out waiting for {what}: {driver.execute_script('return window.log')}")


def _press(driver, label, timeout=60):
    """Press the consent card's button in this player's own viewer."""
    driver.switch_to.default_content()
    end = time.time() + timeout
    while time.time() < end:
        for button in driver.find_elements(By.CSS_SELECTOR, ".askcard button"):
            if button.text.strip() == label:
                button.click()
                driver.switch_to.frame(driver.find_element(By.CSS_SELECTOR, ".inscription-frame"))
                return
        time.sleep(0.3)
    raise AssertionError(f"no {label!r} card appeared")


def test_two_players_trade_an_nft_for_a_token_inside_a_game(world):
    base, state, node, a, b = world
    alice = _signed_up(a, base, state, node, "alicetrades")
    bob = _signed_up(b, base, state, node, "bobtrades")
    _settled(state, node.rpc)          # the claims mined, and their keys read
    _inscribe(state, GAME, 900, alice, "text/html; charset=utf-8", PAGE,
              {"game": {"name": "Trade Test"}})
    _inscribe(state, PIECE, 901, alice, "text/plain", b"a sword")
    pid = _token(state, property_id=140, name="Gold")
    _balance(state, bob, pid, 100 * COIN)

    _in_game(a, base)
    _in_game(b, base)
    for driver, other in ((a, bob), (b, alice)):
        _wait(driver, f"return window.room && window.room.online && "
                      f"window.room.members().some(p => p.id === '{other}')",
              "the other player in the room")

    a.set_script_timeout(30)
    a.execute_script(
        "arcade.swap.trade({with: arguments[0], give: {inscription: arguments[1]},"
        " get: {token: arguments[2], amount: '25'}}).then(r => window.tradeId = r.id,"
        " e => window.tradeError = String(e.message || e));", bob, PIECE, pid)
    _press(a, "Offer")
    trade = _wait(b, "return window.log.find(e => e.status === 'incoming')", "the offer")
    assert trade["give"] == {"token": pid, "amount": "25"}, "Bob's own side: what he gives"
    assert trade["get"] == {"inscription": PIECE}
    assert trade["with"]["tag"] in ("alicetrades", ""), trade["with"]

    b.execute_script("arcade.swap.answer(arguments[0], true);", trade["id"])
    _press(b, "Accept")
    _wait(a, "return window.log.find(e => e.status === 'signed' || e.status === 'failed')",
          "Alice's signed leg", timeout=120, node=node, state=state)
    failed = a.execute_script("return window.log.find(e => e.status === 'failed')")
    assert not failed, failed and failed.get("why")
    _press(b, "Finish", timeout=120)
    _wait(b, "return window.log.find(e => e.status === 'settled' || e.status === 'failed')",
          "the trade in a block", timeout=180, node=node, state=state)
    assert b.execute_script("return window.log.find(e => e.status === 'settled')"), \
        b.execute_script("return window.log")
    _wait(a, "return window.log.find(e => e.status === 'settled')", "Alice hearing it settled",
          timeout=120, node=node, state=state)

    index = state.token_index(state.messaging)
    assert index.inscription(PIECE)["owner"] == bob, "the sword crossed"
    assert int(index.balance(alice, pid)) == 25 * COIN, "the gold crossed back"
    assert int(index.balance(bob, pid)) == 75 * COIN


def test_an_nft_for_an_nft_and_a_trade_said_no_to(world):
    """Both sides an NFT (the proposer signs), then an offer the other declines."""
    base, state, node, a, b = world
    alice, bob = a.execute_script("return window.room.me.id"), b.execute_script("return window.room.me.id")
    mine, theirs = "f0" * 32, "f1" * 32
    _inscribe(state, mine, 910, alice, "text/plain", b"a shield")
    _inscribe(state, theirs, 911, bob, "text/plain", b"a helmet")
    a.execute_script("window.log = []"); b.execute_script("window.log = []")

    a.execute_script("arcade.swap.trade({with: arguments[0], give: {inscription: arguments[1]},"
                     " get: {inscription: arguments[2]}});", bob, mine, theirs)
    _press(a, "Offer")
    trade = _wait(b, "return window.log.find(e => e.status === 'incoming')", "the offer")
    b.execute_script("arcade.swap.answer(arguments[0], true);", trade["id"])
    _press(b, "Accept")
    _wait(a, "return window.log.find(e => e.status === 'signed' || e.status === 'failed')",
          "Alice's signed half", timeout=120, node=node, state=state)
    failed = a.execute_script("return window.log.find(e => e.status === 'failed')")
    assert not failed, failed and failed.get("why")
    _press(b, "Finish", timeout=120)
    _wait(b, "return window.log.find(e => e.status === 'settled' || e.status === 'failed')",
          "the swap in a block", timeout=180, node=node, state=state)
    assert b.execute_script("return window.log.find(e => e.status === 'settled')"), \
        b.execute_script("return window.log")
    index = state.token_index(state.messaging)
    assert index.inscription(mine)["owner"] == bob and index.inscription(theirs)["owner"] == alice

    # And no: Bob declines Alice's next offer; nothing is signed or sent.
    a.execute_script("window.log = []"); b.execute_script("window.log = []")
    a.execute_script("arcade.swap.trade({with: arguments[0], give: {inscription: arguments[1]},"
                     " get: {inscription: arguments[2]}});", bob, theirs, mine)
    _press(a, "Offer")
    trade = _wait(b, "return window.log.find(e => e.status === 'incoming')", "the second offer")
    b.execute_script("arcade.swap.answer(arguments[0], false, 'not today');", trade["id"])
    said = _wait(a, "return window.log.find(e => e.status === 'declined')", "the no")
    assert said["id"] == trade["id"]
    assert node.rpc.call("getrawmempool") == [], "a declined trade sends nothing"
