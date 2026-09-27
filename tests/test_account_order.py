"""An account on the token book: an ask, a bid, and taking them back off.

The book was the one part of the exchange an account could not reach at all.
Every route that touches it ends in `TokenSender.prepare`, which selects the
inputs, funds and signs out of the NODE's wallet -- so what an account needed
was never a new kind of transaction, only the same `MetaDExTrade` funded from
an address this machine never held the key to. `/account/token/send` and
`/account/token/create` already do exactly that, and this file repeats their
bytes rather than importing them: two places writing the same payload is the
check that a route nobody was watching did not quietly write a different one.

The token is issued on the chain and paid over for real, not inserted into the
index, and that is the load-bearing choice in this file. The question these
tests ask is what the ENGINE makes of an order -- whether it holds an ask's
tokens back, whether a cancel hands them over again -- and an engine reserves
only what it read out of a block. A balance written in by hand would let every
one of those assertions pass while the route was broken.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import node, _seated, _settled        # noqa: F401,E402
from test_funding import _sign                              # noqa: E402
from arcade import encoding, payload as P                    # noqa: E402
from arcade.tokens import (                                  # noqa: E402
    TokenSender, issuance_payload, send_payload)

COIN = 100_000_000
HELD = 500 * COIN           # what each account is given of the one token


def _book(state, txid: str = "", pid: int = 0):
    """The standing orders, read out of the index rather than off a response.

    `book_order` is the table the engine keeps and the consensus hash covers,
    so a row here is the claim that matters: some block carried an order, the
    engine honoured it, and this is whose it was and what it priced.

    `pid` is not a convenience. Every test in this session writes to one chain,
    so an unfiltered read also returns every order every earlier test mined --
    heights 319 and 1907 in a table this test believed held one row. In a full
    run it is every order every earlier FILE mined, which is why the counts here
    say which token they are about.
    """
    index = state.token_index(state.messaging)
    where, args = [], []
    if txid:
        where.append("txid=?")
        args.append(txid)
    if pid:
        where.append("(sale_property=? OR want_property=?)")
        args += [pid, pid]
    sql = "SELECT * FROM book_order"
    if where:
        sql += " WHERE " + " AND ".join(where)
    with index.open() as db:
        return [dict(r) for r in db.conn.execute(sql + " ORDER BY txid", args)]


def _held(state, address: str, pid: int):
    index = state.token_index(state.messaging)
    with index.open() as db:
        row = db.conn.execute(
            "SELECT balance, metadex_reserve FROM balance"
            " WHERE address=? AND property_id=?", (address, pid)).fetchone()
    return ((row["balance"], row["metadex_reserve"]) if row else (0, 0))


def _bookcoin(node, which: int):
    """A person with a real token: their client, their address, the property id.

    Three transactions before the test even starts -- an issuance from the
    node's wallet and a send of half of it to the account -- because a token
    the engine invented out of nothing would reserve nothing, and half of the
    point of an ask is that something is reserved.
    """
    app, state, rpc = node
    client, secret, pubkey, address = _seated(app, state, rpc, which)
    issuer = rpc.call("getnewaddress")
    rpc.call("sendtoaddress", issuer, 2.0)
    _settled(state, rpc)
    sender = TokenSender(rpc, state.messaging.params)
    # A name belonging to this seat and no other, because one name is one token
    # and the first claim wins (D-122). An issuance that repeats a name creates
    # NOTHING, and this chain is shared by every test in the session, so a plain
    # "Bookcoin" here would have issued nothing from the second test onward and
    # the property read below would be another test's token -- one this address
    # holds none of, which is what a page full of empty orders was about.
    made = sender.broadcast(sender.prepare(issuer, issuance_payload(
        name=f"Bookcoin {which}", divisible=True, managed=False,
        amount=2 * HELD)))
    _settled(state, rpc)
    index = state.token_index(state.messaging)
    # The property this transaction created, not the newest one with this name:
    # reading them by name takes whichever came first, and the name is only
    # unique per seat, not per run. Matched on the creating transaction, which
    # is how `test_account_token_create.py` reads one back too.
    found = [p for p in index.properties()
             if str(p["creation_txid"]) == made]
    assert found, f"nothing was created by {made}"
    prop = found[0]
    pid = prop["property_id"]
    sender.broadcast(sender.prepare(
        issuer, send_payload(pid, HELD), reference=address))
    _settled(state, rpc)
    assert _held(state, address, pid) == (HELD, 0), \
        "the account holds the tokens and nothing is reserved yet"
    return {"app": app, "state": state, "rpc": rpc, "client": client,
            "secret": secret, "pubkey": pubkey, "address": address,
            "pid": pid, "name": prop["name"], "issuer": issuer}


def _bookmarks(node, which: int):
    """The same person, in an INDIVISIBLE token, which is where prices break.

    An indivisible token is counted in whole units and priced in eight-decimal
    coins, so an amount times a price is not always a whole number of satoshis —
    three tokens at half a coin each comes to 1.5, and the only question worth
    asking is whether the order says 1.5 or quietly says 1.
    """
    app, state, rpc = node
    client, secret, pubkey, address = _seated(app, state, rpc, which)
    issuer = rpc.call("getnewaddress")
    rpc.call("sendtoaddress", issuer, 2.0)
    _settled(state, rpc)
    sender = TokenSender(rpc, state.messaging.params)
    made = sender.broadcast(sender.prepare(issuer, issuance_payload(
        name=f"Bookmark {which}", divisible=False, managed=False,
        amount=2 * HELD)))
    _settled(state, rpc)
    index = state.token_index(state.messaging)
    found = [p for p in index.properties()
             if str(p["creation_txid"]) == made]
    assert found, f"nothing was created by {made}"
    prop = found[0]
    pid = prop["property_id"]
    sender.broadcast(sender.prepare(
        issuer, send_payload(pid, HELD), reference=address))
    _settled(state, rpc)
    assert _held(state, address, pid) == (HELD, 0), \
        "whole tokens, none of them reserved"
    return {"app": app, "state": state, "rpc": rpc, "client": client,
            "secret": secret, "pubkey": pubkey, "address": address,
            "pid": pid, "name": prop["name"], "issuer": issuer}


def _paid_out(seat, to: str, amount: int):
    """More of the seat's token, from its issuer, over to `to`.

    A token transaction is funded by the address that holds the token -- the
    sender of a message IS that address, so the node cannot stand in with its own
    coins -- and the top-up a fixture hands an issuer does not stretch over the
    several transactions a test asks of it. The one before this came back "there
    is not enough here: 0.00000000 available", which is a refusal with advice in
    it: send a coin to that address and try again once it confirms. So do that,
    here, once, rather than losing a test to the price of saying so.
    """
    seat["rpc"].call("sendtoaddress", seat["issuer"], 2.0)
    _settled(seat["state"], seat["rpc"])
    sender = TokenSender(seat["rpc"], seat["state"].messaging.params)
    made = sender.broadcast(sender.prepare(
        seat["issuer"], send_payload(seat["pid"], amount), reference=to))
    _settled(seat["state"], seat["rpc"])
    return made


def _signed(who, secret, pubkey, answer):
    """Sign what was offered, exactly as offered, and let a block carry it."""
    said = answer.json()
    done = who.post("/account/sign", json={
        "offer": said["offer"], "pubkey": pubkey.hex(),
        "signatures": [_sign(secret, bytes.fromhex(d)).hex()
                       for d in said["sighashes"]]})
    assert done.status_code == 200, done.text
    return done


# --- what the order is made of ----------------------------------------------

def test_an_ask_is_the_payload_the_book_reads_and_nothing_else(node):
    """The screen before signing: the exact bytes, this account's coins, no broadcast.

    The payload is written here a second time on purpose. Wrapped in `AnyData`
    the order would be an opaque blob the token engine cannot reserve against:
    paid for, mined, and invisible -- so `wrap=False` is not a detail of this
    route, it is the difference between an order and a fee.
    """
    seat = _bookcoin(node, 1)
    asked = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "side": "ask", "amount": "10",
        "price": "0.5"})
    assert asked.status_code == 200, asked.text
    offer = asked.json()
    message = P.MetaDExTrade(property_id_for_sale=seat["pid"],
                             amount_for_sale=10 * COIN,
                             property_id_desired=0, amount_desired=5 * COIN)
    assert encoding.encode_class_c(message.encode()).hex() in offer["raw"], \
        "an unwrapped MetaDExTrade, or the engine reserves nobody"
    assert {coin["address"] for coin in offer["inputs"]} == {seat["address"]}, \
        "every coin is the account's own; this node funds an order with nothing"
    assert offer["fee"] > 0 and f"10 {seat['name']}" in offer["what"]
    assert seat["rpc"].call("getrawmempool") == [], "asking spends nothing"
    assert _book(seat["state"], pid=seat["pid"]) == []


def test_a_bid_is_the_other_half_of_the_same_sentence(node):
    """A bid names the tokens it wants and the coins it offers, in one message."""
    seat = _bookcoin(node, 2)
    asked = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "side": "bid", "amount": "10",
        "price": "0.5"})
    assert asked.status_code == 200, asked.text
    message = P.MetaDExTrade(property_id_for_sale=0, amount_for_sale=5 * COIN,
                             property_id_desired=seat["pid"],
                             amount_desired=10 * COIN)
    assert encoding.encode_class_c(message.encode()).hex() \
        in asked.json()["raw"]
    assert asked.json()["total"] == "5" and asked.json()["price"] == "0.5"
    assert {coin["address"] for coin in asked.json()["inputs"]} \
        == {seat["address"]}, \
        "a bid that fell through to the node's own funding would price this " \
        "order exactly right and pay for it with the operator's coins"


# --- what a block makes of it ------------------------------------------------

def test_an_ask_lands_and_the_engine_holds_its_tokens_back(node):
    """The whole reason the ask is checked before it is built.

    Two rows of the ledger change, and both are the engine's doing: a standing
    order in `book_order`, and the tokens moved out of the balance into
    `metadex_reserve`, so the book can never show what the seller has since
    spent. The address on the row is the account's, and the node never held a
    key for it -- which is what makes this an account's order rather than the
    node placing a trade on somebody's behalf.
    """
    seat = _bookcoin(node, 3)
    answer = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "side": "ask", "amount": "10",
        "price": "0.5"})
    done = _signed(seat["client"], seat["secret"], seat["pubkey"], answer)
    _settled(seat["state"], seat["rpc"])

    (row,) = _book(seat["state"], done.json()["txid"])
    assert row["address"] == seat["address"], "the order is the account's"
    assert not seat["rpc"].call("validateaddress", seat["address"])["ismine"], \
        "the node could not have made this order itself"
    assert (row["sale_property"], row["want_property"]) == (seat["pid"], 0)
    assert (row["sale_amount"], row["want_amount"]) == (10 * COIN, 5 * COIN)
    assert row["reserved"] == 10 * COIN
    assert _held(seat["state"], seat["address"], seat["pid"]) \
        == (HELD - 10 * COIN, 10 * COIN)


def test_a_bid_holds_nothing_because_coins_cannot_be_reserved(node):
    """A bid is an intent, and saying so is the honest part of the route.

    There is no covenant that would hold coins back and still let a wallet
    live, so the engine files the order and reserves nothing (D-048). Whether
    the coins are really there is settled when somebody fills it. The balance
    not moving is the assertion: a route that tried to reserve a coin would
    have to invent a lock the protocol does not have.
    """
    seat = _bookcoin(node, 4)
    answer = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "side": "bid", "amount": "10",
        "price": "0.5"})
    done = _signed(seat["client"], seat["secret"], seat["pubkey"], answer)
    _settled(seat["state"], seat["rpc"])

    (row,) = _book(seat["state"], done.json()["txid"])
    assert (row["sale_property"], row["want_property"]) == (0, seat["pid"])
    assert (row["sale_amount"], row["want_amount"]) == (5 * COIN, 10 * COIN)
    assert row["address"] == seat["address"], \
        "the book credits this bid to the account, not to the node that " \
        "carried it"
    assert not seat["rpc"].call("validateaddress", seat["address"])["ismine"], \
        "and it could not have made this one either"
    assert row["reserved"] == 0
    assert _held(seat["state"], seat["address"], seat["pid"]) == (HELD, 0)


# --- what is refused, and why before the fee ---------------------------------

def test_an_ask_for_more_tokens_than_the_account_holds_is_refused(node):
    """Refused here, not in a block.

    The engine would mark the transaction invalid and reserve nothing; the fee
    would be gone either way. So the one check this route makes is the ask's,
    and the sentence says what the engine does rather than pretending the node
    holds anything.
    """
    seat = _bookcoin(node, 5)
    answer = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "side": "ask", "amount": "900",
        "price": "0.5"})
    assert answer.status_code == 400
    assert "holds" in answer.json()["detail"]
    assert seat["rpc"].call("getrawmempool") == []


def test_a_price_that_comes_to_under_a_satoshi_is_refused(node):
    """Integers only: an order that prices a token below a satoshi cannot be
    honoured by anything, and it is the ratio, not either number, that says so."""
    seat = _bookcoin(node, 6)
    answer = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "side": "ask", "amount": "0.00000001",
        "price": "0.00000001"})
    assert answer.status_code == 400
    assert "satoshi" in answer.json()["detail"]


def test_a_token_that_does_not_exist_is_refused(node):
    seat = _bookcoin(node, 7)
    answer = seat["client"].post("/account/order", json={
        "property_id": 9999, "side": "ask", "amount": "1", "price": "1"})
    assert answer.status_code == 400
    assert "no token" in answer.json()["detail"]


def test_a_side_that_is_neither_is_refused(node):
    """`side` picks which of the two integers is the token, so it is not decoration."""
    seat = _bookcoin(node, 8)
    answer = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "side": "both", "amount": "1",
        "price": "1"})
    assert answer.status_code == 400
    assert "ask or a bid" in answer.json()["detail"]


def test_an_order_that_does_not_name_its_side_is_refused(node):
    """There is no safe default between a sell and a buy, only a guess.

    The page says which side every time; this is for the payload that does not,
    which is either of the two transactions and `side` is what decides which of
    the two integers is the token. Defaulting it would have the node trade an
    account's coins on the strength of an absent key.
    """
    seat = _bookcoin(node, 16)
    answer = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "amount": "1", "price": "1"})
    assert answer.status_code == 400
    assert "ask or a bid" in answer.json()["detail"]
    assert seat["rpc"].call("getrawmempool") == []


def test_an_indivisible_order_is_priced_in_coins_and_not_in_satoshis(node):
    """Four whole tokens at half a coin each is an order for TWO COINS.

    The two kinds of token have different scales, and one line in this route
    forgot which was which. A divisible token's units carry eight decimals and
    its price is per whole token, so their product has one COIN too many in it
    and the division takes it out. An indivisible token's units already ARE
    whole tokens, so its product is satoshis plain and dividing it asks for a
    hundred millionth of what was typed: four tokens at half a coin each stood
    on the book wanting two satoshis — two hundred-millionths of a coin — while
    the box above it went on saying a half, and both signatures stood over the
    two satoshis. The line was `/exchange/order`'s first, so this was priced
    wrong on the live site before it was anywhere else, and nothing on this
    chain quotes a whole token, which is what kept it out of every test.
    """
    seat = _bookmarks(node, 17)
    answer = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "side": "ask", "amount": "4",
        "price": "0.5"})
    assert answer.status_code == 200, answer.text
    assert answer.json()["total"] == "2", \
        "the number typed, not its own hundred-millionth"
    assert answer.json()["price"] == "0.5"
    message = P.MetaDExTrade(property_id_for_sale=seat["pid"],
                             amount_for_sale=4,
                             property_id_desired=0, amount_desired=2 * COIN)
    assert encoding.encode_class_c(message.encode()).hex() \
        in answer.json()["raw"], \
        "the coin leg is two coins in the bytes, not only on the page"

    # Three of them at the same price is an order for a coin and a half, and a
    # whole number of satoshis is all the chain asks of it.
    answer = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "side": "ask", "amount": "3",
        "price": "0.5"})
    assert answer.status_code == 200, answer.text
    assert answer.json()["total"] == "1.5"


def test_a_product_that_lands_between_two_satoshis_is_refused(node):
    """Three hundred-millionths of a token at half a coin each is 1.5 satoshis.

    This is the refusal the divisible token reaches, and it is the other way
    round from what this file used to say: whole tokens times an eight-decimal
    price always comes out in whole satoshis, so an indivisible order cannot
    land here at all. What can is a fraction of a divisible token so small that
    the product falls between two satoshis — and rounded down there, the price
    standing on the book is not the price that was typed.
    """
    seat = _bookcoin(node, 21)
    answer = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "side": "ask", "amount": "0.00000003",
        "price": "0.5"})
    assert answer.status_code == 400, answer.text
    assert "whole satoshis" in answer.json()["detail"]
    assert seat["rpc"].call("getrawmempool") == [], \
        "refused here, not in a block that cost a fee to be invalid"


def test_a_second_ask_of_the_same_tokens_is_refused_before_the_first_lands(node):
    """An order in the mempool is a claim the ledger has not honoured yet.

    `index.balance` is what the blocks say, and the engine reserves when a block
    lands, so an ask broadcast a minute ago is invisible to that number while
    anybody reading the book can see it plainly. Two asks of everything the
    address holds therefore both passed the only check this route makes, and the
    second paid its fee to be refused inside its own block — the exact fee the
    check exists to save. What fixes it is already on the book, marked
    `pending`: what comes off the balance is what this address has resting in the
    pool, and nothing else, because a mined ask is already out of the balance.
    """
    seat = _bookcoin(node, 18)
    answer = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "side": "ask", "amount": "500",
        "price": "1"})
    _signed(seat["client"], seat["secret"], seat["pubkey"], answer)
    assert seat["rpc"].call("getrawmempool"), "broadcast, and in no block yet"
    assert _held(seat["state"], seat["address"], seat["pid"]) == (HELD, 0), \
        "the ledger has reserved nothing, which is the whole trap"

    again = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "side": "ask", "amount": "500",
        "price": "1"})
    assert again.status_code == 400, again.text
    assert "already on this book" in again.json()["detail"]
    assert len(seat["state"].token_index(seat["state"].messaging).book(
        seat["pid"])["asks"]) == 1, "the refusal was about one real order"
    _settled(seat["state"], seat["rpc"])
    assert len(_book(seat["state"], pid=seat["pid"])) == 1, \
        "and the pool order was one order"


def test_an_ask_for_a_token_the_account_does_not_hold_is_refused(node):
    """The one check this route makes, aimed at the right wallet.

    Every other test here gives the account and the node the same number of the
    same token, so a route that read the node's own addresses instead of this
    address's balance would refuse these numbers with this sentence and still
    pass all of them. This is the arrangement that tells the two readings
    apart: the node's issuer address holds a thousand of this token and the
    account signing holds none of it.
    """
    seat = _bookcoin(node, 19)
    other, osecret, opubkey, theirs = _seated(seat["app"], seat["state"],
                                              seat["rpc"], 20)
    answer = other.post("/account/order", json={
        "property_id": seat["pid"], "side": "ask", "amount": "1",
        "price": "1"})
    assert answer.status_code == 400, answer.text
    detail = answer.json()["detail"]
    assert "holds" in detail and seat["name"] in detail, detail
    assert "holds 0" in detail, \
        "the figure is this account's nothing, not the node's thousand"
    assert seat["rpc"].call("getrawmempool") == []


def test_an_order_that_cannot_pay_its_own_fee_is_refused(node):
    """Holding the tokens is not quite enough: somebody has to pay to say so.

    The arrangement matters here, and the obvious one cannot be built. An address
    cannot hold tokens without also holding a coin: a token send appends the
    reference output the sender rule demands, and that output is the dust floor —
    0.01 of a coin here, more than forty orders' worth of fee. Nor can the coin
    be given back: `fees.DUST_LIMIT` is both what the token send must hand over
    and the smallest payment the network will relay, so an address holding one
    coin can move none of it, and an address holding the change from an order
    still holds more than the next order costs. Every account in this file that
    has tokens has a fee, and no fixture can arrange otherwise.

    So the fee is asked for where it is the ONLY thing the route needs, which is a
    bid. A bid reserves nothing — coins cannot be reserved (D-048) — and this
    route deliberately checks it against no balance at all, so what is left
    standing between an account and a bid is that a coin exists to pay for the
    transaction that says it. This account has none: no coins, no key published,
    and it holds none of the token either, which the bid does not care about.

    The refusal has to come from this route rather than from an offer that cannot
    be funded, because the alternative is a confirmation card asking a person to
    sign a transaction that cannot exist, and a signature spent on nothing is the
    fee this check exists to save.
    """
    app, state, rpc = node
    seat = _bookcoin(node, 22)
    other, osecret, opubkey, theirs = _seated(app, state, rpc, 23, coins=(),
                                              key=False)
    said = other.get("/account").json()
    assert said["spendable"] == 0, \
        "the premise, asserted rather than hoped: not a coin here to pay with"

    answer = other.post("/account/order", json={
        "property_id": seat["pid"], "side": "bid", "amount": "1",
        "price": "1"})
    assert answer.status_code == 400, answer.text
    assert "not enough" in answer.json()["detail"], answer.text
    assert rpc.call("getrawmempool") == []


def test_a_second_offer_asked_while_the_first_is_in_the_pool_spends_another_coin(node):
    """Two transactions naming one coin: the network takes one, refuses the other.

    This is what `exclude` is for, and it is not about two unanswered offers.
    Nothing is claimed until something is broadcast -- `Flights` remembers what
    this node sent that no block has read yet -- so two offers that were only
    ASKED for name the same coin and that is right: a person who asks twice and
    signs once has spent nothing, and those are two questions, not two
    transactions. The exposure is the offer asked AFTER the first one went out and
    before its block: the index still lists the coin that broadcast spent, which
    is the exact gap this book exists to fill. Without the exclusion the next
    order is built over a coin the account's own transaction is already spending,
    and comes back `txn-mempool-conflict` after a confirmation card was shown.
    """
    seat = _bookcoin(node, 24)
    first = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "side": "ask", "amount": "10", "price": "1"})
    assert first.status_code == 200, first.text
    _signed(seat["client"], seat["secret"], seat["pubkey"], first)
    pool = seat["rpc"].call("getrawmempool")
    assert pool, "out, and deliberately in no block"

    second = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "side": "ask", "amount": "20", "price": "1"})
    assert second.status_code == 200, second.text
    one = {(c["txid"], c["vout"]) for c in first.json()["inputs"]}
    two = {(c["txid"], c["vout"]) for c in second.json()["inputs"]}
    assert one and two, "an offer that names no coin is no offer"
    assert not (one & two), \
        "the first is spending that coin already; the second may not name it"
    assert seat["rpc"].call("getrawmempool") == pool, \
        "and asking for the second one broadcast nothing"
    _settled(seat["state"], seat["rpc"])


# --- taking it back ----------------------------------------------------------

def test_a_cancel_gives_the_tokens_back_and_leaves_the_other_side_alone(node):
    """Two asks and a bid, then one cancel of the asks.

    A cancel names a pair and a side, not an order -- there is no order id on
    the chain to name (D-042) -- so it takes every resting ask that account has
    on this token, and this is the test that says so out loud rather than
    letting the word "cancel" imply more precision than the protocol has. The
    bid is the control: same account, same token, other side, still standing.
    """
    seat = _bookcoin(node, 9)
    for amount, price in (("10", "0.5"), ("20", "1")):
        answer = seat["client"].post("/account/order", json={
            "property_id": seat["pid"], "side": "ask", "amount": amount,
            "price": price})
        _signed(seat["client"], seat["secret"], seat["pubkey"], answer)
        _settled(seat["state"], seat["rpc"])
    answer = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "side": "bid", "amount": "5", "price": "2"})
    bid = _signed(seat["client"], seat["secret"], seat["pubkey"], answer)
    _settled(seat["state"], seat["rpc"])

    assert len(_book(seat["state"], pid=seat["pid"])) == 3
    assert _held(seat["state"], seat["address"], seat["pid"]) \
        == (HELD - 30 * COIN, 30 * COIN)

    answer = seat["client"].post("/account/order/cancel", json={
        "property_id": seat["pid"], "side": "ask"})
    assert answer.status_code == 200, answer.text
    assert answer.json()["every"] is True
    cancel = _signed(seat["client"], seat["secret"], seat["pubkey"], answer)
    _settled(seat["state"], seat["rpc"])

    rows = _book(seat["state"], pid=seat["pid"])
    assert [r["txid"] for r in rows] == [bid.json()["txid"]], \
        "the cancel took both asks and not the bid"
    assert _held(seat["state"], seat["address"], seat["pid"]) == (HELD, 0), \
        "the engine gave back what the asks were holding"
    assert _book(seat["state"], cancel.json()["txid"]) == [], \
        "a cancel is a deletion, not a row of its own"


def test_a_cancel_names_a_pair_and_a_side_and_nothing_fininer(node):
    """The payload again, because a cancel is the one route that over-reaches.

    Somebody who meant to pull one order down takes all of them on that side
    down with it. The route cannot offer better than the protocol can, so what
    it owes instead is a payload that says plainly which pair and which side,
    and a page that repeats it.
    """
    seat = _bookcoin(node, 10)
    answer = seat["client"].post("/account/order/cancel", json={
        "property_id": seat["pid"], "side": "bid"})
    assert answer.status_code == 200, answer.text
    message = P.MetaDExCancelPair(property_id_for_sale=0,
                                  property_id_desired=seat["pid"])
    assert encoding.encode_class_c(message.encode()).hex() \
        in answer.json()["raw"]
    assert "cancel this account's bids" in answer.json()["what"]
    assert {coin["address"] for coin in answer.json()["inputs"]} \
        == {seat["address"]}, \
        "a cancel spends this account's coin and nobody else's"


def test_a_cancel_for_a_token_that_does_not_exist_is_refused(node):
    seat = _bookcoin(node, 11)
    answer = seat["client"].post("/account/order/cancel",
                                 json={"property_id": 9999, "side": "ask"})
    assert answer.status_code == 400
    assert "no token" in answer.json()["detail"]


# --- the page that reaches them ----------------------------------------------

def _publicly(state):
    """This node, served to the internet rather than to its own machine.

    One switch, read by `arcade/web/door.py` and by `_public_request`, and all
    three readings below turn on it: what the page may say, and which of its
    forms it may draw, are decided by the answer to "is somebody reaching this
    from outside?".
    """
    state.public = True


def _page(where, pid):
    """The pair page as a browser gets it, with the line breaks folded away.

    Folded because this file asserts about sentences, and the template breaks
    its sentences over lines the way anybody writing prose in HTML would. A test
    that matched one line of a wrapped sentence would pass while the page said
    something else.
    """
    answer = where.get(f"/exchange/pair/{pid}")
    assert answer.status_code == 200, answer.text
    return " ".join(answer.text.split())


def test_the_public_token_page_shows_nobody_the_node_s_balance(node):
    """The hole this route had, on the live site, in plain text.

    `/exchange/pair/3` on app.dogecoinarcade.com answered any stranger who
    asked with "You hold 0 Arcade and 191092.3664 coins" -- the operator's own
    balance, because this one route asked the node what it held before it asked
    who was asking. Every other public page has the guard and says why. The
    census in `test_public.py` could not catch this one: nothing was LINKED
    where the door would shut, and the disclosure was in a number printed on the
    page. So the test has to be about a number.
    """
    seat = _bookcoin(node, 12)
    balance = float(seat["rpc"].call("getbalance"))
    assert balance > 0, "the node has coins, which is what makes this a test"

    _publicly(seat["state"])
    try:
        body = _page(seat["app"], seat["pid"])
    finally:
        seat["state"].public = False
    assert f"{balance:.4f}" not in body, "the node's coins are not the market's"
    assert "You hold" not in body, \
        "and a stranger is not told they hold anything at all, not even a tidy " \
        "zero: the tick belongs to the reader, and there is no reader here"
    assert 'action="/exchange/order"' not in body, \
        "and no form here spends a wallet this page is not holding"
    assert 'fetch("/account/order"' not in body, \
        "nor does a script block reach an account route they cannot sign"
    assert "Sign in to put an order" in body


def test_an_account_on_the_token_page_sees_its_own_numbers(node):
    """One template, three readings of it.

    The token holding comes from the index and the coins from this node's watch
    of that one address; the node's wallet is not asked anything at all. Where
    the form points is the second proof -- `/account/order` funds the order out
    of this account's own inputs, while `action="/exchange/order"` would have
    stood a stranger's click behind the operator's coins.
    """
    seat = _bookcoin(node, 13)
    balance = float(seat["rpc"].call("getbalance"))

    _publicly(seat["state"])
    try:
        body = _page(seat["client"], seat["pid"])
    finally:
        seat["state"].public = False
    assert f"{balance:.4f}" not in body
    assert f"You hold 500 {seat['name']}" in body, \
        "what the index says it holds"
    assert 'action="/exchange/order"' not in body
    assert 'fetch("/account/order"' in body and 'id="book-order"' in body, \
        "the sentence alone is not the proof: /account/order/cancel contains it"
    assert 'id="pull-ask"' not in body and 'id="pull-bid"' not in body, \
        "nothing of theirs is resting, so there is nothing to cancel and no " \
        "button that would cost a fee to press"


def test_the_page_marks_what_is_yours_and_names_no_closed_route(node):
    """Two accounts on one book, looked at by one of them.

    The rows first: an order of yours says so, and the button beside the panel
    is the account's own cancel. Then somebody else's ask, which must NOT carry
    a `Take` form -- that route is the node's and the door shuts it here. A form
    that answers "Not here" to the person it was drawn for is the bug
    `test_public.py` has caught twice before on other pages; that census cannot
    reach this page, which needs a token and a chain to be drawn at all, so the
    check has to live here.
    """
    seat = _bookcoin(node, 14)
    _publicly(seat["state"])
    try:
        answer = seat["client"].post("/account/order", json={
            "property_id": seat["pid"], "side": "ask", "amount": "10",
            "price": "0.5"})
        _signed(seat["client"], seat["secret"], seat["pubkey"], answer)
        _settled(seat["state"], seat["rpc"])

        other, osecret, opubkey, theirs = _seated(seat["app"], seat["state"],
                                                  seat["rpc"], 15)
        _paid_out(seat, theirs, HELD)
        _settled(seat["state"], seat["rpc"])
        answer = other.post("/account/order", json={
            "property_id": seat["pid"], "side": "ask", "amount": "20",
            "price": "1"})
        _signed(other, osecret, opubkey, answer)
        _settled(seat["state"], seat["rpc"])

        body = _page(seat["client"], seat["pid"])
    finally:
        seat["state"].public = False
    assert "yours" in body, "one of these two asks is the reader's"
    assert 'id="pull-ask"' in body and "/account/order/cancel" in body
    assert 'id="pull-bid"' not in body, \
        "this account has asks and no bids, and a cancel of the side nobody is " \
        "on is a transaction that costs a fee to cancel nothing"
    assert "/exchange/order/cancel" not in body
    assert 'action="/exchange/fill"' not in body
    assert 'class="small take-price"' in body, \
        "and the row is takeable by an account now: the press asks the maker's " \
        "node, and this node still signs nothing of the trade"
    seat["state"].public = False


def test_an_order_still_in_the_pool_says_so_on_the_page(node):
    """The state a person stares at for ten minutes, and nothing tested it.

    Every other page test here mines before it looks, so the three things this
    page does with an order that has no block yet — the `pool` pill, the line in
    the list of their own orders, and the price that must not be offered to
    anybody yet — were never drawn at all. An order in the pool is an order
    while anybody reading the book can see it and no ledger has honoured it,
    which is the least obvious state this page can be in.
    """
    seat = _bookcoin(node, 25)
    _publicly(seat["state"])
    try:
        answer = seat["client"].post("/account/order", json={
            "property_id": seat["pid"], "side": "ask", "amount": "10",
            "price": "0.5"})
        _signed(seat["client"], seat["secret"], seat["pubkey"], answer)
        assert seat["rpc"].call("getrawmempool"), "broadcast, and in no block"
        body = _page(seat["client"], seat["pid"])
    finally:
        seat["state"].public = False
    assert "yours" in body, "the row is the reader's before the ledger's"
    assert ">pool<" in body, "and the page says where the order is"
    assert "in the mempool" in body, "the same in the list of their own orders"
    assert "when its block lands" in body, \
        "it is an ask that cannot be taken yet, and saying when it can be is " \
        "the honest version of refusing to draw a button"
    assert 'action="/exchange/fill"' not in body
    _settled(seat["state"], seat["rpc"])


def test_nobody_signed_in_is_told_to_sign_in_by_both_routes(node):
    """The door lets these two through to the internet; the route does the rest.

    `/account/order` and `/account/order/cancel` are in `door.PUBLIC_POST`
    because the account's own key pays for whatever comes out of them — which is
    also the only reason either one opens at all. That is an assumption the door
    entry rests on, and every call elsewhere in this file is made by a client
    that already signed in. A stranger reaching them has to be told to sign in,
    which is a different answer from the door's "Not here" and from a form.
    """
    from arcade.web import door

    seat = _bookcoin(node, 26)
    assert "/account/order" in door.PUBLIC_POST
    assert "/account/order/cancel" in door.PUBLIC_POST
    _publicly(seat["state"])
    try:
        for route in ("/account/order", "/account/order/cancel"):
            refused = seat["app"].post(route, json={
                "property_id": seat["pid"], "side": "ask"})
            assert refused.status_code == 403, f"{route}: {refused.text}"
            assert "sign in" in refused.json()["detail"], refused.text
    finally:
        seat["state"].public = False
    assert seat["rpc"].call("getrawmempool") == []


# --- what is standing --------------------------------------------------------

def _placed(seat, side: str, amount: str, price: str):
    """An order on the book, signed and mined: the txid that placed it.

    The three lines every test above repeats -- ask for it, sign it, let a block
    carry it -- gathered once, so that the assertions below are about what the
    list says and not about getting an order onto the book first.
    """
    answer = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "side": side, "amount": amount,
        "price": price})
    assert answer.status_code == 200, answer.text
    txid = _signed(seat["client"], seat["secret"], seat["pubkey"],
                   answer).json()["txid"]
    _settled(seat["state"], seat["rpc"])
    return txid


def _listed(who, pid: int):
    """The whole answer, and the rows in it that are about this token.

    Split because one chain carries every test in this session and an account is
    the same address in all of them: a list of an address can hold a row that an
    earlier test put there on a token named after a different seat. So `count` is
    checked against the whole answer -- it is the field a program reads first --
    while the rows below are about one token and say so.
    """
    answer = who.post("/account/order/list", json={})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["count"] == len(body["orders"]), body
    return body, [o for o in body["orders"] if o["property_id"] == pid]


def test_the_list_says_what_an_ask_came_to_and_touches_nothing(node):
    """The read that was missing between placing an order and cancelling it.

    An order is a transaction and not a row anybody keeps (D-042), so before this
    a program that could put a price on the book had to hoard its own txids or
    guess what was left of the amount (a tester, 2026-09-27). Everything the
    answer says is therefore something the ENGINE already knows -- the amount it
    is holding, the price its two integers come to, which block carried it -- and
    the load-bearing assertion is the last one: that asking changed none of it.
    """
    seat = _bookcoin(node, 27)
    order = _placed(seat, "ask", "10", "0.5")
    before = _book(seat["state"], pid=seat["pid"])
    pool = seat["rpc"].call("getrawmempool")
    blocks = seat["rpc"].call("getblockcount")

    body, rows = _listed(seat["client"], seat["pid"])
    assert body["address"] == seat["address"], "whose orders these are"
    assert body["chain"] == seat["state"].messaging.network
    assert len(rows) == 1, rows
    one = rows[0]
    assert one["order"] == order
    assert one["side"] == "ask" and one["property_id"] == seat["pid"]
    assert one["name"] == seat["name"]
    assert one["tokens"] == 10 * COIN and one["amount"] == "10"
    assert one["coins"] == 5 * COIN and one["total"] == "5", \
        "ten of the token for five coins, which is what the two integers say"
    assert one["price"] == "0.50000000", \
        "the price the pair comes to, worked out the way the book works it out, " \
        "and not the string that happened to be typed into the box"
    assert one["pending"] is False and one["block"] > 0
    assert one["ahead"] == 0
    assert "held" not in one, \
        "the coins figure belongs to a bid, the one side that cannot be checked " \
        "when it is placed (D-048)"

    named = seat["client"].post("/account/order/list",
                               json={"property_id": seat["pid"]})
    assert named.status_code == 200, named.text
    assert named.json()["orders"] == rows, \
        "narrowing to the token this address has orders on answers the same: the " \
        "filter is about which token, not about which order"

    assert seat["rpc"].call("getrawmempool") == pool
    assert seat["rpc"].call("getblockcount") == blocks
    assert _book(seat["state"], pid=seat["pid"]) == before, \
        "a read -- nothing broadcast, nothing reserved, nothing re-dated"


def test_an_order_with_no_block_yet_lists_as_pending(node):
    """D-061 from the maker's side: the state that lasts ten minutes.

    The order is theirs the moment it is broadcast and fillable only once a block
    carries it, because the engine reserves nothing before then. A list that said
    "no orders" in that window is the bug D-061 exists to stop; a list that said
    "standing" without saying WHERE it is would be the same bug wearing a smile.
    """
    seat = _bookcoin(node, 28)
    answer = seat["client"].post("/account/order", json={
        "property_id": seat["pid"], "side": "ask", "amount": "10", "price": "1"})
    order = _signed(seat["client"], seat["secret"], seat["pubkey"], answer)
    assert seat["rpc"].call("getrawmempool"), "out, and deliberately in no block"

    body, rows = _listed(seat["client"], seat["pid"])
    assert len(rows) == 1, "the order is theirs before the ledger's"
    assert rows[0]["order"] == order.json()["txid"]
    assert rows[0]["pending"] is True and rows[0]["block"] == 0
    assert rows[0]["ahead"] == 0, "last by time, but nothing else stands at this " \
        "price, so there is nobody in front of it either"

    _settled(seat["state"], seat["rpc"])
    body, rows = _listed(seat["client"], seat["pid"])
    assert rows[0]["order"] == order.json()["txid"]
    assert rows[0]["pending"] is False and rows[0]["block"] > 0, \
        "the same order, and now a block says it is standing"


def test_a_cancel_takes_the_rows_out_before_its_own_block_lands(node):
    """Standing is not the same as not-cancelled, and the pool is the proof.

    Two asks at two prices -- one cancel takes both, because a cancel names a
    pair and a side and the chain has no order id to name (D-042). Then the
    interesting half: the cancel is broadcast and unmined, and the list is
    already empty. The row has not been deleted anywhere -- the ledger is built
    out of blocks -- so this is the route repeating the book's own rule rather
    than remembering it, which is the only way a withdrawn price stops being a
    price in the ninety seconds before it is confirmed.
    """
    seat = _bookcoin(node, 29)
    first = _placed(seat, "ask", "10", "0.5")
    second = _placed(seat, "ask", "20", "1")
    body, rows = _listed(seat["client"], seat["pid"])
    assert {r["order"] for r in rows} == {first, second}
    assert sorted(r["price"] for r in rows) == ["0.50000000", "1.00000000"]

    answer = seat["client"].post("/account/order/cancel", json={
        "property_id": seat["pid"], "side": "ask"})
    assert answer.status_code == 200, answer.text
    cancel = _signed(seat["client"], seat["secret"], seat["pubkey"], answer)
    assert seat["rpc"].call("getrawmempool"), "the cancel is out, in no block"

    body, rows = _listed(seat["client"], seat["pid"])
    assert rows == [], "a price somebody has withdrawn is not a price, not even " \
        "one whose cancel no miner has taken up yet"
    _settled(seat["state"], seat["rpc"])
    body, rows = _listed(seat["client"], seat["pid"])
    assert rows == [], "and the block agrees"
    assert _book(seat["state"], cancel.json()["txid"]) == [], \
        "the cancel is not itself an order"


def test_a_bid_says_whether_the_coins_behind_it_are_there(node):
    """D-048 made readable: a resting bid is a promise about coins nobody held.

    Both of these were accepted, and both SHOULD be -- an order to buy with coins
    that are not there is legal, unfilled, and the bidder's own business, which is
    why no check was put in the way of the second one. What no node could tell the
    maker until now is that the two are different. A bid pays a fee to fail when
    the address has spent its coins since, and this is the one place that answer
    exists.
    """
    seat = _bookcoin(node, 30)
    cheap = _placed(seat, "bid", "1", "1")
    dear = _placed(seat, "bid", "1", "10")

    body, rows = _listed(seat["client"], seat["pid"])
    by = {r["order"]: r for r in rows}
    assert set(by) == {cheap, dear}, rows
    assert by[cheap]["side"] == "bid"
    assert by[cheap]["tokens"] == COIN and by[cheap]["amount"] == "1"
    assert by[cheap]["coins"] == COIN and by[cheap]["total"] == "1", \
        "one token for one coin: the two integers, with the sides swapped the way " \
        "a bid swaps them"
    assert by[cheap]["price"] == "1.00000000"
    assert by[dear]["coins"] == 10 * COIN
    assert by[cheap]["short"] is False and by[dear]["short"] is True, \
        "this seat holds about five test coins: under one bid, not under the other"
    assert by[dear]["held"] < by[dear]["coins"], \
        "what the row says the address holds, and what the bid would pay"
    assert by[cheap]["held"] >= by[cheap]["coins"]
    assert by[cheap]["held"] == by[dear]["held"], \
        "one balance for the address, read once and said beside both rows"
    assert float(by[dear]["held_amount"]) == by[dear]["held"] / COIN


def test_the_list_says_who_queued_first_at_the_same_price(node):
    """D-083 put where the person who has to rely on it can read it.

    Two asks at one price, the other account's in an earlier block. A taker is
    pointed at the one that queued first, and a maker who queues behind somebody
    is told to expect that -- so the number that decides whether their order is
    filled next was invisible to them. It is one row of their own list now.
    """
    seat = _bookcoin(node, 31)
    other, osecret, opubkey, theirs = _seated(seat["app"], seat["state"],
                                              seat["rpc"], 32)
    _paid_out(seat, theirs, 100 * COIN)
    them = dict(seat, client=other, secret=osecret, pubkey=opubkey,
                address=theirs)

    first = _placed(them, "ask", "10", "1")
    mine = _placed(seat, "ask", "20", "1")

    body, rows = _listed(seat["client"], seat["pid"])
    assert len(rows) == 1 and rows[0]["order"] == mine, \
        "their order is theirs -- this is a maker's list, not a market read"
    assert rows[0]["ahead"] == 1, \
        "one ask at this price was in an earlier block, and a taker is sent to " \
        "it before this one"
    body, theirs_rows = _listed(other, seat["pid"])
    assert theirs_rows[0]["order"] == first
    assert theirs_rows[0]["ahead"] == 0, "the same queue, read from the front"


def test_the_list_answers_a_stranger_with_a_reason(node):
    """Why this route is in the public list at all, and what that owes.

    It is there because a program with a key is the maker this feature is for,
    and such a program reaches this node over the internet, not from its own
    machine. It is a read, so the only thing opening it discloses is what the
    asker's own address already has standing. A stranger who is not signed in has
    to be told to sign in -- a 403 with the reason in it, not a 404 that leaves
    them wondering whether the route exists.
    """
    from arcade.web import door

    seat = _bookcoin(node, 33)
    assert "/account/order/list" in door.PUBLIC_POST
    _publicly(seat["state"])
    try:
        refused = seat["app"].post("/account/order/list", json={})
    finally:
        seat["state"].public = False
    assert refused.status_code == 403, refused.text
    assert "sign in" in refused.json()["detail"], refused.text

