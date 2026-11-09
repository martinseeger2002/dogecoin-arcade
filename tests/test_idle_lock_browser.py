"""The wallet locks itself after a while with nothing done (2026-11-09, security review): the unlocked words stayed in
every tab's sessionStorage until sign-out or the tab closed, so a shared computer somebody walked away from kept them
open. Now, after the minutes chosen on /me (wallet.js idleLockMinutes) with nothing done in ANY tab, every tab forgets
them -- never while a page signs or sends. The tests choose a few seconds."""
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
SECONDS = 3                        # the tests' minutes: 0.05


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    import uvicorn
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    state = AppState(home=tmp_path_factory.mktemp("idlelock"),
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


@pytest.fixture
def browser():
    driver = browsers.launch()
    yield driver
    driver.quit()


def _load(browser):
    browser.set_script_timeout(60)
    assert browser.execute_async_script(LOAD) is True


def _tab(browser, base, phrase):
    browser.get(f"{base}/join")
    _load(browser)
    browser.execute_script("sessionStorage.setItem('arcade-open-wallet', arguments[0]);"
                           "sessionStorage.setItem('arcade-open-wallets', JSON.stringify([arguments[0]]));", phrase)


def _stored(browser):
    for _ in range(20):                       # a page drawn again after the lock answers once it has loaded
        try:
            return browser.execute_script(STORED)
        except Exception:
            time.sleep(0.25)
    return browser.execute_script(STORED)


def _until_locked(browser, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if _stored(browser) == [None, None]:
            return True
        time.sleep(0.25)
    return False


def test_nothing_done_for_the_chosen_minutes_locks_every_tab_but_never_during_a_send(browser, served):
    _tab(browser, served, "marker one")
    first = browser.current_window_handle
    browser.switch_to.new_window("tab")
    _tab(browser, served, "marker two")
    second = browser.current_window_handle
    browser.switch_to.window(first)
    # A send under way in the first tab (wallet.js working()): neither tab locks, however long it takes.
    browser.execute_script("document.body.dataset.working = 'yes'; window.w.setIdleLock(arguments[0] / 60);", SECONDS)
    time.sleep(SECONDS * 2.5)
    assert _stored(browser)[0] == "marker one", "not under a send"
    browser.switch_to.window(second)
    assert _stored(browser)[0] == "marker two", "nor in another tab while one sends"
    browser.switch_to.window(first)
    browser.execute_script("delete document.body.dataset.working;")
    assert _until_locked(browser, SECONDS * 5), "the send ended and nothing was done: locked"
    browser.switch_to.window(second)
    assert _until_locked(browser, SECONDS * 3), "every tab"
    # Still signed in (no sign-out was asked of the node), and the page says why it is locked.
    _load(browser)
    said = browser.execute_script("const t = document.querySelector('.arcadetoast'); return t && t.textContent;")
    assert said and "locked after" in said and "still signed in" in said, said
    assert browser.execute_script("return JSON.parse(localStorage.getItem('arcade.idle-locked')).minutes") == SECONDS / 60


def test_a_held_spend_lock_in_any_tab_keeps_the_wallet_open(browser, served):
    _tab(browser, served, "marker three")
    browser.execute_script("""
        navigator.locks.request("arcade-spend", () => new Promise((r) => { window.release = r; }));
        window.w.setIdleLock(arguments[0] / 60);""", SECONDS)
    time.sleep(SECONDS * 2.5)
    assert _stored(browser)[0] == "marker three", "a spend waiting on its lock is a send under way"
    browser.execute_script("window.release();")
    assert _until_locked(browser, SECONDS * 5)


def test_coming_back_after_the_minutes_locks_before_the_first_touch_counts(browser, served):
    """A machine that slept, or a tab whose timers were held: the first key pressed on coming back must not be the
    activity that keeps the words open."""
    _tab(browser, served, "marker four")
    browser.execute_script("localStorage.setItem('arcade.idle-lock', '1');")
    time.sleep(5.5)                           # past the few seconds a tab's own touch is trusted for
    browser.execute_script("""
        localStorage.setItem('arcade.last-active', String(Date.now() - 5 * 60000));
        window.dispatchEvent(new KeyboardEvent('keydown', {key: 'a'}));""")
    assert _until_locked(browser, 3), "locked on the touch, not counted as kept open by it"
    assert time.time() * 1000 - float(browser.execute_script(
        "return localStorage.getItem('arcade.last-active');")) < 10000, "and counted from then"


def test_off_never_locks(browser, served):
    _tab(browser, served, "marker five")
    browser.execute_script("""
        window.w.setIdleLock(0);
        localStorage.setItem('arcade.last-active', String(Date.now() - 120 * 60000));
        window.dispatchEvent(new KeyboardEvent('keydown', {key: 'a'}));""")
    time.sleep(1.5)
    assert _stored(browser)[0] == "marker five"
    assert browser.execute_script("return [window.w.idleLockMinutes(), window.w.IDLE_DEFAULT]") == [0, 15]
