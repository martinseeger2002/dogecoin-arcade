"""An account reading its own messages, with a key the node has not got.

The node scans the chain and keeps candidate payloads; it cannot tell
which are whose, so it hands them over and the browser finds out by
trying. What is tested here is that the trying works, that what is not
yours stays shut, and that the node learns nothing by handing it over.
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

from arcade import seed                                          # noqa: E402
from arcade.messaging.envelope import (                          # noqa: E402
    Header, TYPE_SINGLE, seal_message,
)
from arcade.messaging.keys import Identity                       # noqa: E402


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
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    state = AppState(
        home=tmp_path_factory.mktemp("inbox"),
        messaging=ChainContext(network="regtest", role="messaging",
                               label="Testnet",
                               datadir=pathlib.Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=pathlib.Path("/nonexistent")))
    port = _free_port()
    config = uvicorn.Config(create_app(state), host="127.0.0.1", port=port,
                            log_level="error", timeout_graceful_shutdown=1.0)
    server = uvicorn.Server(config)
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
    assert not thread.is_alive(), "the server is still serving a keep-alive connection"


def a_message(state, to_public, body, txid, height=100, sender=None):
    """One message on the chain, as the scanner would have filed it.

    `seal_message`, not `seal_ciphertext`: what the scanner stores is the
    CLEARTEXT HEADER followed by the ciphertext, and the header is what
    carries `clen` -- the length that tells a reader how much of a Class B
    payload is real and how much is padding. These tests used the bare
    ciphertext at first and passed, against a shape that never occurs on a
    chain.
    """
    them = sender or Identity.generate()
    blob = seal_message(them, to_public, Header(type=TYPE_SINGLE), body)
    with state.store() as store:
        store.add_candidate(txid, height, 0, height * 10, "nThem", blob,
                            TYPE_SINGLE, None, None)
    return them


@pytest.fixture
def signed_in(browser, served):
    """A fresh page, an account with a seat, and its twelve words."""
    from nacl.signing import SigningKey

    from arcade import accounts

    base, state = served
    browser.get(f"{base}/join")
    browser.set_script_timeout(120)
    assert browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        import("/messaging.js").then((m) => { window.m = m; done(true); },
                                     (e) => done(String(e)));""") is True

    phrase = seed.generate()
    key = SigningKey(seed.login_key(phrase))
    challenge = browser.execute_async_script("""
        const done = arguments[0];
        fetch('/auth/challenge').then(r => r.json()).then(done);""")
    signature = key.sign(accounts.login_message(
        challenge["origin"], challenge["nonce"])).signature
    opened = browser.execute_async_script("""
        const done = arguments[2];
        fetch('/auth/login', {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({pubkey: arguments[0].pubkey,
                                nonce: arguments[0].nonce,
                                signature: arguments[0].signature,
                                join: true})})
          .then(r => r.json()).then(done);""",
        {"pubkey": key.verify_key.encode().hex(),
         "nonce": challenge["nonce"], "signature": signature.hex()}, None)
    assert opened.get("pubkey"), opened
    # A fresh IndexedDB for each test, or one test's inbox is the next's.
    browser.execute_async_script("""
        const done = arguments[0];
        const kill = indexedDB.deleteDatabase("arcade-messages");
        kill.onsuccess = kill.onerror = kill.onblocked = () => done(true);""")
    return browser, base, state, phrase


def _collect(browser, phrase):
    return browser.execute_async_script("""
        const done = arguments[1];
        (async () => {
          try {
            const me = await window.m.identity(arguments[0]);
            const out = await window.m.collect(me);
            done({...out, inbox: await window.m.inbox()});
          } catch (e) { done({error: String(e && e.message || e)}); }
        })();""", phrase)


def test_an_account_opens_its_own_messages(signed_in):
    browser, base, state, phrase = signed_in
    me = Identity.from_secret_bytes(seed.messaging_key(phrase))
    them = a_message(state, me.public_bytes, b"hello from the chain", "aa" * 32)

    got = _collect(browser, phrase)
    assert "error" not in got, got
    assert got["opened"] == 1, got
    assert len(got["inbox"]) == 1
    only = got["inbox"][0]
    assert bytes.fromhex(only["body"]).endswith(b"hello from the chain")
    assert only["sender"] == them.public_bytes.hex(), "and who wrote it"
    assert only["txid"] == "aa" * 32


def test_what_is_not_yours_stays_shut(signed_in):
    """Most of what the node hands over is addressed to other people."""
    browser, base, state, phrase = signed_in
    me = Identity.from_secret_bytes(seed.messaging_key(phrase))
    a_message(state, Identity.generate().public_bytes, b"not for you", "bb" * 32)
    a_message(state, Identity.generate().public_bytes, b"nor this", "cc" * 32)
    a_message(state, me.public_bytes, b"but this one is", "dd" * 32)

    got = _collect(browser, phrase)
    assert "error" not in got, got
    # `looked` counts everything the node has, including candidates left by
    # other tests in this module -- which is the point: a browser looks at
    # every message on the chain and opens the ones that are its own.
    assert got["looked"] >= 3
    assert got["opened"] == 1, "exactly one of them was ours"
    assert len(got["inbox"]) == 1
    assert bytes.fromhex(got["inbox"][0]["body"]).endswith(b"but this one is")


def test_it_asks_only_for_what_it_has_not_seen(signed_in):
    """The cursor is kept in the browser: a node that remembered which
    messages an account had read would know which messages are theirs."""
    browser, base, state, phrase = signed_in
    me = Identity.from_secret_bytes(seed.messaging_key(phrase))
    a_message(state, me.public_bytes, b"the first", "11" * 32)

    # The first pass reads everything on the chain, whatever is there.
    first = _collect(browser, phrase)
    assert first["opened"] == 1

    again = _collect(browser, phrase)
    assert again["looked"] == 0, "nothing new, nothing fetched"
    assert len(again["inbox"]) == 1, "and the first is still there"

    a_message(state, me.public_bytes, b"the second", "22" * 32)
    third = _collect(browser, phrase)
    assert third["looked"] == 1, "only the new one"
    assert third["opened"] == 1
    assert len(third["inbox"]) == 2


def test_a_stranger_cannot_ask_at_all(served):
    """A seat is needed -- not because the bytes are secret, they are on a
    public chain, but because this is not an open firehose."""
    import httpx

    base, state = served
    a_message(state, Identity.generate().public_bytes, b"x", "ee" * 32)
    answer = httpx.get(f"{base}/account/messages")
    assert answer.status_code == 403


def test_the_node_cannot_open_what_it_hands_over(signed_in):
    """The claim underneath all of this. The node has the bytes and no key,
    and its own scanner marks nothing as opened."""
    browser, base, state, phrase = signed_in
    me = Identity.from_secret_bytes(seed.messaging_key(phrase))
    a_message(state, me.public_bytes, b"private", "ff" * 32)
    _collect(browser, phrase)

    with state.store() as store:
        rows = store.candidates_for_others(after=0, limit=10)
        assert any(r["txid"] == "ff" * 32 for r in rows)
        unopened = store.unopened_candidates()
    assert any(r["txid"] == "ff" * 32 for r in unopened), \
        "the node never opened it, because it cannot"
    assert state.identity is None, "and it has no identity of its own here"
