"""The lock script built in a real browser (coins.js htlcRedeem/htlcParse/htlcAddress) is
byte for byte the one arcade/htlc.py builds. A tab locks its own coins with these bytes,
so if the two disagreed a trade would fail -- or worse, lock to a script nobody can spend.
"""

import pathlib
import secrets
import socket
import sys
import threading
import time

import pytest

pytest.importorskip("playwright.sync_api", reason="needs playwright")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from arcade import htlc as H                                           # noqa: E402
from arcade.config import NETWORKS                                     # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    import uvicorn
    from playwright.sync_api import sync_playwright
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext
    state = AppState(home=tmp_path_factory.mktemp("htlcjs"),
                     messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                                            datadir=pathlib.Path("/nonexistent")),
                     ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                                         datadir=pathlib.Path("/nonexistent")))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1", port=port,
                                           log_level="error", timeout_graceful_shutdown=1.0))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        tab = browser.new_page()
        tab.goto(f"http://127.0.0.1:{port}/join")
        tab.evaluate("async () => { window.coins = await import('/coins.js'); }")
        yield tab
        browser.close()
    server.should_exit = True
    thread.join(timeout=5)


def _key() -> bytes:
    return bytes([2 + secrets.randbelow(2)]) + secrets.token_bytes(32)


@pytest.mark.parametrize("locktime", [500_000_000, 1_800_000_000, 2 ** 31 - 1, 2 ** 31, 2 ** 32 - 1])
def test_the_browser_writes_the_same_lock(page, locktime):
    _, h = H.make_secret()
    claimer, refunder = _key(), _key()
    python = H.redeem_script(h, claimer, refunder, locktime)
    js = page.evaluate("""([h, c, r, t]) => coins.hex(coins.htlcRedeem(coins.unhex(h), coins.unhex(c),
                                                                   coins.unhex(r), t))""",
                       [h.hex(), claimer.hex(), refunder.hex(), locktime])
    assert js == python.hex()
    said = page.evaluate("(r) => { const p = coins.htlcParse(r); return p && "
                         "[coins.hex(p.secretHash), coins.hex(p.claimer), p.locktime, coins.hex(p.refunder)]; }",
                         python.hex())
    assert said == [h.hex(), claimer.hex(), locktime, refunder.hex()]


def test_the_browser_refuses_what_is_not_a_lock(page):
    _, h = H.make_secret()
    good = H.redeem_script(h, _key(), _key(), 1_800_000_000).hex()
    for bad in (good[:-2], "00" + good, "64" + good[2:], good[:-4] + "ac67"):   # cut, prefixed, wrong opcodes
        assert page.evaluate("(r) => coins.htlcParse(r)", bad) is None
    with pytest.raises(Exception):
        page.evaluate("([h, c]) => coins.htlcRedeem(coins.unhex(h), coins.unhex(c), coins.unhex(c), 1800000000)",
                      [h.hex(), _key().hex()])
    with pytest.raises(Exception):
        page.evaluate("([h, c, r]) => coins.htlcRedeem(coins.unhex(h), coins.unhex(c), coins.unhex(r), 1000)",
                      [h.hex(), _key().hex(), _key().hex()])


def test_the_browser_finds_the_same_lock_address(page):
    _, h = H.make_secret()
    redeem = H.redeem_script(h, _key(), _key(), 1_800_000_000)
    for network in ("test", "main", "doge"):
        params = NETWORKS[network]
        js = page.evaluate("async ([r, v]) => coins.htlcAddress(coins.unhex(r), v)",
                           [redeem.hex(), params.scripthash_version])
        assert js == H.address_of(redeem, params), network
