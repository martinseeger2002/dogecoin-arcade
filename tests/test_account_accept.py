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

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import (_inscribed, _pair, _seated,          # noqa: E402
                                _settled, _signed, node)
from test_funding import _sign                                         # noqa: E402
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
