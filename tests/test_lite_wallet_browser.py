"""Lite's own Send, Receive and Transactions tabs, in a real browser on regtest: an account's coins sent from the tab
(opened and signed in the browser by wallet.js), then the node's own (signed by the node, held until Send), each
reviewed first and paying only what was typed."""

import pathlib
import sys

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")
from selenium.webdriver.common.by import By                      # noqa: E402
from selenium.webdriver.support.ui import Select, WebDriverWait  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_send_browser import _catch_up, browser, funded, served  # noqa: F401,E402
from test_profile_pay_browser import _paying  # noqa: E402


def _wait(driver, seconds=60):
    return WebDriverWait(driver, seconds, poll_frequency=0.2)


def _send_from_the_tab(browser, base, to, amount):
    browser.get(f"{base}/lite")
    browser.find_element(By.CSS_SELECTOR, '[data-tab="send"]').click()
    _wait(browser).until(lambda d: Select(d.find_element(By.ID, "w-chain-send")).options)
    Select(browser.find_element(By.ID, "w-chain-send")).select_by_value("regtest")
    browser.find_element(By.ID, "w-to").send_keys(to)
    browser.find_element(By.ID, "w-amount").send_keys(amount)
    browser.find_element(By.CSS_SELECTOR, '#w-form button[type="submit"]').click()
    _wait(browser).until(lambda d: d.find_element(By.ID, "w-review").is_displayed()
                         or d.find_element(By.ID, "w-said").text)
    return browser.find_element(By.ID, "w-terms").text


def test_an_account_sends_from_lites_send_tab(funded):
    browser, base, state, node, made = funded
    theirs = node.rpc.call("getnewaddress")
    terms = _send_from_the_tab(browser, base, theirs, "2.5")
    assert theirs in terms and "2.5" in terms and "Leaves your wallet" in terms, terms
    assert _paying(node, theirs) == [], "nothing goes before Send"
    browser.find_element(By.ID, "w-go").click()
    _wait(browser).until(lambda d: d.find_element(By.ID, "w-said").text.startswith("Sent."))
    assert _paying(node, theirs) == [2.5]

    node.rpc.call("generate", 1)
    _catch_up(state)
    browser.find_element(By.CSS_SELECTOR, '[data-tab="transactions"]').click()
    Select(browser.find_element(By.ID, "w-chain-tx")).select_by_value("regtest")
    _wait(browser).until(lambda d: "Sent" in d.find_element(By.ID, "w-history").text)
    browser.find_element(By.CSS_SELECTOR, '[data-tab="receive"]').click()
    assert browser.find_element(By.ID, "w-address").text == made["address"]
    assert browser.find_elements(By.CSS_SELECTOR, "#w-qr svg")


def test_a_typed_amount_the_offer_does_not_match_signs_nothing(funded):
    browser, base, state, node, made = funded
    theirs = node.rpc.call("getnewaddress")
    _send_from_the_tab(browser, base, theirs, "1.5")
    browser.execute_script("document.getElementById('w-amount').value = '9'")   # changed after the review
    browser.find_element(By.CSS_SELECTOR, '#w-form button[type="submit"]').click()
    _wait(browser).until(lambda d: d.find_element(By.ID, "w-review").is_displayed())
    assert "9" in browser.find_element(By.ID, "w-terms").text, "a new review for the new amount, not the old offer"
    browser.find_element(By.ID, "w-cancel").click()
    assert _paying(node, theirs) == []


def test_the_operator_sends_the_nodes_coins_from_the_same_tab(funded):
    browser, base, state, node, made = funded
    browser.delete_all_cookies()
    browser.execute_script("sessionStorage.clear();")
    theirs = node.rpc.call("getnewaddress")
    terms = _send_from_the_tab(browser, base, theirs, "3")
    assert theirs in terms and "Leaves the wallet" in terms, terms
    assert _paying(node, theirs) == []
    browser.find_element(By.ID, "w-go").click()
    _wait(browser).until(lambda d: d.find_element(By.ID, "w-said").text.startswith("Sent."))
    assert _paying(node, theirs) == [3.0]
