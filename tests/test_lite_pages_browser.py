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
    second = _signed_up(browser, state, f"booker{int(time.time() * 1000) % 100000}")
    node.rpc.call("sendtoaddress", second["address"], 50.0)        # a message is a transaction: coins to send it
    node.rpc.call("generate", 1)
    _catch_up(state)
    _keys(browser, {"network": "regtest", "version": state.messaging.params.pubkeyhash_version})
    return browser, base, state, node, first


def test_join_keys_makes_the_node_yours_and_signs_in_by_the_words(browser, lite):
    """The full /join/keys's doors in Lite's Join panel: the operator's username and password set at the machine (the
    node stays its own operator, so it keeps its name: state.operator_tag), then an account in by its twelve words."""
    base, state, node = lite
    browser.delete_all_cookies()
    browser.get(f"{base}/join/keys")
    browser.execute_script("sessionStorage.clear(); localStorage.clear();")
    browser.get(f"{base}/join/keys")
    _wait(browser).until(lambda d: d.find_element(By.ID, "j-op-set").is_displayed())
    assert not browser.find_elements(By.ID, "password-save"), "the window's panel, not the full page"
    browser.find_element(By.ID, "j-set-name").send_keys("pumpkin")
    for box in ("j-set-pw", "j-set-pw2"):
        browser.find_element(By.ID, box).send_keys("a long enough password")
    browser.find_element(By.CSS_SELECTOR, '#j-set-form button[type="submit"]').click()
    _wait(browser).until(lambda d: state.operator)
    assert state.credentials().anybody() and not state.setting(f"address:{state.operator}")

    browser.delete_all_cookies()
    browser.get(f"{base}/join/keys")
    _wait(browser).until(lambda d: d.find_element(By.ID, "j-op-in").is_displayed())
    assert not browser.find_element(By.ID, "j-op-set").is_displayed(), "set once, from the machine"
    words = browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        import("/signin.js").then((s) => s.generate()).then(done, (e) => done(String(e)));""")
    assert len(words.split()) == 12, words
    browser.find_element(By.ID, "j-lost").click()
    browser.find_element(By.ID, "j-phrase").send_keys(words)
    browser.find_element(By.CSS_SELECTOR, '#j-words-in button[type="submit"]').click()
    _wait(browser).until(lambda d: not d.current_url.endswith("/join/keys"))
    who = browser.execute_async_script("""
        const done = arguments[0];
        fetch("/auth/who").then((r) => r.json()).then(done, () => done({}));""")
    assert who.get("pubkey") and who["pubkey"] != state.operator, who


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
        cats = [b.text.split()[0] for b in browser.find_elements(By.CSS_SELECTOR, "#o-mui .mui-tabs button")]
        assert cats == ["All", "Core", "Apps", "Node", "Themes"], cats
        browser.find_element(By.CSS_SELECTOR, '#o-mui [data-cat="app"]').click()
        listed = browser.find_element(By.CSS_SELECTOR, "#o-mui tbody").text
        assert "Feed" in listed and "Address book" not in listed, "Apps lists the apps only"
        box = browser.find_element(By.CSS_SELECTOR, "#o-mui input[type=search]")
        box.send_keys("feed")
        assert [r.text.split("\n")[0] for r in browser.find_elements(By.CSS_SELECTOR, "#o-mui tbody tr")] \
            and "Messages" not in browser.find_element(By.CSS_SELECTOR, "#o-mui tbody").text
    finally:
        state.set_setting("seats", before)
        state.accounts().seats = before


def _letters_to(node, state):
    """Messages waiting in the mempool: a send from the tab or the console is one transaction there."""
    return len(node.rpc.call("getrawmempool"))


def test_an_account_writes_from_the_messages_tab_and_from_its_console(reachable):
    browser, base, state, node, them = reachable
    browser.get(f"{base}/lite#messages")
    _wait(browser).until(lambda d: d.find_element(By.ID, "m-main").is_displayed())
    browser.find_element(By.ID, "m-to").send_keys("@" + them["tag"])
    browser.find_element(By.CSS_SELECTOR, '#m-new button[type="submit"]').click()
    _wait(browser).until(lambda d: d.find_element(By.ID, "m-write").is_displayed())
    before = _letters_to(node, state)
    browser.find_element(By.ID, "m-text").send_keys("hello from the Lite window")
    browser.find_element(By.CSS_SELECTOR, '#m-write button[type="submit"]').click()
    _wait(browser).until(lambda d: d.find_element(By.ID, "m-said").text in ("Sent.",) or "bad" in
                         d.find_element(By.ID, "m-said").get_attribute("class"))
    assert browser.find_element(By.ID, "m-said").text == "Sent.", browser.find_element(By.ID, "m-said").text
    assert _letters_to(node, state) > before, "the letter went out as a transaction"
    _wait(browser).until(lambda d: "hello from the Lite window" in d.find_element(By.ID, "m-letters").text)

    # The console, as the account: its own messages, read and written in this browser.
    browser.execute_script("""
        const line = document.getElementById('line');
        for (const text of ['listconversations', 'sendmessage @' + arguments[0] + ' and from its console']) {
          line.value = text;
          line.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
        }""", them["tag"])
    _wait(browser).until(lambda d: '"sent": true' in d.find_element(By.ID, "out").get_attribute("textContent"))
    said = browser.find_element(By.ID, "out").get_attribute("textContent")
    assert "@" + them["tag"] in said and "open only in your own browser" not in said, said


def test_the_operator_writes_the_nodes_messages_from_the_same_tab(reachable):
    browser, base, state, node, them = reachable
    state.ensure_identity()                                # the node's letters are paid from its identity's coins
    node.rpc.call("sendtoaddress", state.derived_address, 50)
    node.rpc.call("generate", 1)
    _catch_up(state)
    browser.delete_all_cookies()
    browser.execute_script("sessionStorage.clear();")
    browser.get(f"{base}/lite#messages")
    _wait(browser).until(lambda d: d.find_element(By.ID, "m-main").is_displayed())
    browser.find_element(By.ID, "m-to").send_keys("@" + them["tag"])
    browser.find_element(By.CSS_SELECTOR, '#m-new button[type="submit"]').click()
    _wait(browser).until(lambda d: d.find_element(By.ID, "m-write").is_displayed())
    browser.find_element(By.ID, "m-text").send_keys("from the node")
    browser.find_element(By.CSS_SELECTOR, '#m-write button[type="submit"]').click()
    _wait(browser).until(lambda d: d.find_element(By.ID, "m-said").text)
    assert browser.find_element(By.ID, "m-said").text == "Sent.", browser.find_element(By.ID, "m-said").text
    with state.store() as store:
        assert store.conn.execute("SELECT COUNT(*) FROM sent").fetchone()[0] >= 1, "kept as the node's own copy"
