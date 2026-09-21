"""An account sending its own coins, signed in the browser.

The same handshake as claiming a name, carrying money -- which is the
point of there being one handshake. What is tested here is the part that
is different: that nothing is signed until somebody has been shown who is
being paid and what it costs.
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
    import uvicorn
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    class Pointed(ChainContext):
        def credentials(self):
            return regtest.rpc._creds

        @property
        def params(self):
            return regtest.params

    home = tmp_path_factory.mktemp("send")
    chain = Pointed(network="regtest", role="messaging", label="Testnet",
                    datadir=regtest.datadir)
    state = AppState(home=home, messaging=chain,
                     ledger=ChainContext(network="main", role="ledger",
                                         label="Mainnet",
                                         datadir=pathlib.Path("/nonexistent")))
    (home / "tokens-chain").write_text("regtest\n")
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
def funded(browser, served):
    """A signed-up account with coins in it, on a fresh page."""
    base, state, node = served
    browser.get(f"{base}/join")
    browser.set_script_timeout(120)
    assert browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        import("/wallet.js").then((m) => { window.w = m; done(true); },
                                  (e) => done(String(e)));""") is True
    name = f"spender{int(time.time() * 1000) % 100000}"
    made = browser.execute_async_script("""
        const done = arguments[3];
        window.w.signUp(arguments[0], arguments[1],
                        {network: "regtest", version: arguments[2]})
          .then((r) => { window.wallet = r.wallet;
                         done({tag: r.tag, address: r.address, given: r.given}); },
                (e) => done({error: String(e.message || e)}));""",
        name, "a long enough password",
        state.messaging.params.pubkeyhash_version)
    assert "error" not in made, made
    # Funded directly rather than by the faucet. Every test here signs up
    # from 127.0.0.1, so only the first would be paid -- the faucet's
    # one-a-day-per-connection rule doing exactly its job. What the faucet
    # does is tested where that is the subject (tests/test_faucet.py).
    node.rpc.call("sendtoaddress", made["address"], 50.0)
    node.rpc.call("generate", 1)
    _catch_up(state)
    browser.execute_async_script("""
        const done = arguments[0];
        window.w.waitForCoins(30).then(done);""")
    return browser, base, state, node, made


def test_an_offer_says_who_is_being_paid_and_what_it_costs(funded):
    """Nothing is signed until somebody has been shown that."""
    browser, base, state, node, made = funded
    theirs = node.rpc.call("getnewaddress")
    offer = browser.execute_async_script("""
        const done = arguments[2];
        window.w.offerSend(arguments[0], arguments[1]).then(done,
          (e) => done({error: String(e.message || e)}));""", theirs, "5")
    assert "error" not in offer, offer
    assert offer["to"] == theirs
    assert offer["amount"] == 5_00000000
    assert offer["fee"] > 0
    assert offer["what"].startswith("send ")
    assert node.rpc.call("getrawmempool") == [], "and nothing has gone out"


def test_confirming_it_sends_the_coins(funded):
    browser, base, state, node, made = funded
    theirs = node.rpc.call("getnewaddress")
    sent = browser.execute_async_script("""
        const done = arguments[2];
        (async () => {
          try {
            const offer = await window.w.offerSend(arguments[0], arguments[1]);
            const done2 = await window.w.confirm(window.wallet, offer);
            done({txid: done2.txid, fee: offer.fee});
          } catch (e) { done({error: String(e.message || e)}); }
        })();""", theirs, "5")
    assert "error" not in sent, sent
    assert len(sent["txid"]) == 64
    assert sent["txid"] in node.rpc.call("getrawmempool")

    node.rpc.call("generate", 1)
    got = node.rpc.call("getreceivedbyaddress", theirs, 1)
    assert abs(float(got) - 5.0) < 1e-8, "the coins arrived"


def test_a_tag_is_resolved_and_the_address_is_shown(funded):
    """Somebody paying @robin should see the address their coins are
    going to before they sign."""
    browser, base, state, node, made = funded
    # The account that signed up already claimed nothing; claim now so the
    # chain has a name to resolve.
    claimed = browser.execute_async_script("""
        const done = arguments[0];
        window.w.claim(window.wallet).then(done,
          (e) => done({error: String(e.message || e)}));""")
    assert "error" not in claimed, claimed
    node.rpc.call("generate", 1)
    _catch_up(state)

    offer = browser.execute_async_script("""
        const done = arguments[2];
        window.w.offerSend(arguments[0], arguments[1]).then(done,
          (e) => done({error: String(e.message || e)}));""",
        "@" + made["tag"], "1")
    # Paying yourself is refused, which is how we know the tag resolved to
    # this account's own address rather than being taken literally.
    assert "own address" in offer.get("error", ""), offer


def test_a_name_nobody_holds_is_refused(funded):
    browser, base, state, node, made = funded
    offer = browser.execute_async_script("""
        const done = arguments[2];
        window.w.offerSend(arguments[0], arguments[1]).then(done,
          (e) => done({error: String(e.message || e)}));""", "@nobodyatall", "1")
    assert "nobody holds" in offer.get("error", ""), offer


def test_more_than_there_is_cannot_be_offered(funded):
    browser, base, state, node, made = funded
    offer = browser.execute_async_script("""
        const done = arguments[2];
        window.w.offerSend(arguments[0], arguments[1]).then(done,
          (e) => done({error: String(e.message || e)}));""",
        node.rpc.call("getnewaddress"), "1000000")
    assert "not enough" in offer.get("error", ""), offer
