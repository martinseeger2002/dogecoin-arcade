"""An account taking a price off the book: what the question would cost, and of whom.

The previous file gave an account the two ends of the book, an order and a
cancel. This is the middle of it, and the middle is a READ, for one reason that
no amount of browser code gets around: the question that makes a maker's node
answer has to be sealed to the messaging key that node announced for the maker's
address, and that key is in this node's address book, which a tab cannot look in.
A tab pressing `Take` knows an order id and an amount and nothing else, so the
route exists to say the four things it cannot work out -- which order the queue
chose, what that prices to, whose key, whose node -- and to say nothing else,
because the thing that answers a question about a trade must not be the thing
that starts one. Every test here that asks "what changed" is answered with
"nothing", and one of them is written to fail the moment that stops being true.

The seats are 90 and up. `_bookcoin` names its token after its seat, because one
name is one token and the first claim wins (D-122), and in a full run the chain
is shared with `test_account_order.py`, which has taken seats 1 through 26. A
repeated name issues NOTHING and the property read back is somebody else's
token, which is how a page full of empty orders was explained once before.

The block is high on purpose, because a seat is one identity for the whole
session: a reused number is the same person doing two tests, and two tests that
each assume they were the first thing that ever happened to that person cannot
both be right. Seats 31 and 32 are already `test_account_offer.py:495` and 41
through 85 belong to `test_account_accept.py`, which is not a style complaint
but a broken assertion -- the test here that needs a maker with NO published key
would have found one announced at 41, and the one that needs an address holding
exactly ONE output would have found two at 45.
"""

import contextlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import node, _seated, _settled        # noqa: F401,E402
from test_account_order import (                              # noqa: E402
    _book, _bookcoin, _bookmarks, _page, _paid_out, _publicly, _signed)
from test_funding import _sign                                # noqa: E402
from arcade import swap as swaplib, utxos                      # noqa: E402
from arcade.messaging import api as apilib                    # noqa: E402
from arcade.messaging import envelope as envelopelib          # noqa: E402
from arcade.messaging.keys import Identity                    # noqa: E402
from arcade.messaging.scanner import Scanner                  # noqa: E402
from arcade.shopkeeper import Shopkeeper                      # noqa: E402

COIN = 100_000_000


def _order(seat, pid, amount, price, side="ask"):
    """Put one of this seat's orders on the book, in a block, and return the row.

    Through the routes and not the index: an order a test wrote into `book_order`
    by hand would prove the read against a row the engine never agreed to hold,
    and half of what is being read here is which real, standing order the queue
    landed on.
    """
    said = seat["client"].post("/account/order", json={
        "property_id": pid, "side": side, "amount": amount, "price": price})
    assert said.status_code == 200, said.text
    done = _signed(seat["client"], seat["secret"], seat["pubkey"], said)
    assert done.status_code == 200, done.text
    _settled(seat["state"], seat["rpc"])
    rows = [r for r in _book(seat["state"], pid=pid)
            if r["address"] == seat["address"]]
    assert len(rows) == 1, f"one order of theirs, the book says {len(rows)}"
    return rows[0]


def _take(seat, order, amount):
    return seat["client"].post("/account/take",
                               json={"order": order, "amount": amount})


def _seated_bookcoin(node, which, **kw):
    """A seat by itself, as the book helpers hand one out, but as a dict.

    Only for the tests that need a seat WITHOUT the messaging key, which is the
    one thing `_bookcoin` insists on and the thing one test here is about.
    """
    app, state, rpc = node
    client, secret, pubkey, address = _seated(app, state, rpc, which, **kw)
    return {"app": app, "state": state, "rpc": rpc, "client": client,
            "secret": secret, "pubkey": pubkey, "address": address}


# --- the queue, which is the reason the route is a read at all --------------

def test_a_price_is_asked_in_the_order_the_ask_queued(node):
    """Three people at one price, and the press is a question about the price.

    Both halves belong in one test because together they are the rule, and
    either one alone lets the other break. Press the third row and the trade
    goes to whoever queued first (D-083) -- a tab aiming at the bottom row of a
    price level is asking to buy at that price, not to choose which of three
    identical orders gets the coins. Press the FIRST row and it still goes to
    the first: the row that was clicked is a candidate like any other, and the
    version of this that skipped it walked the queue downwards on every press,
    so the maker at the head of it never filled while three people kept
    pressing the top of the page.

    `moved` is the second half of the same sentence and the half the card
    speaks: the answer has to name the order it actually took up.
    """
    first = _bookcoin(node, 90)
    second = _seated_bookcoin(node, 91)
    third = _seated_bookcoin(node, 92)
    _paid_out(first, second["address"], 100 * COIN)
    _paid_out(first, third["address"], 100 * COIN)
    pid = first["pid"]

    row_a = _order(first, pid, "1", "2")
    row_b = _order(second, pid, "1", "2")
    row_c = _order(third, pid, "1", "2")

    taker = _seated_bookcoin(node, 93, coins=(20.0, 1.0))
    from_bottom = _take(taker, row_c["txid"], "1")
    assert from_bottom.status_code == 200, from_bottom.text
    assert from_bottom.json()["order"] == row_a["txid"], \
        "the maker that queued first is the one being asked"
    assert from_bottom.json()["moved"] is True

    from_top = _take(taker, row_a["txid"], "1")
    assert from_top.status_code == 200, from_top.text
    assert from_top.json()["order"] == row_a["txid"], \
        "pressing the head of the queue asks the head of the queue"
    assert from_top.json()["moved"] is False
    # Both presses priced the same trade, off the row they chose and not off
    # whichever row happened to be clicked: two asks at one price are one price.
    assert from_bottom.json()["coins"] == from_top.json()["coins"] == 2 * COIN


# --- the arithmetic, which is the other half --------------------------------

def test_a_part_of_a_price_is_rounded_up_and_never_down(node):
    """D-062, at the smallest price the book will hold.

    One whole token for one satoshi, and half a token taken. Exact arithmetic
    says half a satoshi, and the engine's price guard refuses a leg that pays
    LESS than the order's ratio, so a figure rounded the other way is not a
    cheaper trade -- it is an answer that cannot become a transaction, after a
    message fee was spent to get it. Rounding up costs this taker one satoshi
    and the maker keeps their ratio.
    """
    maker = _bookcoin(node, 94)
    row = _order(maker, maker["pid"], "1", "0.00000001")
    taker = _seated_bookcoin(node, 95)
    said = _take(taker, row["txid"], "0.5")
    assert said.status_code == 200, said.text
    terms = said.json()
    assert terms["coins"] == -(-row["want_amount"] * (COIN // 2)
                              // row["sale_amount"]) == 1, \
        "one satoshi, because a half of one is not a thing the chain can pay"
    assert terms["tokens"] == COIN // 2
    assert terms["amount"] == "0.5", "what the box said, said back"


def test_whole_tokens_are_counted_whole_and_priced_in_eight_decimals(node):
    """The same arithmetic on an INDIVISIBLE token, where the scales differ.

    An indivisible token's `sale_amount` is already whole tokens and its price
    is per whole token, so the product that priced the ask carried one COIN
    more than a divisible one's would. Read back here, on the taking side, so
    the two halves of an order are not each other's opposite in scale -- a
    four-of-a-token ask that read as four satoshis on the way in was the bug
    this shape caught once already.
    """
    maker = _bookmarks(node, 96)
    row = _order(maker, maker["pid"], "4", "0.5")
    taker = _seated_bookcoin(node, 97)
    said = _take(taker, row["txid"], "2")
    assert said.status_code == 200, said.text
    terms = said.json()
    assert row["sale_amount"] == 4 and row["want_amount"] == 2 * COIN
    assert terms["coins"] == COIN == -(-row["want_amount"] * 2 // row["sale_amount"])
    assert terms["amount"] == "2" and terms["tokens"] == 2


# --- the refusals, all of which belong before a fee is spent ----------------

def test_a_bid_is_not_something_an_account_can_take(node):
    """The whole of "an account can only take an ask", said by the route.

    Filling a bid means handing over tokens, and the half that does that is
    signed by the wallet holding them -- which is precisely the wallet this
    node has never been given the key to. So it is a refusal and not a
    transaction here, and it is its own test rather than a comment because it
    is the boundary of what this feature is.
    """
    maker = _bookcoin(node, 98)
    row = _order(maker, maker["pid"], "1", "2", side="bid")
    taker = _seated_bookcoin(node, 99)
    said = _take(taker, row["txid"], "1")
    assert said.status_code == 400, said.text
    assert "wallet holding the tokens" in said.json()["detail"], said.text


def test_a_maker_with_no_published_key_is_not_refused_after_a_fee(node):
    """Nobody can be asked, and that is said with no message bought.

    The key the seal goes to is the node's to know, and an address with no
    announcement is an address with nobody reading what is sent to it. The
    queue skips such a maker when there is another at the price to ask
    (D-042), so this is the case where they are the only one: the refusal has
    to arrive as an answer, here, where it costs nothing, and not as a
    question on the chain that goes unanswered forever.
    """
    issuer = _bookcoin(node, 100)
    silent = _seated_bookcoin(node, 101, key=False)
    _paid_out(issuer, silent["address"], 100 * COIN)
    row = _order(silent, issuer["pid"], "1", "2")
    taker = _seated_bookcoin(node, 102)
    said = _take(taker, row["txid"], "1")
    assert said.status_code == 400, said.text
    assert "has not published a messaging key" in said.json()["detail"], \
        said.text


def test_taking_your_own_order_is_refused(node):
    """A price of yours is not a trade with yourself.

    The twin refuses it the same way, and for the same reason: it would put a
    price on a public page as though a stranger had paid it.
    """
    both = _bookcoin(node, 103)
    row = _order(both, both["pid"], "1", "2")
    said = _take(both, row["txid"], "1")
    assert said.status_code == 400, said.text
    assert "your own" in said.json()["detail"], said.text


def test_one_output_is_told_to_split_before_the_question(node):
    """D-051's advice, at the one moment it is worth anything.

    Taking a price spends two of this address's outputs, one in the trade and
    one to pay for the message asking about it. A wallet with one output finds
    that out at the last step of a trade it already paid to ask about, which is
    the sequence D-051 exists to break; so this route counts them and says the
    advice before anything is spent. It is the same refusal `/account/accept`
    says, arrived at the same way -- the index's own view of the address, less
    what this account has already committed -- rather than `listunspent`, which
    knows nothing about a key this node never held.
    """
    maker = _bookcoin(node, 104)
    row = _order(maker, maker["pid"], "1", "2")
    poor = _seated_bookcoin(node, 105, coins=(5.0,))
    first = _take(poor, row["txid"], "1")
    assert first.status_code == 400, first.text
    assert "Split it first" in first.json()["detail"], first.text

    split = poor["client"].post("/account/send", json={
        "to": poor["address"], "amount": "1"})
    assert split.status_code == 200, split.text
    # Offered is not spent. Nothing is on the chain until this seat's own key
    # signs what was offered, so without the signature the address keeps the
    # one output the test started it with and the advice below is tested against
    # a split that never happened.
    done = _signed(poor["client"], poor["secret"], poor["pubkey"], split)
    assert done.status_code == 200, done.text
    _settled(poor["state"], poor["rpc"])
    again = _take(poor, row["txid"], "1")
    assert again.status_code == 200, again.text
    assert again.json()["coins"] == 2 * COIN, "the advice worked"


# --- and what it does NOT do ------------------------------------------------

def test_the_read_costs_nothing_and_starts_nothing(node):
    """The whole claim of the route, in one test.

    No transaction, so no block and no fee and no change in what the account
    holds. No message, so the maker's node was never asked and has nothing
    queued to answer. No row, so there is no half-finished trade sitting on
    anybody's page waiting for a second half that was never requested. A tab
    can press this a hundred times to see what a price means and leave the
    book exactly as it found it.
    """
    maker = _bookcoin(node, 106)
    row = _order(maker, maker["pid"], "1", "2")
    taker = _seated_bookcoin(node, 107)
    state = taker["state"]
    index = state.token_index(state.messaging)
    with contextlib.closing(index.open()) as db:
        before = [c["value"] for c in utxos.unspent(db, taker["address"])]
    height = taker["rpc"].call("getblockcount")
    pool = taker["rpc"].call("getrawmempool")

    said = _take(taker, row["txid"], "1")
    assert said.status_code == 200, said.text
    assert said.json()["maker"] == maker["address"]
    assert said.json()["to"], "the key the tab could not have found for itself"

    assert taker["rpc"].call("getblockcount") == height, "no block, no transaction"
    assert taker["rpc"].call("getrawmempool") == pool, "nothing even in the pool"
    with contextlib.closing(index.open()) as db:
        after = [c["value"] for c in utxos.unspent(db, taker["address"])]
    assert after == before, "the taker holds exactly what it held"


# --- and the answer, which is the half a page cannot see ---------------------

def _announced(seat, key: str):
    """Re-publish what this address's messaging key is, and mean it."""
    said = seat["client"].post("/account/announce", json={"key": key})
    assert said.status_code == 200, said.text
    done = _signed(seat["client"], seat["secret"], seat["pubkey"], said)
    assert done.status_code == 200, done.text
    _settled(seat["state"], seat["rpc"])


def _opened(state, rpc):
    """Mine, and let THIS node open the machine messages that were for it.

    `_settled` scans as it mines and carries no identity, so an envelope aimed at
    a program on this node is filed and left shut -- the scanner's opening hangs
    off the tail of a scan whose cursor is already at the tip. Everything else in
    this file stays inside one node's own accounts and never needs to open
    anything; this test does, because the thing it is asking whether an account
    can reach IS a program.
    """
    _settled(state, rpc)
    with state.messaging.rpc() as mrpc, state.store() as store:
        scanner = Scanner(mrpc, state.messaging.params, store,
                          identity=state.ensure_identity())
        scanner.scan()
        scanner.open_pending()


def _payable(state, rpc):
    """Ordinary outputs for the wallet that pays for answers.

    A shopkeeper's answer is a transaction out of the node's own wallet, and an
    envelope funded from a coinbase is invisible to every scanner -- `tx.extract`
    refuses a pubkey input -- including the one on the machine that made it. After
    the 101 blocks every seat mines, the wallet holds a mountain of coinbase, so
    without this the answer below would be sent, confirmed, and unreadable, which
    is the worst way for a test to fail. Two ordinary sends to the address the
    node pays from, with a block between, is what `test_many_accounts` does for
    the same reason.
    """
    wallet = state.derived_address
    for _ in range(2):
        rpc.call("sendtoaddress", wallet, 3.0)
        _settled(state, rpc)


def test_the_maker_answers_what_a_take_would_ask(node):
    """The one test that asks whether the question reached anybody.

    Every other test here stops at the answer `/account/take` gives, which is a
    READ and proves nothing about the trade it describes. This is the other half
    of the sentence: the tab takes the key the route named for it, seals a
    `fill` question to that key the way `messaging.js::sealForProgram` does, and
    the maker's node answers -- back to the account's own key, which the node
    that carried it cannot open.

    The maker here is the node's own wallet, and that is not a shortcut, it is
    the arrangement that exists. An answer is built by `offer_for_order`, which
    refuses an order that is not in the wallet it was asked in -- "that order is
    not this wallet's to fill" -- and a node only ever reads an envelope sealed
    to its own key, because `scanner.py` files an `api_message` for nobody else
    and `shopkeeper.tick` reads exactly those rows. An account's ask on this
    same node is therefore answered by that account's own tab, and nothing in
    the browser answers a `fill` question yet. So this test proves the half that
    does work end to end and says, by its shape, which half is still owed.

    Two bytes are load-bearing and worth naming where they are set. `swapv` is
    `swaplib.PROTOCOL` because a shopkeeper that cannot match it answers "update
    one of us" instead of the price, and the body carries neither `re` nor `ok`
    because `_swap_message` screens THOSE on the way in and returns a fill
    before it ever reaches that screen.
    """
    app, state, rpc = node
    # The node's identity FIRST, before anything asks where its home is. Until
    # `ensure_identity` runs there is no pinned address, and `home_address`
    # answers with the wallet's named account address instead -- a real address
    # this node can spend from, and not the one this test puts tokens on. That
    # is the whole of the difference between an order on the book and a flash
    # nobody read.
    nodekey = state.ensure_identity().public_bytes
    wallet = state.derived_address
    issuer = _bookcoin(node, 108)
    # The order being taken is the node's own: tokens over to its address and
    # The operator's form, so the wallet that receives the question is the
    # wallet the order stands on. `_payable` goes LAST and after the top-up:
    # `_paid_out` funds its issuer with a wallet-wide coin send, and coin
    # selection is not loyal to the pile this test just put at the home
    # address -- funding first hands those outputs straight back.
    _paid_out(issuer, wallet, 100 * COIN)
    _payable(state, rpc)
    placed = app.post("/exchange/order", data={
        "csrf_token": state.csrf_token, "property_id": issuer["pid"],
        "side": "ask", "amount": "1", "price": "2"}, follow_redirects=False)
    assert placed.status_code == 303, placed.text
    # The operator's route answers 303 whether or not it understood the order --
    # every refusal it makes is a flash and not a status -- so say which of the
    # two this was, instead of letting an empty book be the only clue.
    assert "Order on the book" in (state.notice or ""), state.notice
    _settled(state, rpc)
    mine = [r for r in _book(state, pid=issuer["pid"]) if r["address"] == wallet]
    assert len(mine) == 1, f"one ask of the node's, the book says {mine}"
    row = mine[0]
    # What a live node's own announcement did on the chain, recorded the way the
    # scanner records it: the key at this address has to BE this node's identity
    # or the question is sealed to somebody who cannot answer it.
    with state.store() as store:
        store.add_key_announcement("nodekey", wallet, nodekey, "ff",
                                   int(rpc.call("getblockcount")), 0,
                                   stated=True)

    taker = _seated_bookcoin(node, 109, coins=(20.0, 1.0), key=False)
    me = Identity.generate()
    _announced(taker, me.public_bytes.hex())
    # A shopkeeper's first pass on a chain only parks its cursor -- what was in
    # the inbox before there was a shopkeeper was not an order to it -- so a
    # question that arrives before that first pass is skipped for good. A live
    # node has ticked a hundred times by then; a fixture has to be given the
    # cursor, which is what `test_many_accounts` does for the same reason.
    assert Shopkeeper(state).tick() == 0, "the first tick only parks the cursor"

    terms = _take(taker, row["txid"], "1")
    assert terms.status_code == 200, terms.text
    to = bytes.fromhex(terms.json()["to"])
    assert to == state.ensure_identity().public_bytes, \
        "the key the route named is the key that can answer"

    body = json.dumps({"swap": "fill", "swapv": swaplib.PROTOCOL,
                       "order": row["txid"],
                       "tokens": terms.json()["tokens"],
                       "buyer": terms.json()["buyer"]},
                      separators=(",", ":")).encode()
    # `/account/talk` writes the envelope header itself and is handed only the
    # ciphertext, so the payload `apilib.seal` assembles has to be split at
    # exactly that header -- and said out loud, because a message that is
    # prefixed twice opens as nothing and looks like a broken route.
    payload = apilib.seal(me, to, body)
    hlen = len(envelopelib.Header(type=envelopelib.TYPE_API, clen=0).encode())
    assert envelopelib.Header(type=envelopelib.TYPE_API,
                              clen=len(payload) - hlen).encode() \
        == payload[:hlen], "the header the node would write is this one"

    asked = taker["client"].post("/account/talk", json={
        "op": "send", "to": to.hex(), "sealed": payload[hlen:].hex()})
    assert asked.status_code == 200, asked.text
    sent = _signed(taker["client"], taker["secret"], taker["pubkey"], asked)
    assert sent.status_code == 200, sent.text
    ask_txid = sent.json()["txid"]
    _opened(state, rpc)

    # The answer is a transaction out of this node's own wallet, and by now the
    # home address has paid the order's own fee and holds nothing -- so
    # `funded_address` falls back on the biggest pile in the wallet, which is
    # coinbase, and a message paid from coinbase is unreadable to every node on
    # the chain, this one included. Fund the paying address again, last thing
    # before the tick that spends it.
    _payable(state, rpc)
    assert Shopkeeper(state).tick() == 1, \
        "one question in front of the maker's node, one answer made"
    _opened(state, rpc)                      # so the taker can read the reply

    said = taker["client"].get("/account/messages?after=0")
    assert said.status_code == 200, said.text
    answers = []
    for row_ in said.json()["candidates"]:
        try:
            sender, protocol, fingerprint, plain = apilib.open_stamped(
                me, bytes.fromhex(row_["payload"]))
        except envelopelib.EnvelopeError:
            continue
        made = json.loads(plain)
        if made.get("swap") == "fill":
            answers.append((sender, made))
    assert len(answers) == 1, \
        f"the answer came back sealed to the one who asked, got {answers}"
    sender, made = answers[0]
    assert made.get("ok") is True, made
    # `re` is the QUESTION's transaction, not the order's -- `_answer` fills it
    # from the row the envelope arrived in. That is why the tab keys its answers
    # by the txid it just broadcast, which `askTheBook` hands back, and names the
    # order separately, inside the offer.
    assert made.get("re") == ask_txid, \
        "it answers the question that was asked, by that transaction"
    assert made["offer"]["order"] == row["txid"], \
        "and the offer it sent is for the order this take was about"
    assert made["offer"]["seller"] == wallet, \
        "from the wallet whose order it is"
    assert sender == state.ensure_identity().public_bytes, \
        "the maker's node answered, not this one guessing on its behalf"


# --- and the pass-over, which only the taker could see ----------------------

def test_the_maker_sees_that_the_queue_walked_past_their_order(node):
    """a tester S-work-2: "skipped" as a state, on the page the maker opens.

    The queue steps past a maker nobody can message rather than refusing the
    press, because one address with no published key would otherwise hold up a
    price for everybody (D-042). Right for the market, silent for the maker:
    their order sits there looking exactly like a fillable one, and the only
    place the pass-over left a mark was the log of the person who walked past
    it. So the same table the queue reads is read when this page is drawn, and
    the row says what is the matter with it -- an order that will never be
    asked about is a thing somebody is paying to hold.

    Nothing new is stored about anybody to say it: the sentence is derived from
    the announcement table at the moment of drawing, which is why publishing a
    key makes it go away with no change to the order.
    """
    issuer = _bookcoin(node, 110)
    silent = _seated_bookcoin(node, 111, key=False)
    _paid_out(issuer, silent["address"], 100 * COIN)
    _order(silent, issuer["pid"], "1", "2")

    _publicly(silent["state"])
    try:
        quiet = _page(silent["client"], issuer["pid"])
    finally:
        silent["state"].public = False
    assert "cannot be asked" in quiet, "the row says what is wrong with it"
    assert "steps past them" in quiet, "and the page says what to do about it"

    _announced(silent, (bytes([0x77, 111]) + bytes(30)).hex())
    _publicly(silent["state"])
    try:
        loud = _page(silent["client"], issuer["pid"])
    finally:
        silent["state"].public = False
    assert "cannot be asked" not in loud, \
        "the order never changed; the address became somebody"
    assert "steps past them" not in loud

    # And it stays news rather than furniture: a maker who CAN be asked is never
    # shown the sentence, so a page that says it is saying something.
    named = _bookcoin(node, 112)
    _order(named, named["pid"], "1", "2")
    _publicly(named["state"])
    try:
        usual = _page(named["client"], named["pid"])
    finally:
        named["state"].public = False
    assert "cannot be asked" not in usual
    assert "steps past them" not in usual


# --- the answer becoming a trade -------------------------------------------

def _asked_of_the_node(node, which_maker: int, which_taker: int) -> dict:
    """A node's own ask, asked about by an account's key, and answered.

    The scene from `test_the_question_reaches_the_maker_and_comes_back_sealed`,
    in one place, because what it is made of is forty lines of chain facts and
    not a fixture anybody could reuse by accident: the node's identity taken
    BEFORE anything asks where its home is, the order standing on the wallet
    that will receive the question, the announcement that makes that wallet
    somewhere a message can be read, the shopkeeper's first tick that only
    parks a cursor, and the funding of the address that pays for the answer --
    last, because an answer paid from coinbase is unreadable by the node that
    sent it. What comes back is the scene: the order, what the read priced, the
    taker's own identity, the transaction that carried the question, and the
    offer the maker sent back sealed to that identity and nobody else's.
    """
    app, state, rpc = node
    nodekey = state.ensure_identity().public_bytes
    wallet = state.derived_address
    issuer = _bookcoin(node, which_maker)
    _paid_out(issuer, wallet, 100 * COIN)
    _payable(state, rpc)
    placed = app.post("/exchange/order", data={
        "csrf_token": state.csrf_token, "property_id": issuer["pid"],
        "side": "ask", "amount": "1", "price": "2"}, follow_redirects=False)
    assert placed.status_code == 303, placed.text
    assert "Order on the book" in (state.notice or ""), state.notice
    _settled(state, rpc)
    rows = [r for r in _book(state, pid=issuer["pid"]) if r["address"] == wallet]
    assert len(rows) == 1, f"one ask of the node's, the book says {rows}"
    with state.store() as store:
        store.add_key_announcement("nodekey", wallet, nodekey, "ff",
                                   int(rpc.call("getblockcount")), 0, stated=True)

    taker = _seated_bookcoin(node, which_taker, coins=(20.0, 1.0), key=False)
    me = Identity.generate()
    _announced(taker, me.public_bytes.hex())
    assert Shopkeeper(state).tick() == 0, "the first tick only parks the cursor"

    terms = _take(taker, rows[0]["txid"], "1")
    assert terms.status_code == 200, terms.text
    body = json.dumps({"swap": "fill", "swapv": swaplib.PROTOCOL,
                       "order": rows[0]["txid"],
                       "tokens": terms.json()["tokens"],
                       "buyer": terms.json()["buyer"]},
                      separators=(",", ":")).encode()
    payload = apilib.seal(me, nodekey, body)
    hlen = len(envelopelib.Header(type=envelopelib.TYPE_API, clen=0).encode())
    asked = taker["client"].post("/account/talk", json={
        "op": "send", "to": nodekey.hex(), "sealed": payload[hlen:].hex()})
    assert asked.status_code == 200, asked.text
    sent = _signed(taker["client"], taker["secret"], taker["pubkey"], asked)
    assert sent.status_code == 200, sent.text
    _opened(state, rpc)
    _payable(state, rpc)
    assert Shopkeeper(state).tick() == 1, \
        "one question in front of the maker's node, one answer made"
    _opened(state, rpc)                      # so the taker can read the reply

    made = None
    for read in taker["client"].get("/account/messages?after=0").json()["candidates"]:
        try:
            _sender, _protocol, _fingerprint, plain = apilib.open_stamped(
                me, bytes.fromhex(read["payload"]))
        except envelopelib.EnvelopeError:
            continue
        said = json.loads(plain)
        if said.get("swap") == "fill":
            made = said
    assert made is not None and made.get("ok") is True, \
        f"the maker answered, and sealed to the one who asked: {made}"
    return {"app": app, "state": state, "rpc": rpc, "nodekey": nodekey,
            "wallet": wallet, "order": rows[0], "terms": terms.json(),
            "taker": taker, "me": me, "ask_txid": sent.json()["txid"],
            "offer": made["offer"], "pid": issuer["pid"]}


def test_an_answer_becomes_a_transaction_the_maker_can_finish(node):
    """The whole take, ending in a broadcast. This is the half that was missing.

    Everything above this line in this file stops at the answer, and the answer
    is a promise: it names an outpoint, a price and an order, and it is signed
    by nobody. A node taking a price for its own wallet turns it into bytes with
    `swap.build`, which chooses coins out of that wallet and calls
    `signrawtransaction` -- and an account's key is not in any wallet, which is
    exactly why the post about the take had to be corrected the day it went up.
    So this is the sentence that post was missing, run to the end: the answer
    comes in, `/account/take/build` makes it a transaction out of the index and
    signs nothing, the key signs the inputs from `signed_from`,
    `/account/take/sign` pastes them and broadcasts NOTHING -- because the
    maker's input is still an empty scriptSig and only the maker can fill it --
    and `swap.countersign`, which is what the maker's node does when that
    message lands in its inbox, finishes it and puts it on the chain.

    The last four assertions are the ones that matter. A half whose input 0
    carries a scriptSig would be a finished trade this node signed by itself;
    the tokens moving is the only proof that the whole chain of parts fit
    together; and the order leaving the book is the proof that the engine read
    the finished transaction as filling THAT order and not as a swap between
    two strangers.
    """
    scene = _asked_of_the_node(node, 113, 114)
    state, rpc, taker = scene["state"], scene["rpc"], scene["taker"]
    index = state.token_index(state.messaging)
    offer = scene["offer"]

    built = taker["client"].post("/account/take/build", json={
        "offer": offer, "chain": scene["terms"]["chain"]})
    assert built.status_code == 200, built.text
    say = built.json()
    assert say["signed_from"] == 1, \
        "the maker's coin is input 0, and this key is asked for nothing before it"
    assert say["inputs"][0]["txid"] == offer["outpoint"]["txid"]
    assert say["inputs"][0]["vout"] == offer["outpoint"]["vout"]
    assert say["inputs"][0]["address"] == scene["wallet"]
    for n, coin in enumerate(say["inputs"][1:], 1):
        assert coin["address"] == taker["address"], \
            f"input {n} spends {coin['address']}, and only the taker's own coins " \
            "belong from input 1 on"
    assert len(say["sighashes"]) == len(say["inputs"]) - 1
    assert say["offer"] == offer["id"] and say["to"] == scene["nodekey"].hex(), \
        "the two things the next step cannot work out on its own"
    assert say["coins"] == 2 * COIN and say["fee"] > 0, \
        "the price off the order, and a fee the buyer pays"
    assert rpc.call("getrawmempool") == [], "a build spends nothing"
    assert [r["txid"] for r in _book(state, pid=scene["pid"])] == \
        [scene["order"]["txid"]], "and the book did not even notice"

    signed = taker["client"].post("/account/take/sign", json={
        "offer": offer, "chain": say["chain"], "raw": say["raw"],
        "signatures": [_sign(taker["secret"], bytes.fromhex(d)).hex()
                       for d in say["sighashes"]],
        "pubkey": taker["pubkey"].hex()})
    assert signed.status_code == 200, signed.text
    half = signed.json()
    assert half["signed"] is True
    parts = rpc.call("decoderawtransaction", half["hex"])
    assert parts["vin"][0]["txid"] == offer["outpoint"]["txid"]
    assert parts["vin"][0]["scriptSig"]["hex"] == "", \
        "this is a HALF: the maker's signature is what it is waiting for"
    assert parts["vin"][1]["scriptSig"]["hex"], "and this account's is in it"
    assert rpc.call("getrawmempool") == [], \
        "nothing was broadcast here -- the maker broadcasts, not this node"

    # The maker's node, doing what its shopkeeper does when the finished half
    # arrives in its inbox: check it against its own offer, sign its own input,
    # broadcast. This is the point where a take either ends in a trade or does
    # not, and it is the point every test in this file so far stopped before.
    txid = swaplib.countersign(rpc, index, state.offers,
                              state.offers.get(half["offer"]), half["hex"])
    assert txid
    _settled(state, rpc)
    assert index.balance(taker["address"], scene["pid"]) == 1 * COIN, \
        "the token the order sold is now the taker's"
    assert not [r for r in _book(state, pid=scene["pid"])
                if r["address"] == scene["wallet"]], \
        "and the order it filled is off the book, because it is filled"


def test_an_answer_meant_for_another_key_builds_nothing(node):
    """The offer is sealed to one account, and the build honours that.

    The answer arrives in a browser and nowhere else, but a browser is a place
    where bytes move: a tab can be told to post an answer it was not given. If
    this route built from any offer it was handed, a second account could be
    shown a transaction spending ITS coins to fill somebody else's trade -- or,
    with the fields swapped, one holding the key could be walked into signing a
    trade priced for another. `check_offer` refuses both on the same line, the
    one that says whose address the offer names as its buyer, and it is the
    maker's own `offer_for_order` that wrote that name in the first place.
    """
    from arcade.web import door

    scene = _asked_of_the_node(node, 115, 116)
    other = _seated_bookcoin(node, 117, coins=(20.0, 1.0))
    refused = other["client"].post("/account/take/build", json={
        "offer": scene["offer"], "chain": scene["terms"]["chain"]})
    assert refused.status_code == 400, refused.text
    assert scene["taker"]["address"] in refused.json()["detail"], \
        "it says whose answer it was, which is the fact the asker needs"
    assert scene["taker"]["address"] != other["address"]
    assert scene["rpc"].call("getrawmempool") == []

    # And the pair is public in the door's sense, which for a route that costs
    # nothing and spends nothing means one thing only: signed by nobody, it
    # answers nothing, with the reason in it.
    for path in ("/account/take/build", "/account/take/sign"):
        assert path in door.PUBLIC_POST, path
        _publicly(scene["state"])
        try:
            quiet = scene["app"].post(path, json={})
        finally:
            scene["state"].public = False
        assert quiet.status_code == 403, quiet.text
        assert "sign in" in quiet.json()["detail"], quiet.text


def test_a_signed_answer_that_no_longer_fits_is_refused(node):
    """A block lands between the bytes and the signature: nothing is traded.

    The answer is minutes old by the time a person signs it, and in that time
    the coins it was built out of may have moved. The signature is over the
    first bytes, so the honest answer is a refusal that says what happened --
    not a transaction whose inputs are not the ones anybody looked at, and not
    the maker's node finding out a block later. The same comparison is what
    `/account/buy/sign` runs, and this is the same sentence, said of a book
    answer instead of a listing.
    """
    scene = _asked_of_the_node(node, 118, 119)
    taker, rpc = scene["taker"], scene["rpc"]
    built = taker["client"].post("/account/take/build", json={
        "offer": scene["offer"], "chain": scene["terms"]["chain"]})
    assert built.status_code == 200, built.text
    say = built.json()

    # A coin the account did not have when it asked now has a block under it,
    # which is the plainest way to make this node build the same trade out of
    # different coins. Smallest-sufficient selection means it does.
    rpc.call("sendtoaddress", taker["address"], 3.0)
    _settled(scene["state"], rpc)

    again = taker["client"].post("/account/take/build", json={
        "offer": scene["offer"], "chain": say["chain"]})
    assert again.status_code == 200, again.text
    assert again.json()["raw"] != say["raw"], \
        "the new coins have to actually change the bytes, or this test proves " \
        "nothing about the old ones being refused"

    stale = taker["client"].post("/account/take/sign", json={
        "offer": scene["offer"], "chain": say["chain"], "raw": say["raw"],
        "signatures": [_sign(taker["secret"], bytes.fromhex(d)).hex()
                       for d in say["sighashes"]],
        "pubkey": taker["pubkey"].hex()})
    assert stale.status_code == 400, stale.text
    assert "different transaction" in stale.json()["detail"], stale.text
    assert rpc.call("getrawmempool") == []
    assert [r["txid"] for r in _book(scene["state"], pid=scene["pid"])] == \
        [scene["order"]["txid"]], "the order stands, unasked and unfilled"

