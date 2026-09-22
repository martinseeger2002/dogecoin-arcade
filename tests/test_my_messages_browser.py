"""The account's Messages page: the same messenger, driven in a browser.

The operator: "Messages on arcade Web should look the same as Messages look on
arcade local, and should work the same. Reading from the mempool and such."

So the page is the same two panes with the same classes, and these tests
are about the half that is genuinely different: the conversations are
assembled in the browser out of what it opened and what it sent, and a
message read out of the MEMPOOL has to stop saying "not in a block yet"
once its block lands. The node hands a mempool row over at height 0 and
promotes it in place, so a browser that only ever reads forward would
never see the promotion.
"""

import json
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

from selenium.webdriver.common.by import By                      # noqa: E402
from selenium.webdriver.common.keys import Keys                  # noqa: E402

from arcade import seed                                          # noqa: E402
from arcade.messaging.keys import Identity                        # noqa: E402


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
        home=tmp_path_factory.mktemp("mymessages"),
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
    yield f"http://127.0.0.1:{port}", state
    server.should_exit = True
    thread.join(timeout=5)


THEM = "bb" * 32           # a peer's messaging key, as the store holds it
OTHER = "cc" * 32


def _hexed(text: str) -> str:
    return text.encode().hex()


def _seed_store(browser, letters, contacts=()):
    """Put messages and contacts where the page reads them from.

    Straight into IndexedDB, because that IS the page's input: there is no
    server-side copy of any of this by design, so seeding through the
    server would be seeding something the page never looks at.
    """
    return browser.execute_async_script("""
        const done = arguments[2];
        const open = indexedDB.open("arcade-messages", 3);
        open.onupgradeneeded = () => {
          const db = open.result;
          if (!db.objectStoreNames.contains("mail"))
            db.createObjectStore("mail", {keyPath: "txid"});
          if (!db.objectStoreNames.contains("marks"))
            db.createObjectStore("marks");
          if (!db.objectStoreNames.contains("book"))
            db.createObjectStore("book", {keyPath: "tag"});
          if (!db.objectStoreNames.contains("parts"))
            db.createObjectStore("parts", {keyPath: "txid"});
        };
        open.onerror = () => done("open: " + open.error);
        open.onsuccess = () => {
          const db = open.result;
          const tx = db.transaction(["mail", "book"], "readwrite");
          for (const letter of arguments[0])
            tx.objectStore("mail").put(letter);
          for (const entry of arguments[1])
            tx.objectStore("book").put(entry);
          tx.oncomplete = () => done("ok");
          tx.onerror = () => done("put: " + tx.error);
        };""", letters, list(contacts))


def _wipe(browser):
    return browser.execute_async_script("""
        const done = arguments[0];
        const open = indexedDB.open("arcade-messages", 3);
        open.onupgradeneeded = () => {
          const db = open.result;
          if (!db.objectStoreNames.contains("mail"))
            db.createObjectStore("mail", {keyPath: "txid"});
          if (!db.objectStoreNames.contains("marks"))
            db.createObjectStore("marks");
          if (!db.objectStoreNames.contains("book"))
            db.createObjectStore("book", {keyPath: "tag"});
          if (!db.objectStoreNames.contains("parts"))
            db.createObjectStore("parts", {keyPath: "txid"});
        };
        open.onerror = () => done("open: " + open.error);
        open.onsuccess = () => {
          const db = open.result;
          const tx = db.transaction(["mail", "marks", "book"], "readwrite");
          tx.objectStore("mail").clear();
          tx.objectStore("marks").clear();
          tx.objectStore("book").clear();
          tx.oncomplete = () => done("ok");
          tx.onerror = () => done("clear: " + tx.error);
        };""")


@pytest.fixture(scope="module")
def signed_in(browser, served):
    """A seat, and the wallet open in this tab -- which is what the page
    expects of somebody who signed in and walked to another page."""
    base, _ = served
    browser.get(f"{base}/join/keys")
    browser.delete_all_cookies()
    for _ in range(80):
        if browser.execute_script(
                "return document.body.dataset.joinReady === 'yes'"):
            break
        time.sleep(0.25)
    phrase = seed.generate()
    answer = browser.execute_async_script("""
        const done = arguments[1];
        Promise.all([import("/signin.js"), import("/wallet.js")])
          .then(([s, w]) => s.signIn(arguments[0], {join: true})
                             .then(() => { w.remember(arguments[0]); }))
          .then(() => done(true), e => done(String(e.message || e)));""",
        phrase)
    assert answer is True, answer
    return phrase


def _page(browser, base, query=""):
    browser.get(f"{base}/me/messages{query}")
    for _ in range(80):
        if browser.execute_script(
                "return document.body.dataset.messagesReady === 'yes'"):
            break
        time.sleep(0.25)
    else:
        raise AssertionError("the messages page never finished drawing")


def test_the_page_is_the_same_two_panes_as_the_wallets(browser, served,
                                                       signed_in):
    """Same markup, same classes, so it is the same page and not a second
    design of the same thing."""
    base, _ = served
    _page(browser, base)
    _wipe(browser)
    _page(browser, base)
    assert browser.find_element(By.CSS_SELECTOR, ".msgr").is_displayed()
    assert browser.find_element(By.CSS_SELECTOR, ".msgr aside.threads")
    assert browser.find_element(By.CSS_SELECTOR, ".msgr section.convo")
    assert browser.find_element(By.CSS_SELECTOR, ".threads-head strong").text \
        == "Conversations"
    assert browser.find_element(By.ID, "no-threads").is_displayed()
    # Nothing chosen yet, so the conversation pane says what to do.
    assert browser.find_element(By.ID, "nothing").is_displayed()
    assert not browser.find_element(By.ID, "talking").is_displayed()


def test_messages_become_conversations_with_bubbles_each_way(browser, served,
                                                             signed_in):
    base, _ = served
    _page(browser, base)
    _wipe(browser)
    now = int(time.time())
    assert _seed_store(browser, [
        {"txid": "a" * 64, "cursor": 10, "when": now - 300, "height": 900,
         "from_address": "nTheirAddress", "sender": THEM, "peer": THEM,
         "mine": False, "body": _hexed("are you there")},
        {"txid": "b" * 64, "from_cursor": 10, "when": now - 200, "height": 0,
         "to_address": "nTheirAddress", "peer": THEM, "tag": "robin",
         "mine": True, "body": _hexed("I am here")},
    ], [{"tag": "robin", "address": "nTheirAddress", "key": THEM,
         "fingerprint": "ffff", "added": now}]) == "ok"
    _page(browser, base)

    rows = browser.find_elements(By.CSS_SELECTOR, "a.thread")
    assert len(rows) == 1
    assert "@robin" in rows[0].text
    assert "You: I am here" in rows[0].text
    rows[0].click()
    time.sleep(0.6)

    assert browser.find_element(By.ID, "convo-name").text == "@robin"
    assert browser.find_element(By.ID, "convo-sub").text == "nTheirAddress"
    theirs = browser.find_elements(By.CSS_SELECTOR, ".bubble-row.theirs")
    mine = browser.find_elements(By.CSS_SELECTOR, ".bubble-row.mine")
    assert len(theirs) == 1 and len(mine) == 1
    assert "are you there" in theirs[0].text
    assert "I am here" in mine[0].text
    # Their message is in a block and says which; ours is not in one yet
    # and says nothing about when, because the only time available is when
    # this browser pressed send.
    assert "block 900" in theirs[0].text
    assert "unconfirmed" in mine[0].text


def test_something_read_out_of_the_pool_says_so(browser, served, signed_in):
    """An incoming message at height 0 is real and readable -- it just is
    not in a block yet, and the bubble says exactly that."""
    base, _ = served
    _page(browser, base)
    _wipe(browser)
    now = int(time.time())
    assert _seed_store(browser, [
        {"txid": "c" * 64, "cursor": 11, "when": now - 60, "height": 0,
         "from_address": "nTheirAddress", "sender": THEM, "peer": THEM,
         "mine": False, "body": _hexed("straight out of the pool")},
    ]) == "ok"
    _page(browser, base)
    browser.find_element(By.CSS_SELECTOR, "a.thread").click()
    time.sleep(0.6)
    bubble = browser.find_element(By.CSS_SELECTOR, ".bubble-row.theirs")
    assert "straight out of the pool" in bubble.text
    assert "in the pool, not in a block yet" in bubble.text


def test_a_block_promotes_what_was_read_out_of_the_pool(browser, served,
                                                        signed_in):
    """The one the node cannot do for us.

    A mempool row is handed over at height 0 and PROMOTED IN PLACE when the
    block arrives -- same row, same rowid. A browser that only ever asks
    for rows after its cursor therefore never hears about the promotion, so
    the scan rewinds over anything of ours still pending and reads that
    window again. Asking the node about our own txids by name would tell it
    which messages are ours, which is the whole thing this arrangement
    refuses to say.
    """
    base, _ = served
    _page(browser, base)
    _wipe(browser)
    now = int(time.time())
    assert _seed_store(browser, [
        {"txid": "d" * 64, "from_cursor": 40, "when": now - 120, "height": 0,
         "to_address": "nTheirAddress", "peer": THEM, "tag": "robin",
         "mine": True, "body": _hexed("did this land")},
    ]) == "ok"

    asked = browser.execute_async_script("""
        const done = arguments[0];
        const asked = [];
        const real = window.fetch.bind(window);
        // This browser has already read past the pending row: cursor 43,
        // the row at 40. Without the rewind it would ask for 44 onwards
        // and never hear that 40 got a block.
        const mark = (pubkey) => new Promise((ok, no) => {
          const open = indexedDB.open("arcade-messages", 3);
          open.onsuccess = () => {
            const tx = open.result.transaction(["marks"], "readwrite");
            tx.objectStore("marks").put(43, "cursor:" + pubkey);
            tx.oncomplete = ok;
            tx.onerror = () => no(tx.error);
          };
          open.onerror = () => no(open.error);
        });
        window.fetch = function (url) {
          const where = String(url);
          if (where.startsWith("/account/messages")) {
            asked.push(where);
            return Promise.resolve(new Response(JSON.stringify({
              cursor: 44, newest: 44, more: false,
              candidates: [{cursor: 44, txid: "dddddddddddddddddddddddddddd"
                             + "dddddddddddddddddddddddddddddddddddd",
                            height: 1204, when: 1760000000,
                            from_address: "nMine", type: 1, msg_id: null,
                            countdown: null, payload: "00"}],
            }), {status: 200,
                 headers: {"Content-Type": "application/json"}}));
          }
          return real(url);
        };
        import("/messaging.js").then(async (m) => {
          const me = await m.identity(
            "abandon abandon abandon abandon abandon abandon "
            + "abandon abandon abandon abandon abandon about");
          await mark(m.hex(me.publicKey));
          await m.collect(me);
          const letters = await m.inbox();
          done({asked, letters});
        }).catch(e => done({error: String(e.message || e)}));""")
    assert "error" not in asked, asked
    # It rewound: the window it asked for starts BEFORE the pending row.
    assert any("after=39" in where for where in asked["asked"]), asked["asked"]
    landed = [l for l in asked["letters"] if l["txid"].startswith("dddd")]
    assert landed and landed[0]["height"] == 1204
    # And the page now says which block instead of "unconfirmed".
    _page(browser, base)
    browser.find_element(By.CSS_SELECTOR, "a.thread").click()
    time.sleep(0.6)
    bubble = browser.find_element(By.CSS_SELECTOR, ".bubble-row.mine")
    assert "block 1,204" in bubble.text
    assert "unconfirmed" not in bubble.text


@pytest.fixture
def landed(served):
    """File message rows in the node's store, and take them out afterwards.

    They have to come out. An account has no inbox of the node's making --
    it rebuilds one by reading the chain from where its own key starts --
    so a candidate row that is left behind comes back as a conversation
    after the next `_wipe` wipes the cursor, and whichever test runs next
    is left counting somebody else's traffic.
    """
    from arcade.messaging.envelope import TYPE_CHUNK

    _, state = served
    filed = []

    def land(txid, height, payload, msg_id, countdown, when=None):
        with state.store() as store:
            store.add_candidate(txid, height, 0,
                                when if when is not None else height * 10,
                                "nThem", payload, TYPE_CHUNK, msg_id, countdown)
        filed.append(txid)

    yield land
    with state.store() as store:
        store.conn.execute(
            "DELETE FROM candidate WHERE txid IN (%s)"
            % ",".join("?" * len(filed)), filed)


def _chunked(recipient_public, message, pieces):
    """A message sealed once and cut into `pieces`, as the sender cuts it.

    The framing comes from the production `Header.encode()`; only the cut
    is spelled out here, because what a chunk is depends on how much a
    Class B output carries, and that is tested in `test_messaging*`.
    """
    from arcade.messaging.envelope import (Header, TYPE_CHUNK,
                                           new_message_id, seal_ciphertext)

    msg_id = new_message_id()
    cipher = seal_ciphertext(Identity.generate(), recipient_public,
                             Header(type=TYPE_CHUNK, msg_id=msg_id), message)
    even = -(-len(cipher) // pieces)
    rows = []
    for index in range(pieces):
        piece = cipher[index * even:(index + 1) * even]
        head = Header(type=TYPE_CHUNK, msg_id=msg_id,
                      countdown=pieces - index - 1, clen=len(piece))
        rows.append((head.encode() + piece, msg_id, head.countdown))
    return rows


def test_a_message_that_came_in_pieces_arrives_in_one(
        browser, served, signed_in, landed):
    """The one thing an account could not do until now: receive anything
    bigger than a single transaction. A long message is ONE sealed box
    split across dozens of them, and the browser has to put it back.

    Filed through the node's own store rather than through a fake, because
    the point of the test is the whole road -- candidate rows out of
    SQLite, handed over unlabelled by `/account/messages`, tried against a
    key that lives only in this tab, and drawn as a picture.
    """
    from arcade.messaging import content

    base, _ = served
    _page(browser, base)
    _wipe(browser)
    me = Identity.from_secret_bytes(seed.messaging_key(signed_in))
    body = content.build("the arcade, as a picture", content.Attachment(
        name="boxa.png", content_type="image/png",
        data=b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 3))
    now = int(time.time())
    for index, (payload, msg_id, countdown) in enumerate(
            _chunked(me.public_bytes, body, 3)):
        landed("%064x" % (900 + index), 100 + index, payload, msg_id,
               countdown, when=now - 300 + index)
    _page(browser, base)
    for _ in range(60):
        if browser.find_elements(By.CSS_SELECTOR, "a.thread"):
            break
        time.sleep(0.25)
    else:
        raise AssertionError("the pieces never came together: "
            + browser.find_element(By.ID, "checking").text)
    # One conversation, and its preview is the caption, not a piece.
    rows = browser.find_elements(By.CSS_SELECTOR, "a.thread")
    assert len(rows) == 1, [row.text for row in rows]
    assert "the arcade, as a picture" in rows[0].text
    rows[0].click()
    time.sleep(0.8)

    img = browser.find_element(By.CSS_SELECTOR, ".bubble img.media")
    assert img.get_attribute("src").startswith("blob:")
    assert img.get_attribute("alt") == "boxa.png"
    bubble = browser.find_element(By.CSS_SELECTOR, ".bubble .text")
    assert bubble.text == "the arcade, as a picture"
    # One message, not three: filed under the transaction that started it.
    assert len(browser.find_elements(By.CSS_SELECTOR, ".bubble-row")) == 1
    saved = browser.find_element(By.CSS_SELECTOR, ".bubble a.file")
    assert saved.get_attribute("download") == "boxa.png"


def test_pieces_wait_for_the_rest_of_themselves(browser, served, signed_in,
                                                landed):
    """Half a message is not a message. The pieces that arrived are kept in
    this browser and tried again on the next look, because the node hands
    over what has landed so far and a cursor only ever moves forward -- a
    browser that forgot them would need to be lucky about which page of the
    chain it happened to be looking at.

    The hole is in the MIDDLE. Countdowns run down to zero, so a message
    that lost its FIRST chunk looks exactly like a shorter message that is
    complete, and nothing can tell the two apart -- the node's scanner
    decides the same way.
    """
    base, _ = served
    _page(browser, base)
    _wipe(browser)
    me = Identity.from_secret_bytes(seed.messaging_key(signed_in))
    rows = []
    for index, (payload, msg_id, countdown) in enumerate(_chunked(
            me.public_bytes, b"the second half of a message, arriving late" * 12,
            3)):
        rows.append(("%064x" % (950 + index), 300 + index, payload, msg_id,
                     countdown))

    for index in (0, 2):               # the ends, with the middle still to come
        landed(*rows[index])
    _page(browser, base)
    time.sleep(2.5)
    assert not browser.find_elements(By.CSS_SELECTOR, "a.thread"), \
        "a message with a hole in it is not shown as half a message"

    landed(*rows[1])
    browser.find_element(By.ID, "check").click()
    for _ in range(60):
        if browser.find_elements(By.CSS_SELECTOR, "a.thread"):
            break
        time.sleep(0.25)
    else:
        raise AssertionError("the piece that was waited for never finished it: "
            + browser.find_element(By.ID, "checking").text)
    threads = browser.find_elements(By.CSS_SELECTOR, "a.thread")
    assert len(threads) == 1, [row.text for row in threads]
    threads[0].click()
    time.sleep(0.8)
    text = browser.find_element(By.CSS_SELECTOR, ".bubble-row .text")
    assert text.text.startswith("the second half of a message")
    assert len(browser.find_elements(By.CSS_SELECTOR, ".bubble-row")) == 1, \
        "and it arrived once, not once per look"


def test_unread_is_counted_and_clears_when_it_is_read(browser, served,
                                                      signed_in):
    base, _ = served
    _page(browser, base)
    _wipe(browser)
    now = int(time.time())
    assert _seed_store(browser, [
        {"txid": "e" * 64, "cursor": 12, "when": now - 60, "height": 5,
         "from_address": "nTheirAddress", "sender": THEM, "peer": THEM,
         "mine": False, "body": _hexed("one")},
        {"txid": "f" * 64, "cursor": 13, "when": now - 30, "height": 6,
         "from_address": "nTheirAddress", "sender": THEM, "peer": THEM,
         "mine": False, "body": _hexed("two")},
    ]) == "ok"
    _page(browser, base)
    badge = browser.find_element(By.CSS_SELECTOR, "a.thread .badge.new")
    assert badge.text == "2"
    browser.find_element(By.CSS_SELECTOR, "a.thread").click()
    time.sleep(0.8)
    _page(browser, base)
    assert not browser.find_elements(By.CSS_SELECTOR, "a.thread .badge.new")


def test_two_people_are_two_conversations(browser, served, signed_in):
    base, _ = served
    _page(browser, base)
    _wipe(browser)
    now = int(time.time())
    assert _seed_store(browser, [
        {"txid": "1" * 64, "cursor": 20, "when": now - 400, "height": 5,
         "from_address": "nOne", "sender": THEM, "peer": THEM,
         "mine": False, "body": _hexed("from the first")},
        {"txid": "2" * 64, "cursor": 21, "when": now - 100, "height": 6,
         "from_address": "nTwo", "sender": OTHER, "peer": OTHER,
         "mine": False, "body": _hexed("from the second")},
    ], [{"tag": "robin", "address": "nOne", "key": THEM, "added": now}]) == "ok"
    _page(browser, base)
    rows = browser.find_elements(By.CSS_SELECTOR, "a.thread")
    assert len(rows) == 2
    # Newest first, and somebody not in the book is shown by their address
    # rather than by a name nobody can check.
    assert "nTwo" in rows[0].text
    assert "@robin" in rows[1].text


def test_enter_sends_rather_than_adding_a_line(browser, served, signed_in):
    """The keyboard path is the one most likely to be pressed twice and the
    one that broke on the wallet's page: Enter painted "Sending..." over a
    form that never posted. Here there is no chain behind the node, so what
    is asserted is that Enter TRIES -- the composer empties nothing and the
    trouble line fills in, which only happens on the send path."""
    base, _ = served
    _page(browser, base)
    _wipe(browser)
    now = int(time.time())
    assert _seed_store(browser, [
        {"txid": "3" * 64, "cursor": 30, "when": now - 60, "height": 7,
         "from_address": "nOne", "sender": THEM, "peer": THEM,
         "mine": False, "body": _hexed("say something back")},
    ], [{"tag": "robin", "address": "nOne", "key": THEM, "added": now}]) == "ok"
    _page(browser, base)
    browser.find_element(By.CSS_SELECTOR, "a.thread").click()
    time.sleep(0.6)
    box = browser.find_element(By.ID, "body")
    box.send_keys("a reply")
    box.send_keys(Keys.ENTER)
    for _ in range(40):
        if browser.find_element(By.ID, "trouble").is_displayed():
            break
        time.sleep(0.25)
    assert browser.find_element(By.ID, "trouble").is_displayed()
    # Enter did not put a newline in the box either.
    assert "\n" not in browser.find_element(By.ID, "body").get_attribute("value")


def test_starting_one_from_the_address_book(browser, served, signed_in):
    base, _ = served
    _page(browser, base)
    _wipe(browser)
    now = int(time.time())
    assert _seed_store(browser, [], [
        {"tag": "robin", "address": "nOne", "key": THEM, "added": now}]) == "ok"
    _page(browser, base)
    browser.find_element(By.ID, "new").click()
    box = browser.find_element(By.ID, "who-new")
    box.send_keys("@robin")
    browser.find_element(By.ID, "start").click()
    for _ in range(40):
        if browser.find_element(By.ID, "talking").is_displayed():
            break
        time.sleep(0.25)
    assert browser.find_element(By.ID, "convo-name").text == "@robin"
    assert "No messages yet" in browser.find_element(By.ID, "bubbles").text
