"""The enforced script policy in a real browser (phase 2, 2026-11-09, security review): with Admin -> Safety -> "Enforce
the script policy" on, a page's own scripts run and a script slipped into it does not -- an injected <script>, an
on...= attribute, a javascript: address -- while one carrying the page's nonce still would. The pages built in Python
(the globe, the mesh Browser) run, and a draft previewed through /preview-frame runs its own scripts."""

import pathlib
import socket
import sys
import threading
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                  # noqa: E402

from selenium.webdriver.common.by import By                      # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait          # noqa: E402

from arcade.web.app import SCRIPT_POLICY_SETTING, script_policy_forced  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    import uvicorn
    from pathlib import Path
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    state = AppState(
        home=tmp_path_factory.mktemp("scriptpolicy"),
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=Path("/nonexistent")))
    state.set_setting(SCRIPT_POLICY_SETTING, True)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1", port=port, log_level="critical",
                                           timeout_graceful_shutdown=1.0))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("test server did not start")
    yield f"http://127.0.0.1:{port}", state
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    yield driver
    driver.quit()


#: Slips three scripts into the page the ways an injection would, and one carrying the page's own nonce (read from a
#: script of the page, as only the page itself can). Each sets a mark if it runs.
INJECT = """
var head = document.head, own = document.querySelector('script[nonce]');
var bare = document.createElement('script'); bare.textContent = 'window.__bare = 1'; head.appendChild(bare);
var div = document.createElement('div');
div.innerHTML = '<img src="/nowhere.png" onerror="window.__attr = 1">'
  + '<a id="jslink" href="javascript:window.__js = 1">x</a>';
document.body.appendChild(div);
document.getElementById('jslink').click();
var good = document.createElement('script'); good.nonce = own ? own.nonce : '';
good.textContent = 'window.__nonced = 1'; head.appendChild(good);
"""

MARKS = "return [window.__bare, window.__attr, window.__js, window.__nonced]"


def _injected(browser):
    browser.execute_script(INJECT)
    time.sleep(1.0)                                       # the picture's error and the link's navigation are not sync
    return browser.execute_script(MARKS)


def test_enforced_the_page_runs_its_own_scripts_and_no_injected_one(browser, served):
    base, state = served
    browser.get(base + "/join")
    WebDriverWait(browser, 15).until(lambda d: d.execute_script("return document.readyState") == "complete")
    assert browser.execute_script("return typeof window.arcadeTheme") == "function", "base.html's own script ran"
    assert browser.execute_script("return typeof window.arcadePress") == "function", "and its own module"
    bare, attr, js, nonced = _injected(browser)
    assert (bare, attr, js) == (None, None, None), "an injected script, handler or javascript: link ran"
    assert nonced == 1, "a script with the page's nonce still runs: the test would see a broken page as a pass"
    browser.set_script_timeout(10)
    beat = browser.execute_async_script("""
        var done = arguments[0];
        var w = new Worker(URL.createObjectURL(new Blob(['postMessage(7)'], {type: 'text/javascript'})));
        w.onmessage = function (e) { done(e.data); }; w.onerror = function () { done('stopped'); };""")
    assert beat == 7, "the trade runner's blob: timer worker (wallet_js.js) still starts"


def test_switched_off_the_same_injection_runs(browser, served):
    """Phase 1 (report-only) is what the switch goes back to: nothing is stopped, so the test above means something."""
    base, state = served
    if script_policy_forced():
        pytest.skip("this run enforces the script policy for every page (ARCADE_ENFORCE_SCRIPT_POLICY)")
    state.set_setting(SCRIPT_POLICY_SETTING, False)
    try:
        browser.get(base + "/join")
        WebDriverWait(browser, 15).until(lambda d: d.execute_script("return document.readyState") == "complete")
        bare, attr, js, nonced = _injected(browser)
        assert (bare, attr, nonced) == (1, 1, 1)
    finally:
        state.set_setting(SCRIPT_POLICY_SETTING, True)


def test_the_pages_built_in_python_run_under_it(browser, served):
    base, _ = served
    browser.get(base + "/globe")
    WebDriverWait(browser, 15).until(lambda d: d.execute_script("return typeof window.THREE") == "object")
    browser.get(base + "/browser")
    WebDriverWait(browser, 15).until(lambda d: d.execute_script("return Array.isArray(window.back)"))


def test_a_draft_previewed_through_the_preview_frame_runs_its_scripts(browser, served):
    """A srcdoc frame would take the page's policy and stop the draft's own script; /preview-frame has its own."""
    base, _ = served
    browser.get(base + "/join")
    WebDriverWait(browser, 15).until(lambda d: d.execute_script("return typeof window.arcadePreview") == "function")
    browser.execute_script("""
        var f = document.createElement('iframe'); f.id = 'drafted'; f.setAttribute('sandbox', 'allow-scripts');
        document.body.appendChild(f);
        arcadePreview(f, '<p id="said">not run</p><button id="b" onclick="this.textContent=1">b</button>'
          + '<script>document.getElementById("said").textContent = "ran"<\\/script>');""")
    outer = browser.find_element(By.ID, "drafted")
    browser.switch_to.frame(outer)
    try:
        inner = WebDriverWait(browser, 15).until(lambda d: d.find_elements(By.TAG_NAME, "iframe") or None)[0]
        browser.switch_to.frame(inner)
        WebDriverWait(browser, 15).until(lambda d: d.find_element(By.ID, "said").text == "ran")
        browser.execute_script("document.getElementById('b').click()")
        assert browser.find_element(By.ID, "b").text == "1", "the draft's own handlers run too"
    finally:
        browser.switch_to.default_content()
    # Drawn again in place, without loading the frame again.
    browser.execute_script("arcadePreview(document.getElementById('drafted'),"
                           " '<p id=\"said\">x</p><script>document.getElementById(\"said\").textContent = \"again\"<\\/script>')")
    browser.switch_to.frame(outer)
    try:
        browser.switch_to.frame(WebDriverWait(browser, 15).until(
            lambda d: d.find_elements(By.TAG_NAME, "iframe") or None)[0])
        WebDriverWait(browser, 15).until(lambda d: d.find_elements(By.ID, "said")
                                         and d.find_element(By.ID, "said").text == "again")
    finally:
        browser.switch_to.default_content()


def test_the_preview_frame_takes_no_draft_from_a_page_that_did_not_frame_it(browser, served):
    """Opened on its own (or by another window), it draws nothing it is sent."""
    base, _ = served
    browser.get(base + "/preview-frame")
    browser.execute_script("window.postMessage({arcadePreview: '<p id=said>x</p>'}, '*')")
    time.sleep(0.5)
    assert not browser.find_elements(By.TAG_NAME, "iframe")


def test_the_switch_in_admin_turns_it_off_and_on(browser, served):
    base, state = served
    if script_policy_forced():
        pytest.skip("this run holds it on (ARCADE_ENFORCE_SCRIPT_POLICY): the switch is drawn disabled")
    browser.get(base + "/admin#safety")
    box = WebDriverWait(browser, 15).until(lambda d: d.find_elements(By.ID, "script_policy_enforce") or None)[0]
    assert box.is_selected()
    browser.execute_script("document.querySelector('[data-tab=safety]').click()")
    browser.execute_script("arguments[0].click()", box)
    browser.execute_script("document.getElementById('scriptpolicysave').click()")
    WebDriverWait(browser, 15).until(lambda d: "Saved" in d.find_element(By.ID, "scriptpolicysaid").text)
    assert state.setting(SCRIPT_POLICY_SETTING) is False
    browser.execute_script("arguments[0].click()", browser.find_element(By.ID, "script_policy_enforce"))
    browser.execute_script("document.getElementById('scriptpolicysave').click()")
    WebDriverWait(browser, 15).until(lambda d: "on from the next page" in d.find_element(By.ID, "scriptpolicysaid").text)
    assert state.setting(SCRIPT_POLICY_SETTING) is True
