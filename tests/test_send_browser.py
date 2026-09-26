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
    yield f"http://127.0.0.1:{port}", state, regtest
    server.should_exit = True
    thread.join(timeout=5)
    assert not thread.is_alive(), "the server is still serving a keep-alive connection"


def _catch_up(state):
    """Index every block the node has before the test reads anything.

    `sync()` answers None when another thread is already mid-pass -- the page
    poll in the server this test drives, usually -- and that is not the same
    thing as being caught up. Reading it as caught up is why these files passed
    alone and failed in a full run: alone, one pass covers the session node's
    short chain, and in a full run that node has mined thousands of blocks and
    one lost pass leaves the rest unwalked. `current` is what a page shows, and
    it counts an index as current when the floor is above the tip and there is
    genuinely nothing to read.
    """
    index = state.token_index(state.messaging)
    tip = 0
    for _ in range(200):
        with state.messaging.rpc() as rpc:
            tip = rpc.get_block_count()
        if index.status(tip)["current"]:
            return index
        if index.stopped is not None:
            raise AssertionError(f"the index stopped: {index.stopped}")
        index.sync(max_blocks=500)
        time.sleep(0.05)
    raise AssertionError(f"the index will not catch up: {index.status(tip)}")


def _keys(browser, chain):
    """The open wallet, taken the way a page takes it after a reload.

    The page reloads itself. `base.html` polls `/events` every five seconds and
    reloads when the generation moves; signing up and claiming both bump it, and
    `_catch_up` then takes as long as the poll to finish, which is long enough
    for the refresh to land in the middle of a test and hand back a new
    document. What that costs is the handle, and the handle is not something
    this file can keep for itself: a wallet holds functions as well as bytes, so
    there is no version of it that holds the keys in Python and passes them back
    in. wallet.js answers this already -- `signUp` remembers the words in
    sessionStorage and `opened` rebuilds the keys from them, which is what every
    page that needs a key calls after a navigation. Asking the module the way a
    reloaded page does is the difference between a test that survives the
    refresh and one that reads `window.w is undefined` and calls it a flake.
    """
    got = browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        import("/wallet.js").then((m) => { window.w = m;
                                           return m.opened(arguments[0]); })
          .then((wallet) => { if (wallet) window.wallet = wallet;
                              done(!!wallet); },
                (e) => done(String(e)));""", chain)
    assert got is True, "this tab has no open wallet left to sign with"


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
    _keys(browser, {"network": "regtest",
                    "version": state.messaging.params.pubkeyhash_version})
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
    _keys(browser, {"network": "regtest",
                    "version": state.messaging.params.pubkeyhash_version})

    offer = browser.execute_async_script("""
        const done = arguments[2];
        window.w.offerSend(arguments[0], arguments[1]).then(done,
          (e) => done({error: String(e.message || e)}));""",
        "@" + made["tag"], "1")
    # The tag resolved to this account's own address rather than being taken
    # literally, and the offer says so before anything is signed. (Paying
    # yourself is allowed now: it is how one coin becomes two.)
    assert "error" not in offer, offer
    assert made["address"] in offer.get("what", ""), offer


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


def test_the_browser_works_out_what_it_is_being_asked_to_sign(funded):
    """What a person is shown before they sign is this tab's own reading.

    `offer.fee` and `offer.what` are what the node says it built. `checked`
    reads the transaction the offer carries and derives the outputs, the
    change and the fee from its bytes, which is worth nothing unless the two
    agree for an honest node -- so that is what is asserted here, alongside
    a destination address that came out of an output script rather than out
    of the offer's `to`. The check runs again inside `confirm`; this is the
    same computation one request earlier, which is why the page can show it
    before anything is signed.
    """
    browser, base, state, node, made = funded
    theirs = node.rpc.call("getnewaddress")
    shown = browser.execute_async_script("""
        const done = arguments[2];
        (async () => {
          try {
            const offer = await window.w.offerSend(arguments[0], arguments[1]);
            const seen = await window.w.checked(offer, window.wallet);
            done({says: seen.says, fee: seen.fee, node: offer.fee,
                  change: seen.change, signs: seen.signs,
                  mine: window.wallet.address,
                  pays: seen.pays.map((p) => [p.to, !!p.mine])});
          } catch (e) { done({error: String(e.message || e)}); }
        })();""", theirs, "5")
    assert "error" not in shown, shown
    assert shown["fee"] == shown["node"], "checked against the node's, not echoed"
    assert [theirs, False] in shown["pays"], shown["pays"]
    assert shown["change"] > 0
    assert [shown["mine"], True] in shown["pays"], "it knows its own change"
    assert shown["signs"] == {"from": 0, "of": 1}
    assert theirs in shown["says"] and "in fees" in shown["says"], shown["says"]
    assert node.rpc.call("getrawmempool") == [], "checking signs nothing"


def test_a_transaction_that_is_not_the_one_offered_is_refused(funded):
    """The hole this closes, closed.

    Two offers, one wallet, same coins, different destinations: the second
    transaction swapped into the first offer is exactly what a dishonest
    node would hand over -- real hashes of a real transaction, over a
    transaction that pays somewhere else. Signing it is what "the node never
    holds your key" was supposed to make impossible, and it does not happen:
    the refusal arrives in the tab, before the key is used, and the node is
    never asked.
    """
    browser, base, state, node, made = funded
    honest = node.rpc.call("getnewaddress")
    elsewhere = node.rpc.call("getnewaddress")
    refused = browser.execute_async_script("""
        const done = arguments[2];
        (async () => {
          try {
            const one = await window.w.offerSend(arguments[0], "5");
            const two = await window.w.offerSend(arguments[1], "5");
            const hostile = Object.assign({}, one, {raw: two.raw});
            const out = await window.w.confirm(window.wallet, hostile);
            done({txid: out.txid});
          } catch (e) { done({error: String(e.message || e)}); }
        })();""", honest, elsewhere)
    assert "txid" not in refused, f"it signed a swapped transaction: {refused}"
    assert "Nothing was signed" in refused["error"], refused
    assert node.rpc.call("getrawmempool") == [], "nothing left this machine"
    assert abs(float(node.rpc.call("getreceivedbyaddress", elsewhere, 0))) < 1e-8
