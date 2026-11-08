"""What the ArcadeCore window does, in the website's look, in a real browser (2026-11-08, SadFrog: "when you change the
theme, thats it everything is styled differently but the availability and capability doesnt change"): the theme picked
and worn on /look, Your tabs honoured by the website's bar and the window's tabs, the console on /console, Build's
guide typing a line into it, API keys made on /me/keys; and the other way, the
website's pages opened inside the window from Go, a framed page's link to the console opening the window's own."""

import pathlib
import sys
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                  # noqa: E402
from test_digidollar_wallet_browser import _serve, _wait  # noqa: E402

from selenium.webdriver.common.by import By                      # noqa: E402
from selenium.webdriver.common.keys import Keys                  # noqa: E402


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    driver.set_window_size(1100, 950)
    yield driver
    driver.quit()


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    base, state, server, thread = _serve(tmp_path_factory.mktemp("coreweb"), False)
    yield base, state
    server.should_exit = True
    thread.join(timeout=5)


def _website(browser, base, path):
    """`path` in the website's look, whatever this browser wore before."""
    browser.get(f"{base}/docs")
    browser.execute_script("localStorage.clear(); localStorage.setItem('arcade.deskview', 'web')")
    browser.get(base + path)


def _press(browser, selector):
    browser.execute_script("document.querySelector(arguments[0]).click()", selector)


def _bg(browser):
    return browser.execute_script("return getComputedStyle(document.body).backgroundColor")


def test_the_operator_wears_a_theme_and_hides_a_tab_from_the_websites_look(browser, served, tmp_path):
    import json
    from arcade import packages
    from arcade.lite import commands as cmdlib
    base, state = served
    folder = packages.template(tmp_path, "sunset")
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["look"]["light"].update(window="#fdf0e0", bar="#f6dcc0")
    del manifest["look"]["dark"]
    (folder / "manifest.json").write_text(json.dumps(manifest))
    op = cmdlib.Caller(state, "operator", local=True)
    cmdlib.run_line(op, f"installtheme {folder}")
    browser.delete_all_cookies()
    try:
        # /look: the operator's themes, as Options -> Themes; making Sunset the arcade's dresses the website at once.
        _website(browser, base, "/look")
        browser.execute_script("document.documentElement.dataset.theme = 'light'")
        before = _bg(browser)
        _wait(browser, "return document.getElementById('o-themes').textContent.includes('Sunset')")
        _press(browser, '#o-themes tr:nth-child(2) input[type="radio"]')
        _wait(browser, "return getComputedStyle(document.body).backgroundColor === 'rgb(253, 240, 224)'")
        assert before != "rgb(253, 240, 224)"
        assert state.setting(packages.THEME), "the arcade's theme, as settheme keeps it"
        _website(browser, base, "/docs")
        browser.execute_script("document.documentElement.dataset.theme = 'light'")
        assert _bg(browser) == "rgb(253, 240, 224)", "every website page wears it"

        # Your tabs, on the same page: Feed unticked and saved is gone from the website's bar and the window's tabs.
        _website(browser, base, "/look")
        _wait(browser, "return document.querySelector('#yourtabs-list input[value=\"feed\"]')")
        assert browser.find_element(By.ID, "yourtabs").is_displayed(), "the window's dialog, drawn as the page"
        _press(browser, '#yourtabs-list input[value="feed"]')
        _press(browser, "#yourtabs-save")
        for _ in range(80):                                # kept, then the page reloads
            if "feed" in str(state.setting("lite_tabs")):
                break
            time.sleep(0.25)
        assert state.setting("lite_tabs") == {"node": ["feed"]}, state.setting("lite_tabs")
        _website(browser, base, "/docs")
        bar = [a.get_attribute("href") for a in browser.find_elements(By.CSS_SELECTOR, "header nav a")]
        assert not any(h.endswith("/feed") for h in bar), bar
        browser.get(f"{base}/lite")
        assert not browser.find_elements(By.CSS_SELECTOR, '.toolbar [data-module="feed"]'), "nor a tab in the window"
    finally:
        state.set_setting("lite_tabs", None)
        cmdlib.run_line(op, "removetheme sunset")


def test_the_console_and_builds_guide_on_the_website(browser, served):
    base, state = served
    browser.delete_all_cookies()
    _website(browser, base, "/console")
    _wait(browser, "return document.body.dataset.consoleReady === 'yes'")
    line = browser.find_element(By.ID, "line")
    line.send_keys("help", Keys.ENTER)
    _wait(browser, "return document.getElementById('out').textContent.includes('listthemes') || "
                   "(document.getElementById('out').textContent.length > 120 && document.getElementById('out').textContent)")
    assert "listthemes" in browser.find_element(By.ID, "out").text, browser.find_element(By.ID, "out").text
    line.send_keys("nosuchcommand", Keys.ENTER)
    _wait(browser, "return document.querySelector('#out .err')")
    line.send_keys(Keys.ARROW_UP)
    assert line.get_attribute("value") == "nosuchcommand", "Up walks back through what was typed"

    # Build's guide, step 4: "Type it in the console" types the line into the website's console.
    _website(browser, base, "/build")
    _press(browser, '#build [data-step="4"]')
    _press(browser, '#build [data-type^="trytheme"]')
    _wait(browser, "return location.pathname === '/console' && document.body.dataset.consoleReady === 'yes'")
    assert browser.find_element(By.ID, "line").get_attribute("value") == "trytheme ~/my-theme"


def test_api_keys_on_the_website_for_the_operator(browser, served):
    """An account's own keys, from outside, are test_core_in_every_look.py's: at the machine the window and the website
    both give the node's (the caller there is the operator)."""
    from arcade import apikeys as keylib
    base, state = served
    browser.delete_all_cookies()
    _website(browser, base, "/me/keys")
    _wait(browser, "return document.getElementById('k-list').textContent.includes('No keys yet')")
    assert browser.find_element(By.ID, "k-users").is_displayed(), "the operator's say over users' keys"
    browser.find_element(By.ID, "k-name").send_keys("my-site")
    _press(browser, "#k-make")
    _wait(browser, "return document.getElementById('k-shown').textContent.startsWith('ak_')")
    _wait(browser, "return document.getElementById('k-list').textContent.includes('my-site')")
    assert [k.name for k in keylib.KeyStore(state.home).list("node")] == ["my-site"]

    browser.delete_all_cookies()


def test_the_window_opens_the_websites_pages_and_its_links_open_the_windows_dialogs(browser, served):
    base, state = served
    browser.delete_all_cookies()
    browser.get(f"{base}/docs")
    browser.execute_script("localStorage.clear(); localStorage.setItem('arcade.deskview', 'core')")
    browser.get(f"{base}/lite")
    _wait(browser, "return [...document.querySelectorAll('.menu > button')].some(b => b.textContent === 'Go')")
    _press(browser, '.menu [data-go="/docs"]')
    frame = browser.find_element(By.CSS_SELECTOR, 'section[data-panel="page"] iframe')
    _wait(browser, "return !document.querySelector('section[data-panel=\"page\"]').hidden")
    assert browser.current_url.rstrip("/").endswith("/lite"), "still the window"
    browser.switch_to.frame(frame)
    try:
        _wait(browser, "return document.body && document.body.hasAttribute('data-lite')")
        _wait(browser, "return !!document.querySelector('footer a[href=\"/console\"]')")
        browser.execute_script("document.querySelector('footer a[href=\"/console\"]').click()")
    finally:
        browser.switch_to.default_content()
    _wait(browser, "return document.getElementById('debug').classList.contains('open')")
    assert browser.find_element(By.ID, "line").is_displayed(), "the window's own console, not a page in the frame"


def test_admins_open_link_for_a_module_with_no_tab_opens_its_page_in_the_window(browser, served):
    """Admin -> Edition & modules, inside the window: "Open DigiDollar" opens its tab, or, with that tab hidden under
    Your tabs, the module's page itself in the frame (it was a click that did nothing)."""
    from arcade.lite import commands as cmdlib
    from test_digidollar_wallet_browser import PILOT
    base, state = served
    op = cmdlib.Caller(state, "operator", local=True)
    cmdlib.run_line(op, f"installmodule {PILOT}")
    cmdlib.run_line(op, "install digidollar")
    state.set_setting("lite_tabs", {"node": ["digidollar"]})
    try:
        browser.delete_all_cookies()
        browser.get(f"{base}/docs")
        browser.execute_script("localStorage.clear(); localStorage.setItem('arcade.deskview', 'core')")
        browser.get(f"{base}/lite#admin")
        assert not browser.find_elements(By.CSS_SELECTOR, '.toolbar [data-module="digidollar"]')
        frame = browser.find_element(By.CSS_SELECTOR, 'section[data-panel="admin"] iframe')
        _wait(browser, "return (document.querySelector('section[data-panel=\"admin\"] iframe').getAttribute('src') || '')"
                       ".endsWith('/admin')")
        browser.switch_to.frame(frame)
        _wait(browser, "return document.querySelector('.admintabs button[data-tab=\"modules\"]')")
        browser.execute_script("const w = document.getElementById('setupwizard'); if (w) w.remove();"
                               "document.querySelector('.admintabs button[data-tab=\"modules\"]').click()")
        _wait(browser, "return document.querySelector('#modules a[href=\"/module/digidollar\"]')")
        browser.execute_script("document.querySelector('#modules a[href=\"/module/digidollar\"]').click()")
        _wait(browser, "return location.pathname === '/module/digidollar'")
        _wait(browser, "return document.body.hasAttribute('data-lite')")
    finally:
        browser.switch_to.default_content()
        state.set_setting("lite_tabs", None)
