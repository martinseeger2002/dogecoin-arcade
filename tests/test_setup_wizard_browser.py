"""The new operator's setup wizard in a real browser (2026-11-02, approving the mock-up): it opens on Admin while
The operator has no @name, shows the arcade's address to copy before it is funded, keeps Claim off until a free name is
typed and coins can be spent, closes with Later, and never opens once the name is on the chain."""

import json
import os
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

ADDRESS = "nghHJba6wBoWesbYQfkZiMTicoQANkpdvH"


def _state(**kw):
    base = {"need": True, "tag": "", "net": "test", "label": "Pepecoin test", "operator_is_node": True,
            "node": {"ready": False, "progress": 0.06, "blocks": 94210, "headers": 1558431, "online": True,
                     "borrowed": False, "catchup": "behind (block 94,210 of 1,558,431): reads borrowed from @strawberry"},
            "index": {"indexed": 1012000, "tip": 1558431, "current": False,
                      "copy": {"fetch": {"from": "86e25ef9c6f1", "have": 212, "of": 411}}},
            "coins": {"address": ADDRESS, "on_chain": 0, "spendable": 0, "mined": 0, "arrived_at": None},
            "welcome": {"asked": True, "can_ask": True, "senders": [], "welcomed_by": []},
            "claiming": None, "blocked": "index"}
    for k, v in kw.items():
        base[k] = {**base[k], **v} if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return base


STATES = {
    "a-fresh": _state(),
    "b-coins": _state(node={"progress": 0.31, "blocks": 483000}, index={"current": True, "copy": {}, "indexed": 1558431},
                      welcome={"senders": ["@bigchiefenergy"]}, blocked="no coins"),
    "c-claim": _state(node={"progress": 0.31, "blocks": 483000}, index={"current": True, "copy": {}, "indexed": 1558431},
                      coins={"spendable": 100000000, "on_chain": 100000000}, welcome={"welcomed_by": ["@bigchiefenergy"]},
                      blocked=""),
    "d-out": _state(node={"progress": 0.31, "blocks": 483000}, index={"current": True, "copy": {}, "indexed": 1558431},
                    coins={"spendable": 98000000}, claiming={"tag": "dogefeed", "txid": "8f3a" + "0" * 56 + "c21e",
                                                             "at": 1793660000}, blocked="claimed"),
}


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
        home=tmp_path_factory.mktemp("setup"),
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=Path("/nonexistent")))
    state.operator_tag = lambda: ""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1", port=port, log_level="critical",
                                           timeout_graceful_shutdown=1.0))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}", state
    server.should_exit = True


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    yield driver
    driver.quit()


def _wizard(browser):
    return WebDriverWait(browser, 15).until(lambda d: d.find_elements(By.CSS_SELECTOR, "#setupwizard .swz") or None)[0]


def test_it_opens_for_an_operator_with_no_name_and_later_closes_it(browser, served):
    base, state = served
    browser.set_window_size(1100, 1000)
    browser.get(base + "/admin")
    card = _wizard(browser)
    assert "Set up your arcade" in card.text and "1 · Your" in card.text
    card.find_element(By.XPATH, ".//button[normalize-space()='Later']").click()
    assert not browser.find_elements(By.ID, "setupwizard")
    assert not browser.find_elements(By.XPATH, "//h3[contains(., 'First, this arcade needs some Pepecoin test')]"), \
        "the old coins-only sheet gives way to the wizard"


def test_it_never_opens_once_the_operator_has_a_name(browser, served):
    base, state = served
    state.operator_tag = lambda: "robin"
    try:
        browser.get(base + "/admin")
        time.sleep(3)
        assert not browser.find_elements(By.ID, "setupwizard")
    finally:
        state.operator_tag = lambda: ""


@pytest.mark.parametrize("name", list(STATES))
def test_each_state_shows_what_it_should(browser, served, name):
    """The four states of the mock-up, drawn from /admin/api/setup answers (the page's fetch answered here)."""
    base, state = served
    browser.set_window_size(900, 1300)
    browser.get(base + "/admin")
    _wizard(browser)
    said = json.dumps(STATES[name])
    browser.execute_script("""
      var said = JSON.parse(arguments[0]), real = window.fetch;
      window.fetch = function (url, opts) {
        if (String(url).indexOf('/admin/api/setup/name') === 0)
          return Promise.resolve(new Response(JSON.stringify({valid: true, tag: 'dogefeed', taken: false, mine: false})));
        if (String(url) === '/admin/api/setup') return Promise.resolve(new Response(JSON.stringify(said)));
        return real.apply(this, arguments);
      };""", said)
    browser.execute_script("window.arcadeSetup.open()")
    marker = {"a-fresh": "212 of 411", "b-coins": "@bigchiefenergy", "c-claim": "Claim your @name\n@",
              "d-out": "on its way"}[name]
    WebDriverWait(browser, 15).until(lambda d: marker in d.find_element(By.ID, "setupwizard").text)
    card = browser.find_element(By.CSS_SELECTOR, "#setupwizard .swz")
    text = card.text
    if name == "a-fresh":
        assert "Names index" in text and "212 of 411" in text and ADDRESS in text
        assert card.find_elements(By.XPATH, ".//button[normalize-space()='Copy']")
    if name == "b-coins":
        assert "@bigchiefenergy" in text and "Mine a block" in text and ADDRESS in text
    if name == "c-claim":
        box = card.find_element(By.CSS_SELECTOR, "input.swzname")
        claim = card.find_element(By.XPATH, ".//button[starts-with(normalize-space(), 'Claim')]")
        assert not claim.is_enabled(), "nothing typed yet"
        box.send_keys("dogefeed")
        WebDriverWait(browser, 5).until(lambda d: claim.is_enabled())
        assert claim.text == "Claim @dogefeed" and "free" in card.text
        assert len(card.find_elements(By.XPATH, ".//button[contains(., 'Publish')]")) == 0, "one button"
    if name == "d-out":
        assert "@dogefeed is on its way" in text and "waiting for a block" in text
    if os.environ.get("SHOT_DIR"):
        time.sleep(0.6)
        card.screenshot(os.path.join(os.environ["SHOT_DIR"], f"wizard-{name}.png"))
