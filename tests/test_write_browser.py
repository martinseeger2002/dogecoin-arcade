"""One account writing to another, sealed in the browser, on a real chain.

The loop closed: a key looked up from the chain, a message sealed to it
here, a transaction the node builds and broadcasts without learning what
is inside, and the other account opening it by trying its own key.
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

from arcade import seed                                          # noqa: E402
from arcade.messaging.envelope import Header, TYPE_SINGLE        # noqa: E402
from arcade.messaging.keys import Identity                       # noqa: E402


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

    home = tmp_path_factory.mktemp("write")
    chain = Pointed(network="regtest", role="messaging", label="Testnet",
                    datadir=regtest.datadir)
    state = AppState(home=home, messaging=chain,
                     ledger=ChainContext(network="main", role="ledger",
                                         label="Mainnet",
                                         datadir=pathlib.Path("/nonexistent")))
    (home / "tokens-chain").write_text("regtest\n")
    regtest.rpc.call("generate", 140)

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


def _scan(state):
    """Let the node's scanner file everything on the chain.

    One `scan()` is a bounded pass -- 2000 blocks -- and the announcement these
    tests wait for is in the newest block, at the end of the walk. On the short
    chain a file gets when it runs alone, one pass reaches it; on the shared node
    a full run leaves behind, the walk stops short and the key is simply not
    there yet. So keep going until a pass reads nothing, which is the end the
    watcher works to as well.
    """
    from arcade.messaging.scanner import Scanner

    with state.messaging.rpc() as rpc:
        with state.store() as store:
            # No identity: the node cannot open anything and is not asked
            # to. It collects candidates and that is all this needs.
            for _ in range(50):
                if Scanner(rpc, state.messaging.params, store,
                           identity=None).scan().blocks == 0:
                    return
    raise AssertionError("the scanner never reached the tip of the chain")


def _page(browser, base):
    browser.get(f"{base}/join")
    # The node's own view (2026-09-30): an account's view refreshes the page
    # when that account's coins arrive, and this test pays its accounts between
    # steps that keep their wallet on `window`.
    browser.add_cookie({"name": "arcade_view", "value": "node", "path": "/"})
    browser.get(f"{base}/join")
    browser.set_script_timeout(180)
    assert browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        Promise.all([import("/wallet.js"), import("/messaging.js")]).then(
          ([w, m]) => { window.w = w; window.m = m; done(true); },
          (e) => done(String(e)));""") is True


def _sign_up(browser, state, node, name):
    """An account with coins, a claimed name, and a published key."""
    version = state.messaging.params.pubkeyhash_version
    made = browser.execute_async_script("""
        const done = arguments[3];
        window.w.signUp(arguments[0], arguments[1],
                        {network: "regtest", version: arguments[2]})
          .then((r) => { window.wallet = r.wallet;
                         done({tag: r.tag, address: r.address}); },
                (e) => done({error: String(e.message || e)}));""",
        name, "a long enough password", version)
    assert "error" not in made, made
    node.rpc.call("sendtoaddress", made["address"], 50.0)
    node.rpc.call("generate", 1)
    _catch_up(state)
    return made


def test_the_browser_binds_the_same_header_python_does(browser, served):
    """It is authenticated inside the box, so a header that differs by one
    byte is a message the other side refuses as forged."""
    base, state, node = served
    _page(browser, base)
    said = browser.execute_async_script("""
        const done = arguments[0];
        (async () => {
          // the module keeps it private, so it is rebuilt the same way
          const header = new Uint8Array([0x61, 0x72, 0x63, 0x6d, 0x01, 0x01]);
          done(window.m.hex(header));
        })();""")
    assert said == Header(type=TYPE_SINGLE).bound_bytes().hex()


def test_one_account_writes_to_another(browser, served):
    """The whole loop, on a real chain."""
    base, state, node = served
    _page(browser, base)

    # The recipient: signs up, claims a name, publishes a messaging key.
    them = _sign_up(browser, state, node, "hearer")
    their_phrase = browser.execute_script("return window.wallet.phrase")
    claimed = browser.execute_async_script("""
        const done = arguments[0];
        window.w.claim(window.wallet).then(done,
          (e) => done({error: String(e.message || e)}));""")
    assert "error" not in claimed, claimed
    published = browser.execute_async_script("""
        const done = arguments[1];
        (async () => {
          try {
            const me = await window.m.identity(arguments[0]);
            done(await window.m.announce(window.wallet, me, "hearer"));
          } catch (e) { done({error: String(e.message || e)}); }
        })();""", their_phrase)
    assert "error" not in published, published
    node.rpc.call("generate", 1)
    _catch_up(state)
    _scan(state)

    # The sender: a different account, on a fresh page.
    _page(browser, base)
    _sign_up(browser, state, node, "speaker")
    sent = browser.execute_async_script("""
        const done = arguments[2];
        (async () => {
          try {
            const me = await window.m.identity(window.wallet.phrase);
            done(await window.m.write(window.wallet, me, arguments[0],
                                      arguments[1]));
          } catch (e) { done({error: String(e.message || e)}); }
        })();""", "@hearer", "the quiet part out loud")
    assert "error" not in sent, sent
    assert len(sent["txid"]) == 64
    assert sent["tag"] == "hearer"

    node.rpc.call("generate", 1)
    _catch_up(state)
    _scan(state)

    # And the recipient opens it, on their own page, with their own key.
    _page(browser, base)
    got = browser.execute_async_script("""
        const done = arguments[1];
        (async () => {
          try {
            const kill = indexedDB.deleteDatabase("arcade-messages");
            await new Promise((ok) => { kill.onsuccess = kill.onerror =
                                        kill.onblocked = ok; });
            const me = await window.m.identity(arguments[0]);
            const out = await window.m.collect(me);
            done({...out, inbox: await window.m.inbox()});
          } catch (e) { done({error: String(e.message || e)}); }
        })();""", their_phrase)
    assert "error" not in got, got
    assert got["opened"] >= 1, got
    bodies = [bytes.fromhex(m["body"]) for m in got["inbox"]]
    assert any(b.endswith(b"the quiet part out loud") for b in bodies), bodies


def test_somebody_with_no_published_key_cannot_be_written_to(browser, served):
    """Sealing to nothing would be a fee spent on a message nobody can
    open, and the refusal says what to do about it."""
    base, state, node = served
    _page(browser, base)
    _sign_up(browser, state, node, "shouter")
    said = browser.execute_async_script("""
        const done = arguments[2];
        (async () => {
          try {
            const me = await window.m.identity(window.wallet.phrase);
            done(await window.m.write(window.wallet, me, arguments[0],
                                      arguments[1]));
          } catch (e) { done({error: String(e.message || e)}); }
        })();""", "@nobodyatall", "hello?")
    assert "nobody holds" in said.get("error", ""), said
