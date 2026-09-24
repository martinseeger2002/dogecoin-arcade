"""An account buys from a shop, in the browser, with a key no node holds.

`tests/test_many_accounts.py` proves the routes: four transactions, three
kinds of signature, and a trade that is finished by the shop rather than by
the machine that signed it. What that file cannot show is the page, because it
drives the routes directly and the node answers whatever it is handed. Here the
buying is done by a storefront inscribed on the chain, asking the page around
it for a wallet and getting an account instead -- which is the arrangement all
the account work exists for, and the part of it a route test would pass
without.

The node is `state.public = True` throughout, so nothing here is reachable
without a session and the operator's wallet is offered to nobody: the only key
in this file is the one in the browser.
"""

import contextlib
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

from test_inscription_e2e import _put                            # noqa: E402
from test_tokens_web import RegtestContext                       # noqa: E402

from arcade import inscribe, utxos                               # noqa: E402
from arcade.messaging.scanner import Scanner                     # noqa: E402
from arcade.messaging.sender import funded_address               # noqa: E402
from arcade.shopkeeper import Shopkeeper                         # noqa: E402
from arcade.web.app import create_app                            # noqa: E402
from arcade.web.state import AppState                            # noqa: E402
from arcade.web.watcher import BlockWatcher                      # noqa: E402

COIN = 100_000_000
PRICE = 2
PASSWORD = "a long enough password"

#: A storefront, in the shape `docs/inscription-api.md` documents: the terms
#: are the inscription's JSON and the page is a button. It is inscribed, so it
#: runs in a sandbox with an opaque origin and can reach a wallet only by
#: asking the page around it -- which is the thing under test.
SHOP_PAGE = (b"<!doctype html><meta charset=utf-8><title>Browser shop</title>"
             b"<h1>Browser shop</h1><button id=b>Buy it</button><p id=s></p>"
             b"<script src=\"/r/swap.js\"></script><script>"
             b"var steps=[];"
             b"document.getElementById(\"b\").onclick=function(){"
             b"arcade.swap.buy(0,{every:700,timeout:240000,"
             b"step:function(t){steps.push(t);"
             b"document.getElementById(\"s\").textContent=t;}})"
             b".then(function(r){window.out={txid:r.txid,steps:steps};},"
             b"function(e){window.out={error:String(e.message||e),"
             b"steps:steps};});};"
             b"</script>")


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
    """A public arcade, on a chain it has to be told to follow.

    Two roles on one regtest chain, which is the shape `_account_chains`
    describes: one set of words, one chain, and a node that is also the shop.
    The activation height keeps this file's traffic the only arcade state in
    the index; the daemon is shared across the session and whatever is earlier
    is somebody else's transactions.
    """
    import uvicorn

    regtest.generate(200)                       # past coinbase maturity
    height = regtest.rpc.get_block_count() + 1
    # ONE marker, given to both roles. A Class B payload pays a marker address
    # to count as a payload at all, and which address that is comes from the
    # chain context that happened to build the index -- `token_index` keys by
    # network, and with both roles on one network that is either of them --
    # while a transaction is always built with the messaging context's. Set the
    # marker on one role only and the two halves disagree, since the other falls
    # back to the network's derived address: every inscription in the file is
    # then broadcast in a shape nobody can read, the index says it is current,
    # and it has indexed nothing.
    marker = regtest.rpc.call("getnewaddress")
    state = AppState(
        home=tmp_path_factory.mktemp("shop-browser"),
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
    """Index every block before anything is read, however long the walk.

    `sync()` answers None when another thread is already mid-pass -- the page
    poll this test drives, usually -- which is not the same as being caught
    up. Reading it as caught up is why browser files pass alone and fail in a
    full run, where the shared node has mined thousands of blocks.
    """
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
    """Mine, and let the node open the machine messages that were for it.

    The one place in this file that hands a Scanner an identity. Everything
    else here is sealed between the account's key and the shop's, and this node
    carries those without reading them; an order at a shop is the exception,
    because the program that answers it has to open it first.
    """
    daemon.generate(1)
    BlockWatcher(state)._sync_ledgers()
    with state.messaging.rpc() as rpc, state.store() as store:
        scanner = Scanner(rpc, state.messaging.params, store,
                          identity=state.ensure_identity())
        for _ in range(50):
            if scanner.scan().blocks == 0:
                break
        # scan() opens what it fetched and nothing else, and the tail is never
        # reached once the cursor sits at the tip. The watcher's mempool pass
        # would have opened each message a block earlier; there is no watcher
        # here -- see `_keep_the_chain_moving`.
        scanner.open_pending()


def _answered(state, daemon) -> int:
    """The shop's turn: read what arrived, answer it, land the answers."""
    _opened(state, daemon)
    answered = Shopkeeper(state).tick()
    _opened(state, daemon)              # so the buyer can read the reply back
    return answered


def _landed(state, daemon, *txids) -> None:
    """Mine, and wait for the index to have read these transactions.

    A shop is built out of inscriptions and nothing below works until the index
    agrees they exist, so this is where a fixture that merely broadcast has to
    stop and check. Two ways a confirmed inscription is still absent, and the
    message names both because both were found here the hard way: the walk is
    bounded (`_sync_ledgers` reads 500 blocks a pass, one pass a call, which is
    not the tip on the chain a session leaves behind), and an inscription that
    pays the wrong marker is not an inscription at all to the machine reading
    it -- a mistake that leaves the index reporting itself current while it has
    indexed nothing.
    """
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
                f"{conf} confirmations. An inscription is only read when it "
                f"pays the marker the index looks for ({index.params.marker}) "
                f"and the one its builder paid is the messaging chain's "
                f"({state.messaging.params.marker}); if those differ, nothing "
                f"here is indexable. Otherwise: "
                f"{index.stopped or 'the index did not stop'}")


def _keep_the_chain_moving(state, daemon, stop, trouble):
    """Mine and answer, while the browser waits for a block.

    `create_app` starts no watcher, and a browser test must not start the real
    one: its first phase is `_auto_update`, which fetches code from a website
    and runs it. So this is the watcher's shopkeeping half on the same
    rhythm, without its update half.
    """
    while not stop.is_set():
        try:
            _answered(state, daemon)
        except Exception as exc:        # a stuck loop must say so, not hang
            trouble.append(f"{type(exc).__name__}: {exc}")
        stop.wait(1.5)


def _page(browser, base):
    """A page of this arcade's own, with the two libraries loaded on it."""
    browser.get(f"{base}/join")
    browser.set_script_timeout(180)
    assert browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        Promise.all([import("/wallet.js"), import("/messaging.js")]).then(
          ([w, m]) => { window.w = w; window.m = m; done(true); },
          (e) => done(String(e)));""") is True


@pytest.fixture(scope="module")
def shop(browser, served):
    """A shop this node's wallet runs, and an account to buy from it.

    The shop belongs to a wallet rather than to an account because of a rule in
    `swap.make_offer`: a shop answers only when its creator still holds it and
    the wallet answering holds that address. That rule is why the shopkeeper
    can be trusted with nobody at the keyboard, so this is the shape a shop
    has -- and it is also why the buyer has to be somebody else.
    """
    base, state, daemon = served
    keeper = daemon.rpc.call("getnewaddress")
    daemon.rpc.call("sendtoaddress", keeper, 12.0)
    _answered(state, daemon)

    stock = inscribe.plan(b"arcade" * 40, "image/png",
                          '{"name": "Browser shop piece"}')
    assert stock.chunks == 1, "one transaction, so the shop has one to look at"
    piece = _put(daemon, state.messaging.params, keeper, stock.payloads)[0]
    _landed(state, daemon, piece)

    terms = json.dumps({"name": "Browser shop",
                        "shop": {"node": state.ensure_identity()
                                 .public_bytes.hex(),
                                 "listings": [{"give": {"inscription": piece},
                                               "take": {"coins": str(PRICE)}}]}},
                       separators=(",", ":"))
    page = inscribe.plan(SHOP_PAGE, "text/html", terms)
    shop_txid = _put(daemon, state.messaging.params, keeper, page.payloads)[0]
    _landed(state, daemon, shop_txid)

    index = state.token_index(state.messaging)
    assert index.inscription(piece)["owner"] == keeper
    assert index.inscription(shop_txid)["creator"] == keeper == \
        index.inscription(shop_txid)["owner"], "make_offer insists on both halves"

    # The answer to an order is paid for out of this node's own wallet, and the
    # coins it picks there decide whether anybody can read what it sent:
    # `generate` mines pay-to-pubkey coinbase, and the decoder refuses to
    # attribute a pubkey input, so an order answered from a coinbase goes
    # unanswered as far as everyone can see. Ordinary sends make an answer
    # readable -- one per answer, since the change of the first may land
    # elsewhere.
    answerer = funded_address(daemon.rpc, prefer=state.derived_address)
    for _ in range(2):
        daemon.rpc.call("sendtoaddress", answerer, 3.0)
        _answered(state, daemon)
    assert funded_address(daemon.rpc, prefer=state.derived_address) == answerer

    # A shopkeeper's first pass parks its cursor: whatever was in the inbox
    # before there was a shopkeeper was not an order to it.
    assert Shopkeeper(state).tick() == 0

    _page(browser, base)
    made = browser.execute_async_script("""
        const done = arguments[3];
        window.w.signUp(arguments[0], arguments[1],
                        {network: "regtest", version: arguments[2]})
          .then((r) => done({tag: r.tag, address: r.address}),
                (e) => done({error: String(e.message || e)}));""",
        "shopper", PASSWORD, state.messaging.params.pubkeyhash_version)
    assert "error" not in made, made

    # A shop buy pays for three transactions on top of its price, and the trade
    # holds the coins it spends as committed until the shop lands them. So the
    # message that carries the signature has to be paid for out of a coin the
    # trade never touched: one pile to buy with, and one it has no reason to
    # reach for (D-051).
    for pile in (5.0, 0.5):
        daemon.rpc.call("sendtoaddress", made["address"], pile)
        _answered(state, daemon)
    _catch_up(state)
    with contextlib.closing(index.open()) as db:
        assert len(utxos.unspent(db, made["address"])) >= 2, \
            "two outputs, or the buy is refused before anything is paid for"

    return {"base": base, "state": state, "daemon": daemon, "shop": shop_txid,
            "piece": piece, "keeper": keeper, **made}


def _shop_view(browser, shop):
    """The shop page as a stranger's browser sees it, and its sandboxed frame."""
    browser.get(f"{shop['base']}/inscriptions/{shop['shop']}/view")
    frames = browser.find_elements(By.CSS_SELECTOR, "iframe.inscription-frame")
    assert frames, "the shop is an HTML inscription and it drew no frame"
    return browser.page_source, frames[0]


# --- what the page says, before anybody buys ----------------------------------

def test_the_shop_page_names_no_address_of_the_node_serving_it(browser, shop):
    """It used to. The viewer page read this node's wallet for the buttons
    around the frame and printed a balance, plus a "split this address"
    suggestion with one of its addresses in the sentence. On a public instance
    the wallet that buys is the one in the browser, so none of it belongs on
    the page and none of it is said.
    """
    html, _frame = _shop_view(browser, shop)
    held = {row[0] for group in shop["daemon"].rpc.call("listaddressgroupings")
            for row in group}
    held.discard(shop["keeper"])      # the seller, and by name: it IS the shop
    shown = [where for where in held if where in html]
    assert shown == [], f"a stranger's page holds this node's addresses: {shown}"
    assert shop["state"].derived_address not in html
    assert 'action="/exchange/offer"' not in html, \
        "and it is not offered a form this instance refuses"


def test_the_account_is_told_it_is_the_one_answering(browser, shop):
    """`mine` is a refusal for an account rather than an owner's toolkit, and
    the page has to know which it got: an approval window is coming for a
    wallet and for nobody else.
    """
    base, state = shop["base"], shop["state"]
    _page(browser, base)
    said = browser.execute_async_script("""
        const done = arguments[2];
        (async () => {
          try {
            const wallet = await window.w.opened({network: "regtest",
                                                  version: arguments[0]});
            done(await window.w.shopDoor(wallet, arguments[1], {op: "shop"}));
          } catch (e) { done({error: String(e.message || e)}); }
        })();""", state.messaging.params.pubkeyhash_version, shop["shop"])
    assert "error" not in said, said
    assert said["account"] is True, "a key answered, not a wallet"
    assert said["mine"] is False and said["can_buy"] is True
    assert said["seller"] == shop["keeper"] and said["buyer"] == shop["address"]
    assert said["listings"][0]["give"]["txid"] == shop["piece"]
    assert int(said["listings"][0]["take"]["sats"]) == PRICE * COIN
    assert len(said["stamp"]) == 16, "the browser checks this before it seals"


def test_a_trade_in_flight_holds_the_automatic_refresh_off(browser, shop):
    """Every page of this arcade reloads itself when the node has something
    new to show -- and during a buy the node has something new every block,
    because the buy is what is making them. Both halves are right and the page
    in the frame is the one that gets hurt: it is what watches for the shop's
    answer, so the reload took the watch, and the rest of the buy, with it.
    A swap message crossing the bridge is the notice that a trade is being
    carried, and it holds the reload off until the page stops asking.
    """
    _html, frame = _shop_view(browser, shop)
    browser.switch_to.frame(frame)
    browser.execute_script("window.parent.postMessage("
                           "{arcade: 'swap', op: 'shop', seq: 1}, '*');")
    browser.switch_to.default_content()
    assert browser.execute_script(
        "return document.body.dataset.trading || null;") == "yes", \
        "nothing holds the refresh off, so a block lands and the buy dies"


# --- the buy ------------------------------------------------------------------

def test_the_browser_buys_from_the_shop(browser, shop):
    """Three transactions, one approval short of the wallet's version.

    The order, the buyer's half signed in this tab, and the message carrying
    that signature onto a trade this node cannot finish. Blocks between every
    one of them, so the chain is moved by a thread for the length of the buy:
    everything but the signature rides a message, and a message is in a block
    or it is nowhere.
    """
    _html, frame = _shop_view(browser, shop)
    browser.switch_to.frame(frame)
    browser.execute_script("document.getElementById('b').click();")

    stop, trouble = threading.Event(), []
    mover = threading.Thread(target=_keep_the_chain_moving,
                             args=(shop["state"], shop["daemon"], stop, trouble),
                             daemon=True)
    mover.start()
    out = None
    deadline = time.time() + 300
    try:
        while time.time() < deadline:
            out = browser.execute_script("return window.out || null;")
            if out:
                break
            time.sleep(0.5)
    finally:
        stop.set()
        mover.join(timeout=30)
    browser.switch_to.default_content()

    assert out, f"the buy never answered; the node's side: {trouble}"
    assert "error" not in out, f"{out}\nthe node's side: {trouble}"
    assert len(out["txid"]) == 64, out
    assert any("your own key" in line for line in out["steps"]), out["steps"]

    state = shop["state"]
    _answered(state, shop["daemon"])
    index = state.token_index(state.messaging)
    assert index.inscription(shop["piece"])["owner"] == shop["address"], \
        "the piece moved to the key that signed for it"

    # And the seller is made whole: its own output back, plus the price, which
    # is the arithmetic `countersign` refuses to sign without.
    decoded = shop["daemon"].rpc.call("decoderawtransaction",
                                      shop["daemon"].rpc.call("getrawtransaction",
                                                              out["txid"]))
    paid = [each for each in decoded["vout"]
            if shop["keeper"] in ((each.get("scriptPubKey") or {})
                                  .get("addresses") or [])]
    assert paid, decoded["vout"]
    assert int(round(float(paid[0]["value"]) * COIN)) >= PRICE * COIN, decoded


def test_the_shop_signed_it_and_this_node_asked_nobody(shop):
    """Nothing was decided on the account's behalf: there is no approval in
    the book with this shop's page on it, because there was nothing to
    approve -- the signature was the decision, and it was given in the tab.
    """
    state, daemon = shop["state"], shop["daemon"]
    _answered(state, daemon)
    assert daemon.rpc.call("getrawmempool") == [], "everything landed"
    asked = [row for row in state.approvals.pending(state.messaging.network)
             if row.get("page") == shop["shop"]]
    assert asked == [], "an account's buy files nothing to approve"
