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


# --- letting people into a group (2026-11-08) --------------------------------
#
# The node is not asked anything here: who people are, what can be spent and the mailbox come from a stubbed fetch,
# and every send goes through `outbox`, swapped for one that counts. What is checked is what the browser decides to
# send, and what it does with what it reads.

_GROUP_STUBS = """
  const m = window.m, people = arguments[0], opts = arguments[1] || {};
  window.__asked = []; window.__sent = []; window.__mail = window.__mail || [];
  window.__realFetch = window.__realFetch || window.fetch;
  window.fetch = async (url, init) => {
    const u = String(url), json = (o) => new Response(JSON.stringify(o), {status: 200});
    if (u.startsWith("/account/who/")) {
      const name = decodeURIComponent(u.slice(13)).replace(/^@/, "");
      const who = people[name] || Object.values(people).find((p) => p.address === name)
        || (window.__self && window.__self.address === name ? window.__self : null);
      return who ? json(who) : new Response(JSON.stringify({detail: "nobody"}), {status: 404});
    }
    if (u.startsWith("/account/messages")) {
      const after = Number(new URL(u, location.href).searchParams.get("after") || 0);
      return json({candidates: window.__mail.filter((c) => c.cursor > after), more: false,
                   cursor: Math.max(after, ...window.__mail.map((c) => c.cursor))});
    }
    if (u === "/account") return json({spendable: opts.spendable ?? 1e12, mail_from: 0});
    return window.__realFetch(url, init);
  };
  window.__realOutbox = window.__realOutbox || {...m.outbox};
  m.outbox.spendable = async () => opts.spendable ?? 1e12;
  m.outbox.ask = async (to, sealed) => {
    window.__asked.push({to, sealed: m.hex(sealed)});
    if ((opts.fail || []).includes(to)) throw new Error("no coins left for " + to);
    return {cost: 84000000, offer: {}, send: async () => {
      const txid = m.hex(crypto.getRandomValues(new Uint8Array(32)));
      window.__sent.push({to, txid});
      return {said: {txid}, shown: {fee: 0}};
    }};
  };
"""

JSON_PHRASE = '"abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"'

_UNSTUB = """
  window.fetch = window.__realFetch; Object.assign(window.m.outbox, window.__realOutbox);
"""


def _people(*names):
    from arcade.messaging.keys import Identity
    out = {}
    for name in names:
        ident = Identity.generate()
        out[name] = {"tag": name, "key": ident.public_bytes.hex(), "address": "n" + name.ljust(33, "x")[:33]}
    return out


def _run(browser, body, people, opts=None):
    said = browser.execute_async_script(
        "const done = arguments[arguments.length - 1];\n" + _GROUP_STUBS + """
        (async () => { try { return await (async () => {""" + body + """})(); } finally {""" + _UNSTUB + """} })()
          .then(done, (e) => done({error: String((e && e.message) || e)}));""", people, opts or {})
    return said


def test_adding_to_a_group_writes_only_to_the_newcomers_then_our_copy_and_one_line(loaded):
    """@apple's add to a 19-member Riffs group re-sent the key to all 18 and ran out of coins before the newcomer's.
    An add is one letter per NEWCOMER, our own copy, and one line to the group: never a letter to who is already in."""
    browser, _, _ = loaded
    folks = _people("bobadd", "carol", "dave", "dan", "erin")
    said = _run(browser, """
        const me = await m.identity(arguments[2]), wallet = {address: "nMEadd".padEnd(34, "x")};
        window.__self = {tag: "", key: m.hex(me.publicKey), address: wallet.address};
        const g = await m.createGroup(wallet, me, "Big club", ["@bobadd", "@carol", "@dave"], "alice");
        window.__asked = []; window.__sent = [];
        const grown = await m.addToGroup(wallet, me, g, ["@dan", "@erin"]);
        return {asked: window.__asked.map((a) => a.to), sent: window.__sent.map((s) => s.to), me: wallet.address,
                members: grown.members.map((x) => x.tag), failed: grown.failed};
    """.replace("arguments[2]", JSON_PHRASE), folks)
    assert "error" not in said, said
    dan, erin = folks["dan"]["address"], folks["erin"]["address"]
    assert said["sent"] == [dan, erin, said["me"], said["me"]], "newcomers first, then our copy, then the group line"
    assert len(said["sent"]) == 2 + 2, "N new + our copy + one line, not N members + 1"
    for old in ("bobadd", "carol", "dave"):
        assert folks[old]["address"] not in said["asked"], f"{old} is already in and gets nothing"
    assert said["members"] == ["alice", "bobadd", "carol", "dave", "dan", "erin"] and said["failed"] == []


def test_an_add_that_cannot_be_paid_for_sends_nothing(loaded):
    browser, _, _ = loaded
    folks = _people("bobpay", "fran")
    said = _run(browser, """
        const me = await m.identity(arguments[2]), wallet = {address: "nMEpay".padEnd(34, "x")};
        window.__self = {tag: "", key: m.hex(me.publicKey), address: wallet.address};
        const g = await m.createGroup(wallet, me, "Poor club", ["@bobpay"], "alice");
        window.__sent = [];
        let error = "";
        try { await m.addToGroup(wallet, me, g, ["@fran"]); } catch (e) { error = String(e.message || e); }
        return {error_said: error, sent: window.__sent.length,
                members: (await m.groupById(g.id)).members.map((x) => x.tag)};
    """.replace("arguments[2]", JSON_PHRASE), folks, {"spendable": 84000000 * 2})
    assert "error" not in said, said
    assert "Nothing was sent" in said["error_said"] and "3 messages" in said["error_said"], said
    assert said["sent"] == 0 and said["members"] == ["alice", "bobpay"], "not listed when the letter never went"


def test_one_failed_invite_does_not_stop_the_rest(loaded):
    browser, _, _ = loaded
    folks = _people("bobfail", "gail", "hank", "ivy")
    said = _run(browser, """
        const me = await m.identity(arguments[2]), wallet = {address: "nMEfail".padEnd(34, "x")};
        window.__self = {tag: "", key: m.hex(me.publicKey), address: wallet.address};
        const g = await m.createGroup(wallet, me, "Shaky club", ["@bobfail"], "alice");
        window.__sent = [];
        const grown = await m.addToGroup(wallet, me, g, ["@gail", "@hank", "@ivy"]);
        return {sent: window.__sent.map((s) => s.to), me: wallet.address, failed: grown.failed,
                members: (await m.groupById(g.id)).members.map((x) => x.tag)};
    """.replace("arguments[2]", JSON_PHRASE), folks, {"fail": [folks["hank"]["address"]]})
    assert "error" not in said, said
    assert said["sent"] == [folks["gail"]["address"], folks["ivy"]["address"], said["me"], said["me"]]
    assert [f["to"] for f in said["failed"]] == ["@hank"] and "no coins" in said["failed"][0]["error"]
    assert said["members"] == ["alice", "bobfail", "gail", "ivy"], "only who got the key is listed"


def test_the_added_line_reads_the_same_on_both_sides_and_unpatched_readers_see_words(loaded):
    """The ARCB form Arcade V1.0 sends too: `text` for anybody, `v1_added` for the browsers that add people."""
    import json as _json
    from arcade.messaging import content

    browser, _, _ = loaded
    people = [{"tag": "dan", "key": "ab" * 32, "address": "nDan"}, {"tag": "", "key": "cd" * 32, "address": "nErin"}]
    head = _json.dumps({"text": "@v1 added @dan, nErin", "v1_added": people}).encode()
    from_v1 = content.BODY_MAGIC + bytes([content.BODY_VERSION]) + len(head).to_bytes(2, "big") + head
    said = browser.execute_async_script("""
        const done = arguments[2], m = window.m;
        const ours = m.addedLine("alice", arguments[0]);
        const a = m.parseBody(ours), b = m.parseBody(m.unhex(arguments[1]));
        done({ours: m.hex(ours), a: {text: a.text, added: a.added}, b: {text: b.text, added: b.added}});""",
        people, from_v1.hex())
    ours = bytes.fromhex(said["ours"])
    assert said["a"] == {"text": "@alice added @dan, nErin", "added": people}
    assert said["b"] == {"text": "@v1 added @dan, nErin", "added": people}, "V1.0's line reads here"
    assert content.parse(ours).text == "@alice added @dan, nErin" and not content.parse(ours).plain, "and in Python"
    assert content.parse(from_v1).text == said["b"]["text"]


def test_who_was_in_learns_of_the_newcomer_from_the_line_when_it_arrives(loaded):
    """A member reads the group line off the chain: the newcomers join the group here, once, and nobody leaves."""
    import json as _json
    from arcade.messaging.envelope import Header, TYPE_SINGLE, seal_message
    from arcade.messaging.keys import Identity

    browser, _, _ = loaded
    folks = _people("adder", "jill")
    made = _run(browser, """
        const me = await m.identity(arguments[2]), wallet = {address: "nMEread".padEnd(34, "x")};
        window.__self = {tag: "", key: m.hex(me.publicKey), address: wallet.address};
        const g = await m.createGroup(wallet, me, "Readers", ["@adder"], "bobread");
        return {id: g.id, public: g.public};
    """.replace("arguments[2]", JSON_PHRASE), folks)
    assert "error" not in made, made
    jill, adder = folks["jill"], folks["adder"]
    named = [{"tag": "jill", "key": jill["key"], "address": jill["address"]},
             {"tag": "adder", "key": adder["key"], "address": adder["address"]}]       # already in: not twice
    head = _json.dumps({"text": "@adder added @jill", "v1_added": named}).encode()
    body = b"\x01ARCB\x01" + len(head).to_bytes(2, "big") + head
    blob = seal_message(Identity.generate(), bytes.fromhex(made["public"]), Header(type=TYPE_SINGLE), body)
    browser.execute_script("window.__mail = [{txid: arguments[0], cursor: 10**9, when: 1, height: 5,"
                           " from_address: arguments[1], payload: arguments[2]}];", "ef" * 32, adder["address"], blob.hex())
    said = _run(browser, """
        const me = await m.identity(arguments[2]);
        await m.collect(me);
        await m.collect(me);                                                   // read twice: still once
        const g = await m.groupById(arguments[3]);
        const lines = await m.groupLines(g.root || g.id);
        return {members: g.members.map((x) => x.tag), lines: lines.map((l) => l.text)};
    """.replace("arguments[2]", JSON_PHRASE).replace("arguments[3]", _json.dumps(made["id"])), folks)
    browser.execute_script("window.__mail = [];")
    assert "error" not in said, said
    assert said["members"] == ["bobread", "adder", "jill"], said
    assert said["lines"] == ["@adder added @jill"], "shown as the words it carries"
