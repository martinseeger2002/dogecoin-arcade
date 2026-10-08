"""One sign-in card and one password sheet at a time (2026-10-08: "Sometimes when I
open a game in Dogecoin arcade, I have to login two or three times").

A game asks for a seed, a take and a key as it starts. Each caller that found no open
wallet used to draw its own card or sheet on top of the last; now the ones that arrive
while one is open wait for it and share its answer. The node and helpers are
`test_page_doors_browser.py`'s.
"""

import pathlib
import sys

import pytest

pytest.importorskip("selenium",
                    reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_page_doors_browser import browser, served  # noqa: E402,F401


def test_three_key_requests_at_once_open_one_password_sheet(browser, served):
    base, _, _ = served
    browser.get(f"{base}/")
    got = browser.execute_async_script("""
        const done = arguments[0];
        ARCADE_ME = 'someone';                      // signed in, wallet not open
        let sheets = 0;
        arcadeUnlockSheet = () => { sheets++; return new Promise((r) => setTimeout(() => r({opened: true}), 300)); };
        Promise.all([arcadeKeys(null, 'a'), arcadeKeys(null, 'b'), arcadeKeys(null, 'c')])
          .then((keys) => done({sheets, same: keys.every((k) => k && k.opened)}),
                (e) => done({error: String(e.message || e)}));""")
    assert got.get("error") is None, got
    assert got == {"sheets": 1, "same": True}


def test_a_closed_sheet_asks_again_the_next_time(browser, served):
    base, _, _ = served
    browser.get(f"{base}/")
    got = browser.execute_async_script("""
        const done = arguments[0];
        ARCADE_ME = 'someone';
        let sheets = 0;
        arcadeUnlockSheet = () => { sheets++; return Promise.resolve(null); };   // closed without a password
        const fail = (p) => p.then(() => 'opened', (e) => String(e.message || e));
        Promise.all([fail(arcadeKeys(null, 'a')), fail(arcadeKeys(null, 'b'))])
          .then((first) => fail(arcadeKeys(null, 'c')).then((second) => done({sheets, first, second})));""")
    assert got["sheets"] == 2, got                  # one for the pair, one for the later ask
    assert all("Unlock your wallet" in m for m in got["first"] + [got["second"]]), got


def test_three_requests_signed_out_show_one_sign_in_card(browser, served):
    base, _, _ = served
    browser.get(f"{base}/")
    got = browser.execute_async_script("""
        const done = arguments[0];
        ARCADE_ME = '';
        let cards = 0;
        arcadeAsk = () => { cards++; return new Promise((r) => setTimeout(() => r(false), 300)); };
        Promise.all([arcadeSignIn('a'), arcadeSignIn('b'), arcadeSignIn('c')])
          .then(() => done({cards}), (e) => done({error: String(e.message || e)}));""")
    assert got == {"cards": 1}, got
