"""Answering an offer from the side that holds the piece, with a key, not a wallet.

The other file in this pair asks what an offer IS. This one asks what an ANSWER
is, once the holder is an account rather than a wallet -- and the answer turns
out to be a leg, which is the shape `test_account_list` already files on behalf
of a seller and `test_listings` already finishes on behalf of a buyer. What is
new here is not a transaction but a promise: the two signatures are made in a
browser, are never broadcast, and are the whole of what holds the piece out of
any other answer while the buyer sleeps.

So the tests are about what the signatures are made OVER, and about what this
node is still allowed to check when it never saw the key. The price never comes
from the request -- it comes off the offer, off the chain -- and what proves it
is a leg signed at some other price being refused here rather than three blocks
later by a buyer's wallet that cannot finish it. Two refusals are worth the most
here, because they are the two a node with nobody's key could quietly get wrong:
that the piece named by the leg's own bytes is the piece the offer asked about,
and that a piece already answered to one buyer is not answered to a second.

Nothing in this file expects a transaction in the mempool. The answer is a
letter, and the letter belongs to `/account/talk` and to the tab that seals it,
which is why the last thing these routes do is say where to send what they were
just handed.

Against a real regtest node, for the reason every account file gives: the parts
that can be wrong here -- which digests a SINGLE|ANYONECANPAY leg really asks
for, whether the offer the index reads back is the one that was broadcast, what
`Listings.register` makes of a leg priced at something other than the offer --
all look right on paper.
"""

import contextlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import (_inscribed, _pair, _seated,          # noqa: E402
                                _settled, _signed, node)
from test_funding import _sign                                         # noqa: E402
from test_account_tokens import _balance, _token                       # noqa: E402
from test_web import app_state, client                               # noqa: F401,E402

from arcade import encoding, funding, inscriptions as I               # noqa: E402
from arcade import payload as P                                      # noqa: E402
from arcade.ledger import parse_amount                               # noqa: E402

COIN = 100_000_000


def _swap(txid: str, price: int) -> bytes:
    """What an answer writes at output 0: the trade the finished swap IS.

    Written here rather than imported from `app._ask_payload` for the reason
    both `test_account_list` and `test_account_offer` give for the same choice:
    the bytes the signatures stand over have to agree with the bytes a buyer is
    shown, and the only way to check that is for one of them to be written by
    somebody who did not write the other. An answer is a listing at somebody
    else's price, which is also the sentence this whole route rests on.
    """
    body = I.Swap(give=I.Leg(I.LEG_INSCRIPTION, txid=bytes.fromhex(txid)),
                  take=I.Leg(I.LEG_COINS, amount=price)).encode()
    return encoding.encode_class_c(P.AnyData(data=body).encode())


def _swap_token(txid: str, property_id: int, units: int) -> bytes:
    """The same bytes, priced in a token instead.

    And that difference is the whole of the second section of this file: a coin
    price is written twice, once here and once in what the leg pays out, so a
    wrong one is caught by arithmetic. A token price is written only here --
    the engine moves tokens in its ledger on the strength of these bytes --
    which makes reading them the only check there is, and worth a payload
    written by somebody who did not write the route.
    """
    body = I.Swap(give=I.Leg(I.LEG_INSCRIPTION, txid=bytes.fromhex(txid)),
                  take=I.Leg(I.LEG_TOKEN, property_id=property_id,
                             amount=units)).encode()
    return encoding.encode_class_c(P.AnyData(data=body).encode())


def _priced_in(state, address: str, units: int, property_id: int) -> int:
    """A token that exists, and that one address holds.

    Filed in the index rather than mined, for the reason every file that hands
    an account a token gives: an issuance needs a wallet, and an account here is
    a key this node has never seen. It lands in the table `swaplib.holds` reads,
    which is the only balance an answer cares about.
    """
    _token(state, property_id=property_id)
    _balance(state, address, property_id, units)
    return property_id


def _offered(pair, amount: str = "1", **kw) -> str:
    """One bidder's ask, broadcast and read back, as the chain has it."""
    client, secret, pubkey, _address = pair["bidder"]
    asked = client.post("/account/offer",
                        json={"piece": pair["piece"], "amount": amount, **kw})
    assert asked.status_code == 200, asked.text
    txid = _signed(client, secret, pubkey, asked.json()).json()["txid"]
    _settled(pair["state"], pair["rpc"])
    return txid


def _leg(pair, offer: str, **kw) -> dict:
    """What the holder is shown before it signs, as the route's own answer."""
    client = pair["holder"][0]
    asked = client.post("/account/accept",
                        json={"piece": pair["piece"], "offer": offer, **kw})
    assert asked.status_code == 200, asked.text
    return asked.json()


def _signed_answer(pair, offer: str, leg: dict, signatures=None,
                   raw: str = None, pubkey: str = None):
    """Bring the two signatures back. `signatures` overrides what gets sent."""
    client, secret, coin_pubkey, _address = pair["holder"]
    if signatures is None:
        signatures = [_sign(secret, bytes.fromhex(digest),
                            funding.SINGLE_ANYONECANPAY).hex()
                      for digest in leg["sighashes"]]
    return client.post("/account/accept/sign", json={
        "piece": pair["piece"], "offer": offer,
        "raw": leg["raw"] if raw is None else raw,
        "pubkey": coin_pubkey.hex() if pubkey is None else pubkey,
        "signatures": signatures})


# --- what an answer is made of ---------------------------------------------

def test_answering_shows_the_leg_at_the_price_that_was_offered(node):
    """The screen before signing, and the one number that is not asked for.

    There is no price in this request and no price can be put in it: an answer
    is a signature over a price, and the only price worth signing is the one
    that was asked. Everything else is what `/account/list` already shows --
    two of the seller's own coins, two digests, the bytes naming the piece --
    and the two words that are added are where the answer goes and what stamp
    it has to be sealed under, which a tab cannot work out for itself.
    """
    pair = _pair(node, 41, 42)
    offer = _offered(pair)
    holder_client, _hsecret, _hpubkey, hold = pair["holder"]

    asked = holder_client.post("/account/accept",
                               json={"piece": pair["piece"], "offer": offer})
    assert asked.status_code == 200, asked.text
    said = asked.json()
    assert len(said["sighashes"]) == 2 and said["sighashes"][0] != \
        said["sighashes"][1], "two outputs, so two digests"
    assert said["payload"] == _swap(pair["piece"], COIN).hex(), \
        "the bytes it shows are the bytes the swap will be, at THEIR price"
    assert {coin["address"] for coin in said["inputs"]} == {hold}, \
        "both inputs are the seller's -- a leg is its seller's coins in"
    assert int(said["price"]) == COIN and "1 coins" in said["what"]
    assert said["offer"] == offer
    assert said["seal_to"] == (bytes([0x77, 42]) + bytes(30)).hex(), \
        "sealed to the key the bidder announced, not to its address"
    assert said["stamp"][:4] == "4441", "an API stamp, or nothing is sealed to it"
    assert pair["rpc"].call("getrawmempool") == [], "showing an answer spends nothing"
    assert pair["state"].offers.bids("regtest", "in") == [], \
        "and nothing is answered until the signatures come back"


def test_the_two_signatures_are_an_answer_and_nothing_is_filed(node):
    """The whole of the seller's side: a letter, and a promise that is not a row.

    Nothing is broadcast, because there is no transaction to broadcast -- the
    one that decides this trade belongs to the buyer, who has not signed yet.
    Nothing is filed, because filing an answer would put one buyer's price on a
    public page for every stranger to take. What is left is the answer itself,
    in the shape `shopkeeper._bid` reads on the far side, and the two coins the
    leg stands on are noted as spent so the next thing this account buys does
    not spend the coin that pays for this one.
    """
    pair = _pair(node, 43, 44)
    offer = _offered(pair)
    holder_client, _hsecret, _hpubkey, hold = pair["holder"]
    leg = _leg(pair, offer)

    done = _signed_answer(pair, offer, leg)
    assert done.status_code == 200, done.text
    said = done.json()
    answer = said["answer"]
    assert answer["swap"] == "bid" and answer["id"] == offer
    assert answer["ok"] is True and answer["swapv"] == 1
    assert answer["leg"]["seller"] == hold
    assert answer["leg"]["raw"] == leg["raw"]
    assert len(answer["leg"]["signatures"]) == 2
    assert parse_amount(answer["leg"]["amount"], True) == COIN, \
        "the amount is the number a buyer's node parses off the leg"
    assert said["seal_to"] == (bytes([0x77, 44]) + bytes(30)).hex()

    assert pair["rpc"].call("getrawmempool") == [], "an answer is not a transaction"
    assert pair["state"].listings.for_piece(
        leg["inputs"][0]["txid"], int(leg["inputs"][0]["vout"])) == [], \
        "an answered leg is never advertised on a public page"
    (note,) = pair["state"].offers.bids("regtest", "in")
    assert note["direction"] == "in" and note["buyer"] == pair["bidder"][3]
    assert note["inscription"] == pair["piece"]
    assert note["expires"] > note["created"], "it is held, for a while"


def test_the_answer_is_the_leg_a_wallet_finishes(node):
    """The wire contract, checked against the reader rather than the writer.

    The whole design rests on a wallet being able to finish a trade an account
    answered, and `shopkeeper._fill_a_leg` is the code that does it. It reads
    four things out of the answer and derives the rest: the raw bytes, the
    signatures, the key, the price. So this asks of the answer only what that
    reader asks -- that the bytes register as a leg whose seller is the address
    that holds the piece, and that its own bytes name the piece the offer was
    made on -- and asks it here, where a failure costs nothing, instead of in a
    tick loop with nobody watching.
    """
    from arcade import listings as listingslib

    pair = _pair(node, 45, 46)
    offer = _offered(pair)
    holder_client, _hsecret, _hpubkey, hold = pair["holder"]
    leg = _leg(pair, offer)
    answer = _signed_answer(pair, offer, leg).json()["answer"]
    said = answer["leg"]

    row = pair["state"].listings.register(
        pair["rpc"], raw=said["raw"],
        signatures=list(said["signatures"]),
        pubkey=bytes.fromhex(said["pubkey"]), network="regtest",
        owner=said["seller"],
        price=parse_amount(said["amount"], True), record=False)
    assert row["owner"] == hold and int(row["price"]) == COIN
    named = listingslib.named_swap(bytes.fromhex(row["payload"]))
    assert named is not None and named.give.txid.hex() == pair["piece"]


# --- what it refuses, and why ----------------------------------------------

def test_a_leg_signed_at_another_price_is_not_an_answer(node):
    """The price is the one term a signature cannot be trusted about.

    The leg here is real, signed by the right key over two right coins -- and it
    sells the right piece, at 2 coins, because it was built by the listing
    route and not by this one. The offer was made at 1. `Listings.register`
    derives the price from the leg's own arithmetic and refuses the row when it
    disagrees with the price it was handed, and the price it is handed is the
    offer's -- which is how a tab that priced the leg itself is stopped here
    rather than by a buyer's wallet, after a message fee.
    """
    pair = _pair(node, 47, 48)
    offer = _offered(pair)
    holder_client, _hsecret, hpubkey, _hold = pair["holder"]

    priced = holder_client.post("/account/list",
                               json={"piece": pair["piece"], "amount": "2"})
    assert priced.status_code == 200, priced.text
    wrong = priced.json()
    assert wrong["payload"] != _swap(pair["piece"], COIN).hex()

    refused = _signed_answer(pair, offer, wrong)
    assert refused.status_code == 400
    assert "price" in refused.json()["detail"], refused.json()["detail"]
    assert pair["state"].offers.bids("regtest", "in") == [], \
        "a refused answer reserves nothing for anybody"
    assert pair["rpc"].call("getrawmempool") == []


def test_a_leg_that_sells_another_piece_is_not_an_answer(node):
    """Two pieces, one key, and a byte-for-byte reading of what a leg sells.

    The arithmetic closes on this one -- same price, same coins, same seller --
    so nothing in `Listings.register` can see the lie. What gives it away is the
    payload, read the way the buyer's node reads it: a listing's input is a
    COIN, and for a piece that arrived by transfer the coin and the inscription
    are two different transactions. Ask the coin and you describe the wrong
    sale, which is precisely the mistake this route cannot afford.
    """
    pair = _pair(node, 49, 50)
    offer = _offered(pair)
    holder_client, hsecret, hpubkey, _hold = pair["holder"]
    elsewhere = _inscribed(holder_client, pair["state"], pair["rpc"],
                           hsecret, hpubkey, "a different piece entirely")

    other = holder_client.post("/account/list",
                               json={"piece": elsewhere, "amount": "1"})
    assert other.status_code == 200, other.text
    refused = _signed_answer(pair, offer, other.json())
    assert refused.status_code == 400
    assert "sells something else" in refused.json()["detail"]
    assert pair["state"].offers.bids("regtest", "in") == []


def test_a_piece_you_do_not_hold_has_no_answer_to_give(node):
    """The bidder pressing the button on the piece it offered for.

    It has an address, a key, coins enough for two inputs, and an offer of its
    own standing on the piece -- everything except the piece. The sentence is
    the chain's, because the route reads the row's owner rather than whoever
    typed the piece in first.
    """
    pair = _pair(node, 51, 52)
    offer = _offered(pair)
    bidder_client, _bsecret, _bpubkey, _bid = pair["bidder"]

    refused = bidder_client.post("/account/accept",
                                 json={"piece": pair["piece"], "offer": offer})
    assert refused.status_code == 400
    assert "only whoever holds a piece can answer" in refused.json()["detail"]
    assert pair["rpc"].call("getrawmempool") == []


def test_an_offer_that_is_not_on_this_piece_is_not_answerable(node):
    """An offer id and a piece have to be each other's, from the index.

    Both accounts are seated, the piece is the holder's, the offer is real -- it
    is just made for a different piece than the one named in the request. Two
    readings are possible here and only one of them is true: the chain's, which
    says what an offer was made about.
    """
    pair = _pair(node, 53, 54)
    offer = _offered(pair)
    holder_client, hsecret, hpubkey, _hold = pair["holder"]
    elsewhere = _inscribed(holder_client, pair["state"], pair["rpc"],
                           hsecret, hpubkey, "not what was asked about")

    refused = holder_client.post("/account/accept",
                                 json={"piece": elsewhere, "offer": offer})
    assert refused.status_code == 400
    assert "no such offer on this piece" in refused.json()["detail"]


def test_a_piece_answered_to_one_buyer_is_not_answered_to_another(node):
    """Exclusivity, stated as what it is: a receipt, not a lock.

    Two bidders, one piece, one key. The first answer is real -- two signatures
    the node has just checked -- and the second has to be refused while it
    stands, because a node that handed out a second leg would be selling the
    piece twice with no way to say which sale it meant.

    What this cannot pretend is that the receipt is the whole story: the real
    exclusivity is that both legs spend the same coin of the seller's and only
    one of them can ever be in a block. The receipt is what keeps the second
    buyer from being sent a letter and a fee for a leg that was never going to
    settle, and it is remembered by this node and by nowhere else.
    """
    app, state, rpc = node
    pair = _pair(node, 55, 56)
    offer = _offered(pair)
    late_secret = int.from_bytes(bytes([0x61, 57]) + bytes(30), "big")
    other_client, osecret, opubkey, other = _seated(app, state, rpc, 57)
    second = other_client.post("/account/offer",
                               json={"piece": pair["piece"], "amount": "2"})
    assert second.status_code == 200, second.text
    second_id = _signed(other_client, osecret, opubkey,
                        second.json()).json()["txid"]
    _settled(state, rpc)

    _signed_answer(pair, offer, _leg(pair, offer))
    (note,) = state.offers.bids("regtest", "in")
    assert note["buyer"] == pair["bidder"][3]

    refused = pair["holder"][0].post("/account/accept",
                                     json={"piece": pair["piece"],
                                           "offer": second_id})
    assert refused.status_code == 400
    assert "answered to somebody else" in refused.json()["detail"]
    assert len(state.offers.bids("regtest", "in")) == 1, \
        "a refusal answers nobody"
    assert rpc.call("getrawmempool") == []
    assert other != pair["bidder"][3], "and the two buyers are two people"


def test_refusing_signs_nothing_and_spends_no_coin_signature(node):
    """The same question, answered the other way, at the price of a letter.

    No leg, no digests, no coin key: a refusal is words sealed to whoever asked,
    which is the one thing in this pair an account could send for itself through
    `/account/talk`. What the route still checks is the part a tab cannot know
    -- that the piece is this account's to say no about -- and what it still
    says is where the answer goes, because a refusal addressed to nobody is a
    fee spent on silence.
    """
    pair = _pair(node, 58, 59)
    offer = _offered(pair)
    holder_client = pair["holder"][0]

    said = _leg(pair, offer, decision="refuse")
    assert said["refused"] is True
    assert said["answer"]["ok"] is False and said["answer"]["error"] == "refused"
    assert said["answer"]["id"] == offer
    assert "sighashes" not in said and "raw" not in said, \
        "a refusal asks for no signature over any coin"
    assert said["seal_to"] == (bytes([0x77, 59]) + bytes(30)).hex()
    assert pair["rpc"].call("getrawmempool") == []
    assert pair["state"].offers.bids("regtest", "in") == [], \
        "saying no reserves nothing"


def test_the_dial_that_closes_an_answer_says_which_dial_it_was(node):
    """§6: enforced and said, not enforced silently.

    An answer broadcasts nothing and files nothing, and it is still the gesture
    that takes a piece off the market -- so it spends the same dial as the offer
    that asked for it, which is the dial an operator who does not want their node
    trading can close in one place.
    """
    pair = _pair(node, 60, 61)
    offer = _offered(pair)
    holder_client = pair["holder"][0]
    leg = _leg(pair, offer)
    pair["state"].set_setting("quota:trade", 0)

    refused = _signed_answer(pair, offer, leg)
    assert refused.status_code == 400
    assert "trade" in refused.json()["detail"].lower(), refused.json()["detail"]
    assert pair["state"].offers.bids("regtest", "in") == []


# --- a price that is not coins ----------------------------------------------

def test_an_offer_priced_in_a_token_is_answered_by_the_same_leg(node):
    """What a token buys is not inside the transaction at all.

    The leg is the one from the first section, twice signed, two inputs, two
    outputs -- and its payment pays out no price, because a token does not move
    from one output to another. `state.Engine._check_leg` reads a BALANCE and
    `_move_leg` moves the amount between ledgers, on the strength of the payload
    alone. So the answer's `price`, which is the number of coins the leg hands
    over, is zero, and the price itself is in `take` and in the bytes.

    Which is why the leg is checked against `_swap_token` here rather than
    against a number: for a token there is no second record to agree with.
    """
    pair = _pair(node, 67, 68)
    pid = _priced_in(pair["state"], pair["bidder"][3], 10 * COIN, 101)
    offer = _offered(pair, amount="10", kind="token", property_id=pid)

    asked = pair["holder"][0].post("/account/accept",
                                   json={"piece": pair["piece"],
                                         "offer": offer})
    assert asked.status_code == 200, asked.text
    said = asked.json()
    assert int(said["price"]) == 0, "a token pays no coin inside the leg"
    assert said["take"]["kind"] == "token" \
        and int(said["take"]["units"]) == 10 * COIN
    assert "Testcoin" in said["what"], said["what"]
    assert said["payload"] == _swap_token(pair["piece"], pid, 10 * COIN).hex(), \
        "the bytes are the whole of the price, so they are what to check"
    assert len(said["sighashes"]) == 2
    assert pair["rpc"].call("getrawmempool") == []

    done = _signed_answer(pair, offer, said)
    assert done.status_code == 200, done.text
    (note,) = pair["state"].offers.bids("regtest", "in")
    assert note["take"]["kind"] == "token" \
        and int(note["take"]["units"]) == 10 * COIN, \
        "the note holds the token terms, which is what the far side compares"
    assert int(note["number"]) == said["number"]
    assert pair["state"].listings.for_piece(
        said["inputs"][0]["txid"], int(said["inputs"][0]["vout"])) == []


def test_a_leg_whose_bytes_take_another_token_price_is_not_an_answer(node):
    """The arithmetic closes on this one. Only the bytes give it away.

    A real leg, signed by the right key over two right coins, selling the right
    piece -- at the SECOND bidder's price, because it was built for that offer.
    For a coin that lie cannot survive: `Listings.register` works the price back
    out of what the leg pays out and refuses the row. A token pays nothing out,
    so the row closes at zero whatever the payload claims, and the only thing
    left is to read the payload and compare it with the offer -- which is what
    the request that takes the signatures back now does, and what this checks
    was not quietly true.
    """
    app, state, rpc = node
    pair = _pair(node, 69, 70)
    pid = _priced_in(pair["state"], pair["bidder"][3], 10 * COIN, 101)
    offer = _offered(pair, amount="10", kind="token", property_id=pid)

    other_client, osecret, opubkey, other = _seated(app, state, rpc, 71)
    _balance(state, other, pid, 20 * COIN)
    second = other_client.post("/account/offer", json={
        "piece": pair["piece"], "amount": "20", "kind": "token",
        "property_id": pid})
    assert second.status_code == 200, second.text
    second_id = _signed(other_client, osecret, opubkey,
                        second.json()).json()["txid"]
    _settled(state, rpc)

    priced = _leg(pair, second_id)
    assert int(priced["price"]) == 0
    assert priced["payload"] == _swap_token(pair["piece"], pid, 20 * COIN).hex()

    refused = _signed_answer(pair, offer, priced)
    assert refused.status_code == 400
    assert "price" in refused.json()["detail"], refused.json()["detail"]
    assert state.offers.bids("regtest", "in") == [], \
        "a refused answer reserves the piece for nobody"
    assert rpc.call("getrawmempool") == []


def test_the_token_answer_is_the_leg_a_wallet_finishes(node):
    """The wire contract again, this time with nothing in the payment.

    The same four things are what `shopkeeper._fill_a_leg` reads, and the same
    `register` call is what it makes -- with the price it takes from the note it
    wrote when it offered, which for a token is zero coins. So this asks whether
    the answer that route mails can be registered by a wallet that has to finish
    the trade, and whether the token terms survive the round trip: the bytes
    name the piece, the bytes take the token, and the `take` beside them says the
    same thing in the words a page would show. It is the far side's reading, run
    here where a failure costs nothing.
    """
    from arcade import listings as listingslib

    pair = _pair(node, 74, 75)
    pid = _priced_in(pair["state"], pair["bidder"][3], 10 * COIN, 101)
    offer = _offered(pair, amount="10", kind="token", property_id=pid)
    answer = _signed_answer(pair, offer, _leg(pair, offer)).json()["answer"]
    said = answer["leg"]

    row = pair["state"].listings.register(
        pair["rpc"], raw=said["raw"],
        signatures=list(said["signatures"]),
        pubkey=bytes.fromhex(said["pubkey"]), network="regtest",
        owner=said["seller"], price=0, record=False)
    assert int(row["price"]) == 0, "a token pays nothing into the leg"
    assert row["owner"] == pair["holder"][3]
    named = listingslib.named_swap(bytes.fromhex(row["payload"]))
    assert named is not None and named.give.txid.hex() == pair["piece"]
    assert named.take.kind == I.LEG_TOKEN \
        and named.take.property_id == pid \
        and int(named.take.amount) == 10 * COIN, \
        "the bytes are the price, and they say ten of them"
    assert said["take"]["kind"] == "token" \
        and int(said["take"]["units"]) == 10 * COIN \
        and said["take"]["name"] == "Testcoin"
    assert int(said["amount"]) == 0, \
        "and the coins it hands over, which is all `amount` ever meant, are none"


def test_an_answer_refuses_while_the_bidder_cannot_pay_its_own_price(node):
    """D-040, asked on the side that would spend a message fee to learn it.

    An offer escrows nothing: it is a message, and nothing stops a bidder
    spending the tokens it offered before anybody answers. A coin has the same
    hole and `holds` has always covered it by asking the ledger for the balance;
    for a token that question is the ONLY one that can be asked, so this is the
    one place a token answer is checked before a signature is rather than in a
    block that rejects the finished swap.
    """
    pair = _pair(node, 72, 73)
    pid = _priced_in(pair["state"], pair["bidder"][3], 10 * COIN, 101)
    offer = _offered(pair, amount="10", kind="token", property_id=pid)
    _balance(pair["state"], pair["bidder"][3], pid, 0)

    refused = pair["holder"][0].post("/account/accept",
                                     json={"piece": pair["piece"],
                                           "offer": offer})
    assert refused.status_code == 400
    assert "cannot pay" in refused.json()["detail"], refused.json()["detail"]
    assert "sighashes" not in refused.json(), \
        "no signature was asked for, which is the order this has to happen in"
    assert pair["rpc"].call("getrawmempool") == []
    assert pair["state"].offers.bids("regtest", "in") == []

    # And the refusal cost nothing, which is the part a node gets wrong by
    # answering first and noticing after. `note_committed` retires a coin for a
    # leg this account never sends; a refusal that had touched it would leave
    # this same request short of the second coin an answer needs.
    _balance(pair["state"], pair["bidder"][3], pid, 10 * COIN)
    again = pair["holder"][0].post("/account/accept",
                                   json={"piece": pair["piece"],
                                         "offer": offer})
    assert again.status_code == 200, again.text
    assert len(again.json()["sighashes"]) == 2, \
        "the refusal had retired a coin it never spent"


def test_a_token_answer_completes_with_the_bidder_s_own_signatures(node):
    """The account that offered is the account that finishes, in tokens.

    Both halves of this trade belong to accounts whose keys this node has never
    held, and until this change nothing in the tests walked from one to the
    other: `/account/fill` priced the leg it was handed out of `amount`, a token
    answer's `amount` is 0, and `parse_amount` refuses a zero by design -- so the
    route that exists to complete a private trade died on "the amount must be
    more than zero" instead of completing it. So this is the whole of it with
    nothing imported from the operator's side: an offer in a token, an answer the
    holder built and signed, and the bidder's own two requests.

    Hardest is the end. The piece moves, the engine calls the swap VALID, and the
    tokens cross -- which is the only proof that the payload the answer carried
    is the payload `register` priced the leg at, since for a token there is no
    second record of the price to agree with.
    """
    pair = _pair(node, 78, 79)
    pid = _priced_in(pair["state"], pair["bidder"][3], 10 * COIN, 101)
    offer = _offered(pair, amount="10", kind="token", property_id=pid)
    answer = _signed_answer(pair, offer, _leg(pair, offer)).json()["answer"]

    asked = pair["bidder"][0].post("/account/fill",
                                   json={"leg": answer["leg"]})
    assert asked.status_code == 200, asked.text
    said = asked.json()
    assert said["answered"] is True and int(said["price"]) == 0, \
        "a token pays no coin into the leg, and that is not a price of nothing"
    assert said["seller"] == pair["holder"][3], "whose piece, read off the leg"
    assert pair["state"].listings.open_listings("regtest") == [], \
        "an answer between two accounts is not a page for strangers"

    done = pair["bidder"][0].post("/account/fill/sign", json={
        "leg": answer["leg"], "raw": said["raw"],
        "pubkey": pair["bidder"][2].hex(),
        "signatures": [_sign(pair["bidder"][1], bytes.fromhex(d)).hex()
                       for d in said["sighashes"]]})
    assert done.status_code == 200, done.text
    assert int(done.json()["price"]) == 0
    assert done.json()["txid"] in pair["rpc"].call("getrawmempool")

    _settled(pair["state"], pair["rpc"])
    index = pair["state"].token_index(pair["state"].messaging)
    with contextlib.closing(index.open()) as db:
        verdict = db.conn.execute(
            "SELECT valid, invalid_reason FROM arcade_tx WHERE txid = ?",
            (done.json()["txid"],)).fetchone()
    assert verdict["valid"] == 1, verdict["invalid_reason"]
    assert index.inscription(pair["piece"])["owner"] == pair["bidder"][3], \
        "the piece crossed"
    assert int(index.balance(pair["bidder"][3], pid)) == 0
    assert int(index.balance(pair["holder"][3], pid)) == 10 * COIN, \
        "the price crossed the other way, in the ledger rather than an output"


def test_an_answer_taking_more_than_the_bidder_offered_is_not_funded(node):
    """The bytes are the price, so this is the place the bidder reads them.

    A token priced answer is the one trade whose price is nowhere a node is
    forced to agree: `register` closes its arithmetic at zero whatever the
    payload takes, because nothing in the transaction disagrees. `/account/accept`
    compares the bytes against the offer, but the account holding the piece is a
    node, not this node, and what arrives here travels through it. So this route
    compares them too, against the note `/account/offer` wrote from the offer the
    bidder broadcast -- and the refusal has to come before the coin, because the
    next request is a broadcast.

    The leg below is real: right piece, right two coins, right signatures, made
    for the SECOND bidder's offer of 20. Handing it to the one that offered 10 is
    the whole of the trick.
    """
    app, state, rpc = node
    pair = _pair(node, 80, 81)
    pid = _priced_in(pair["state"], pair["bidder"][3], 30 * COIN, 101)
    offer = _offered(pair, amount="10", kind="token", property_id=pid)

    other_client, osecret, opubkey, other = _seated(app, state, rpc, 82)
    _balance(state, other, pid, 20 * COIN)
    second = other_client.post("/account/offer", json={
        "piece": pair["piece"], "amount": "20", "kind": "token",
        "property_id": pid})
    assert second.status_code == 200, second.text
    second_id = _signed(other_client, osecret, opubkey,
                        second.json()).json()["txid"]
    _settled(state, rpc)

    answered = _signed_answer(pair, second_id, _leg(pair, second_id))
    assert answered.status_code == 200, answered.text
    forged = answered.json()["answer"]["leg"]
    assert int(forged["take"]["units"]) == 20 * COIN

    refused = pair["bidder"][0].post("/account/fill", json={"leg": forged})
    assert refused.status_code == 400
    assert "price" in refused.json()["detail"], refused.json()["detail"]

    signed = pair["bidder"][0].post("/account/fill/sign",
                                    json={"leg": forged})
    assert signed.status_code == 400, \
        "the second request is the one that broadcasts, and it refused nothing"
    assert rpc.call("getrawmempool") == []
    index = state.token_index(state.messaging)
    assert int(index.balance(pair["bidder"][3], pid)) == 30 * COIN, \
        "the tokens were spent by a refusal"


# --- the page that asks for it ---------------------------------------------

def test_the_piece_page_answers_for_the_account_that_holds_it(node):
    """The owner's row, which used to be an invitation to offer on itself.

    An account looking at its own piece was shown the offer form, because on
    the public path nothing ever said whose piece it was -- `mine` means "this
    node's wallet holds it", and on this instance it holds nobody's key. So the
    row is its own now, and it says the true thing: nothing has been offered,
    or here is what has, with the route that answers it.
    """
    app, state, rpc = node
    state.public = True
    pair = _pair(node, 62, 63)
    offer = _offered(pair)
    holder_client = pair["holder"][0]

    page = holder_client.get(f"/inscriptions/{pair['piece']}/view").text
    assert "Offered for it" in page and "data-offer" in page
    assert "/account/accept" not in page, \
        "the tab asks for the leg; the page holds no route of its own"
    assert "wallet.answerOffer" in page, "and the answer is the tab's own flow"
    assert "Make an offer" not in page, \
        "nobody is offered a fee to ask their own wallet for a piece"

    state.public = False
    page = holder_client.get(f"/inscriptions/{pair['piece']}/view").text
    assert 'action="/exchange/offer"' in page, \
        "the operator's copy is unchanged by any of this"


def test_the_offers_page_gives_an_account_the_button_it_lacked(node):
    """The sentence that said this could not be done, replaced by the doing.

    Two accounts, one page, and neither sees the other's mail: the row is cut
    out of the looking account's own address, exactly as it always was. What
    changed is only what the row holds -- and the operator's copy still holds
    its own form, posted at its own route, which a public instance shuts.
    """
    app, state, rpc = node
    state.public = True
    pair = _pair(node, 64, 65)
    offer = _offered(pair)
    holder_client = pair["holder"][0]

    page = holder_client.get("/exchange?tab=offers").text
    assert "answering an offer from a tab is not built yet" not in page
    assert offer in page and "data-offer" in page
    assert 'action="/exchange/offers/' not in page, \
        "no form posting at the route this instance shuts"

    _signed_answer(pair, offer, _leg(pair, offer))
    page = holder_client.get("/exchange?tab=offers").text
    assert "accepted" in page and "waiting for" in page, \
        "an answer stands out of the bid book, not out of the node's own offers"

    other, _secret, _pubkey, _address = _seated(app, state, rpc, 66)
    page = other.get("/exchange?tab=offers").text
    assert "Nothing yet." in page, "another person's post is not this account's mail"


def test_the_buyer_is_given_the_way_to_finish_an_answered_offer(node):
    """The half nobody could press (found filming the accept video, 2026-09-26).

    The seller answered, the answer went to the buyer as a sealed message, and
    no page anywhere called /account/fill. Now the buyer's Offers tab and the
    piece page carry a spot per offer of theirs, and the tab finds the answer in
    its own messages and turns the spot into "Complete the purchase". The
    finishing itself is /account/fill and /account/fill/sign, which the tests
    above already walk end to end.
    """
    app, state, rpc = node
    state.public = True
    pair = _pair(node, 80, 81)
    offer = _offered(pair)
    bidder = pair["bidder"][0]

    tab = bidder.get("/exchange?tab=offers").text
    assert f'data-answer-for="{offer}"' in tab, "a spot on the buyer's own row"
    assert "wallet.answersToMe" in tab and "wallet.fill(" in tab

    page = bidder.get(f"/inscriptions/{pair['piece']}/view").text
    assert "Your offer" in page and f'data-answer-for="{offer}"' in page
    holder = pair["holder"][0].get(f"/inscriptions/{pair['piece']}/view").text
    assert "Your offer" not in holder, "the holder answers; it has nothing to finish"
