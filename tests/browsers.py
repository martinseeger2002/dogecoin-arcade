"""Launching headless Firefox, in one place.

Three files opened their own browser and only one of them honoured the
overrides, so on a machine with a snap-packaged Firefox the approvals tests ran
and the showcase and sandbox tests skipped -- 20 tests quietly not running on
the machine that was meant to be the second opinion. The lookup belongs in one
function, and every file that wants a browser calls it.
"""
import os

import pytest
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service as FirefoxService

#: The message a skip carries. It names the way past the usual cause, because
#: the usual cause is packaging rather than a missing browser: /snap/bin/firefox
#: is a symlink to /usr/bin/snap, and geckodriver rejects it with "binary is not
#: a Firefox executable" before it launches anything.
HOW = ("If Firefox is installed somewhere the default lookup misses, point "
       "ARCADE_GECKODRIVER at the driver -- on a snap install that is "
       "/snap/bin/firefox.geckodriver and is enough on its own. "
       "ARCADE_FIREFOX_BINARY overrides the browser too, if the driver cannot "
       "find it.")


def launch(*arguments: str) -> webdriver.Firefox:
    """A headless browser, or a skip that says how to get one.

    Try ARCADE_GECKODRIVER alone first. On a snap install the driver knows where
    its own Firefox lives, so it is the only variable needed; the BOXA confirmed
    that on Ubuntu with ARCADE_GECKODRIVER=/snap/bin/firefox.geckodriver and
    nothing else. ARCADE_FIREFOX_BINARY is for the other shape of odd install,
    where the driver is findable and the browser is not.
    """
    options = Options()
    options.add_argument("-headless")
    for argument in arguments:
        options.add_argument(argument)
    binary = os.environ.get("ARCADE_FIREFOX_BINARY")
    if binary:
        options.binary_location = binary
    driver_path = os.environ.get("ARCADE_GECKODRIVER")
    if not driver_path:
        # A plain Firefox and geckodriver unpacked beside the snap one (2026-10-01: a snap AppArmor update stopped the
        # snap's geckodriver, and every browser test here skipped with "Can not connect to the Service").
        unpacked = os.path.expanduser("~/opt/firefox/geckodriver-wrap")
        if os.access(unpacked, os.X_OK):
            driver_path = unpacked
    service = FirefoxService(executable_path=driver_path) if driver_path else None
    try:
        return webdriver.Firefox(options=options, service=service)
    except Exception as exc:                      # no firefox, no geckodriver
        pytest.skip(f"no usable browser: {exc}. {HOW}")


def allow_wallet(driver, base: str, *pages: str) -> None:
    """Turn wallet access on for these game pages in this browser, as a player does on the game's card
    (base.html, WALLET ACCESS, PER GAME, 2026-10-18): kept in the node's own localStorage, per page."""
    driver.get(f"{base}/r/realtime.js")
    driver.execute_script("for (const i of arguments[0]) localStorage.setItem('arcade.wallet.allow:' + i, 'yes');",
                          list(pages))
