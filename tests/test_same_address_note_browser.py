"""wallet.js sameAddressNote in a real browser (2026-11-09, security review): which receiving address gets "This
address is the same on Dogecoin and DigiByte. Check the chain before sending." and which gets nothing.

The addresses are the public BIP39 test phrase's ("abandon ... about", nobody's account): its m/44'/3'/0'/0/0 is the
published BIP44 Dogecoin vector, and DigiByte Core 9.26.7 derived the very same string from it as a DigiByte address
(test_restore_kit CORE_SAID), which is what the note is about. The views that draw it: test_same_address_note.py."""

import pathlib
import sys

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_coins_browser import browser, served                   # noqa: F401,E402
from test_restore_kit import CORE_SAID                          # noqa: E402

from arcade import script                                        # noqa: E402

NOTE = "This address is the same on Dogecoin and DigiByte. Check the chain before sending."
DOGE_LEGACY = "DBus3bamQjgJULBJtYXpEzDWQRwF5iwxgC"                  # BIP44's Dogecoin vector for the test phrase

ASKED = [
    ("doge", DOGE_LEGACY, NOTE),
    ("dgb", CORE_SAID["dgb"]["pkh"], NOTE),                                         # the same string, said by DigiByte Core
    ("dgb", CORE_SAID["dgb"]["sh"], ""),                                            # P2SH (Nested SegWit): S..., DigiByte's own
    ("dgb", CORE_SAID["dgb"]["wpkh"], ""),                                          # Native SegWit: dgb1...
    ("doge", script.b58check_encode(22, bytes(range(20))), ""),                     # a Dogecoin script hash
    ("dgbtest", script.b58check_encode(126, bytes(range(20))), ""),                 # DigiByte test: its own byte
    ("ltc", CORE_SAID["ltc"]["pkh"], ""),
    ("bel", CORE_SAID["bel"]["sh"], ""),            # Bellscoin's P2SH is byte 30 too, but no chain shares its key
    ("main", DOGE_LEGACY, ""),                      # Pepecoin: not a Dogecoin/DigiByte receive view
    ("doge", "", ""),
    ("doge", "not an address", ""),
]


def test_only_a_legacy_dogecoin_or_digibyte_address_gets_the_note(browser, served):  # noqa: F811
    assert CORE_SAID["dgb"]["pkh"] == DOGE_LEGACY, "DigiByte Core made the Dogecoin vector's string"
    base, _ = served
    browser.get(f"{base}/join")
    browser.set_script_timeout(60)
    got = browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        import("/wallet.js").then((w) => done(arguments[0].map(([net, a]) => w.sameAddressNote(net, a))),
                                  (e) => done(String(e)));
        """, [[net, address] for net, address, _ in ASKED])
    assert got == [want for _, _, want in ASKED], list(zip(ASKED, got))
