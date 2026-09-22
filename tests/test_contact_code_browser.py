"""The contact code the browser builds agrees with the node's own encoder.

`arcade:<network>:<base58 key>:<check>` -- the way two people exchange a
messaging key with nothing on the chain at all (arcade/messaging/contact.py).
An account's identity lives in its browser, so this now has to be built
there too; the two implementations have to agree byte for byte, or a code
one account hands another opens nothing.
"""

import pathlib
import socket
import sys
import threading
import time

import pytest

pytest.importorskip("selenium",
                    reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import browsers                                                  # noqa: E402

from arcade.messaging import contact                              # noqa: E402


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
        home=tmp_path_factory.mktemp("contactcode"),
        messaging=ChainContext(network="regtest", role="messaging",
                               label="Testnet", datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=Path("/nonexistent")),
    )
    port = _free_port()
    config = uvicorn.Config(create_app(state), host="127.0.0.1", port=port,
                            log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("test server did not start")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_the_browser_builds_the_same_code_python_does(browser, served):
    base = served
    browser.get(f"{base}/join")
    browser.set_script_timeout(30)

    public_key = bytes(range(32))          # 00 01 02 ... 1f, deterministic
    made_here = browser.execute_async_script("""
        const done = arguments[1];
        import("/messaging.js").then((m) =>
          m.contactCode("regtest", new Uint8Array(arguments[0])))
          .then(done, (e) => done("error: " + String(e.message || e)));
        """, list(public_key))
    assert not made_here.startswith("error"), made_here

    made_there = contact.encode("regtest", public_key)
    assert made_here == made_there

    # And the node's own decoder accepts what the browser made.
    network, decoded = contact.decode(made_here)
    assert network == "regtest"
    assert decoded == public_key


def test_a_mistyped_code_is_refused_the_same_way(browser, served):
    base = served
    browser.get(f"{base}/join")
    browser.set_script_timeout(30)
    public_key = bytes(range(32, 64))
    code = browser.execute_async_script("""
        const done = arguments[1];
        import("/messaging.js").then((m) =>
          m.contactCode("test", new Uint8Array(arguments[0])))
          .then(done, (e) => done("error: " + String(e.message || e)));
        """, list(public_key))
    mangled = code[:-1] + ("0" if code[-1] != "0" else "1")
    with pytest.raises(contact.ContactError):
        contact.decode(mangled)
