"""Signing out leaves no unlocked words in any tab (2026-11-08, security review): the tab kept up to six phrases
(`arcade-open-wallets`, for switching accounts) and sign-out took out only the current one; the other tabs kept
their lists too."""
import pathlib
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                  # noqa: E402

LOAD = """
const done = arguments[arguments.length - 1];
import("/wallet.js").then((m) => { window.w = m; done(true); }, (e) => done(String(e)));
"""
STORED = "return [sessionStorage.getItem('arcade-open-wallet'), sessionStorage.getItem('arcade-open-wallets')];"


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    import uvicorn
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    state = AppState(home=tmp_path_factory.mktemp("signout"),
                     messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                                            datadir=pathlib.Path("/nonexistent")),
                     ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                                         datadir=pathlib.Path("/nonexistent")))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
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
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _tab(browser, base, current, kept):
    browser.get(f"{base}/join")
    browser.set_script_timeout(60)
    assert browser.execute_async_script(LOAD) is True
    browser.execute_script("sessionStorage.setItem('arcade-open-wallet', arguments[0]);"
                           "sessionStorage.setItem('arcade-open-wallets', JSON.stringify(arguments[1]));",
                           current, kept)


def test_signing_out_clears_every_unlocked_wallet_in_every_tab(served):
    browser = browsers.launch()
    try:
        _tab(browser, served, "marker one", ["marker one", "marker two"])
        first = browser.current_window_handle
        browser.switch_to.new_window("tab")
        _tab(browser, served, "marker three", ["marker three", "marker four"])
        second = browser.current_window_handle
        browser.switch_to.window(first)
        said = browser.execute_async_script("""
            const done = arguments[0];
            window.w.signOut(null).then(() => done("ok"), (e) => done(String(e)));""")
        assert said == "ok", said
        assert browser.execute_script(STORED) == [None, None], "this tab: nothing left"
        browser.switch_to.window(second)
        for _ in range(50):
            if browser.execute_script(STORED) == [None, None]:
                break
            time.sleep(0.1)
        assert browser.execute_script(STORED) == [None, None], "the other tab: nothing left either"
    finally:
        browser.quit()
