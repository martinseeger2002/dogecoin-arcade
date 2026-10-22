"""Admin -> Ords -> the explorer in a real browser (2026-10-21): the viewer opens on #0 of the chain picked at the
top, Next moves to #1, picking another chain at the top switches the explorer to that chain's #0, and the ticker search
(the Exchange's search tool) shows the token and its rich list, 20 holders a page. At phone width it all stacks and
nothing scrolls sideways. ARCADE_SHOTS=<folder> keeps a screenshot of each width there."""

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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _svg(fill: str, words: str) -> bytes:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" '
            f'fill="{fill}"/><text x="50" y="56" font-size="14" text-anchor="middle" fill="#fff" '
            f'font-family="sans-serif">{words}</text></svg>').encode()


def _fill(index, tag: int, count: int, tick: str, holders: int, content: dict) -> list[str]:
    """`count` inscriptions (#0 a picture, the rest text) and one token with `holders` holders, straight into the index."""
    store, ids = index.store, []
    store.x("INSERT OR IGNORE INTO ctypes (ct, ctype) VALUES (1, 'image/svg+xml')")
    store.x("INSERT OR IGNORE INTO ctypes (ct, ctype) VALUES (2, 'text/plain')")
    for k in range(count):
        tx = bytes([tag, k + 1]) * 16
        iid = tx.hex() + "i0"
        ids.append(iid)
        body = _svg("#2f9e6e" if tag == 1 else "#c8962a", f"{tick.upper()} #{k}") if k == 0 else f"hello #{k}".encode()
        content[iid] = ("image/svg+xml" if k == 0 else "text/plain", body)
        store.x("INSERT INTO insc (rid, tx, n, number, height, txpos, ct, proto, size, pieces, burned) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (k + 1, tx, 0, k, index.base + k, 0, 1 if k == 0 else 2, None,
                                                   len(body), 1, None))
        store.x("INSERT INTO at VALUES (?,?,?,?,?)", (k + 1, tx, 0, 0, index.base + k))
    store.x("INSERT INTO tok VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tick, tick, ids[0], 0, "21000000", "1000", 0, str(sum(range(1, holders + 1)) * 1000), "Pdeployer",
             index.base))
    store.many("INSERT INTO bal VALUES (?,?,?,?)",
               [(f"{'P' if tag == 1 else 'D'}holder{k:03d}xxxxxxxxxxxxxxxxxxxxxx", tick, str(k * 1000), "0")
                for k in range(1, holders + 1)])
    return ids


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    import uvicorn
    from pathlib import Path
    from arcade import funding as fundinglib
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    state = AppState(
        home=tmp_path_factory.mktemp("explorer"),
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=Path("/nonexistent")),
        doge=ChainContext(network="doge", role="ledger", label="Dogecoin", conf=Path("/nonexistent/dogecoin.conf")))
    state.set_setting("ord_index:main", "walk")
    state.set_setting("ord_index:doge", "walk")
    indexes = state.ord_indexes()
    content: dict = {}
    ids = {"main": _fill(indexes["main"], 1, 3, "pepi", 45, content),
           "doge": _fill(indexes["doge"], 2, 2, "wow!", 3, content)}
    app = create_app(state)

    class Kept:                                     # the chain's inscriptions, as the node would read them
        def content(self, key):
            if key not in content:
                raise LookupError(key)
            return content[key]
    was = fundinglib.GUARD, fundinglib.GUARDS
    fundinglib.GUARD, fundinglib.GUARDS = Kept(), []
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical",
                                           timeout_graceful_shutdown=1.0))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("test server did not start")
    yield f"http://127.0.0.1:{port}", ids
    server.should_exit = True
    thread.join(timeout=5)
    fundinglib.GUARD, fundinglib.GUARDS = was


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    yield driver
    driver.quit()


def _wait(browser, test, seconds=15):
    return WebDriverWait(browser, seconds).until(lambda d: test(d) or None)


def _facts(browser) -> str:
    return browser.find_element(By.ID, "ordfacts").text


def _pick_chain(browser, net):
    """Click the coin at the top of the Ords tab for this chain (its title holds the chain's name)."""
    want = {"main": "Pepecoin", "doge": "Dogecoin"}[net]
    for b in browser.find_elements(By.CSS_SELECTOR, "#ordcoins button"):
        if want in (b.get_attribute("title") or ""):
            b.click()
            return
    raise AssertionError(f"no coin for {net}: " + repr([b.get_attribute("title") for b in
                                                       browser.find_elements(By.CSS_SELECTOR, "#ordcoins button")]))


def _shot(browser, name):
    folder = os.environ.get("ARCADE_SHOTS")
    if folder:                                           # the whole page, cut to the explorer
        import io
        from PIL import Image
        browser.execute_script("[].forEach.call(document.querySelectorAll('*'), function (e) { var p = "
                               "getComputedStyle(e).position; if (p === 'sticky' || p === 'fixed') "
                               "e.style.position = 'static'; });")   # a stuck header would be drawn mid-page
        time.sleep(0.6)
        box = browser.execute_script("var r = document.getElementById('ordexplorer').getBoundingClientRect();"
                                     "return [r.left + scrollX, r.top + scrollY, r.right + scrollX, r.bottom + scrollY,"
                                     " document.documentElement.scrollWidth];")
        page = Image.open(io.BytesIO(browser.get_full_page_screenshot_as_png()))
        k = page.width / box[4]
        page.crop(tuple(int(v * k) for v in (max(0, box[0] - 8), max(0, box[1] - 8), box[2] + 8, box[3] + 8))).save(
            pathlib.Path(folder) / name)


def _open(browser, base, ids):
    browser.get(base + "/admin#ords")
    try:
        browser.execute_script("localStorage.removeItem('ords.chain')")
    except Exception:                                    # noqa: BLE001 -- no storage: the first chain anyway
        pass
    browser.get(base + "/admin#ords")
    browser.refresh()                                    # the same address again is no reload: a fresh page
    _pick_chain(_wait(browser, lambda d: d.find_elements(By.CSS_SELECTOR, "#ordcoins button") and d), "main")
    _wait(browser, lambda d: ids["main"][0] in _facts(d))


def test_the_viewer_opens_on_number_0_and_the_arrows_move_by_number(browser, served):
    base, ids = served
    browser.set_window_size(1200, 900)
    _open(browser, base, ids)
    assert "#0 of 2" in _facts(browser) and "image/svg+xml" in _facts(browser)
    frame = browser.find_element(By.CSS_SELECTOR, "#ordframe iframe")
    assert frame.get_attribute("sandbox") == "allow-scripts" and frame.get_attribute("src").endswith("/fit")
    assert not browser.find_element(By.ID, "ordprev").is_enabled(), "no Previous at #0"
    browser.find_element(By.ID, "ordnext").click()
    _wait(browser, lambda d: ids["main"][1] in _facts(d))
    _wait(browser, lambda d: "hello #1" in d.find_element(By.CSS_SELECTOR, "#ordframe pre").text), "text shown as text"
    assert browser.find_element(By.ID, "ordprev").is_enabled()
    browser.find_element(By.ID, "ordnext").click()
    _wait(browser, lambda d: ids["main"][2] in _facts(d))
    assert not browser.find_element(By.ID, "ordnext").is_enabled(), "no Next past the last known number"
    # the search takes a number and opens it in the viewer
    box = browser.find_element(By.ID, "ordlookq")
    box.clear()
    box.send_keys("1")
    browser.find_element(By.ID, "ordlookgo").click()
    _wait(browser, lambda d: "Inscription #1" in d.find_element(By.ID, "ordlooksaid").text)
    _wait(browser, lambda d: ids["main"][1] in _facts(d))


def test_the_chain_at_the_top_switches_the_explorer(browser, served):
    base, ids = served
    browser.set_window_size(1200, 900)
    _open(browser, base, ids)
    browser.find_element(By.ID, "ordnext").click()
    _wait(browser, lambda d: ids["main"][1] in _facts(d))
    _pick_chain(browser, "doge")
    _wait(browser, lambda d: ids["doge"][0] in _facts(d))
    assert "#0 of 1" in _facts(browser), "back at #0, on Dogecoin"
    assert "Dogecoin" in browser.find_element(By.ID, "ordxchain").text
    assert browser.find_element(By.ID, "ordtokh").text.startswith("DRC-20")
    _pick_chain(browser, "main")
    _wait(browser, lambda d: ids["main"][0] in _facts(d))
    assert browser.find_element(By.ID, "ordtokh").text.startswith("PRC-20")


def test_a_tick_search_shows_the_token_and_twenty_holders_a_page(browser, served):
    base, ids = served
    browser.set_window_size(1200, 900)
    _open(browser, base, ids)
    q = browser.find_element(By.CSS_SELECTOR, "#ordtickpick .pick-q")
    q.click()
    q.send_keys("pe")
    row = _wait(browser, lambda d: d.find_elements(By.CSS_SELECTOR, '#ordtickpick .pick-row[data-value="pepi"]'))[0]
    browser.execute_script("arguments[0].dispatchEvent(new MouseEvent('mousedown', {bubbles: true}))", row)
    _wait(browser, lambda d: len(d.find_elements(By.CSS_SELECTOR, "#ordtok .ordrich tbody tr")) == 20)
    text = browser.find_element(By.ID, "ordtok").text
    assert "pepi" in text and "Max supply" in text and "21,000,000" in text and "45" in text
    first = browser.find_elements(By.CSS_SELECTOR, "#ordtok .ordrich tbody tr")[0].text
    assert "#1" in first and "Pholder045" in first and "45,000" in first
    _shot(browser, "ords-explorer.png")
    browser.find_element(By.ID, "ordrichnext").click()
    _wait(browser, lambda d: d.find_element(By.ID, "ordrichpage").text == "2")
    rows = browser.find_elements(By.CSS_SELECTOR, "#ordtok .ordrich tbody tr")
    assert len(rows) == 20 and "#21" in rows[0].text and "Pholder025" in rows[0].text
    browser.find_element(By.ID, "ordrichnext").click()
    _wait(browser, lambda d: d.find_element(By.ID, "ordrichpage").text == "3")
    assert len(browser.find_elements(By.CSS_SELECTOR, "#ordtok .ordrich tbody tr")) == 5
    assert not browser.find_element(By.ID, "ordrichnext").is_enabled()
    # the deploy opens in the viewer
    browser.find_element(By.CSS_SELECTOR, "#ordtok .ordxfacts .ordxlink").click()
    _wait(browser, lambda d: ids["main"][0] in _facts(d))


def test_at_phone_width_the_explorer_stacks_and_nothing_scrolls_sideways(browser, served):
    base, ids = served
    browser.set_window_size(390, 844)
    try:
        if browser.execute_script("return window.innerWidth") > 720:
            pytest.skip("this browser would not narrow its window")
        _open(browser, base, ids)
        q = browser.find_element(By.CSS_SELECTOR, "#ordtickpick .pick-q")
        q.click()
        q.send_keys("pepi")
        row = _wait(browser, lambda d: d.find_elements(By.CSS_SELECTOR, '#ordtickpick .pick-row[data-value="pepi"]'))[0]
        browser.execute_script("arguments[0].dispatchEvent(new MouseEvent('mousedown', {bubbles: true}))", row)
        _wait(browser, lambda d: len(d.find_elements(By.CSS_SELECTOR, "#ordtok .ordrich tbody tr")) == 20)
        frame = browser.find_element(By.ID, "ordframe").rect
        facts = browser.find_element(By.ID, "ordfacts").rect
        assert facts["y"] >= frame["y"] + frame["height"] - 1, "the facts under the picture"
        assert frame["width"] <= browser.execute_script("return window.innerWidth"), "the frame fits the width"
        assert not browser.find_element(By.CSS_SELECTOR, "#ordtok .ordrich thead").is_displayed()
        assert browser.execute_script(
            "return document.documentElement.scrollWidth <= window.innerWidth"), "no sideways scroll"
        _shot(browser, "ords-explorer-phone.png")
    finally:
        browser.set_window_size(1200, 900)
