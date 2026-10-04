"""A game's own play without a card, on a test chain (2026-10-04, the operator: "If
it's test net, all signing can be done automatically"; narrowed the same day so a
stranger's page cannot empty a wallet).

What goes through unasked where `page_autosign` is on: a page inscribing its own data,
and a claim from a refereed pool bound to that page -- each under a small cost, and
under a per-page budget. Everything else keeps its card. The node and helpers are
`test_inscribe_bridge_browser.py`'s.
"""

import pathlib
import sys
import time

import pytest

pytest.importorskip("selenium",
                    reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_page_doors_browser import (browser, served, _click, _landed,  # noqa: E402,F401
                                     _load_libs, _opened, _settled, PASSWORD)
from test_inscribe_bridge_browser import _inscribe_here, _yes, JUDGE, _page  # noqa: E402
from selenium.webdriver.common.by import By                           # noqa: E402

PAGE = "a" * 64


def _shown(leaving_sats: int, fee: int = 100000) -> str:
    return "{pays: [{mine: false, value: %d}, {mine: true, value: 5}], fee: %d}" % (
        leaving_sats - fee, fee)


def test_the_policy_is_on_only_for_the_test_chain_and_says_no_past_its_limits(browser, served):
    base, state, _ = served
    browser.get(f"{base}/")
    assert browser.execute_script("return ARCADE_AUTOSIGN") is False, "regtest asks, by default"
    state.set_setting("page_autosign", True)
    try:
        browser.get(f"{base}/")
        js = browser.execute_script
        assert js("return ARCADE_AUTOSIGN") is True
        # a claim: a lot's price and fee, never more than AUTO_CLAIM_MOST
        assert js(f"return arcadeAutoSigns('{PAGE}', {_shown(1_100_000)}, AUTO_CLAIM_MOST)") is True
        assert js(f"return arcadeAutoSigns('{PAGE}', {_shown(6_000_000)}, AUTO_CLAIM_MOST)") is False
        # no page, no yes
        assert js(f"return arcadeAutoSigns('', {_shown(1000)}, AUTO_CLAIM_MOST)") is False
        # a page's budget runs out, and then the cards come back -- for that page only
        n = js(f"let n = 0; while (arcadeAutoSigns('{PAGE}', {_shown(150_000_000)}, AUTO_INSCRIBE_MOST)) n++; return n")
        assert 10 <= n <= 14, n
        assert js(f"return arcadeAutoSigns('{'b' * 64}', {_shown(150_000_000)}, AUTO_INSCRIBE_MOST)") is True
    finally:
        state.set_setting("page_autosign", None)


def test_a_game_saves_without_a_card_where_autosign_is_on(browser, served):
    base, state, daemon = served
    _load_libs(browser, base)
    made = browser.execute_async_script("""
        const done = arguments[3];
        window.w.signUp(arguments[0], arguments[1], {network: "regtest", version: arguments[2]})
          .then((r) => done({tag: r.tag, address: r.address}),
                (e) => done({error: String(e.message || e)}));""",
        "autosaver", PASSWORD, state.messaging.params.pubkeyhash_version)
    assert "error" not in made, made
    daemon.rpc.call("sendtoaddress", made["address"], 5.0)
    _opened(state, daemon)
    _load_libs(browser, base)
    judge = _inscribe_here(browser, state, JUDGE.encode(), "text/javascript", "Race judge")
    _landed(state, daemon, judge)
    _load_libs(browser, base)
    page = _inscribe_here(browser, state, _page(judge), "text/html", "Race")
    _landed(state, daemon, page)
    browser.get(f"{base}/inscriptions/{page}/view")
    time.sleep(4)
    _opened(state, daemon)

    state.set_setting("page_autosign", True)
    try:
        browser.get(f"{base}/inscriptions/{page}/view")
        frame = browser.find_elements(By.CSS_SELECTOR, "iframe.inscription-frame")[0]
        _click(browser, frame, "p")
        plain = _settled(browser, frame, "plain")
        assert plain.get("ok") is True and len(plain.get("txid", "")) == 64, plain
        assert not browser.find_elements(By.CSS_SELECTOR, ".askcard"), "no card was shown"
    finally:
        state.set_setting("page_autosign", None)
    # with it off, the same save asks again
    browser.get(f"{base}/inscriptions/{page}/view")
    frame = browser.find_elements(By.CSS_SELECTOR, "iframe.inscription-frame")[0]
    _click(browser, frame, "p")
    _yes(browser)
    again = _settled(browser, frame, "plain")
    assert again.get("ok") is True, again
