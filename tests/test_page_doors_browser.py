"""What an inscribed page may hold, say and spend in an account's name.

The routes that answer the doors are `test_account_talk.py` and the shop
file; what this file tests is the bridge itself: the page in the sandbox
asking for a wallet and getting an account instead, and the viewer answering
each of the four doors the way D-169 decided -- memory in this browser,
speech under the account's own messaging key, trade as before, and no spend
that nobody is watching.

The node is `state.public = True` throughout, and the page the tests click
in is an HTML inscription running in a sandbox with an opaque origin: it can
reach nothing but the three shims it loads and the questions it posts to the
page around it. The only key in this file is the one in the browser.
"""

import base64
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

from test_tokens_web import RegtestContext                       # noqa: E402

from arcade.messaging import contact as contactlib               # noqa: E402
from arcade.messaging.keys import fingerprint_of                 # noqa: E402
from arcade.messaging.scanner import Scanner                     # noqa: E402
from arcade.web.app import create_app                            # noqa: E402
from arcade.web.state import AppState                            # noqa: E402
from arcade.web.watcher import BlockWatcher                      # noqa: E402

COIN = 100_000_000
PASSWORD = "a long enough password"


def _page_bytes(node_key: str) -> bytes:
    """The inscribed page: three doors, six buttons, nothing of its own.

    Every answer lands on `window` inside the frame, which is the shape a
    page has -- and the reason the test can only read it through the frame:
    the bridge is the frame's only way to the machine showing it.
    """
    return (
        "<!doctype html><meta charset=utf-8><title>Doors</title>"
        "<script src=\"/r/storage.js\"></script>"
        "<script src=\"/r/node.js\"></script>"
        "<script src=\"/r/owner.js\"></script>"
        "<button id=r>remember</button><button id=i>speak</button>"
        "<button id=s>say</button><button id=l>answers</button>"
        "<button id=o>whose</button><button id=d>spend</button><script>"
        "var NODE = '" + node_key + "';"
        "arcade.storage.ready.then(function (s) {"
        "  window.before = s.getItem('visits'); });"
        "document.getElementById('r').onclick = function () {"
        "  arcade.storage.ready.then(function (s) {"
        "    var n = Number(s.getItem('visits') || 0) + 1;"
        "    return s.setItem('visits', String(n))"
        "      .then(function () { window.mem = String(n); });"
        "  }).catch(function (e) { window.mem = 'error: ' + e.message; }); };"
        "document.getElementById('i').onclick = function () {"
        "  arcade.node.identity()"
        "    .then(function (r) { window.nid = {pubkey: r.pubkey,"
        "                       contactcode: r.contactcode, network: r.network}; },"
        "         function (e) { window.nid = {error: String(e.message || e)}; }); };"
        "document.getElementById('s').onclick = function () {"
        "  arcade.node.send(NODE, {hello: 'a page spoke'})"
        "    .then(function (r) { window.out = {txid: r.txid}; },"
        "         function (e) { window.out = {error: String(e.message || e)}; }); };"
        "document.getElementById('l').onclick = function () {"
        "  arcade.node.replies()"
        "    .then(function (rs) { window.rep = rs.map(function (r) { return r.json; }); },"
        "         function (e) { window.rep = {error: String(e.message || e)}; }); };"
        "document.getElementById('o').onclick = function () {"
        "  arcade.owner.identity()"
        "    .then(function (r) { window.own = {owner: r.owner, creator: r.creator,"
        "                      holder: r.holder, network: r.network}; },"
        "         function (e) { window.own = {error: String(e.message || e)}; }); };"
        "document.getElementById('d').onclick = function () {"
        "  arcade.owner.send({kind: 'coins', amount: '0.01', to: 'nobody'})"
        "    .then(function (r) { window.gone = r; },"
        "         function (e) { window.refuse = String(e.message || e); }); };"
        "</script>").encode()


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
def served(tmp_path_factory, regtest):
    """A public arcade on a chain it has to be told to follow -- the shape
    `test_shop_browser.py` builds and for the same reasons: one marker for
    both roles, an activation height that makes this file's traffic the only
    arcade state, and `public = True` so the operator's wallet is offered to
    nobody."""
    import uvicorn

    regtest.generate(200)                       # past coinbase maturity
    height = regtest.rpc.get_block_count() + 1
    marker = regtest.rpc.call("getnewaddress")
    state = AppState(
        home=tmp_path_factory.mktemp("doors-browser"),
        messaging=RegtestContext(network="regtest", role="messaging",
                                 label="Testnet", node=regtest,
                                 activation=height, marker=marker),
        ledger=RegtestContext(network="regtest", role="ledger",
                              label="Testnet", node=regtest, activation=height,
                              marker=marker))
    state.public = True

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
    try:
        yield f"http://127.0.0.1:{port}", state, regtest
    finally:
        state.public = False
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive(), \
            "the server is still serving a keep-alive connection"


def _catch_up(state):
    """Index every block before anything is read, however long the walk."""
    index = state.token_index(state.messaging)
    tip = 0
    for _ in range(400):
        with state.messaging.rpc() as rpc:
            tip = rpc.get_block_count()
        if index.status(tip)["current"]:
            return index
        if index.stopped is not None:
            raise AssertionError(f"the index stopped: {index.stopped}")
        index.sync(max_blocks=500)
        time.sleep(0.05)
    raise AssertionError(f"the index will not catch up: {index.status(tip)}")


def _opened(state, daemon):
    """Mine, and let the node open the machine messages that were for it."""
    daemon.generate(1)
    BlockWatcher(state)._sync_ledgers()
    with state.messaging.rpc() as rpc, state.store() as store:
        scanner = Scanner(rpc, state.messaging.params, store,
                          identity=state.ensure_identity())
        for _ in range(50):
            if scanner.scan().blocks == 0:
                break
        scanner.open_pending()


def _landed(state, daemon, *txids) -> None:
    """Mine, and wait for the index to have read these transactions."""
    daemon.generate(1)
    index = _catch_up(state)
    with state.messaging.rpc() as rpc:
        tip = rpc.get_block_count()
        for txid in txids:
            if index.inscription(txid) is not None:
                continue
            conf = rpc.call("getrawtransaction", txid, True).get("confirmations")
            raise AssertionError(
                f"{txid[:12]} is not in the index, though the index read as far "
                f"as {index.indexed_height()} of {tip} blocks and the chain says "
                f"{conf} confirmations")


def _load_libs(browser, base):
    browser.get(f"{base}/join")
    browser.set_script_timeout(180)
    assert browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        Promise.all([import("/wallet.js"), import("/messaging.js")]).then(
          ([w, m]) => { window.w = w; window.m = m; done(true); },
          (e) => done(String(e)));""") is True


@pytest.fixture(scope="module")
def doors(browser, served):
    """A page inscribed BY the account, so its owner door has a true answer
    to give, in a browser that is that account's browser."""
    base, state, daemon = served
    _load_libs(browser, base)
    made = browser.execute_async_script("""
        const done = arguments[3];
        window.w.signUp(arguments[0], arguments[1],
                        {network: "regtest", version: arguments[2]})
          .then((r) => done({tag: r.tag, address: r.address}),
                (e) => done({error: String(e.message || e)}));""",
        "doortester", PASSWORD, state.messaging.params.pubkeyhash_version)
    assert "error" not in made, made
    daemon.rpc.call("sendtoaddress", made["address"], 3.0)
    _opened(state, daemon)

    node_key = state.ensure_identity().public_bytes.hex()
    # The page goes up the way an account puts anything on the chain: it asks
    # this node for an offer and signs it in this tab. The node cannot
    # inscribe ON an account's behalf -- `sender.send_all` pays to an address
    # it holds the key for -- and a page this node paid for would be this
    # node's page, which is the one thing the owner door's honest `true`
    # cannot rest on.
    inscribed = browser.execute_async_script("""
        const done = arguments[2];
        (async () => {
          try {
            const wallet = await window.w.opened(
              {network: "regtest", version: arguments[0]});
            if (!wallet) throw new Error("no open key in this tab");
            const asked = await fetch("/account/inscribe", {
              method: "POST", headers: {"Content-Type": "application/json"},
              body: JSON.stringify({content: arguments[1],
                                    content_type: "text/html",
                                    name: "Doors", chain: "regtest"})});
            const offer = await asked.json();
            if (!asked.ok) throw new Error(offer.detail || "no offer");
            const out = await window.w.confirm(wallet, offer);
            done({txid: out.txid});
          } catch (e) { done({error: String(e.message || e)}); }
        })();""",
        state.messaging.params.pubkeyhash_version,
        base64.b64encode(_page_bytes(node_key)).decode())
    assert "error" not in inscribed, inscribed
    page = inscribed["txid"]
    _landed(state, daemon, page)

    index = state.token_index(state.messaging)
    assert index.inscription(page)["creator"] == made["address"] == \
        index.inscription(page)["owner"], \
        "the page and the signer are the same person's property -- that is " \
        "what the owner door checks and what it can honestly say yes to"

    return {"base": base, "state": state, "daemon": daemon, "page": page,
            "node": node_key, **made}


def _view(browser, doors):
    """Open the page the way a reader does, and return its sandboxed frame."""
    browser.get(f"{doors['base']}/inscriptions/{doors['page']}/view")
    frames = browser.find_elements(By.CSS_SELECTOR, "iframe.inscription-frame")
    assert frames, "the page is an HTML inscription and it drew no frame"
    return frames[0]


def _click(browser, frame, button):
    browser.switch_to.frame(frame)
    # The frame is found the moment it draws; its buttons exist a moment
    # later, when the document inside it has parsed. Clicking through the
    # null principal before then is a race the sandbox loses loudly.
    browser.set_script_timeout(60)
    browser.execute_async_script("""
        const done = arguments[1];
        const wait = () => document.getElementById(arguments[0])
          ? done(true) : setTimeout(wait, 100);
        wait();""", button)
    browser.execute_script(f"document.getElementById('{button}').click();")
    browser.switch_to.default_content()


def _settled(browser, frame, name, timeout=120):
    """Wait for `window.<name>` inside the frame, and read it once."""
    browser.switch_to.frame(frame)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            got = browser.execute_script(
                f"return typeof window.{name} === 'undefined' "
                f"? null : [window.{name}];")
            if got is not None:
                return got[0]
            time.sleep(0.3)
        raise AssertionError(f"the page never set window.{name}")
    finally:
        browser.switch_to.default_content()


def _shelves(browser):
    return browser.execute_script("""
        return Object.keys(localStorage).filter(
          function (k) { return k.indexOf("arcade.pagestore.") === 0; });""")


# --- the storage door: a memory that lives in the browser ----------------------------------------------------------------

def test_the_page_remembers_between_loads_without_telling_the_node(browser, doors):
    """`arcade.storage` was the wallet's memory of the page. With no wallet
    behind a public page it is THIS browser's memory -- which only means it
    works if surviving a reload does not need the server to have heard of
    it. The node's pagestore is read after every click: it stays empty,
    because a public node that keeps pages' things IS the thing D-169 says
    this node must not be."""
    frame = _view(browser, doors)
    _click(browser, frame, "r")
    assert _settled(browser, frame, "before") is None, \
        "the shelf was not empty before the first visit"
    assert _settled(browser, frame, "mem") == "1"

    frame = _view(browser, doors)            # a reload: everything is new
    assert _settled(browser, frame, "before") == "1", \
        "the page remembered nothing across a load"
    _click(browser, frame, "r")
    assert _settled(browser, frame, "mem") == "2"

    assert doors["state"].pagestore.items(doors["page"]) == {}, \
        "the browser answered, so the server must not have"
    shelf = f"arcade.pagestore.{doors['address']}.{doors['page']}"
    assert _shelves(browser) == [shelf], \
        "the shelf is named for this account and this page, and nothing else"


# --- the node door: speech under the account's own key ---------------------------------------------------------------

def test_a_page_speaks_under_the_accounts_own_key(browser, doors):
    """`arcade.node.send` from a public page is the same envelope the
    operator's wallet would have sealed -- sealed in this tab to a key this
    tab checked, carried by a transaction this account pays for, and openable
    by nobody but the node it went to. The sender the recipient reads is the
    messaging key, not the address that paid: speech and payment are two
    different keys, which is the whole point of the account's key."""
    state, daemon = doors["state"], doors["daemon"]
    frame = _view(browser, doors)

    _click(browser, frame, "i")
    who = _settled(browser, frame, "nid")
    assert "error" not in who, who
    assert who["network"] == "regtest"
    assert contactlib.decode(who["contactcode"])[1].hex() == who["pubkey"], \
        "the identity the page is given opens what the page sends"

    _click(browser, frame, "s")
    sent = _settled(browser, frame, "out")
    assert "error" not in sent, sent
    assert len(sent["txid"]) == 64, sent

    _opened(state, daemon)
    me = state.ensure_identity()
    with state.store() as store:
        rows = [r for r in store.api_messages(fingerprint_of(me.public_bytes),
                                              "regtest")
                if r["txid"] == sent["txid"]]
    assert len(rows) == 1, "the carrier did not land and open on this node"
    row = rows[0]
    assert json.loads(bytes(row["body"])) == {"hello": "a page spoke"}
    assert bytes(row["sender_pubkey"]).hex() == who["pubkey"], \
        "the node reads the key that sealed it, not the coins that paid"

    # And this node answers programs it runs, not greetings it is handed:
    # nothing spoke back, and the page is told exactly that rather than left
    # spinning on a chat that does not exist.
    _click(browser, frame, "l")
    assert _settled(browser, frame, "rep") == []


def test_the_hour_the_page_spends_is_the_accounts_one(browser, doors):
    """The cap a page in a loop hits is the account's message dial -- a tally
    kept in the tab resets with the tab and could only ever be theatre. Here
    the page's one send lands inside thirty an hour, and what proves the
    meter is real is that the send was offered a transaction rather than
    answered with a number from somewhere else: the account paid for it out
    of coins the node cannot sign for."""
    frame = _view(browser, doors)
    _click(browser, frame, "s")
    sent = _settled(browser, frame, "out")
    assert "error" not in sent, sent
    _opened(doors["state"], doors["daemon"])
    pool = doors["daemon"].rpc.call("getrawmempool")
    assert pool == [], "the message landed; the hour it cost was this one's"


# --- the owner door: honest about who, shut against the spend --------------------------------------------------

def test_the_owner_door_says_who_is_looking_and_never_spends(browser, doors):
    """The operator's owner door sends unattended because the wallet holding
    the key is the machine that made the page. A public node was handed no
    such key, so the door answers `identity` truthfully -- so a page can
    grey out its own buttons correctly -- and refuses `send` with the reason
    said as a sentence, pointing at the shop door. Nothing in this test
    should touch the chain, and the mempool is read to prove it did not."""
    frame = _view(browser, doors)
    _click(browser, frame, "o")
    own = _settled(browser, frame, "own")
    assert "error" not in own, own
    assert own["owner"] is True, \
        "this account made the page and still holds it; the door says yes"
    assert own["creator"] == own["holder"] == doors["address"]
    assert own["network"] == "regtest"

    _click(browser, frame, "d")
    refused = _settled(browser, frame, "refuse")
    assert "unattended" in refused, refused
    assert "arcade.swap" in refused, refused
    assert doors["daemon"].rpc.call("getrawmempool") == [], \
        "a page may not spend an account's coins, and here it did not"


# --- and the stranger: the last test, because it deletes the session ---------------------------------------

def test_a_signed_out_stranger_gets_a_shelf_and_no_voice(browser, doors):
    """A page opened by somebody with no seat keeps its memory in the
    browser all the same -- their own machine has `localStorage`, so an
    anonymous shelf is not a capability they did not already have -- but a
    message to another node is sealed AS somebody, and this page is not
    holding a key to seal with. That is said, not faked."""
    browser.delete_all_cookies()
    frame = _view(browser, doors)
    _click(browser, frame, "r")
    assert _settled(browser, frame, "mem") == "1", \
        "the anonymous shelf starts empty and fills like any other"
    frame = _view(browser, doors)
    assert _settled(browser, frame, "before") == "1"
    assert f"arcade.pagestore.anon.{doors['page']}" in _shelves(browser), \
        "the stranger's shelf is the same shape, keyed to anon"

    _click(browser, frame, "i")
    who = _settled(browser, frame, "nid")
    assert "error" in who and "sign in first" in who["error"], who


def test_a_page_in_full_screen_keeps_what_it_saved(browser, doors):
    """2026-09-30, the operator: the arcade's storage was "not saving the game state
    from one session to the next" -- in full screen, where no page answered the
    storage door, so a game fell back to a sandbox that has no storage at all."""
    def full():
        browser.get(f"{doors['base']}/inscriptions/{doors['page']}/full")
        frames = browser.find_elements(By.CSS_SELECTOR, "iframe.inscription-frame")
        assert frames, "the full-screen page draws the frame"
        return frames[0]

    frame = full()
    _click(browser, frame, "r")
    saved = _settled(browser, frame, "mem")
    assert not str(saved).startswith("error"), saved
    frame = full()                                   # a new session of the page
    before = _settled(browser, frame, "before")
    assert before == saved, "what the page saved is there when it comes back"
