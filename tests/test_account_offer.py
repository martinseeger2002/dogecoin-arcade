"""An account offers for somebody else's piece, with a key the node never saw.

An offer is the cheapest gesture an account can make on the chain and the
easiest to get wrong. It moves no coins beyond a fee, reserves nothing -- not
the piece, which belongs to whoever holds it, and not the price, which belongs
to whoever offers it -- and it is read back by a *stranger's* node rather than
by the one that wrote it. So what matters is the payload: an `Offer` in an
`AnyData` in one OP_RETURN, and it is written in this file as well as in the
route, because the two agreeing byte for byte is the whole of the promise that
"I would pay X for piece Y" arrives saying exactly that (D-038).

Two accounts appear in almost every test, and neither is decoration: an offer
is by definition made on a piece that is not yours, so the only way to ask
whether the route reads ownership off the chain or off whoever typed the piece
in first is to have somebody else holding it.

Against a real regtest node, for the reason every account file gives: the parts
that can be wrong here -- which address the ledger credits the offer to, what
the scanner makes of the bytes, whether the answer can find its way back to a
key this node never held -- all look right on paper.
"""

import base64
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_claim import _catch_up, _sign_in                   # noqa: E402
from test_funding import _pubkey, _sign                              # noqa: E402
from test_web import app_state, client                               # noqa: F401,E402

from arcade import encoding, inscriptions as I                       # noqa: E402
from arcade import payload as P                                      # noqa: E402
from arcade.messaging.scanner import Scanner                         # noqa: E402
from arcade.script import b58check_encode, hash160                   # noqa: E402

COIN = 100_000_000


@pytest.fixture
def node(tmp_path, regtest):
    """The application pointed at a regtest node, with nothing in it."""
    from fastapi.testclient import TestClient

    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    class Pointed(ChainContext):
        def credentials(self):
            return regtest.rpc._creds

        @property
        def params(self):
            return regtest.params

    chain = Pointed(network="regtest", role="messaging", label="Testnet",
                    datadir=regtest.datadir)
    state = AppState(home=tmp_path, messaging=chain,
                     ledger=ChainContext(network="main", role="ledger",
                                         label="Mainnet",
                                         datadir=pathlib.Path("/nonexistent")))
    (tmp_path / "tokens-chain").write_text("regtest\n")
    return TestClient(create_app(state)), state, regtest.rpc


def _settled(state, rpc):
    """Mine what is waiting, then read it back twice: the ledger and the mail.

    Two reads because an offer has two readers. The token index is what says
    who holds the piece and what a token costs; the messaging scan is what
    turns an announcement into a key a node can seal to, and it is the reason
    an account with a published key can be ANSWERED. A file that synced only
    the first would pass every test here except the one that matters most.
    """
    rpc.call("generate", 1)
    _catch_up(state, rpc)
    with state.store() as store:
        # A node that adopts a messaging key today reads the mail from today
        # onwards, not from block 0: `state.use_derived_identity` writes this
        # meta and `Scanner.start_height` honours it as a floor. A test store
        # never adopts an identity, so without it the first scan of a test
        # walks every block the earlier tests mined -- one of the two full
        # walks a test here pays for, the other being the index in `_catch_up`.
        # Nothing in this file reads history: the announcement being looked for
        # was mined one block ago.
        name = state.messaging.network
        if store.get_meta(f"identity_height:{name}") is None:
            store.set_meta(f"identity_height:{name}",
                           str(max(rpc.call("getblockcount") - 30, 0)))
        for _ in range(50):
            if Scanner(rpc, state.messaging.params, store,
                       identity=None).scan().blocks == 0:
                break


def _seated(app, state, rpc, which: int, coins=(4.0, 1.0),
            key: bool = True):
    """A person who just took a seat here: a coin key, some coins, a name on the chain.

    Their own `TestClient`, because a cookie jar holds one account and an
    offer needs two of them -- the one who holds the piece and the one
    offering for it -- acting on the same node.

    `key` is the messaging key, published. It is what makes an account
    answerable: an offer to an address with no announcement cannot be
    accepted by anybody, so the fee spent on it is a fee spent to be refused.
    Signing up does this in a browser; here it is one more transaction.
    """
    from fastapi.testclient import TestClient

    mine = TestClient(app.app)
    _sign_in(mine)
    secret = int.from_bytes(bytes([0x61, which]) + bytes(30), "big")
    pubkey = _pubkey(secret)
    address = b58check_encode(state.messaging.params.pubkeyhash_version,
                              hash160(pubkey))
    assert not rpc.call("validateaddress", address).get("ismine"), \
        "the node has no key for this address, which is the whole point"
    rpc.call("generate", 101)
    _catch_up(state, rpc)
    assert mine.post("/account/address", json={
        "address": address, "coin_pubkey": pubkey.hex()}).status_code == 200
    for amount in coins:
        rpc.call("sendtoaddress", address, amount)
    _settled(state, rpc)
    if key:
        said = mine.post("/account/announce",
                         json={"key": (bytes([0x77, which]) + bytes(30)).hex()})
        assert said.status_code == 200, said.text
        _signed(mine, secret, pubkey, said.json())
        _settled(state, rpc)
    return mine, secret, pubkey, address


def _inscribed(who, state, rpc, secret, pubkey, text: str) -> str:
    """One piece this account put on the chain itself, and its txid."""
    offered = who.post("/account/inscribe", json={
        "content": base64.b64encode(text.encode()).decode(),
        "content_type": "text/plain; charset=utf-8"})
    assert offered.status_code == 200, offered.text
    done = _signed(who, secret, pubkey, offered.json())
    assert done.status_code == 200, done.text
    _settled(state, rpc)
    return done.json()["txid"]


def _signed(who, secret, pubkey, offer):
    """Sign what was offered, exactly as offered, and hand it back."""
    return who.post("/account/sign", json={
        "offer": offer["offer"], "pubkey": pubkey.hex(),
        "signatures": [_sign(secret, bytes.fromhex(d)).hex()
                       for d in offer["sighashes"]]})


def _payload(txid: str, price: int) -> bytes:
    """The bytes an offer writes: `Offer` inside `AnyData`, in one OP_RETURN.

    Written here as well as in the route on purpose -- the same reason
    `test_account_list` writes a listing's payload twice. The offer that the
    other side's node reads is this one, and the only check worth having is
    that a route nobody was looking at did not write a different trade.
    """
    body = I.Offer(txid=bytes.fromhex(txid),
                   take=I.Leg(I.LEG_COINS, amount=price)).encode()
    return encoding.encode_class_c(P.AnyData(data=body).encode())


def _pair(node, holder: int, bidder: int, coins=(4.0, 1.0), key=True):
    """Somebody holding a piece, and somebody else who wants it."""
    app, state, rpc = node
    holder_client, hsecret, hpubkey, hold = _seated(app, state, rpc, holder)
    piece = _inscribed(holder_client, state, rpc, hsecret, hpubkey,
                       f"piece {holder}")
    bidder_client, bsecret, bpubkey, bid = _seated(
        app, state, rpc, bidder, coins=coins, key=key)
    return {"app": app, "state": state, "rpc": rpc, "piece": piece,
            "holder": (holder_client, hsecret, hpubkey, hold),
            "bidder": (bidder_client, bsecret, bpubkey, bid)}


# --- what the offer is made of ----------------------------------------------

def test_an_offer_is_one_transaction_saying_what_it_would_pay(node):
    """The screen before signing: the bytes, the fee, and nothing spent.

    Asking is not offering. Nothing is in the mempool yet, no coin of this
    account is locked or reserved, and the piece stays exactly where it was.
    """
    pair = _pair(node, 1, 2)
    bidder_client, _bsecret, _bpubkey, bid = pair["bidder"]

    asked = bidder_client.post("/account/offer",
                               json={"piece": pair["piece"], "amount": "1"})
    assert asked.status_code == 200, asked.text
    offer = asked.json()
    assert _payload(pair["piece"], COIN).hex() in offer["raw"], \
        "the payload it offers is the payload the other side will read"
    assert {coin["address"] for coin in offer["inputs"]} == {bid}, \
        "every coin is the offerer's own; this node funds itself out of nothing"
    assert offer["fee"] > 0, "an offer is a transaction and a fee is its cost"
    assert f"#{offer['number']}" in offer["what"] and "1 coins" in offer["what"]
    assert pair["rpc"].call("getrawmempool") == [], "asking spends nothing"


def test_the_offer_lands_and_the_holders_node_reads_it_back(node):
    """The whole point of the exercise: a stranger's node can see the offer.

    The chain is what an answer gets checked against, and the holder is not
    obliged to have ever published a key or visited this node (D-042). So the
    offer is looked for the way their node looks for it -- `offers_on`, read
    off the ledger -- and not in any book this node keeps.
    """
    pair = _pair(node, 3, 4)
    bidder_client, bsecret, bpubkey, bid = pair["bidder"]
    holder_client, _hsecret, _hpubkey, hold = pair["holder"]
    index = pair["state"].token_index(pair["state"].messaging)

    asked = bidder_client.post("/account/offer",
                               json={"piece": pair["piece"], "amount": "1"})
    done = _signed(bidder_client, bsecret, bpubkey, asked.json())
    assert done.status_code == 200, done.text
    _settled(pair["state"], pair["rpc"])

    standing = index.offers_on([hold])
    assert [o["buyer"] for o in standing] == [bid], \
        "the offer reaches the holder by the chain, addressed to the offerer"
    (offer,) = standing
    assert offer["inscription"] == pair["piece"]
    assert int(offer["take_amount"]) == COIN and int(offer["take_kind"]) == 3
    assert index.offer(done.json()["txid"])["txid"] == done.json()["txid"]
    # The piece did not move, and nothing was reserved against it.
    assert index.inscription(pair["piece"])["owner"] == hold


def test_the_node_remembers_what_it_asked_for(node):
    """The note in `state.offers`, written by the same route that broadcast.

    It is not what any page reads -- every offer a page shows comes off the
    index -- but a reply that arrives before this offer's own block needs its
    terms, and a node that never wrote the row would have nothing to check it
    against. Same book, same shape, as the operator's route leaves.
    """
    pair = _pair(node, 5, 6)
    bidder_client, bsecret, bpubkey, bid = pair["bidder"]

    asked = bidder_client.post("/account/offer",
                               json={"piece": pair["piece"], "amount": "1"})
    txid = _signed(bidder_client, bsecret, bpubkey, asked.json()
                   ).json()["txid"]

    (bid_row,) = pair["state"].offers.bids("regtest", "out")
    assert bid_row["id"] == txid, "keyed by the transaction that made it"
    assert bid_row["owner"] == pair["holder"][3] and bid_row["buyer"] == bid
    assert bid_row["inscription"] == pair["piece"]
    assert bid_row["take"]["kind"] == "coins" and bid_row["take"]["sats"] == \
        COIN
    assert bid_row["expires"] > bid_row["created"]


# --- what it refuses, and why ---------------------------------------------

def test_an_account_that_published_no_key_cannot_offer(node):
    """Nobody can answer it, so the fee would buy a dead letter.

    The offer itself goes on the chain and needs no key of the holder's. What
    a key is needed FOR is the answer: a seller's node looks the buyer's
    address up to find something to seal its reply to, and refuses the offer
    outright when the lookup comes back empty. Refusing there would be the
    honest answer and a bad one -- by then the fee was paid.
    """
    pair = _pair(node, 7, 8, key=False)
    bidder_client, _bsecret, _bpubkey, bid = pair["bidder"]
    with pair["state"].store() as store:
        assert store.key_for(bid) is None, "no announcement on the chain"

    refused = bidder_client.post("/account/offer",
                                 json={"piece": pair["piece"], "amount": "1"})
    assert refused.status_code == 400
    assert "publish your key" in refused.json()["detail"]
    assert pair["rpc"].call("getrawmempool") == []
    assert pair["state"].offers.bids("regtest", "out") == []


def test_a_piece_you_hold_yourself_is_not_offerable(node):
    """It is the refusal the operator's route gives, and it comes from the chain.

    An offer on your own piece spends a fee to tell yourself something you
    know. The account's own address is the whole of the test -- the route is
    being asked whether it read the row's owner or trusted the request.
    """
    app, state, rpc = node
    who, secret, pubkey, _address = _seated(app, state, rpc, 9)
    piece = _inscribed(who, state, rpc, secret, pubkey, "my own piece")

    refused = who.post("/account/offer", json={"piece": piece, "amount": "1"})
    assert refused.status_code == 400
    assert "already yours" in refused.json()["detail"]
    assert rpc.call("getrawmempool") == []


def test_an_offer_of_more_than_the_account_holds_is_refused_before_the_fee(node):
    """D-040, moved forward to before the fee is spent.

    An offer is not a promise this node could keep: it holds neither the piece
    nor the price, and nothing in this transaction reserves either. So the
    only thing stopping a five-hundred-coin offer from an address holding five
    is this check, and the refusal that would otherwise arrive comes from
    whoever holds it, a block later, having done nothing wrong.
    """
    pair = _pair(node, 11, 12, coins=(4.0, 1.0))
    bidder_client, _bsecret, _bpubkey, _bid = pair["bidder"]

    refused = bidder_client.post("/account/offer",
                                 json={"piece": pair["piece"], "amount": "500"})
    assert refused.status_code == 400
    detail = refused.json()["detail"]
    assert "less than" in detail and "500" in detail, detail
    assert pair["rpc"].call("getrawmempool") == []

    again = bidder_client.post("/account/offer",
                               json={"piece": pair["piece"], "amount": "4"})
    assert again.status_code == 200, again.text
    # the coins it does hold are still offerable


def test_a_price_in_a_token_that_does_not_exist_is_refused(node):
    """The token half of the same rule, at the cheapest thing it can cost.

    `leg_of` will not build a leg in a property the ledger has never heard of,
    so this account is refused before the balance is asked about. The
    balance-only refusal beneath it -- an offer in a token the account does not
    hold -- is the same two lines as the coin case above and reads the ledger
    rather than this node's index of an address.
    """
    pair = _pair(node, 13, 14)
    bidder_client, _bsecret, _bpubkey, _bid = pair["bidder"]

    refused = bidder_client.post("/account/offer", json={
        "piece": pair["piece"], "amount": "1", "kind": "token",
        "property_id": "424242"})
    assert refused.status_code == 400
    assert "no token" in refused.json()["detail"]
    assert pair["rpc"].call("getrawmempool") == []


def test_no_such_piece_and_no_price_are_refused(node):
    """Two sentences a form gets wrong more often than any other."""
    pair = _pair(node, 15, 16)
    bidder_client, _bsecret, _bpubkey, _bid = pair["bidder"]

    gone = bidder_client.post("/account/offer",
                              json={"piece": "ab" * 32, "amount": "1"})
    assert gone.status_code == 400
    assert "no such inscription" in gone.json()["detail"]

    silent = bidder_client.post("/account/offer",
                                json={"piece": pair["piece"], "amount": ""})
    assert silent.status_code == 400
    # The route's own sentence, which is what `mintpad.take_of` says of a price
    # nobody typed -- an offer reuses the mintpad's reading of a leg, so it
    # inherits that wording rather than inventing a second one for the same gap.
    assert "say what one costs" in silent.json()["detail"]
    assert pair["rpc"].call("getrawmempool") == []


def test_an_operator_who_closed_trades_says_which_dial_closed_them(node):
    """Zero is a decision somebody made, not a number this account ran out of.

    One dial covers both trading surfaces -- an offer and an order are one
    gesture seen from different pages -- so the sentence names trades, and an
    account whose operator has closed them is left with another node or its
    own, which is what §6 says a closed door must say.
    """
    pair = _pair(node, 17, 18)
    bidder_client, _bsecret, _bpubkey, _bid = pair["bidder"]
    pair["state"].set_setting("quota:trade", 0)

    refused = bidder_client.post("/account/offer",
                                 json={"piece": pair["piece"], "amount": "1"})
    assert refused.status_code == 400
    assert "not taking trades" in refused.json()["detail"]
    assert pair["rpc"].call("getrawmempool") == []

    pair["state"].set_setting("quota:trade", 30)
    assert bidder_client.post("/account/offer", json={
        "piece": pair["piece"], "amount": "1"}).status_code == 200


# --- the page that asks for it --------------------------------------------

def test_the_piece_page_offers_an_account_its_own_form(node):
    """The section that said, in words, that this could not be done.

    It used to, and it was right at the time: what was under it was the
    operator's form, which asks this node to sign something only the
    visitor's key can sign. So the test is that the sentence is gone, the
    fields are there, and the one thing that must not come back -- a form
    posted at the node's own `/exchange/offer` -- stays off a public page.
    """
    app, state, rpc = node
    state.public = True
    who, secret, pubkey, _address = _seated(app, state, rpc, 19)
    piece = _inscribed(who, state, rpc, secret, pubkey, "looked at publicly")

    page = who.get(f"/inscriptions/{piece}/view").text
    assert "Make an offer" in page
    assert 'id="offer-amount"' in page and 'id="offer-it"' in page
    assert "/account/offer" in page, "and it asks the account's own route"
    assert 'action="/exchange/offer"' not in page, \
        "a public page never offers the form this instance refuses"
    # The sentence, not the phrase: base.html says out loud that the nav items
    # which are not built yet are not links, and that comment is a good thing.
    assert "Offering from an account is not built yet" not in page

    state.public = False
    page = who.get(f"/inscriptions/{piece}/view").text
    assert 'action="/exchange/offer"' in page, \
        "the operator's own copy still offers its own form"


# --- the page that says whose offers these are ------------------------------

def test_the_offers_page_shows_an_account_only_offers_of_theirs(node):
    """One offer, seen by two accounts, and neither sees the other's mail.

    `/exchange` is in the account's navigation and the door opens it to a
    stranger, so it is a public page. Until now it answered "whose pieces are
    these" with this machine's wallet whoever asked -- which is exactly right
    on the operator's own copy and wrong everywhere else: an account was shown
    the offers standing on pieces this node holds and offered a button that
    posts to a route a public instance refuses. The chain already knows whose
    piece an offer sits on, and `offers_on` takes the address list it is handed
    and nothing else, so this is one read changed rather than a page rewritten
    (D-172).

    The button stays off, deliberately. Accepting means signing a leg with the
    key that holds the piece, and on this instance that key is in the tab and
    nowhere else -- which is the half that is not built, and what the page says
    instead of a button that would be refused.
    """
    app, state, rpc = node
    state.public = True
    pair = _pair(node, 21, 22)
    holder_client, _hsecret, _hpubkey, _hold = pair["holder"]
    bidder_client, bsecret, bpubkey, bid = pair["bidder"]

    asked = bidder_client.post("/account/offer",
                               json={"piece": pair["piece"], "amount": "1"})
    assert asked.status_code == 200, asked.text
    assert _signed(bidder_client, bsecret, bpubkey,
                   asked.json()).status_code == 200
    _settled(state, rpc)

    page = holder_client.get("/exchange?tab=offers").text
    assert bid in page, "the offer that stands on this account's own piece"
    assert 'action="/exchange/offers/' not in page, \
        "no button that posts at the door this instance shuts"
    assert "not built yet" in page

    other, _secret, _pubkey, _address = _seated(app, state, rpc, 23)
    page = other.get("/exchange?tab=offers").text
    assert "Nothing yet." in page and bid not in page, \
        "another person's post is not this account's mail"


def test_the_market_page_prices_for_whoever_is_looking(node):
    """The same table, three sentences, and none of them a refused route.

    A listing row offers one of two things: a form to make an offer, or the
    way to change a price that is already yours. Both of those used to be the
    operator's -- the form spends this node's wallet, and the price page lives
    under `/exchange/sell/`, which a public instance does not open -- so an
    account saw two controls it could not use. Here the first becomes the piece
    page, which is where an account's own offer form already lives, and the
    second becomes the account's NFTs page, which is where listing lives.

    The operator's copy is in this test for the same reason the piece page is:
    the branch has to be a per-viewer read and not a swap, and the way to know
    it stayed a read is to ask the same page as the person it was always for.
    """
    app, state, rpc = node
    state.public = True
    pair = _pair(node, 31, 32)
    holder_client, hsecret, hpubkey, _hold = pair["holder"]
    bidder_client, bsecret, bpubkey, _bid = pair["bidder"]

    theirs = _inscribed(bidder_client, state, rpc, bsecret, bpubkey,
                        "a piece somebody else priced")
    for (who, secret, pubkey, piece) in ((holder_client, hsecret, hpubkey,
                                          pair["piece"]),
                                         (bidder_client, bsecret, bpubkey,
                                          theirs)):
        priced = who.post("/account/nft/sell", json={
            "piece": piece, "amount": "2", "kind": "coins"})
        assert priced.status_code == 200, priced.text
        assert _signed(who, secret, pubkey,
                       priced.json()).status_code == 200
        _settled(state, rpc)

    page = holder_client.get("/exchange?tab=market").text
    assert 'action="/exchange/offer"' not in page, \
        "a public page never offers the form this instance refuses"
    assert f'href="/inscriptions/{theirs}/view">Make offer' in page, \
        "an offer comes from the piece, where the account's own form is"
    assert 'href="/me/nfts"' in page and "Yours" in page, \
        "and a price of yours is changed where an account prices"

    state.public = False
    page = holder_client.get("/exchange?tab=market").text
    assert 'action="/exchange/offer"' in page, \
        "the operator's own copy still offers its own form"

