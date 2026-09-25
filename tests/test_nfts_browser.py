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
from selenium.common.exceptions import WebDriverException        # noqa: E402
from selenium.webdriver.common.by import By                      # noqa: E402
from selenium.webdriver.support import expected_conditions as ec  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait          # noqa: E402
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

    Nothing is signed by this, and it has to be asked before the leg is filed:
    `offerListing` spends nothing to build a leg, `checkedListing` is arithmetic
    on the bytes that came back, and the leg comes back the same because the
    same two coins are unspent and uncommitted -- which stops being true the
    moment `/account/list/sign` notes them as committed to the row. `raw` is
    what gets compared against the filed row below.
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
    """An account holding one piece it inscribed itself, and coins to spare.

    Two coins is the floor, not the amount. A leg that names what it sells
    signs twice -- one input stands over the bytes naming the piece, the other
    over the price -- so the address needs two of them, and an inscription
    leaves its holder with exactly one: a Class C inscribe spends the coin it
    was funded with and pays the change back to the same address.

    Funded with more than the floor because this is one account and five tests,
    and the one test that FILES a listing retires the two coins that leg stands
    over for the rest of the module (`Flights.note_committed` -- nothing this
    account broadcasts ever spends them, the buyer's transaction does, maybe in
    a week). An account with exactly two coins can answer the first test that
    opens the panel and refuses every one after it, which is a fact about the
    coin book rather than about the page. The payments come from the node's own
    wallet, not from the account's, because the account's is the one the
    inscribe above swallowed.
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

    # Coins enough that more than one test can ask for a leg. Two is what a
    # leg needs; a filed listing spends this account's claim on the pair it
    # stood over for as long as it stands, so the piles below are what the rest
    # of the module has to work with. Each is larger than PRICE plus a fee --
    # the largest free coin is the one that pays the price -- and each smaller
    # than the inscribe's change, so the first leg any test asks for is built
    # out of the change and the first of these, the way it was before there
    # were extra piles.
    for _ in range(3):
        node.rpc.call("sendtoaddress", made["address"], 2.0)
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

    The leg is read again BEFORE the button is pressed, not afterwards. Filing
    a listing retires the two coins it stands over as far as this account is
    concerned -- `Flights.note_committed`, because the transaction that spends
    them is a stranger's and may never be broadcast at all -- so a derivation
    asked for once the row exists is a request for a second listing out of
    coins already promised to the first, and the node answers it with a refusal
    that is correct. Asked for here, before the key is used, the same leg comes
    back for the same reason it did when the page asked: nothing in between
    spends anything.
    """
    browser, base, state, node, made, piece = seller
    _panel(browser)
    _type(browser, "list-price", PRICE)
    _click(browser, "list-it")
    _wait(browser, lambda d: d.execute_script(CONFIRM) is False)
    shown = _shown(browser, piece)
    assert "error" not in shown, shown
    _click(browser, "list-sign")
    _wait(browser, lambda d: len(_rows(state)) == 1)
    # Wait for the sentence, do not read it once. The row is in the node's book
    # a moment before this tab's fetch comes back and writes what it did, so a
    # single read lands between the two and comes back empty -- which is what
    # this assertion printed for a while. `_wait` watches #trouble on the way,
    # so a signing that threw arrives as the page's own words rather than as a
    # blank.
    _wait(browser, lambda d: "Listed" in _show(d, "news"))

    news = _show(browser, "news")
    assert "Listed" in news and "Nothing was broadcast" in news, news
    assert node.rpc.call("getrawmempool") == [], "a listing is not a broadcast"

    row = _rows(state)[0]
    assert row["owner"] == made["address"]
    assert row["price"] == PRICE_SATS
    assert row["status"] == "open"
    assert piece in row["payload"], "the row does not name this piece"

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


# --- the three sizes, on the box that makes the bytes permanent ----------------
# Messages were given Large / Medium / Small on 2026-09-13 (`arcadePick` in
# `base.html`). Inscribe never was, and that is the whole reason a phone photo
# met "that is 41 pieces in one item" on test.dogecoinarcade.com on 2026-09-24:
# this panel offers ONE transaction, and the only thing that lets a picture fit
# inside one is re-encoding it here, in the tab, before anything is uploaded.
#
# What these tests check is the seam the copy depends on -- that the bytes the
# tab goes on to pay for are the smaller bytes it measured, and not the file as
# it came off the disk. A page that prints "0.9 pieces, 0.1 coins" while
# uploading the original would pass every server-side test there is.

MAKE_A_PHOTO = """
const done = arguments[0];
const c = document.createElement('canvas');
c.width = 2400; c.height = 1600;
const g = c.getContext('2d');
// Noise, not flat colour: a flat image compresses to nothing and every size
// would fit in one piece.
const img = g.createImageData(c.width, c.height);
for (let i = 0; i < img.data.length; i += 4) {
  img.data[i] = (i * 7) % 255; img.data[i+1] = (i * 13) % 255;
  img.data[i+2] = (i * 29) % 255; img.data[i+3] = 255;
}
g.putImageData(img, 0, 0);
c.toBlob(function (blob) {
  const input = document.getElementById('file');
  const dt = new DataTransfer();
  dt.items.add(new File([blob], 'photo.png', {type: 'image/png'}));
  input.files = dt.files;
  input.dispatchEvent(new Event('change'));
  done(blob.size);
}, 'image/png');
"""

READ_THE_BOX = """
const buttons = [...document.querySelectorAll('#picked-sizes button')];
const input = document.getElementById('file');
return {
  labels: buttons.map(b => b.textContent.replace(/\\s+/g, ' ').trim()),
  chosen: buttons.filter(b => b.classList.contains('on'))
                 .map(b => b.querySelector('strong').textContent),
  says: document.getElementById('picked-text').textContent,
  name: input.files[0] ? input.files[0].name : null,
  type: input.files[0] ? input.files[0].type : null,
  bytes: input.files[0] ? input.files[0].size : null,
};
"""


def _sizes(browser, script, want=3, seconds=60.0):
    """Wait for the three buttons, since measuring them takes a moment."""
    stop = time.time() + seconds
    while time.time() < stop:
        state_ = browser.execute_script(script)
        if len(state_["labels"]) == want:
            return state_
        time.sleep(0.5)
    raise AssertionError(f"the page offered {state_['labels']!r}, not {want}")


def test_the_inscribe_box_offers_the_three_sizes(browser, seller):
    browser, base, state, node, made, piece = seller
    browser.set_script_timeout(60)
    original = browser.execute_async_script(MAKE_A_PHOTO)
    assert original > 200_000, "the test photo has to be worth shrinking"

    box = _sizes(browser, READ_THE_BOX)
    assert [label.split()[0] for label in box["labels"]] == \
        ["Large", "Medium", "Small"], box["labels"]
    assert box["chosen"] == ["Medium"], "Medium is the sensible default"

    # The form carries the measured one, which is the only claim above it that
    # a person cannot check for themselves.
    assert box["bytes"] < original / 2, (box["bytes"], original)
    assert box["name"] == "photo.jpg" and box["type"] == "image/jpeg"

    # Each button says what that choice costs, and how many transactions it
    # costs it in -- the pieces being the part a kilobyte figure hides.
    for label in box["labels"]:
        assert "·" in label, f"no size and cost on {label!r}"
    assert "pieces" in box["labels"][1], \
        "a many-piece choice that does not say so is the 41-pieces surprise"
    assert "re-encoded here at Medium" in box["says"], box["says"]


def test_choosing_a_size_for_an_inscription_can_be_undone(browser, seller):
    """Large has to mean the file as it was, or it is not offered."""
    browser, base, state, node, made, piece = seller
    browser.set_script_timeout(60)
    original = browser.execute_async_script(MAKE_A_PHOTO)
    _sizes(browser, READ_THE_BOX)

    browser.execute_script("""
        [...document.querySelectorAll('#picked-sizes button')]
          .filter(b => b.textContent.indexOf('Large') === 0)[0].click();""")
    time.sleep(0.4)
    large = browser.execute_script(READ_THE_BOX)
    assert large["chosen"] == ["Large"]
    assert large["bytes"] == original, "Large is the file as it was"
    assert large["name"] == "photo.png"
    assert "re-encoded" not in large["says"], large["says"]

    browser.execute_script("""
        [...document.querySelectorAll('#picked-sizes button')]
          .filter(b => b.textContent.indexOf('Small') === 0)[0].click();""")
    time.sleep(0.4)
    small = browser.execute_script(READ_THE_BOX)
    assert small["chosen"] == ["Small"]
    assert small["bytes"] < large["bytes"] / 4


MAKE_A_FOLDER = """
const done = arguments[0];
const input = document.getElementById('build');
// Firefox will not take a directory from a script, and the node rebuilds the
// layout from the NAMES alone (`app._save_upload` keeps `Path(name).name` and
// routes on the suffix), so a flat list of correctly-named files is the same
// build to it. Stripping the folder attribute is how the live acceptance
// driver sends one, too.
input.removeAttribute('webkitdirectory');
input.removeAttribute('directory');
input.multiple = true;
const dt = new DataTransfer();
const meta = JSON.stringify([1, 2, 3].map(n => ({name: 'Pic ' + n,
                                                image: 'images/' + n + '.png',
                                                edition: n})));
dt.items.add(new File([meta], '_metadata.json', {type: 'application/json'}));
const sizes = {};
let left = 3;
function picture(n) {
  const c = document.createElement('canvas');
  // 64x64 of REAL noise, which is the only picture that satisfies the two
  // things this test needs at once. A run puts one item in one transaction and
  // an item cannot be more than one piece (7,628 bytes), so the folder has to
  // be over that as it stands and under it once re-encoded -- and a JPEG of
  // random pixels is a few kilobytes while its PNG is some fifteen, because
  // PNG has nothing to say about randomness. (A periodic pattern, which is what
  // `(i * 7) % 255` is, PNGs to almost nothing: a 48x48 tile of it is 1.3 KB
  // and there would be no choice left to test.) A phone photograph is over a
  // piece at every size this page offers, which is the other step's problem.
  c.width = 64; c.height = 64;
  const g = c.getContext('2d');
  const img = g.createImageData(c.width, c.height);
  for (let i = 0; i < img.data.length; i += 4) {
    img.data[i] = Math.floor(Math.random() * 256);
    img.data[i+1] = Math.floor(Math.random() * 256);
    img.data[i+2] = Math.floor(Math.random() * 256);
    img.data[i+3] = 255;
  }
  g.putImageData(img, 0, 0);
  c.toBlob(function (blob) {
    sizes[n] = blob.size;
    dt.items.add(new File([blob], String(n) + '.png', {type: 'image/png'}));
    if (--left) return;
    input.files = dt.files;
    input.dispatchEvent(new Event('change'));
    done(sizes);
  }, 'image/png');
}
for (let n = 1; n <= 3; n++) picture(n);
"""

READ_THE_FOLDER = """
const buttons = [...document.querySelectorAll('#build-sizes button')];
return {
  labels: buttons.map(b => b.textContent.replace(/\\s+/g, ' ').trim()),
  chosen: buttons.filter(b => b.classList.contains('on'))
                 .map(b => b.querySelector('strong').textContent),
  says: document.getElementById('picked-build-text').textContent,
};
"""


def _uploads(state):
    """The build folders this node has been sent, as they stand right now."""
    return {one.parent for one in (state.home / "collections").glob("*/images")}


def test_a_folder_goes_up_at_the_size_chosen(browser, seller):
    """The collection box converts on the way up, and the build still reads.

    Two halves, and the second is the one that could break quietly: the sizes
    are measured here rather than swapped into the input, so a converted
    picture arrives with a new suffix. A build is keyed by the STEM of its
    pictures (`collections.read_build`), so `1.jpg` is still item 1 -- and if
    that ever stopped being true, this upload would come back as "no image for
    edition 1" rather than inscribe forty small pictures.
    """
    browser, base, state, node, made, piece = seller
    browser.set_script_timeout(180)
    originals = browser.execute_async_script(MAKE_A_FOLDER)
    assert all(one > 7628 for one in originals.values()), \
        f"this art would go up as it stands, so the choice decides nothing: {originals}"
    before = _uploads(state)

    folder = _sizes(browser, READ_THE_FOLDER)
    assert [label.split()[0] for label in folder["labels"]] == \
        ["Large", "Medium", "Small"], folder["labels"]
    assert folder["chosen"] == ["Medium"], folder["labels"]
    for label in folder["labels"]:
        assert "·" in label, label
    assert "pieces" in folder["labels"][0], folder["labels"]
    assert "in dust and fees" in folder["says"], folder["says"]

    # Large is not the same number under a different label. At that size these
    # items are each more than a piece, and a run cannot carry an item that is
    # more than a piece -- so the page says it here, rather than letting the
    # node say it one upload later.
    browser.execute_script("""
        [...document.querySelectorAll('#build-sizes button')]
          .filter(b => b.textContent.indexOf('Large') === 0)[0].click();""")
    time.sleep(0.4)
    large = browser.execute_script(READ_THE_FOLDER)
    assert "more than a piece" in large["says"], large["says"]

    browser.execute_script("""
        [...document.querySelectorAll('#build-sizes button')]
          .filter(b => b.textContent.indexOf('Small') === 0)[0].click();""")
    time.sleep(0.4)

    _click(browser, "run-start")
    _wait(browser, lambda d: "Written down" in _show(d, "news"))
    news = _show(browser, "news")
    assert "3 items" in news, news

    (saved,) = _uploads(state) - before
    names = sorted(p.name for p in (saved / "images").iterdir())
    assert names == ["1.jpg", "2.jpg", "3.jpg"], names
    assert (saved / "json" / "_metadata.json").is_file()
    small = sum(p.stat().st_size for p in (saved / "images").iterdir())
    assert small < sum(originals.values()) / 2, \
        f"the folder went up at {small} against {sum(originals.values())}"


def test_a_folder_of_nothing_to_re_encode_is_offered_no_sizes(browser, seller):
    """Three buttons that all did the same thing would be a lie about it."""
    browser, base, state, node, made, piece = seller
    browser.set_script_timeout(60)
    browser.execute_async_script("""
        const done = arguments[0];
        const input = document.getElementById('build');
        input.removeAttribute('webkitdirectory');
        input.multiple = true;
        const dt = new DataTransfer();
        dt.items.add(new File(['[]'], '_metadata.json',
                              {type: 'application/json'}));
        dt.items.add(new File(['not a picture'], 'notes.txt',
                              {type: 'text/plain'}));
        input.files = dt.files;
        input.dispatchEvent(new Event('change'));
        done(true);""")
    # The trouble row still holds the refusal the listing tests asked for, and
    # `_wait` reads any line in it as this test going wrong. Nothing here spends
    # anything to clear it, so it is emptied the way the page's own handlers do.
    browser.execute_script("const t = document.getElementById('trouble');"
                           "t.textContent = ''; t.hidden = true;")
    _wait(browser, lambda d: _show(d, "picked-build", "hidden") is False)
    said = _show(browser, "picked-build-text")
    assert "nothing here to re-encode" in said, said
    assert _show(browser, "build-sizes", "hidden") is True, "buttons for nothing"


MAKE_A_NOTE = """
const input = document.getElementById('file');
const body = new Uint8Array(200);
for (let i = 0; i < body.length; i++) body[i] = (i * 7 + 11) % 251;
const dt = new DataTransfer();
dt.items.add(new File([body], 'a note.txt', {type: 'text/plain'}));
input.files = dt.files;
input.dispatchEvent(new Event('change'));
return true;
"""


def _receipt(browser):
    return browser.execute_script(
        "try { return sessionStorage.getItem('arcade.inscribed.regtest');"
        "} catch (e) { return null; }")


def _loaded(browser):
    _wait(browser, lambda d: d.execute_script(
        "return document.body.dataset.inscribeReady") == "yes")


def test_a_finished_inscribe_is_still_reported_when_the_page_comes_back(
        browser, seller):
    """The live failure of 2026-09-25, caught in a regtest page.

    There the file went on chain complete and the tab came back silent: the
    broadcast bumps the generation (`/account/sign`), the poller reloads the
    page over the news row, and the row only ever held the sentence for the
    seconds between them. What holds it now is a receipt in sessionStorage,
    which every load prints and no load drops until `/content` answers with
    the file's whole bytes -- which is the last piece's block seen from the
    page, and the exact moment the sentence stops being news.

    The test does not chase the poller's own reloads. Whenever the page
    happens to come back, the sentence is either already printed or this
    test's next refresh prints it; what is asserted is the three states,
    each in its turn: printed and kept while unmined, printed and dropped
    once mined, and finally quiet.
    """
    browser, base, state, node, made, piece = seller
    _quiet(state)
    browser.execute_script("const t = document.getElementById('trouble');"
                           "t.textContent = ''; t.hidden = true;")
    browser.execute_script(MAKE_A_NOTE)
    browser.find_element(By.ID, "inscribe-it").click()
    alert = WebDriverWait(browser, 60, poll_frequency=0.3).until(
        ec.alert_is_present())
    assert "a note.txt" in alert.text, alert.text
    alert.accept()
    _wait(browser, lambda d: bool(_show(d, "news").strip()))
    said = _show(browser, "news")
    assert said.startswith("Inscribed: a note.txt"), said

    kept = json.loads(_receipt(browser))
    assert kept["says"] == said, (kept, said)
    assert kept["chain"] == "regtest" and kept["bytes"] == 200, kept
    assert len(kept["root"]) == 64, kept

    # Nothing has been mined, so this load prints the sentence and keeps
    # the receipt: `/content` has no row to answer from yet.
    browser.refresh()
    _loaded(browser)
    assert _show(browser, "news") == said, "the page came back and forgot"
    assert _receipt(browser), "it is still on its way, so the receipt stays"

    node.rpc.call("generate", 1)
    _catch_up(state)
    _quiet(state)
    # Indexing that block bumps the generation, so the page may have
    # reloaded on its own already -- and that load would have printed the
    # sentence and dropped the receipt, `/content` answering whole by now.
    # But nothing mines by itself here, so this test must not assume the
    # poller's reload came: it waits a little for that arrival, and if the
    # page never came back on its own, it comes back because the test says
    # so. Either load is the one the live page gets from its watcher.
    stop = time.time() + 20
    landed = False
    while time.time() < stop:
        try:
            if ("Inscribed" in _show(browser, "news")
                    and _receipt(browser) is None):
                landed = True
                break
        except WebDriverException:
            pass        # reloading underneath the read
        time.sleep(0.5)
    if not landed:
        browser.refresh()
        _loaded(browser)
        assert "Inscribed" in _show(browser, "news"), \
            "after its block the page would not say it: " \
            + repr(_show(browser, "news"))

    # That load asked `/content` and found the file whole, so the receipt is
    # spent -- and a page that comes back after this says nothing, because
    # the item itself is on the page.
    stop = time.time() + 20
    while time.time() < stop and _receipt(browser) is not None:
        time.sleep(0.5)
    assert _receipt(browser) is None, "it outlived the file it was for"
    browser.refresh()
    _loaded(browser)
    assert _show(browser, "news") == "", "told twice for one file"
