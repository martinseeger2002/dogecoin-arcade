"""Signing up and claiming the name on the chain, in a browser.

The second half of signing up, and the first thing a wallet does with its
own coins: a transaction built by the node, signed by a key the node has
never seen, and read back off the chain as a name.
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
def served(tmp_path_factory, regtest):
    """The application against a real regtest node, with coins to give."""
    import uvicorn
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    class Pointed(ChainContext):
        def credentials(self):
            return regtest.rpc._creds

        @property
        def params(self):
            return regtest.params

    home = tmp_path_factory.mktemp("claim")
    chain = Pointed(network="regtest", role="messaging", label="Testnet",
                    datadir=regtest.datadir)
    state = AppState(home=home, messaging=chain,
                     ledger=ChainContext(network="main", role="ledger",
                                         label="Mainnet",
                                         datadir=pathlib.Path("/nonexistent")))
    (home / "tokens-chain").write_text("regtest\n")
    # Coins to give away, and a chain long enough to spend them.
    regtest.rpc.call("generate", 120)

    port = _free_port()
    config = uvicorn.Config(create_app(state), host="127.0.0.1", port=port,
                            log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("test server did not start")
    yield f"http://127.0.0.1:{port}", state, regtest
    server.should_exit = True
    thread.join(timeout=5)


def _catch_up(state):
    index = state.token_index(state.messaging)
    for _ in range(50):
        result = index.sync(max_blocks=500)
        if result is None or not result.connected:
            break
    return index


@pytest.fixture
def loaded(browser, served):
    """A fresh page and a fresh import, per test.

    Not module-scoped: the page polls `/events` and reloads when something
    new appears, so a page left open by one test has had the document --
    and `window.w` with it -- replaced by the time the next one runs. That
    is the page doing its job; the test has to stop assuming otherwise.
    """
    base, state, node = served
    browser.get(f"{base}/join")
    browser.set_script_timeout(120)
    ready = browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        import("/wallet.js").then((m) => { window.w = m; done(true); },
                                  (e) => done(String(e)));""")
    assert ready is True, ready
    return browser, base, state, node


def test_a_new_account_claims_its_own_name_on_the_chain(loaded):
    """End to end, as a person does it: a name, a password, coins from the
    faucet, and a claim signed by a key this node has never seen."""
    browser, base, state, node = loaded
    version = state.messaging.params.pubkeyhash_version

    made = browser.execute_async_script("""
        const done = arguments[3];
        window.w.signUp(arguments[0], arguments[1],
                        {network: "regtest", version: arguments[2]})
          .then((r) => { window.wallet = r.wallet;
                         done({tag: r.tag, address: r.address,
                               given: r.given, why: r.no_coins}); },
                (e) => done({error: String(e.message || e)}));""",
        "claimant", "a long enough password", version)
    assert "error" not in made, made
    assert made["given"] > 0, f"the faucet paid: {made.get('why')}"

    # Let the coins land and the index see them.
    node.rpc.call("generate", 1)
    _catch_up(state)
    waited = browser.execute_async_script("""
        const done = arguments[0];
        window.w.waitForCoins(30).then(done);""")
    assert waited["balance"] > 0, waited
    assert waited["name"] == "claimant", "the node knows what to call them"
    assert waited["tag"] == "", "and the chain does not, yet"

    # The claim: built by the node, signed in the browser, broadcast.
    claimed = browser.execute_async_script("""
        const done = arguments[0];
        window.w.claim(window.wallet).then(done,
          (e) => done({error: String(e.message || e)}));""")
    assert "error" not in claimed, claimed
    assert claimed["what"] == "claim @claimant"
    assert len(claimed["txid"]) == 64

    node.rpc.call("generate", 1)
    index = _catch_up(state)
    assert index.address_of("claimant") == made["address"], \
        "the chain says the name is theirs"

    after = browser.execute_async_script("""
        const done = arguments[0];
        window.w.state().then(done);""")
    assert after["tag"] == "claimant", "and the page says so"


def test_a_name_taken_on_the_chain_cannot_be_claimed_twice(loaded):
    browser, base, state, node = loaded
    version = state.messaging.params.pubkeyhash_version
    made = browser.execute_async_script("""
        const done = arguments[3];
        window.w.signUp(arguments[0], arguments[1],
                        {network: "regtest", version: arguments[2]})
          .then((r) => { window.wallet = r.wallet; done({tag: r.tag}); },
                (e) => done({error: String(e.message || e)}));""",
        "latecomer", "a long enough password", version)
    assert "error" not in made, made
    node.rpc.call("generate", 1)
    _catch_up(state)
    browser.execute_async_script("""
        const done = arguments[0];
        window.w.waitForCoins(30).then(done);""")

    taken = browser.execute_async_script("""
        const done = arguments[1];
        window.w.claim(window.wallet, arguments[0]).then(
          (r) => done({ok: r.txid}),
          (e) => done({error: String(e.message || e)}));""", "claimant")
    assert "is taken" in taken.get("error", ""), taken
