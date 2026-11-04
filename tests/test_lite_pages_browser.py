"""Lite's own Join, Backup, Address book and Options, in a real browser on regtest: an account made and signed in from
the Join panel ARCADE Lite serves at /join and its words shown again at /me/backup, a claimed name added to the book and
paid from it, and the operator's Options."""

import pathlib
import sys
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")
from selenium.webdriver.common.by import By                      # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait          # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_send_browser import _catch_up, _keys, browser, funded, served  # noqa: F401,E402
from test_profile_pay_browser import _signed_up  # noqa: E402
from test_write_browser import _scan  # noqa: E402


def _wait(driver, seconds=60):
    return WebDriverWait(driver, seconds, poll_frequency=0.2)


@pytest.fixture
def lite(served):
    """ARCADE Lite's /join, for one test: the edition is read on every request."""
    base, state, node = served
    state.set_setting("edition", "lite")
    yield base, state, node
    state.set_setting("edition", None)


def test_an_account_is_made_and_signed_in_from_lites_join(browser, lite):
    base, state, node = lite
    browser.delete_all_cookies()
    browser.get(f"{base}/join")
    browser.execute_script("sessionStorage.clear(); localStorage.clear();")
    browser.get(f"{base}/join")
    assert browser.find_element(By.ID, "j-make").is_displayed(), "the Join panel is open at /join"
    name, password = f"lite{int(time.time() * 1000) % 100000}", "a long enough password"
    browser.find_element(By.ID, "j-tag").send_keys(name)
    browser.find_element(By.ID, "j-pw").send_keys(password)
    browser.find_element(By.ID, "j-pw2").send_keys(password)
    browser.find_element(By.CSS_SELECTOR, '#j-make button[type="submit"]').click()
    _wait(browser).until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#j-words li")) == 12)
    words = [li.text for li in browser.find_elements(By.CSS_SELECTOR, "#j-words li")]
    done = browser.find_element(By.ID, "j-done")
    assert not done.is_enabled(), "Done waits until the words are said to be written down"
    browser.find_element(By.ID, "j-kept").click()
    done.click()
    _wait(browser).until(lambda d: not d.current_url.endswith("/join"))

    browser.get(f"{base}/join")
    _wait(browser).until(lambda d: d.find_elements(By.ID, "j-out"))
    browser.find_element(By.ID, "j-out").click()
    _wait(browser).until(lambda d: d.current_url.rstrip("/") == base)
    browser.get(f"{base}/join")
    browser.find_element(By.ID, "j-in-tag").send_keys(name)
    browser.find_element(By.ID, "j-in-pw").send_keys(password)
    browser.find_element(By.CSS_SELECTOR, '#j-in button[type="submit"]').click()
    _wait(browser).until(lambda d: not d.current_url.endswith("/join"))
    who = browser.execute_async_script("""
        const done = arguments[0];
        fetch("/auth/who").then((r) => r.json()).then(done, () => done({}));""")
    assert who.get("pubkey"), who

    browser.get(f"{base}/me/backup")
    browser.find_element(By.ID, "k-pw").send_keys(password)
    browser.find_element(By.CSS_SELECTOR, '#k-show button[type="submit"]').click()
    _wait(browser).until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#k-words li")) == 12
                         or d.find_element(By.ID, "k-said").text)
    shown = [li.text for li in browser.find_elements(By.CSS_SELECTOR, "#k-words li")]
    assert shown == words, browser.find_element(By.ID, "k-said").text


@pytest.fixture
def reachable(funded):
    """@name with its messaging key on the chain (what sign-up's setUp publishes; the book takes only those), then a
    second account at the keyboard."""
    browser, base, state, node, first = funded
    done = browser.execute_async_script("""
        const done = arguments[1];
        (async () => {
          try {
            const mail = await import("/messaging.js");
            await window.w.claim(window.wallet);
            await mail.announce(window.wallet, await mail.identity(window.wallet.phrase), arguments[0]);
            done("");
          } catch (e) { done(String(e.message || e)); }
        })();""", first["tag"])
    assert done == "", done
    node.rpc.call("generate", 1)
    _catch_up(state)
    _scan(state)
    browser.delete_all_cookies()
    browser.get(f"{base}/join")
    browser.execute_script("sessionStorage.clear(); localStorage.clear();")
    assert browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        import("/wallet.js").then((m) => { window.w = m; done(true); }, (e) => done(String(e)));""") is True
    _signed_up(browser, state, f"booker{int(time.time() * 1000) % 100000}")
    _keys(browser, {"network": "regtest", "version": state.messaging.params.pubkeyhash_version})
    return browser, base, state, node, first


def test_a_name_is_added_to_the_book_and_paid_from_it(reachable):
    browser, base, state, node, them = reachable
    browser.get(f"{base}/lite")
    browser.find_element(By.CSS_SELECTOR, '[data-tab="addresses"]').click()
    browser.find_element(By.ID, "b-tag").send_keys("@" + them["tag"])
    browser.find_element(By.CSS_SELECTOR, '#b-add button[type="submit"]').click()
    _wait(browser).until(lambda d: "@" + them["tag"] in d.find_element(By.ID, "b-people").text
                         or d.find_element(By.ID, "b-said").text)
    assert "@" + them["tag"] in browser.find_element(By.ID, "b-people").text, browser.find_element(By.ID, "b-said").text
    assert them["address"] in browser.find_element(By.ID, "b-people").text
    pay = next(b for b in browser.find_elements(By.CSS_SELECTOR, "#b-people button") if b.text == "Pay")
    pay.click()
    assert browser.find_element(By.ID, "w-to").get_attribute("value") == "@" + them["tag"]
    assert browser.find_element(By.ID, "w-amount").is_displayed(), "the Send tab, ready for the amount"


def test_the_operator_changes_seats_and_sees_the_modules_in_options(browser, served):
    base, state, node = served
    browser.delete_all_cookies()
    browser.get(f"{base}/lite")
    before = state.accounts().seats
    browser.find_element(By.XPATH, "//nav//button[text()='Settings']").click()
    browser.find_element(By.CSS_SELECTOR, '[data-options="general"]').click()
    _wait(browser).until(lambda d: d.find_element(By.ID, "o-seats").get_attribute("value"))
    assert browser.find_elements(By.CSS_SELECTOR, "#o-face option")
    seats = browser.find_element(By.ID, "o-seats")
    seats.clear()
    seats.send_keys(str(before + 3))
    browser.find_element(By.ID, "o-seats-go").click()
    try:
        _wait(browser).until(lambda d: d.find_element(By.ID, "o-said").text == "Saved.")
        assert state.accounts().seats == before + 3
        browser.find_element(By.CSS_SELECTOR, '[data-otab="modules"]').click()
        _wait(browser).until(lambda d: "Feed" in d.find_element(By.ID, "o-modules").text)
    finally:
        state.set_setting("seats", before)
        state.accounts().seats = before
