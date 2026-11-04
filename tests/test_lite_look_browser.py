"""The full arcade's own pages in ARCADE Lite wear the Lite window's look (arcade/lite/templates/pages.css): Admin with
every tab and control it has, drawn in Core's greys and system fonts, its tabs as the window's tab row. In a real
browser; ARCADE_LITE_SHOTS=<dir> also keeps a screenshot of each page."""

import os
import pathlib
import sys

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")
from selenium.webdriver.common.by import By                      # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait          # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_send_browser import browser, served  # noqa: F401,E402


def _look(browser, selector, *props):
    return browser.execute_script(
        "const e = document.querySelector(arguments[0]); if (!e) return null;"
        "const s = getComputedStyle(e); return arguments[1].map((p) => s.getPropertyValue(p));", selector, list(props))


def _press(browser, selector):
    """Click it as the page's own handler takes it: a test node's first-run setup card sits over the page."""
    browser.execute_script("document.querySelector(arguments[0]).click()", selector)


def _shot(browser, name):
    where = os.environ.get("ARCADE_LITE_SHOTS")
    if where:
        browser.set_window_size(1280, 900)
        browser.save_screenshot(str(pathlib.Path(where) / f"{name}.png"))


def test_admin_wears_the_lite_look_in_lite_and_its_own_in_full(browser, served):
    base, state, node = served
    browser.delete_all_cookies()
    browser.get(f"{base}/admin")
    full_tab = _look(browser, ".admintabs button", "border-radius")
    assert full_tab and full_tab[0] != "3px 3px 0px 0px", "the full edition keeps its own look"
    state.set_setting("edition", "lite")
    try:
        for page in ("/admin", "/wallet", "/contacts"):
            browser.get(base + page)
            assert browser.find_element(By.CSS_SELECTOR, "body[data-lite]")
            body = _look(browser, "body", "font-size", "font-family", "background-color")
            assert body[0] == "13px" and "system-ui" in body[1], (page, body)
            _shot(browser, page.strip("/"))
        browser.get(base + "/admin")
        tab = _look(browser, ".admintabs button", "border-radius", "font-weight")
        assert tab[0] == "3px 3px 0px 0px", tab
        panel = _look(browser, ".panel", "border-radius")
        assert panel == ["3px"], panel
        assert browser.find_element(By.CSS_SELECTOR, ".brand a").text.endswith(" Lite")
        tabs = [b.text for b in browser.find_elements(By.CSS_SELECTOR, ".admintabs button")]
        assert "Coins & chains" in tabs and "Edition & modules" in tabs, "every control Admin has is still there"
        bar = [a.text for a in browser.find_elements(By.CSS_SELECTOR, "header nav a")]
        assert bar == ["Overview", "Send", "Receive", "Transactions", "Address book", "Apps", "Help", "Admin"], bar
        assert _look(browser, ".admintabs + *, .panel", "box-shadow") == ["none"]
        _press(browser, '.admintabs button[data-tab="coins"]')
        WebDriverWait(browser, 30).until(lambda d: d.find_element(By.ID, "chainrows").is_displayed())
        _shot(browser, "admin-coins")
        assert browser.find_element(By.LINK_TEXT, "Send").get_attribute("href").endswith("/#send")
        browser.get(base + "/#send")                   # (a test node's setup card covers the bar)
        assert browser.find_element(By.ID, "w-form").is_displayed(), "the bar's Send opens the window's own tab"
    finally:
        state.set_setting("edition", None)
