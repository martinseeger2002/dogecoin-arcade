"""Imported wallets across a password change (plan item 18): the Restore page seals the main wallet under a new
password; the imported wallets' record, sealed under the old one, is then said to be sealed under an earlier
password (never silently empty), opens with that earlier password, and is sealed under the new one at its next change.
"""

import hashlib
import pathlib
import socket
import sys
import threading
import time

import pytest

pytest.importorskip("selenium", reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                  # noqa: E402

from arcade import script                                        # noqa: E402

TAG = "restoretester"
OLD = "the first password of this test account"
NEW = "the second password of this test account"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def browser():
    driver = browsers.launch()
    yield driver
    driver.quit()


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    import uvicorn
    from pathlib import Path
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    state = AppState(
        home=tmp_path_factory.mktemp("restore"),
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=Path("/nonexistent")),
    )
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1", port=port, log_level="error",
                                           timeout_graceful_shutdown=1.0))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("test server did not start")
    yield f"http://127.0.0.1:{port}", state
    server.should_exit = True
    thread.join(timeout=5)


def _wif(seed: bytes) -> str:
    return script.b58check_encode(0xf1, hashlib.sha256(seed).digest() + b"\x01")


FLOW = """
const done = arguments[arguments.length - 1];
const [tag, oldPw, newPw, first, second] = arguments;
const CHAIN = {network: "regtest", version: 111};
const out = {};
import("/wallet.js").then(async (w) => {
  try {
    await w.signUp(tag, oldPw, CHAIN);
    let held = await w.opened(CHAIN);
    const phrase = held.phrase;
    // An import with no password: asked for, nothing registered.
    try { await w.importWallet(CHAIN, {kind: "wif", secret: first}); out.noPassword = "imported";
    } catch (e) { out.noPassword = !!e.needsPassword; }
    out.nothingYet = (await w.importedOnNode()).chains.regtest.length;
    // A wrong password: refused, nothing registered.
    try { await w.importWallet(CHAIN, {kind: "wif", secret: first, password: "not it"}); out.wrong = "imported";
    } catch (e) { out.wrong = String(e.message); }
    const one = await w.importWallet(CHAIN, {kind: "wif", secret: first, name: "First", password: oldPw});
    out.first = one.wallet.name;
    // The Restore page: the same words, a new password.
    await w.restore(phrase, newPw, CHAIN);
    w.forgetOpen();
    await w.signIn(tag, newPw, CHAIN);
    held = await w.opened(CHAIN);
    out.stale = !!(w.importsOpen(held.pubkey) || {}).stale;
    try { await w.openImports(held, newPw); out.openNew = "opened"; } catch (e) { out.openNew = !!e.stale; }
    const back = await w.openStaleImports(held, oldPw);
    out.back = back.map((x) => x.name);
    // The next change seals them under the new password.
    const two = await w.importWallet(CHAIN, {kind: "wif", secret: second, password: newPw});
    out.second = two.wallet.name;
    w.forgetOpen();
    await w.signIn(tag, newPw, CHAIN);
    held = await w.opened(CHAIN);
    const now = w.importsOpen(held.pubkey);
    out.after = {stale: !!now.stale, names: now.wallets.map((x) => x.name)};
    done({ok: true, out});
  } catch (e) { done({ok: false, error: String(e.message || e), out}); }
});
"""


def test_a_new_password_keeps_the_imported_wallets(browser, served):
    base, state = served
    browser.get(f"{base}/join")
    browser.delete_all_cookies()
    browser.set_script_timeout(240)
    said = browser.execute_async_script(FLOW, TAG, OLD, NEW, _wif(b"one"), _wif(b"two"))
    assert said["ok"], said
    out = said["out"]
    assert out["noPassword"] is True and out["nothingYet"] == 0
    assert "not your password" in out["wrong"]
    assert out["first"] == "First"
    assert out["stale"] is True and out["openNew"] is True
    assert out["back"] == ["First"]
    assert out["second"] == "Wallet 1"
    assert out["after"] == {"stale": False, "names": ["First", "Wallet 1"]}
