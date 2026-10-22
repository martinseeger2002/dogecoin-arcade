"""Admin -> Overview's Arcades panel, in a real browser (2026-10-21, approving the mockup): a row per arcade,
this one first, each drawn with the arcade's own coin faces; at phone width each row is a small card, the header row
hidden, and nothing scrolls sideways."""

import pathlib
import socket
import sys
import threading
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                  # noqa: E402
from test_admin import _mesh_of                                  # noqa: E402

from selenium.webdriver.common.by import By                      # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait          # noqa: E402


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
        home=tmp_path_factory.mktemp("arcades"),
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=Path("/nonexistent")))
    other = "b" * 64
    mesh = _mesh_of("a" * 64, [other], rtts={other: 48.0}, offers={other: {"doge": {"h": 1}, "main": {"h": 1}}},
                    said={other: {"tag": "bigchiefenergy", "hosts": ["app.dogecoinarcade.com"]}},
                    sharing={"main": {"sharing": True}})
    mesh.status = lambda: {"linked": True, "node": "a" * 64, "peers": 1, "known": 1, "rtt_ms": [48.0]}
    state.mesh = mesh
    state.operator_tag = lambda: "strawberry"
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
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    yield driver
    driver.quit()


def test_the_arcades_panel_draws_a_row_per_arcade_with_coin_faces(browser, served):
    browser.set_window_size(1200, 900)
    browser.get(served + "/admin#overview")
    rows = WebDriverWait(browser, 15).until(
        lambda d: d.find_elements(By.CSS_SELECTOR, "#arcrows .arcrow:not(.archead)") or None)
    assert len(rows) == 2
    assert "@strawberry" in rows[0].text and "this arcade" in rows[0].text
    assert "@bigchiefenergy" in rows[1].text and "app.dogecoinarcade.com" in rows[1].text
    assert "linked" in rows[1].text and "48 ms" in rows[1].text
    faces = rows[1].find_elements(By.CSS_SELECTOR, ".cores img.coin")
    assert [f.get_attribute("src").rsplit("/", 1)[1] for f in faces] == ["coin-pep.png", "coin-doge.png"]
    assert browser.execute_script("return [].every.call(document.querySelectorAll('#arcades img.coin'),"
                                  " function (i) { return i.complete && i.naturalWidth > 0; })"), "every face loads"
    unshared = rows[0].find_elements(By.CSS_SELECTOR, ".cores img.coin.unshared")
    assert unshared and float(unshared[0].value_of_css_property("opacity")) == pytest.approx(0.4)
    card = browser.find_element(By.ID, "arcadescard").text
    assert "2" in card and "running" in card and "1 linked to this one" in card
    cards = browser.find_element(By.ID, "cards").text
    assert "balance" not in cards and "notifications" not in cards


def test_at_phone_width_each_arcade_is_a_card_and_nothing_scrolls_sideways(browser, served):
    browser.set_window_size(390, 844)
    try:
        if browser.execute_script("return window.innerWidth") > 720:
            pytest.skip("this browser would not narrow its window")
        browser.get(served + "/admin#overview")
        WebDriverWait(browser, 15).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "#arcrows .arcrow:not(.archead)") or None)
        assert not browser.find_element(By.CSS_SELECTOR, "#arcrows .archead").is_displayed()
        assert browser.find_element(By.CSS_SELECTOR, "#arcrows .arcrow:not(.archead) .lab").is_displayed()
        assert browser.execute_script(
            "return document.documentElement.scrollWidth <= window.innerWidth"), "no sideways scroll"
    finally:
        browser.set_window_size(1200, 900)


def test_your_arcade_has_a_row_of_wallet_coins_and_the_arcades_card(browser, served):
    """2026-10-21: on Your arcade the wallets are a row of coins, lit when shown and grey when hidden, and the
    Arcades card from Admin -> Overview sits below it. Tapping a coin greys it and keeps it so."""
    browser.set_window_size(390, 844)
    try:
        browser.get(served + "/")
        rows = WebDriverWait(browser, 15).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "#arcrows .arcrow:not(.archead)") or None)
        assert len(rows) == 2 and "@strawberry" in rows[0].text
        assert not rows[0].find_elements(By.CSS_SELECTOR, ".arctip"), "no tip to this arcade's own operator"
        tip = rows[1].find_element(By.CSS_SELECTOR, ".arctip")
        assert tip.get_attribute("href").endswith("/me/wallet?to=%40bigchiefenergy"), "tip any operator on the list"
        assert rows[1].find_elements(By.CSS_SELECTOR, ".cores img.coin"), "Cores, as on Admin"
        assert "48 ms" in rows[1].text, "and the link speed"
        chips = browser.find_elements(By.CSS_SELECTOR, "#wallet-chains .wchip")
        assert chips and all(c.get_attribute("aria-pressed") == "true" for c in chips)
        assert browser.execute_script(
            "return document.documentElement.scrollWidth <= window.innerWidth"), "no sideways scroll"
        chips[0].click()
        WebDriverWait(browser, 10).until(lambda d: "hidden" in d.find_element(By.ID, "wallet-chains-said").text)
        img = chips[0].find_element(By.CSS_SELECTOR, "img")
        WebDriverWait(browser, 5).until(lambda d: float(img.value_of_css_property("opacity")) == pytest.approx(0.35))
        browser.get(served + "/")
        WebDriverWait(browser, 15).until(lambda d: d.find_elements(By.CSS_SELECTOR, "#wallet-chains .wchip"))
        assert browser.find_elements(By.CSS_SELECTOR, "#wallet-chains .wchip")[0].get_attribute("aria-pressed") == "false"
        shots = "/tmp/claude-1000/-home-robin/55cfedea-2658-4819-80e4-33320972c0bb/scratchpad/"
        browser.find_element(By.ID, "wallet-chains").screenshot(shots + "me-wallets.png")
        WebDriverWait(browser, 15).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "#arcrows .arcrow:not(.archead)") or None)
        browser.find_element(By.ID, "arcades").screenshot(shots + "me-arcades.png")
    finally:
        browser.set_window_size(1200, 900)
