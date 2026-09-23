"""The seller's screen: a piece goes up for sale and nothing is broadcast.

Everything under this page is tested where it belongs -- the route pair in
`tests/test_account_list.py`, this browser's reading of a leg in
`tests/test_coins_browser.py`. What is only reachable from the page is the
seam between them: that the two signatures a seller hands over are made over
the digests the seller was SHOWN, and that putting a piece up for sale moves
nothing on the chain.

That seam is worth a whole file because a listing is the one thing an account
does that leaves a signature on a server. Everything else an account signs is
broadcast in the same breath and spent, so a wrong signature is a transaction
the network refuses. A leg is kept, and a wrong signature on it is a row that
reads as a sale, advertises a piece nobody can buy, and says so on a public
page until the piece is spent somewhere else.
"""

import base64
import pathlib
import socket
import sys
import threading
import time

import pytest

pytest.importorskip("selenium",
                    reason="browser tests need selenium: pip install .[dev]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from selenium.common.exceptions import WebDriverException        # noqa: E402
import browsers                                                  # noqa: E402

from arcade import funding                                       # noqa: E402
from arcade.script import iter_pushes                            # noqa: E402
from arcade.txbuild import build_raw_tx, p2pkh_script            # noqa: E402

COIN = 100_000_000
PRICE = "1"
PRICE_SATS = COIN

#: Read the way `execute_script` wants it: a script with no `return` hands back
#: None, which is not False, and a panel that opened reads as one that never
#: did. Every other read in this file says `return` for the same reason.
CONFIRM = "return document.getElementById('list-confirm').hidden"


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
    import uvicorn
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    class Pointed(ChainContext):
        def credentials(self):
            return regtest.rpc._creds

        @property
        def params(self):
            return regtest.params

    home = tmp_path_factory.mktemp("listpage")
    chain = Pointed(network="regtest", role="messaging", label="Testnet",
                    datadir=regtest.datadir)
    state = AppState(home=home, messaging=chain,
                     ledger=ChainContext(network="main", role="ledger",
                                         label="Mainnet",
                                         datadir=pathlib.Path("/nonexistent")))
    (home / "tokens-chain").write_text("regtest\n")
    regtest.rpc.call("generate", 120)

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
    yield f"http://127.0.0.1:{port}", state, regtest
    server.should_exit = True
    thread.join(timeout=5)
    assert not thread.is_alive(), "the server is still serving a connection"


def _catch_up(state):
    """Index every block the node has before the test reads anything.

    As in `tests/test_send_browser.py`, for the reason written there: `sync()`
    answers None when another thread is already mid-pass, which is not the
    same as being caught up, and reading it as caught up is what made browser
    tests pass alone and fail in a full run.
    """
    index = state.token_index(state.messaging)
    tip = 0
    for _ in range(200):
        with state.messaging.rpc() as rpc:
            tip = rpc.get_block_count()
        if index.status(tip)["current"]:
            return index
        if index.stopped is not None:
            raise AssertionError(f"the index stopped: {index.stopped}")
        index.sync(max_blocks=500)
        time.sleep(0.05)
    raise AssertionError(f"the index will not catch up: {index.status(tip)}")


def _quiet(state, seconds=2.0):
    """Wait until the node stops moving, so nothing reloads the page mid-test.

    `base.html` polls `/events` and reloads when the generation moves, and the
    generation moves when the watcher indexes a block -- minutes after the RPC
    call that mined it, and independently of this test. Every browser file here
    has to live with that; most of them only hold a wallet, which `opened` can
    hand out again after a reload. This one holds the buttons on a page, so it
    waits for the reloading to be over instead of chasing the handle.
    """
    stop = time.time() + 120
    while time.time() < stop:
        was = state.generation
        time.sleep(seconds)
        if state.generation == was:
            time.sleep(seconds)
            if state.generation == was:
                return
    raise AssertionError("the node never stopped moving")


def _keys(browser, chain):
    """The open wallet, on the page that is loaded right now.

    `wallet.opened` rebuilds the keys from the words this tab remembers, which
    is what every page here does after a navigation. The test needs the same
    handle the page has, so it asks the same module the same way -- and asks
    again if the document went away while it was asking, which is the one
    failure mode a page that reloads itself has.
    """
    stop = time.time() + 60
    while time.time() < stop:
        try:
            got = browser.execute_async_script("""
                const done = arguments[arguments.length - 1];
                import("/wallet.js").then((m) => { window.w = m;
                                                 return m.opened(arguments[0]); })
                  .then((wallet) => { if (wallet) window.wallet = wallet;
                                      done(!!wallet); },
                        (e) => done(String(e)));""", chain)
            if got is True:
                return
        except WebDriverException:
            pass        # reloaded underneath us; the next document is the one
        time.sleep(0.2)
    raise AssertionError("this tab has no open wallet left to sign with")


def _rows(state):
    return state.listings.open_listings("regtest")


def _show(browser, id_, key="textContent"):
    return browser.execute_script(
        "return document.getElementById(arguments[0])[arguments[1]]", id_, key)


def _type(browser, id_, value):
    browser.execute_script(
        "document.getElementById(arguments[0]).value = arguments[1]", id_, value)


def _click(browser, id_):
    assert browser.execute_script(
        "const b = document.getElementById(arguments[0]);"
        "if (!b) return false; b.click(); return true;", id_)


def _press_on_the_card(browser, words):
    """Press a card's own button, by the words on it.

    The cards are built in a loop and their buttons carry no ids, which is how
    the page is written; the words are what a person reads, so they are what
    this looks for. A renamed button fails here rather than quietly leaving a
    stale panel open from the last test.
    """
    found = browser.execute_script("""
        const want = arguments[0];
        for (const b of document.querySelectorAll("button")) {
          if (b.textContent.trim() === want) { b.click(); return true; }
        }
        return false;""", words)
    assert found, f"no button on the page reads {words!r}"


def _trouble(browser):
    return _show(browser, "trouble") or ""


def _wait(browser, said, seconds=60.0):
    """Wait for something the page does on its own, watching for its refusal.

    A lost document is not a failure here -- it is the page reloading itself,
    after which the answer arrives from the new one. A refusal is, and it is
    raised with the page's own words rather than as a timeout.
    """
    stop = time.time() + seconds
    while time.time() < stop:
        try:
            if said(browser):
                return
            complained = _trouble(browser)
            if complained:
                raise AssertionError(f"the page refused: {complained}")
        except AssertionError:
            raise
        except WebDriverException:
            pass
        time.sleep(0.05)
    raise AssertionError("the page never got there")


def _refused(browser, seconds=60.0):
    """Wait for the page to say no, and hand back what it said.

    `_wait` cannot be used for this: its safety net is that a refusal means the
    test went wrong, and here the refusal is the whole point. Reading the line
    once per go is also the only way to watch for it -- read it twice and the
    first read is still empty while the second one has the words in it, which
    looks exactly like a page that refused for no reason.
    """
    stop = time.time() + seconds
    while time.time() < stop:
        try:
            said = _trouble(browser)
            if said:
                return said
        except WebDriverException:
            pass
        time.sleep(0.05)
    raise AssertionError("the page neither refused nor agreed")


def _shown(browser, piece):
    """Ask this tab for the same leg again, and for its own reading of it.

    Nothing is signed by this. `offerListing` spends nothing to build a leg and
    `checkedListing` is arithmetic on the bytes that came back -- and the leg
    comes back the same because the same two coins are unspent, which is why
    `raw` is compared against the filed row below.
    """
    return browser.execute_async_script("""
        const done = arguments[2];
        (async () => {
          try {
            const coins = await import("/coins.js");
            const leg = await window.w.offerListing(arguments[0], arguments[1],
                                                    "regtest");
            const keys = window.w.keysOn(window.wallet, "regtest");
            const seen = await window.w.checkedListing(leg, window.wallet);
            const sigs = [];
            for (const h of seen.hashes)
              sigs.push(coins.hex(await coins.signInput(
                keys.key, coins.unhex(h), coins.SINGLE_ANYONECANPAY)));
            done({raw: leg.raw, hashes: seen.hashes, sigs: sigs,
                  node: leg.sighashes, signs: seen.signs,
                  pub: coins.hex(keys.pubkey),
                  price: Number(seen.listing.sats)});
          } catch (e) { done({error: String(e.message || e)}); }
        })();""", piece, PRICE)


_ASK = """
    const done = arguments[1];
    (async () => {
      try {
        const asked = await fetch("/account/inscribe", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({content: arguments[0],
                                content_type: "text/plain",
                                name: "a note", chain: "regtest"}),
        });
        const offer = await asked.json();
        if (!asked.ok) throw new Error(offer.detail || "no offer");
        await window.w.checked(offer, window.wallet);
        window.__offer = offer;
        done({asked: offer.raw});
      } catch (e) { done({error: String(e.message || e)}); }
    })();"""

_SIGN = """
    const done = arguments[0];
    window.w.confirm(window.wallet, window.__offer).then(
      (out) => done({txid: out.txid}),
      (e) => done({error: String(e.message || e)}));"""


def _inscribe(browser, state, node, where, content):
    """Put one piece on the chain from this tab, and hand back its txid.

    Two scripts, because only the second of them costs anything. The first
    fetches an offer and reads it the way a page would -- fetches and
    arithmetic over bytes -- so a document that goes away underneath it can be
    asked again and get the same answer. The second signs it and broadcasts,
    and a blind second attempt at that would put two pieces on the chain out
    of one coin. So it runs only once the node has stopped moving -- the
    moving is what reloads an open page -- and it is retried only after the
    mempool has proved that nothing got out.
    """
    for attempt in (1, 2):
        _quiet(state)
        _keys(browser, where)
        asked = browser.execute_async_script(_ASK, content)
        assert "error" not in asked, asked
        before = set(node.rpc.call("getrawmempool"))
        try:
            out = browser.execute_async_script(_SIGN)
        except WebDriverException:
            if set(node.rpc.call("getrawmempool")) != before:
                raise AssertionError(
                    "the tab broadcast the inscribe and then went away, and "
                    "asking it again would inscribe the same piece twice")
            assert attempt == 1, "the tab will not hold still to sign"
            continue
        assert "error" not in out, out
        return out["txid"]


@pytest.fixture(scope="module")
def seller(browser, served):
    """An account holding one piece it inscribed itself, and two coins.

    Two coins is not a preference. A leg that names what it sells signs twice
    -- one input stands over the bytes naming the piece, the other over the
    price -- so the address needs two of them, and an inscription leaves its
    holder with exactly one: a Class C inscribe spends the coin it was funded
    with and pays the change back to the same address. Hence the second
    `sendtoaddress` below, which is the same advice the page gives when it
    refuses: send yourself a little change and list it again.
    """
    base, state, node = served
    # The 120 blocks in `served` are still to be indexed, and each one the
    # watcher finds bumps the generation an open page reloads on. Index them
    # and let that settle before there is a document to reload, or the refresh
    # lands inside a script that is still waiting for its own fetch.
    _catch_up(state)
    _quiet(state)
    browser.get(f"{base}/join")
    browser.set_script_timeout(120)
    assert browser.execute_async_script("""
        const done = arguments[arguments.length - 1];
        import("/wallet.js").then((m) => { window.w = m; done(true); },
                                  (e) => done(String(e)));""") is True
    made = browser.execute_async_script("""
        const done = arguments[3];
        window.w.signUp(arguments[0], arguments[1],
                        {network: "regtest", version: arguments[2]})
          .then((r) => { window.wallet = r.wallet;
                         done({tag: r.tag, address: r.address}); },
                (e) => done({error: String(e.message || e)}));""",
        f"seller{int(time.time() * 1000) % 100000}",
        "a long enough password", state.messaging.params.pubkeyhash_version)
    assert "error" not in made, made
    node.rpc.call("sendtoaddress", made["address"], 10.0)
    node.rpc.call("generate", 1)
    _catch_up(state)
    where = {"network": "regtest",
             "version": state.messaging.params.pubkeyhash_version}

    piece = _inscribe(browser, state, node, where,
                      base64.b64encode(b"the piece this page sells").decode())
    node.rpc.call("generate", 1)
    _catch_up(state)

    # The second coin, so there are two to sign with. From the node's own
    # wallet rather than from the account's, because the account's is the one
    # the inscribe above just swallowed.
    node.rpc.call("sendtoaddress", made["address"], 1.0)
    node.rpc.call("generate", 1)
    _catch_up(state)

    held = state.token_index(state.messaging).inscriptions(
        owner=made["address"], limit=20)
    assert any(row["txid"] == piece for row in held), \
        "the piece never arrived in this account's own list"

    # Nothing is mining now, but the node is still catching up with what did,
    # and every block it indexes reloads an open page. Load the page only once
    # that has stopped, or the reload lands between a button and its handler.
    # From here on nothing mines at all, so the document the tests below hold
    # onto is the one they finish in.
    _quiet(state)
    browser.get(f"{base}/me/nfts")
    _wait(browser, lambda d: d.execute_script(
        "return document.body.dataset.nftsReady") == "yes")
    _keys(browser, where)
    return browser, base, state, node, made, piece


def _panel(browser):
    """Press the card's listing button, and check the panel that opens."""
    _press_on_the_card(browser, "Sell it without you")
    assert _show(browser, "listing", "hidden") is False
    assert _show(browser, "list-confirm", "hidden") is True
    return _show(browser, "listing-what")


def test_the_card_offers_to_sell_the_piece_without_the_seller(browser, seller):
    browser, base, state, node, made, piece = seller
    heading = _panel(browser)
    assert "#" in heading and "without you" in heading, heading
    # The words on this panel are the whole disclosure of what a listing is,
    # so the two things a seller could be sorry about afterwards are both
    # said: it cannot be unsent, and spending the piece is what ends it.
    body = browser.execute_script(
        "return document.getElementById('listing').textContent")
    assert "cannot be called back" in body, body
    assert "spends it" in body, body


def test_reading_it_back_shows_this_tabs_own_sentence(browser, seller):
    """What is printed before the key is used came out of the leg's bytes.

    `leg.what` is a sentence the node wrote beside the transaction, which is
    why it is nowhere on this page. What is printed is the price, the piece
    and the change, as this tab worked them out of the outputs.
    """
    browser, base, state, node, made, piece = seller
    _panel(browser)
    _type(browser, "list-price", PRICE)
    _click(browser, "list-it")
    _wait(browser, lambda d: d.execute_script(CONFIRM) is False)
    says = _show(browser, "list-says")
    assert f"{PRICE}.00000000" in says, says
    assert piece[:16] in says, says
    assert "comes back to you" in says, says
    assert "reserved for the fee" in says, says
    assert _rows(state) == [], "reading signs nothing, so nothing was filed"
    assert node.rpc.call("getrawmempool") == [], "and nothing was broadcast"


def test_the_button_signs_over_the_digests_the_page_showed(browser, seller):
    """The claim the design rests on, checked at the page it is made at.

    Three things, and the third is the reason for the first two:

      * the leg this browser derived its digests from is byte for byte the leg
        that ended up in the node's book, so the digests are the ones the page
        was shown rather than a recomputation of something similar;
      * those digests are what `funding.sighash` produces from the filed row's
        own bytes, which is the chain's arithmetic and not this node's say-so;
      * the signature on each input of that row is what signing those digests
        with this account's key produces. Signing is deterministic here
        (RFC 6979), which is what makes the third one a check rather than a
        tautology.

    A leg that names a piece carries one signature per input, each standing
    over the output at its own index, so two digests and two signatures are
    what it has to have -- and both are checked, because the one over the
    payment alone promises coins and not the piece.
    """
    browser, base, state, node, made, piece = seller
    _panel(browser)
    _type(browser, "list-price", PRICE)
    _click(browser, "list-it")
    _wait(browser, lambda d: d.execute_script(CONFIRM) is False)
    _click(browser, "list-sign")
    _wait(browser, lambda d: len(_rows(state)) == 1)

    news = _show(browser, "news")
    assert "Listed" in news and "Nothing was broadcast" in news, news
    assert node.rpc.call("getrawmempool") == [], "a listing is not a broadcast"

    row = _rows(state)[0]
    assert row["owner"] == made["address"]
    assert row["price"] == PRICE_SATS
    assert row["status"] == "open"
    assert piece in row["payload"], "the row does not name this piece"

    shown = _shown(browser, piece)
    assert "error" not in shown, shown
    assert shown["price"] == PRICE_SATS
    assert shown["signs"] == {"from": 0, "of": 2}, "a named leg signs twice"
    assert shown["hashes"] == shown["node"], \
        "the tab took the node's digests instead of working its own out"
    assert len(set(shown["hashes"])) == 2, "one digest for two inputs"

    with state.messaging.rpc() as rpc:
        decoded = rpc.call("decoderawtransaction", row["leg"])
    outs = [(int(round(float(o["value"]) * COIN)),
             bytes.fromhex(o["scriptPubKey"]["hex"])) for o in decoded["vout"]]
    ins = [{"txid": v["txid"], "vout": v["vout"]} for v in decoded["vin"]]
    assert build_raw_tx([(i["txid"], i["vout"]) for i in ins], outs) \
        == shown["raw"], "the leg the page read is not the leg that was filed"

    script = p2pkh_script(made["address"])
    mine = [funding.sighash(ins, outs, n, script,
                            sighash_type=funding.SINGLE_ANYONECANPAY).hex()
            for n in range(len(ins))]
    assert mine == shown["hashes"], "the digests are not the chain's own"

    for n, spent in enumerate(decoded["vin"]):
        pushes = iter_pushes(bytes.fromhex(spent["scriptSig"]["hex"]))
        # The last byte of a scriptSig's first push is the sighash type, and
        # the last two characters of the hex the tab handed back are the very
        # same byte.
        assert pushes[0][:-1].hex() == shown["sigs"][n][:-2], \
            f"input {n} is signed over some other transaction"
        assert pushes[0][-1] == funding.SINGLE_ANYONECANPAY
        assert bytes(pushes[1]).hex() == shown["pub"], \
            "the key on the leg is not this account's"


def test_pressing_no_signs_nothing_and_files_nothing(browser, seller):
    browser, base, state, node, made, piece = seller
    before = len(_rows(state))
    _panel(browser)
    _type(browser, "list-price", PRICE)
    _click(browser, "list-it")
    _wait(browser, lambda d: d.execute_script(CONFIRM) is False)
    _click(browser, "list-no")
    assert _show(browser, "list-confirm", "hidden") is True
    assert len(_rows(state)) == before, "the panel filed a listing nobody signed"
    assert node.rpc.call("getrawmempool") == []


def test_no_price_is_refused_on_the_page_not_by_a_dead_button(browser, seller):
    browser, base, state, node, made, piece = seller
    before = len(_rows(state))
    _panel(browser)
    _type(browser, "list-price", "")
    _click(browser, "list-it")
    # Said by the node, which is the first place a blank can be noticed for
    # certain: the page has no price to read until the leg comes back, and a
    # leg is exactly what a blank must not be allowed to build.
    said = _refused(browser)
    assert "enter an amount" in said, said
    assert _show(browser, "list-confirm", "hidden") is True, \
        "a refusal that still offers the signing button is a trap"
    assert len(_rows(state)) == before
    assert node.rpc.call("getrawmempool") == []
