"""Signing up publishes the name and the key, without being asked.

A name that is only in this node's table is not a name, and an account
nobody can write to is not reachable. Both are transactions, so neither
can happen AT signup -- the faucet's coins have to land first -- and both
are done for somebody rather than offered as buttons they have to find.
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
from selenium.webdriver.common.by import By                      # noqa: E402


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

    home = tmp_path_factory.mktemp("setup")
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


def test_signing_up_claims_the_name_and_publishes_the_key(browser, served):
    """Neither is a decision somebody should have to find a button for."""
    base, state, node = served
    browser.get(f"{base}/join")
    browser.set_script_timeout(240)
    for _ in range(120):
        if browser.execute_script(
                "return document.body.dataset.signupReady === 'yes'"):
            break
        time.sleep(0.25)

    browser.find_element(By.ID, "tag").send_keys("byitself")
    browser.find_element(By.ID, "pw").send_keys("a long enough password")
    browser.find_element(By.ID, "make").click()

    # The words appear at once; the setting-up runs behind them.
    for _ in range(80):
        if browser.find_element(By.ID, "written").is_displayed():
            break
        time.sleep(0.25)
    assert len(browser.find_elements(By.CSS_SELECTOR, "#words li")) == 12

    # The faucet's coins need a block before anything can be spent.
    for _ in range(40):
        if node.rpc.call("getrawmempool"):
            break
        time.sleep(0.5)
    node.rpc.call("generate", 1)
    _catch_up(state)

    note = browser.find_element(By.ID, "setup-note")
    for _ in range(160):
        if "on their way" in note.text or "could not" in note.text \
                or "not published" in note.text or "no coins" in note.text:
            break
        time.sleep(0.5)
    assert "on their way" in note.text, note.text

    # Both transactions are real, and the chain agrees a block later.
    node.rpc.call("generate", 1)
    index = _catch_up(state)
    assert index.address_of("byitself"), "the name is on the chain"

    from arcade.messaging.scanner import Scanner
    with state.messaging.rpc() as rpc:
        with state.store() as store:
            # Until a pass reads nothing: one scan() is 2000 blocks, and the
            # block with the announcement in it is at the end of a walk that a
            # full run's shared node leaves several passes long.
            for _ in range(50):
                if Scanner(rpc, state.messaging.params, store,
                           identity=None).scan().blocks == 0:
                    break
            else:
                raise AssertionError("the scanner never reached the tip")
        with state.store() as store:
            said = store.key_for(index.address_of("byitself"))
    assert said is not None, "and the key is published where people look"


def test_the_steps_are_shown_rather_than_hidden(browser, served):
    """Somebody who leaves before it finishes should know what was done."""
    base, state, node = served
    browser.get(f"{base}/join")
    for _ in range(120):
        if browser.execute_script(
                "return document.body.dataset.signupReady === 'yes'"):
            break
        time.sleep(0.25)
    body = browser.page_source
    assert "your name on the chain" in body
    assert "so people can write to you" in body
    assert "You can leave before it finishes" in body
