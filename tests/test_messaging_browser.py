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


# --- the same format, in pieces and with a file inside it -------------------

def _split(them, recipient_public, message, room):
    """`_plan_chunked`'s framing, spelled out: seal ONCE, then cut the
    ciphertext. The header comes from the production `Header.encode()`, so
    the browser is read against what the node actually writes."""
    from arcade.messaging.envelope import (Header, TYPE_CHUNK, new_message_id,
                                           seal_ciphertext)

    msg_id = new_message_id()
    cipher = seal_ciphertext(them, recipient_public,
                             Header(type=TYPE_CHUNK, msg_id=msg_id), message)
    pieces = -(-len(cipher) // room)
    even = -(-len(cipher) // pieces)
    parts = []
    for index in range(pieces):
        piece = cipher[index * even:(index + 1) * even]
        head = Header(type=TYPE_CHUNK, msg_id=msg_id,
                      countdown=pieces - index - 1, clen=len(piece))
        parts.append({
            "txid": "%064x" % (index + 1), "cursor": 10 + index,
            "when": 1760000000 + index, "height": 1200 + index,
            "from_address": "nSenderAddress",
            "payload": (head.encode() + piece).hex(),
        })
    return parts


def test_the_browser_puts_back_together_what_python_split_up(loaded):
    """An account receives a chunked message or it receives nothing, and
    until this there was no code anywhere that let it receive one.

    The pieces are handed over in reverse, because that is what a node
    hands over: rows in the order they were indexed, which for a message
    still being sent is any order at all.
    """
    from arcade.encoding import MAX_CLASS_B_PAYLOAD
    from arcade.messaging import content

    browser, base, state = loaded
    phrase = seed.generate()
    me = Identity.from_secret_bytes(seed.messaging_key(phrase))
    them = Identity.generate()
    body = content.build("the arcade, as a picture", content.Attachment(
        name="boxa.png", content_type="image/png",
        data=b"\x89PNG\r\n\x1a\n" + bytes(2048) * 8))
    room = MAX_CLASS_B_PAYLOAD - 4 - 18         # the node's own ceiling
    parts = _split(them, me.public_bytes, body, room)
    assert len(parts) > 2, "a test of one chunk proves nothing about reassembly"

    said = browser.execute_async_script("""
        const done = arguments[2];
        (async () => {
          const me = await window.m.identity(arguments[0]);
          const out = window.m.assemble(arguments[1], me);
          done({opened: out.opened.length, waiting: out.waiting,
                spent: out.spent.length,
                plain: out.opened.length ? window.m.hex(out.opened[0].plain) : "",
                sender: out.opened.length ? window.m.hex(out.opened[0].sender) : "",
                txid: out.opened.length ? out.opened[0].txid : "",
                height: out.opened.length ? out.opened[0].height : 0});
        })();""", phrase, list(reversed(parts)))
    assert said == {"opened": 1, "waiting": 0, "spent": len(parts),
                    "plain": body.hex(), "sender": them.public_bytes.hex(),
                    "txid": parts[0]["txid"], "height": parts[-1]["height"]}, said
    # Filed under the transaction that STARTED it, dated by the one that
    # finished it -- the same two the node's own scanner stores, so the
    # message does not move when its last chunk is confirmed.


def test_a_message_with_a_piece_missing_is_not_shown_as_half(loaded):
    """Completion is self-describing, so a message that stopped halfway is
    waited for rather than printed in pieces. And a chunk forged against
    somebody else's message id cannot make a real message unreadable:
    chunks group by sender, so the forgery fails on its own."""
    browser, base, state = loaded
    phrase = seed.generate()
    me = Identity.from_secret_bytes(seed.messaging_key(phrase))
    them = Identity.generate()
    # Cut small on purpose. How big a chunk the node makes is `sender`'s
    # business and is tested there; what is under test is the countdown.
    parts = _split(them, me.public_bytes, bytes(range(256)) * 3, 200)
    assert len(parts) > 1, parts

    def count(given):
        return browser.execute_async_script("""
            const done = arguments[2];
            (async () => {
              const me = await window.m.identity(arguments[0]);
              const out = window.m.assemble(arguments[1], me);
              done({opened: out.opened.length, waiting: out.waiting,
                    spent: out.spent.length});
            })();""", phrase, given)

    assert count(parts[:-1]) == {"opened": 0, "waiting": 1, "spent": 0}, \
        "the last chunk carries countdown 0; without it nothing is known"
    # The final chunk of the message, republished by a stranger under the
    # same message id. Grouped by sender it is a group of one that opens
    # for nobody, and the real message still arrives. It is kept rather
    # than thrown away, because "it did not open" and "it is not finished"
    # are the same observation from here.
    forgery = dict(parts[-1], txid="f" * 64, from_address="nStrangerAddress")
    assert count(parts + [forgery]) == {
        "opened": 1, "waiting": 1, "spent": len(parts)}, \
        "an injected piece poisons its own group and not the real one"


def test_a_body_with_a_file_means_the_same_things_on_both_sides(loaded):
    """`content.py` and `media.py` are the node's reading of a decrypted
    body. This is the second reading, and an attachment is the one place a
    wrong reading stops being cosmetic: a file rendered by its declared
    type is a file chosen by the attacker."""
    from arcade import media
    from arcade.messaging import content

    browser, base, state = loaded
    png = b"\x89PNG\r\n\x1a\n" + bytes(64)
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    cases = [
        content.build("just words, with a £ and an emoji 🎮"),
        content.build("look at this", content.Attachment(
            name="../../somewhere/boxa .png", content_type="image/png", data=png)),
        content.build("a file of my own", content.Attachment(
            name="boxa.png", content_type="image/png", data=svg)),
        content.build("hello", profile=content.Profile(
            name="Ganawendan", mainnet_address="DHn8ezCimJ5zG3S6cuPjEzKW9WNiiAC7f6")),
        b"<not a body at all>",
    ]
    said = browser.execute_async_script("""
        const done = arguments[1];
        done(arguments[0].map((hexed) => {
          const body = window.m.parseBody(window.m.unhex(hexed));
          return {text: body.text, plain: body.plain,
                  file: body.file ? {name: body.file.name, type: body.file.type,
                                     size: body.file.size,
                                     shown: body.file.media
                                       ? body.file.media.mime : null} : null,
                  profile: body.profile};
        }));""", [c.hex() for c in cases])

    for raw, got in zip(cases, said):
        parsed = content.parse(raw)
        assert got["text"] == parsed.text, raw[:40]
        assert got["plain"] is parsed.plain, raw[:40]
        if parsed.attachment is None:
            assert got["file"] is None, raw[:40]
        else:
            assert got["file"]["name"] == parsed.attachment.name, raw[:40]
            assert got["file"]["size"] == len(parsed.attachment.data), raw[:40]
            # The type it is SHOWN as comes from the bytes, on both sides.
            kind = media.renderable(parsed.attachment.data)
            assert got["file"]["shown"] == (None if kind is None else kind.mime), \
                "an SVG labelled image/png is a download, not an <img>"
        if parsed.profile is None:
            assert got["profile"] is None, raw[:40]
        else:
            assert got["profile"] == parsed.profile.as_dict(), raw[:40]


def test_a_bare_file_says_what_it_was_in_words(loaded):
    """A file with no caption has no text, and both halves have to make the
    same thing up in the same way or the thread preview and the bubble
    disagree about what was said."""
    from arcade.messaging import content

    browser, base, state = loaded
    body = content.build("", content.Attachment(
        name="boxa.png", content_type="image/png",
        data=b"\x89PNG\r\n\x1a\n" + bytes(64)))
    said = browser.execute_async_script("""
        const done = arguments[1];
        done(window.m.text({body: arguments[0]}));""", body.hex())
    assert said == content.own_copy(body)[0].decode()
