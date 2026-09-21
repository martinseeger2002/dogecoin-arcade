"""The messaging envelope, opened and sealed in a browser.

Two implementations of one format, and the only honest check is that each
can read what the other wrote. A sealed box whose nonce is derived
differently opens for nobody, silently, and looks exactly like a wrong
key -- so nothing here tests the browser against itself.
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

import nacl.public                                               # noqa: E402

from arcade import seed                                          # noqa: E402
from arcade.messaging.keys import Identity, fingerprint_of       # noqa: E402


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
        home=tmp_path_factory.mktemp("msg"),
        messaging=ChainContext(network="regtest", role="messaging",
                               label="Testnet",
                               datadir=pathlib.Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=pathlib.Path("/nonexistent")))
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
    yield f"http://127.0.0.1:{port}", state
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def loaded(browser, served):
    base, state = served
    browser.get(f"{base}/join")
    browser.set_script_timeout(90)
    ready = browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        import("/messaging.js").then((m) => { window.m = m; done(true); },
                                     (e) => done(String(e)));""")
    assert ready is True, ready
    return browser, base, state


# --- the identity ------------------------------------------------------------

def test_the_same_words_give_the_same_identity_on_both_sides(loaded):
    """The node derives it in Python today; the browser must land on the
    same key, or a wallet restored in a browser is a different person."""
    browser, base, state = loaded
    phrase = seed.generate()
    said = browser.execute_async_script("""
        const done = arguments[1];
        window.m.identity(arguments[0]).then(
          (id) => done({pub: window.m.hex(id.publicKey),
                        fingerprint: id.fingerprint}),
          (e) => done({error: String(e.message || e)}));""", phrase)
    assert "error" not in said, said

    mine = Identity.from_secret_bytes(seed.messaging_key(phrase))
    assert said["pub"] == mine.public_bytes.hex()
    assert said["fingerprint"] == fingerprint_of(mine.public_bytes)


def test_it_is_not_the_key_that_spends(loaded):
    browser, base, state = loaded
    phrase = seed.generate()
    said = browser.execute_async_script("""
        const done = arguments[1];
        (async () => {
          const signer = await import("/signin.js");
          const coins = await import("/coins.js");
          const id = await window.m.identity(arguments[0]);
          const s = await signer.toSeed(arguments[0]);
          const coin = await coins.coinKey(s, "regtest", 0);
          done({identity: window.m.hex(id.publicKey),
                coin: window.m.hex(coin.pubkey),
                login: (await signer.accountKey(s)).pubkey});
        })();""", phrase)
    assert said["identity"] != said["coin"] != said["login"]
    assert said["identity"] != said["login"]


# --- each side reading what the other wrote -----------------------------------

def test_the_browser_opens_what_python_sealed(loaded):
    """The whole of it: a sealed box made by PyNaCl, opened by tweetnacl
    and a vendored Blake2b, with the nonce derived the way libsodium does."""
    browser, base, state = loaded
    phrase = seed.generate()
    me = Identity.from_secret_bytes(seed.messaging_key(phrase))
    secret = b"the exact bytes, and nothing about them guessed"
    sealed = nacl.public.SealedBox(me.public).encrypt(secret)

    said = browser.execute_async_script("""
        const done = arguments[2];
        (async () => {
          const id = await window.m.identity(arguments[0]);
          const out = window.m.openSealedBox(window.m.unhex(arguments[1]), id);
          done(out === null ? {error: "it did not open"}
                            : {plain: window.m.hex(out)});
        })();""", phrase, sealed.hex())
    assert "error" not in said, said
    assert bytes.fromhex(said["plain"]) == secret


def test_python_opens_what_the_browser_sealed(loaded):
    browser, base, state = loaded
    phrase = seed.generate()
    me = Identity.from_secret_bytes(seed.messaging_key(phrase))
    message = "and back the other way, with a £ and an emoji 🎮"

    # Everything is built with the MODULE's own helpers, not with the
    # sandbox's. Selenium runs injected script in a different JavaScript
    # realm from the page, so `new TextEncoder()` here produces a
    # Uint8Array from that realm and tweetnacl -- which lives in the
    # page's -- refuses it as "unexpected type, use Uint8Array". The bytes
    # are identical; the constructor is not. Nothing about this is true of
    # a real page, and an hour can go into looking for it in the crypto.
    said = browser.execute_async_script("""
        const done = arguments[2];
        (async () => {
          try {
            const to = window.m.unhex(arguments[0]);
            const plain = window.m.unhex(arguments[1]);
            done({sealed: window.m.hex(window.m.sealedBox(plain, to))});
          } catch (e) { done({error: String(e && e.message || e)}); }
        })();""", me.public_bytes.hex(), message.encode().hex())
    assert "error" not in said, said
    opened = nacl.public.SealedBox(me.secret).decrypt(
        bytes.fromhex(said["sealed"]))
    assert opened.decode() == message


def test_a_sealed_box_for_somebody_else_does_not_open(loaded):
    """It returns nothing rather than raising, and nothing is the answer:
    most of what a scanner hands over is addressed to other people."""
    browser, base, state = loaded
    stranger = Identity.generate()
    sealed = nacl.public.SealedBox(stranger.public).encrypt(b"not for you")
    said = browser.execute_async_script("""
        const done = arguments[2];
        (async () => {
          const id = await window.m.identity(arguments[0]);
          const out = window.m.openSealedBox(window.m.unhex(arguments[1]), id);
          done({opened: out !== null});
        })();""", seed.generate(), sealed.hex())
    assert said["opened"] is False


# --- both layers, which is what is actually on the chain -----------------------

def test_the_browser_opens_a_whole_message_from_the_chain(loaded):
    """`seal_ciphertext` is what the node broadcasts: a sealed box around
    the sender's key and an authenticated box. Opening it has to give back
    both the sender and the plaintext, or a reader cannot tell who wrote
    what."""
    from arcade.messaging.envelope import Header, TYPE_SINGLE, seal_message

    browser, base, state = loaded
    phrase = seed.generate()
    me = Identity.from_secret_bytes(seed.messaging_key(phrase))
    them = Identity.generate()
    header = Header(type=TYPE_SINGLE)
    body = b"a message that went on a chain"
    # The whole payload: cleartext header, then ciphertext. That is what is
    # broadcast and what a scanner stores.
    blob = seal_message(them, me.public_bytes, header, body)

    said = browser.execute_async_script("""
        const done = arguments[2];
        (async () => {
          const id = await window.m.identity(arguments[0]);
          const out = window.m.openMessage(window.m.unhex(arguments[1]), id);
          done(out === null ? {error: "it did not open"}
               : {sender: window.m.hex(out.sender),
                  plain: window.m.hex(out.plain)});
        })();""", phrase, blob.hex())
    assert "error" not in said, said
    assert said["sender"] == them.public_bytes.hex(), "who wrote it"
    plain = bytes.fromhex(said["plain"])
    # `openMessage` checks the bound header against the cleartext one and
    # strips it, so what comes back is the message itself.
    assert plain == body, "and what they wrote"
