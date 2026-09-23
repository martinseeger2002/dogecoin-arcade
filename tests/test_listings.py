"""A listing: a signature the node was handed, and the swap it finishes into.

`arcade/listings.py` is the state where the node holds no key at all. An account
signs a leg in its own browser, hands the signature over, and the node's only
power is to complete it or to stand out of the way. That makes the interesting
failures invisible in the happy path, so they are asserted here rather than
assumed: a row whose numbers do not match the bytes it stores, a leg that pays
somewhere its signer never agreed to, a signature made by a key that is not the
address the row is listed from, a listing whose piece has already been spent, a
finished swap that leaves too little for a block.

Half of the file needs no coins. `check_leg` reads a leg and not the chain, so
the checks that a listing page's numbers rest on can be proved against a
transaction out of no chain at all -- and they are the checks that an
unvalidated POST would otherwise skip straight into a public page. Where a
transaction has to be judged it is judged by a real pepecoind, and both the
seller's and the buyer's signatures are made in this file by a throwaway secp:
the node is never given either key, which is the difference between this book
and `swap.Offers`.
"""

import inspect
import time
from types import SimpleNamespace

import pytest

from arcade import fees, funding, listings, utxos
from arcade.script import b58check_encode, hash160
from arcade.txbuild import build_raw_tx, op_return_script, p2pkh_script, push

from test_funding import COIN, PARAMS, _pubkey, _sign, db
from test_funding_leg import _coin, _index, _key, _named, _paste, _two_coins

NETWORK = "regtest"

#: The rate a listing is priced at: what a block asks, which is also the floor
#: `paste_leg` refuses to complete under. Priced quieter than this, a swap
#: relays and never confirms, and both sides believe a trade is done.
RATE = fees.MIN_FEE_PER_KB
LISTED_FOR = 7200.0

#: Two accounts, and this node holds the key of neither.
SELLER = 0x5566778899001122556677889900112255667788990011225566778899001122
BUYER = 0x6677889900112233667788990011223366778899001122336677889900112233

#: A piece out of no chain at all, for the checks that read a leg rather than
#: the chain.
PIECE = {"txid": "ab" * 32, "vout": 0, "value": 5 * COIN}

#: A second coin of the same seller's, for the same reason. A leg that names
#: what it sells has two of these, because one signature reaches one output.
SPARE = {"txid": "cd" * 32, "vout": 2, "value": COIN}

#: What a named listing sells, as payload DATA. Not a valid arcade swap on
#: purpose: a pepecoind never reads these bytes, it only checks that a
#: signature stands over the output carrying them.
NAMING = _named(b"the-piece" * 3)


@pytest.fixture
def book(tmp_path):
    return listings.Listings(tmp_path / "listings.sqlite")


def _listed(rpc, db, book, secret=SELLER, price=COIN, rate=RATE,
            amount=5.0) -> tuple[dict, bytes, str]:
    """A piece put up for sale by a wallet whose key this node never had."""
    pubkey, address = _key(rpc, db, secret, amount)
    piece = _coin(db, address)
    leg = funding.build_leg(PARAMS, address, piece, coins=price, rate=rate)
    signatures = [_sign(secret, bytes.fromhex(digest),
                        funding.SINGLE_ANYONECANPAY).hex()
                  for digest in leg.sighashes]
    listing = book.from_leg(rpc, leg, signatures, pubkey, NETWORK, address,
                            price, LISTED_FOR)
    return listing, pubkey, address


def _asked(rpc, db, secret=SELLER, price=COIN, rate=RATE,
           amount=5.0) -> tuple[funding.Leg, bytes, str]:
    """The half of a listing that exists before a browser has signed anything.

    The leg and its signatures are what the second request carries back; nothing
    here remembers the first one, which is the thing these tests are about.
    """
    pubkey, address = _key(rpc, db, secret, amount)
    leg = funding.build_leg(PARAMS, address, _coin(db, address), coins=price,
                            rate=rate)
    return leg, pubkey, address


def _filed(book, rpc, leg, secret, pubkey, address, price=COIN,
           what="a piece, priced"):
    """File a leg the way the route will: bytes in, nothing remembered."""
    signatures = [_sign(secret, bytes.fromhex(digest),
                        funding.SINGLE_ANYONECANPAY).hex()
                  for digest in leg.sighashes]
    return book.register(rpc, raw=leg.raw, signatures=signatures,
                         pubkey=pubkey, network=NETWORK, owner=address,
                         price=price, seconds=LISTED_FOR, what=what)


def _buy(rpc, db, secret, address, listing, rate=RATE) -> funding.Unsigned:
    """The buyer's half, the way a node that holds nobody's key builds one.

    The listed piece goes first and stays unsigned; the payment to the seller
    goes at output 0, because that is the only position the leg's signature
    reaches. What comes back is what a browser would be shown.
    """
    piece = listing["input"]
    paid = (int(listing["output"]["value"]),
            bytes.fromhex(listing["output"]["script"]))
    return funding.build_partial(
        db, PARAMS, address, [piece],
        [paid, (int(piece["value"]), p2pkh_script(address))],
        rate=rate, what="a listing")


def _signatures(secret, unsigned: funding.Unsigned) -> list[str]:
    return [_sign(secret, bytes.fromhex(h)).hex() for h in unsigned.sighashes]


def _named_listing(rpc, db, book, secret=SELLER, price=COIN, rate=RATE,
                   amount=5.0, naming: bytes = NAMING) -> tuple[dict, bytes, str]:
    """A piece put up for sale with the bytes that say what it is.

    `_listed`'s twin in the other shape: two coins of the seller's go in, one to
    sign the payload and one to sign the payment, because a `SINGLE` signature
    reaches the output standing at its own input's index and no other.

    `utxos.unspent` answers largest first, so the bigger coin is always the
    piece. That matters in the tests that spend one of the two: the one they
    leave behind has to be the one the listing still names.
    """
    pubkey, address = _key(rpc, db, secret, amount)
    piece, spare = _two_coins(rpc, db, address)[:2]
    leg = funding.build_leg(PARAMS, address, piece, coins=price, rate=rate,
                            payload=naming, coin=spare)
    signatures = [_sign(secret, bytes.fromhex(digest),
                        funding.SINGLE_ANYONECANPAY).hex()
                  for digest in leg.sighashes]
    listing = book.from_leg(rpc, leg, signatures, pubkey, NETWORK, address,
                            price, LISTED_FOR)
    return listing, pubkey, address


def _bid(rpc, db, secret, address, listing, rate=RATE) -> funding.Unsigned:
    """The buyer's half of a listing that names its piece.

    Both of the seller's coins come first and neither is signed here: input 0 is
    the piece and input 1 is the coin its second signature stands on. Then the
    payload at output 0 and the payment at output 1, in that order and nowhere
    else -- those two positions are what the seller's two digests were made
    over, and a byte moved between them is a different trade.
    """
    piece, coin = listing["input"], listing["coin"]
    payload = bytes.fromhex(listing["payload"])
    paid = (int(listing["output"]["value"]),
            bytes.fromhex(listing["output"]["script"]))
    return funding.build_partial(
        db, PARAMS, address, [piece, coin],
        [(0, op_return_script(payload)), paid,
         (int(piece["value"]), p2pkh_script(address))],
        rate=rate, what="a listing that names its piece")


def _withdraw(rpc, db, secret, address, value: int) -> str:
    """Spend a wallet's whole coin back to itself, without this node's help.

    Twice in this file and once more below, because it is the one action that
    matters twice over: it is how a piece comes off the market, and it is the
    only way a listing is ever cancelled. The key never comes near this node, so
    the transaction is made here -- and the payment has to leave room for a fee
    priced the way a listing prices everything, at what a block asks.
    """
    fee = funding.price(1, [(0, p2pkh_script(address))], RATE, change=True)
    away = funding.build(db, PARAMS, address, [(value - fee,
                                                p2pkh_script(address))],
                         rate=RATE, what="withdrawing it")
    raw = funding.assemble(away, [_sign(secret, bytes.fromhex(h)).hex()
                                  for h in away.sighashes], _pubkey(secret))
    txid = rpc.call("sendrawtransaction", raw)
    rpc.call("generate", 1)
    return txid


def _landed(rpc, txid: str) -> list[tuple[int, str]]:
    return [(int(round(out["value"] * COIN)), out["scriptPubKey"]["hex"])
            for out in rpc.call("getrawtransaction", txid, True)["vout"]]


def _no_half() -> SimpleNamespace:
    """A buyer's half that is never reached, for the refusals before it."""
    return SimpleNamespace(inputs=[], outputs=[], sighashes=[])


def test_a_listing_fills_and_pays_what_the_leg_said(regtest, db, book):
    """The whole path, with a node that signs nothing.

    List, buy, combine, broadcast, mine. The seller's output is the one it
    signed before it had a buyer, and the buyer's own signature is what makes
    the rest of the transaction honest -- which is the pairing a leg was built
    to allow, and nothing wider than that.
    """
    rpc = regtest.rpc
    listing, _seller_key, seller = _listed(rpc, db, book, SELLER, price=COIN)
    buyer_key, buyer = _key(rpc, db, BUYER, 8.0)

    half = _buy(rpc, db, BUYER, buyer, listing)
    assert half.signed_from == 1, "the listed piece is input 0, and not ours to sign"
    assert half.outputs[0][0] == listing["output"]["value"]

    raw = listings.paste_leg(rpc, listing, half, _signatures(BUYER, half),
                             buyer_key)
    filled = rpc.call("sendrawtransaction", raw)
    rpc.call("generate", 1)
    assert len(filled) == 64 and rpc.call("getrawtransaction", filled, 1)["confirmations"] >= 1, \
        "a block took a swap this node only pasted together"

    landed = _landed(rpc, filled)
    assert (int(listing["output"]["value"]),
            listing["output"]["script"]) in landed, \
        "the seller was not paid what its own signature committed to"
    assert (int(listing["input"]["value"]), p2pkh_script(buyer).hex()) in landed, \
        "the buyer did not end up holding the piece it paid for"
    assert len(landed) == 3, "seller, buyer, change: an output nobody counted"

    book.close(listing["id"], "filled", spent_by=filled)
    again = book.get(listing["id"])
    assert again["status"] == "filled" and again["spent_by"] == filled
    assert again["owner"] == seller


def test_a_listing_is_filed_from_its_bytes_and_not_from_a_memory(regtest, db,
                                                                 book):
    """The door a route files through: two requests, nothing kept between them.

    `from_leg` is what code that just built a leg calls. This is what happens
    when the signature arrives in a later request, possibly at a node that has
    been restarted since: the leg's bytes and the chain are the whole evidence,
    and the arithmetic has to close against both.
    """
    rpc = regtest.rpc
    leg, seller_key, seller = _asked(rpc, db, SELLER, price=COIN)
    listing = _filed(book, rpc, leg, SELLER, seller_key, seller, price=COIN)

    assert listing["fee"] == funding.swap_fee(RATE), \
        "a row priced from memory would say something else here"
    assert listing["price"] == COIN and listing["owner"] == seller
    assert listing["status"] == "open"

    buyer_key, buyer = _key(rpc, db, BUYER, 8.0)
    half = _buy(rpc, db, BUYER, buyer, listing)
    raw = listings.paste_leg(rpc, listing, half, _signatures(BUYER, half),
                             buyer_key)
    filled = rpc.call("sendrawtransaction", raw)
    rpc.call("generate", 1)
    landed = _landed(rpc, filled)
    assert (int(listing["output"]["value"]),
            listing["output"]["script"]) in landed, \
        "a listing filed from bytes has to pay exactly what one filed from a Leg pays"
    assert (int(listing["input"]["value"]), p2pkh_script(buyer).hex()) in landed


def test_a_leg_is_refused_unless_its_numbers_close(regtest, db, book):
    """The price is not in the signature, so it is arithmetic or nothing.

    A leg pays its seller `piece + price - fee`. Post a price that is not the one
    the signed output was built from and the row would advertise a bargain no
    signature commits to -- which is the same lie as an unsigned listing, only
    quieter, because the row looks checked.
    """
    rpc = regtest.rpc
    leg, seller_key, seller = _asked(rpc, db, SELLER, price=COIN)
    with pytest.raises(listings.ListingError) as refused:
        _filed(book, rpc, leg, SELLER, seller_key, seller, price=2 * COIN)
    assert "not built from this piece at this price" in str(refused.value), \
        str(refused.value)

    _withdraw(rpc, db, SELLER, seller, int(leg.inputs[0]["value"]))
    with pytest.raises(listings.ListingError) as gone:
        _filed(book, rpc, leg, SELLER, seller_key, seller, price=COIN)
    assert "not a piece to list" in str(gone.value), str(gone.value)
    assert book.open_listings(NETWORK) == [], \
        "a refusal that filed a row would have put a ghost on the listings page"


def test_a_leg_is_refused_when_the_bytes_are_not_a_legs(regtest, db, book):
    """A signature for a different transaction, and a leg that arrived finished.

    A SIGHASH_ALL signature over a leg's own digest is not a seller's leg: it
    commits to every output, so the swap it belongs to has to be known before
    the key is used -- exactly the thing a leg exists to avoid. It would be
    refused by the network eventually, when the buyer's own outputs were added,
    and by then a listing page would be advertising a piece nobody can buy.
    """
    rpc = regtest.rpc
    leg, seller_key, seller = _asked(rpc, db, SELLER, price=COIN)
    with pytest.raises(listings.ListingError) as signed:
        book.register(rpc, raw=leg.raw,
                      signatures=[_sign(SELLER, bytes.fromhex(leg.sighashes[0]),
                                        funding.SIGHASH_ALL).hex()],
                      pubkey=seller_key, network=NETWORK, owner=seller,
                      price=COIN, seconds=LISTED_FOR)
    assert "SINGLE|ANYONECANPAY" in str(signed.value), str(signed.value)

    pasted = listings.sign_leg(
        leg, [_sign(SELLER, bytes.fromhex(leg.sighashes[0]),
                    funding.SINGLE_ANYONECANPAY).hex()], seller_key)
    with pytest.raises(listings.ListingError) as twice:
        book.register(rpc, raw=pasted,
                      signatures=[_sign(SELLER, bytes.fromhex(leg.sighashes[0]),
                                        funding.SINGLE_ANYONECANPAY).hex()],
                      pubkey=seller_key, network=NETWORK, owner=seller,
                      price=COIN, seconds=LISTED_FOR)
    assert "already has a scriptSig" in str(twice.value), str(twice.value)


def test_a_listing_is_cancelled_by_spending_the_piece(regtest, db, book):
    """The only cancellation there is, and the refusal it leaves behind.

    Deleting the row would be theatre: the signature is already out the door. So
    this spends the piece -- with the seller's own key, made outside this node,
    which is the whole point -- and the listing still open in the book has to
    say the piece is gone rather than combine a transaction the chain would
    reject for a reason nobody reading it would recognise.
    """
    rpc = regtest.rpc
    listing, _seller_key, seller = _listed(rpc, db, book, SELLER)
    txid = _withdraw(rpc, db, SELLER, seller, int(listing["input"]["value"]))
    assert len(txid) == 64 and rpc.call("getrawtransaction", txid, 1)["confirmations"] >= 1, \
        "the seller took its own piece off the market the only way there is"

    with pytest.raises(listings.ListingError) as refused:
        listings.paste_leg(rpc, listing, _no_half(), [], _pubkey(BUYER))
    assert "spent or unseen" in str(refused.value), str(refused.value)


def test_a_page_reads_the_chain_and_not_the_row(regtest, db, book):
    """`piece_held` is the one answer a listing page is allowed to show.

    An open row and a spent piece are both true at the same time, and a page
    that read the row would advertise a piece that no longer exists. This is
    the same reading `paste_leg` refuses on, which is the reason for having one
    function give the answer instead of a page and a combiner each computing
    their own.
    """
    rpc = regtest.rpc
    listing, _seller_key, seller = _listed(rpc, db, book, SELLER)
    assert listings.piece_held(rpc, listing) == int(listing["input"]["value"]), \
        "a piece that is sitting there has to read as sitting there"

    _withdraw(rpc, db, SELLER, seller, int(listing["input"]["value"]))
    assert listings.piece_held(rpc, listing) is None, \
        "the only cancellation there is happened, and this is where it shows"
    assert book.get(listing["id"])["status"] == "open", \
        "the row was left alone, which is the honest half of the story"


def test_a_buyer_cannot_pay_for_a_listing_out_of_the_sellers_coins(regtest, db,
                                                                  book):
    """`swap.countersign`'s oldest rule, carried to the path that has no seller.

    A seller is paid out of its own output and never out of its own wallet. A
    buyer that put a second coin of the seller's beside the listed piece would
    be asking the seller to fund its own sale -- and in a self-fill, where buyer
    and seller are the same account, every buyer input is the seller's, so this
    is also the check that stops a wash trade being advertised as a sale.
    """
    rpc = regtest.rpc
    listing, _seller_key, seller = _listed(rpc, db, book, SELLER)
    rpc.call("sendtoaddress", seller, 3.0)
    _index(rpc, db, {seller})
    extra = [c for c in utxos.unspent(db, seller)
             if (c["txid"], c["vout"]) != (listing["input"]["txid"],
                                           listing["input"]["vout"])]
    assert extra, "the seller needs a second coin for this to mean anything"

    paid = (int(listing["output"]["value"]),
            bytes.fromhex(listing["output"]["script"]))
    half = funding.Unsigned(
        raw="", inputs=[listing["input"],
                        {"txid": extra[0]["txid"], "vout": extra[0]["vout"],
                         "value": extra[0]["value"], "address": seller}],
        outputs=[paid, (int(listing["input"]["value"]),
                        p2pkh_script(seller))],
        signed_from=1, what="a self-fill")
    with pytest.raises(listings.ListingError) as refused:
        listings.paste_leg(rpc, listing, half,
                           [_sign(BUYER, b"\x01" * 32).hex()], _pubkey(BUYER))
    assert "belongs to the seller" in str(refused.value), str(refused.value)


def test_a_finished_swap_that_shorts_the_block_is_refused(regtest, db, book):
    """The fee was reserved when the leg was signed; this is what enforces it.

    A buyer that makes the finished transaction bigger than the listing priced
    owes the difference out of its own change. Nothing in the leg can be
    adjusted to cover it -- the seller's output is fixed the moment it signs --
    so the alternative to refusing here is a swap that relays, never confirms,
    and is believed done by both sides.
    """
    rpc = regtest.rpc
    listing, _seller_key, _seller = _listed(rpc, db, book, SELLER)
    buyer_key, buyer = _key(rpc, db, BUYER, 8.0)
    half = _buy(rpc, db, BUYER, buyer, listing)
    assert len(half.outputs) == 3, "the buyer needs change to pay itself back"
    generous = funding.Unsigned(
        raw=half.raw, inputs=half.inputs,
        outputs=[half.outputs[0], half.outputs[1],
                 (half.outputs[2][0] + 10_000, half.outputs[2][1])],
        sighashes=half.sighashes, what=half.what, signed_from=1)

    with pytest.raises(listings.ListingError) as refused:
        listings.paste_leg(rpc, listing, generous, _signatures(BUYER, generous),
                           buyer_key)
    assert "for a block and it costs" in str(refused.value), str(refused.value)


# --- listings that say what they sell ----------------------------------------
#
# Everything above trades a coin, where the coin IS the thing and one signature
# over one payment says the whole deal. Below is a listing that names a piece:
# the bytes that identify it ride in the swap as an output, and they are pinned
# by a second signature over a second coin of the seller's. That is two digests
# instead of one, which is two chances to get the pairing wrong, and the refusals
# below are each one of those ways.

def test_a_listing_that_names_its_piece_fills_unchanged(regtest, db, book):
    """List, buy, broadcast, mine -- with the description riding along.

    Nothing here needed the node to be trusted with a key: the seller signed the
    payload before it had a buyer, the buyer read the bytes off the listing and
    paid for a transaction carrying exactly those, and a pepecoind checked two
    signatures and read none of the bytes. What a block took is the proof.
    """
    rpc = regtest.rpc
    listing, _seller_key, seller = _named_listing(rpc, db, book)
    assert listing["payload"] == NAMING.hex(), \
        "the row holds the bytes, not a hash of them"
    assert listing["coin"] and listing["coin"]["txid"] != \
        listing["input"]["txid"], "the second signature stands on its own coin"
    assert listing["fee"] == funding.swap_fee(RATE, op_return_script(NAMING)), \
        "the reservation is priced at the swap this listing became"

    buyer_key, buyer = _key(rpc, db, BUYER, 8.0)
    half = _bid(rpc, db, BUYER, buyer, listing)
    assert half.signed_from == 2, \
        "both of the seller's coins are in front, and neither is ours to sign"
    assert half.outputs[0] == (0, op_return_script(NAMING))

    raw = listings.paste_leg(rpc, listing, half, _signatures(BUYER, half),
                             buyer_key)
    filled = rpc.call("sendrawtransaction", raw)
    rpc.call("generate", 1)
    assert len(filled) == 64 and rpc.call("getrawtransaction", filled, 1)["confirmations"] >= 1, \
        "a block would not take a swap signed over the wrong output"

    landed = _landed(rpc, filled)
    assert (0, op_return_script(NAMING).hex()) in landed, \
        "the block does not say what was sold, which was the whole point"
    assert (int(listing["output"]["value"]),
            listing["output"]["script"]) in landed, \
        "the seller was not paid what its own second signature committed to"
    assert (int(listing["input"]["value"]), p2pkh_script(buyer).hex()) in landed, \
        "the buyer did not end up holding the piece it paid for"
    assert len(landed) == 4, "payload, seller, piece, change: an output nobody counted"

    book.close(listing["id"], "filled", spent_by=filled)
    assert book.get(listing["id"])["status"] == "filled"


def test_a_completion_that_rewrites_the_bytes_is_refused(regtest, db, book):
    """One byte changed, in the payload and nowhere else.

    Every other check in `paste_leg` passes this completion -- the payment is
    exactly as listed, both of the seller's coins are in place, the buyer's
    coins are real and its change is honest. A coin-only listing cannot tell
    the difference, which is precisely why the payload is compared here: output
    0 is the one output the seller's FIRST signature reaches, and a buyer that
    quietly renamed the piece would be spending against a signature over a
    thing its seller never agreed to sell.
    """
    rpc = regtest.rpc
    listing, _seller_key, _seller = _named_listing(rpc, db, book)
    buyer_key, buyer = _key(rpc, db, BUYER, 8.0)
    half = _bid(rpc, db, BUYER, buyer, listing)
    script = half.outputs[0][1]
    rewritten = funding.Unsigned(
        raw=half.raw, inputs=half.inputs,
        outputs=[(0, script[:-1] + bytes([script[-1] ^ 0x01]))]
        + list(half.outputs[1:]),
        sighashes=half.sighashes, what=half.what,
        signed_from=half.signed_from)

    with pytest.raises(listings.ListingError) as refused:
        listings.paste_leg(rpc, listing, rewritten,
                           _signatures(BUYER, rewritten), buyer_key)
    assert "does not carry the bytes this listing sells" in str(refused.value), \
        str(refused.value)


def test_a_listing_whose_second_coin_is_spent_is_refused(regtest, db, book):
    """The seller spent the coin behind its own second signature, and not the piece.

    This is the new failure the second input brings: the listing is still
    halfway alive. `piece_held` still says the piece is sitting there, so the
    page still advertises it, and it is right to -- but the payment on it is now
    signed by nothing, because the outpoint that signature stands on is in
    somebody else's transaction. Completing it would pay a seller out of a
    signature the chain has already walked past.

    So this one is not cancelled and has to say so differently: the seller has
    to go build a new leg out of coins it still holds.
    """
    rpc = regtest.rpc
    listing, _seller_key, seller = _named_listing(rpc, db, book)
    assert listings.piece_held(rpc, listing) == int(listing["input"]["value"])

    _withdraw(rpc, db, SELLER, seller, int(listing["coin"]["value"]))

    assert listings.piece_held(rpc, listing) == int(listing["input"]["value"]), \
        "the piece is untouched -- this is not a listing being cancelled"
    with pytest.raises(listings.ListingError) as refused:
        listings.paste_leg(rpc, listing, _no_half(), [], _pubkey(BUYER))
    assert "the coin the seller's second signature stands on" in (
        str(refused.value)), str(refused.value)
    assert book.get(listing["id"])["status"] == "open", \
        "the row is the honest half of the story: nothing here could cancel it"


def test_a_leg_signed_once_is_not_a_listing_that_names_a_thing(regtest, db, book):
    """What a browser that lost half its work would post back.

    Filed, the row would advertise the payload as sold while the only signature
    behind it was made over the payment -- which says nothing about any piece.
    The count comes out of the leg's own inputs, so refusing it asks nothing of
    a memory of what this node once offered.
    """
    rpc = regtest.rpc
    pubkey, seller = _key(rpc, db, SELLER, 5.0)
    piece, spare = _two_coins(rpc, db, seller)[:2]
    leg = funding.build_leg(PARAMS, seller, piece, coins=COIN, rate=RATE,
                            payload=NAMING, coin=spare)
    one = _sign(SELLER, bytes.fromhex(leg.sighashes[0]),
                funding.SINGLE_ANYONECANPAY).hex()

    with pytest.raises(listings.ListingError) as refused:
        book.register(rpc, raw=leg.raw, signatures=[one], pubkey=pubkey,
                      network=NETWORK, owner=seller, price=COIN,
                      seconds=LISTED_FOR, what="a piece, named")
    assert "signature came with it" in str(refused.value), str(refused.value)
    assert book.open_listings(NETWORK) == [], \
        "a leg with an unsigned output is not a listing to put on a page"


def test_a_row_whose_numbers_do_not_match_its_leg_is_refused(regtest):
    """A listing page reads its price straight out of this table.

    So a row is checked on the way in and not only on the way out: a column that
    drifted from the signature it describes puts a number in front of a stranger
    that no signature backs, which is the same lie as an unsigned listing and
    harder to notice because the row looks checked. The price is not in the
    signature and cannot be, so it has to be the number the other columns force.
    """
    rpc = regtest.rpc
    tamper = {
        "output value": lambda row: {**row, "output": {**row["output"],
                                                      "value": row["output"]["value"] + 1}},
        "input value": lambda row: {**row, "input": {**row["input"],
                                                    "value": row["input"]["value"] + COIN}},
        "fee": lambda row: {**row, "fee": row["fee"] + 1},
        "price": lambda row: {**row, "price": row["price"] + 1},
    }
    for name, twist in tamper.items():
        with pytest.raises(listings.ListingError) as refused:
            listings.check_leg(rpc, twist(_row(SELLER)))
        assert "leg" in str(refused.value).lower() or "listing" in str(
            refused.value).lower(), f"{name}: {refused.value}"


def test_a_leg_paying_where_its_seller_never_agreed_is_refused(regtest):
    """The payment at output 0 is the one thing a leg commits to, so it is the
    one thing a row must not be able to misreport in either direction."""
    rpc = regtest.rpc
    elsewhere = p2pkh_script(_address_of(BUYER)[1]).hex()
    row = _row(SELLER)
    says_otherwise = {**row, "output": {**row["output"], "script": elsewhere}}
    with pytest.raises(listings.ListingError) as refused:
        listings.check_leg(rpc, says_otherwise)
    assert "not the payment" in str(refused.value), str(refused.value)

    signed = _leg_of(SELLER, out_script=elsewhere)
    with pytest.raises(listings.ListingError) as refused:
        listings.check_leg(rpc, {**says_otherwise, "leg": signed})
    assert "was not told where" in str(refused.value), str(refused.value)


def test_a_leg_signed_by_a_key_that_is_not_the_listed_address_is_refused(regtest):
    """What an unverified POST would have written down.

    The account path hands this node a signature and an address and nothing
    else. If the key behind the signature is not the key behind the address, the
    row credits one account's sale to another -- and every check downstream
    trusts the row, not the request.
    """
    rpc = regtest.rpc
    row = _row(SELLER)
    with pytest.raises(listings.ListingError) as refused:
        listings.check_leg(rpc, {**row, "leg": _re_signed(row, BUYER)})
    assert "not the key behind the address" in str(refused.value), (
        str(refused.value))


def test_a_leg_that_does_not_sign_one_output_each_is_refused(regtest):
    """Not a style rule. `SINGLE` binds an output to its input's INDEX, so a leg
    with a second input and one output, or a second output and one input, is a
    signature over a transaction nobody described to the signer.

    Two inputs and two outputs IS a leg -- that is the shape that names a piece
    -- and `test_a_listing_that_names_its_piece_fills_unchanged` is the proof.
    What is refused here is a leg whose counts disagree, because then one of its
    two outputs is signed by nothing.
    """
    rpc = regtest.rpc
    row = _row(SELLER)
    wider = build_raw_tx([(PIECE["txid"], PIECE["vout"]),
                          (PIECE["txid"], 1)],
                         [(row["output"]["value"],
                           bytes.fromhex(row["output"]["script"]))])
    with pytest.raises(listings.ListingError) as refused:
        listings.check_leg(rpc, {**row, "leg": _script_into(wider, row["leg"])})
    assert "one output for every signature" in str(refused.value), (
        str(refused.value))

    paid = int(row["output"]["value"])
    taller = build_raw_tx([(PIECE["txid"], PIECE["vout"])],
                          [(paid // 2, bytes.fromhex(row["output"]["script"])),
                           (paid - paid // 2, p2pkh_script(row["owner"]))])
    with pytest.raises(listings.ListingError) as refused:
        listings.check_leg(rpc, {**row, "leg": _script_into(taller, row["leg"])})
    assert "one output for every signature" in str(refused.value), (
        str(refused.value))


def test_a_listing_past_its_expiry_is_not_completed(regtest, book):
    """What the page has to say, in the refusal, because it is the truth.

    An expiry is a promise from this node, not a term of the signature. The node
    stops offering the leg; a signature it handed out earlier stays good until a
    block spends the piece. Refusing quietly here would let the difference
    between those two states go unnoticed by whoever is standing in front of the
    page waiting for a trade to happen.
    """
    rpc = regtest.rpc
    row = {**_row(SELLER, created=time.time() - LISTED_FOR - 60.0),
           "id": "gone-quiet"}
    book.add(row)
    assert book.expire_due(NETWORK) == 1, "the sweep files it as expired"
    assert book.get("gone-quiet")["status"] == "expired"

    with pytest.raises(listings.ListingError) as refused:
        listings.paste_leg(rpc, book.get("gone-quiet"), _no_half(), [],
                           _pubkey(BUYER))
    assert "expired" in str(refused.value)
    assert "still good until the piece is spent" in str(refused.value), (
        str(refused.value))


def test_a_listing_that_is_already_closed_is_not_completed(regtest):
    rpc = regtest.rpc
    row = {**_row(SELLER), "status": "filled"}
    with pytest.raises(listings.ListingError) as refused:
        listings.paste_leg(rpc, row, _no_half(), [], _pubkey(BUYER))
    assert "that listing is filled" in str(refused.value), str(refused.value)


def test_the_book_remembers_what_its_seller_signed(regtest, tmp_path):
    """It has to survive a restart, which is what makes it a file and not a tab.

    `web/account.Offers` is in memory because an offer that outlived its index
    would be a lie about coins. A listing is the opposite case: the signature
    outlives the request that made it, so forgetting it at a restart would be
    forgetting a promise somebody still holds a signature for. What a restart
    genuinely invalidates is the piece's own status, and that is asked of the
    chain by `check_leg` rather than remembered.
    """
    rpc = regtest.rpc
    path = tmp_path / "listings.sqlite"
    row = {**_row(SELLER, created=time.time() - 100.0),
           "id": "listed-while-nobody-was-looking"}
    listings.Listings(path).add(row)

    again = listings.Listings(path)
    back = again.get(row["id"])
    assert back["leg"] == row["leg"], "a row read back is the row that was written"
    listings.check_leg(rpc, back)
    assert again.for_piece(PIECE["txid"], PIECE["vout"])[0]["id"] == row["id"]
    assert len(again.open_listings(NETWORK)) == 1

    again.close(row["id"], "filled", spent_by="cd" * 32)
    filed = again.get(row["id"])
    assert filed["status"] == "filled" and filed["spent_by"] == "cd" * 32
    assert again.open_listings(NETWORK) == [], "a filled listing is not for sale"


def test_a_listing_shows_a_buyer_its_terms_and_not_its_bookkeeping(regtest):
    """What crosses to another node, and what stays home."""
    row = _row(SELLER)
    shown = listings.public(row)
    assert "status" not in shown and "spent_by" not in shown
    assert shown["leg"] == row["leg"] and shown["price"] == COIN

    named_row = _named_row(SELLER, NAMING)
    named = listings.public(named_row)
    assert named["payload"] == NAMING.hex() and named["coin"] == named_row["coin"], \
        "a buyer cannot complete a named listing without the bytes and the coin"


# --- the parts of a listing that never needed a chain -------------------------

def _address_of(secret: int) -> tuple[bytes, str]:
    pubkey = _pubkey(secret)
    return pubkey, b58check_encode(PARAMS.pubkeyhash_version, hash160(pubkey))


def _script_into(unsigned_raw: str, from_leg: str) -> str:
    """Put one leg's finished scriptSig into the first input of another transaction."""
    leg = bytes.fromhex(from_leg)
    at = 5 + 36
    return _paste(unsigned_raw, 0, leg[at + 1:at + 1 + leg[at]])


def _leg_of(secret: int, out_script: str = "", pays: str = "") -> str:
    """A leg signed by `secret`, optionally paying somewhere it was not told."""
    _, address = _address_of(secret)
    script = bytes.fromhex(out_script) if out_script \
        else p2pkh_script(pays or address)
    paid = PIECE["value"] + COIN - funding.swap_fee(RATE)
    digest = funding.sighash([PIECE], [(paid, script)], 0, script,
                             sighash_type=funding.SINGLE_ANYONECANPAY)
    return _paste(funding.build_raw_tx([(PIECE["txid"], PIECE["vout"])],
                                       [(paid, script)]),
                  0, push(_sign(secret, digest,
                               funding.SINGLE_ANYONECANPAY)) + push(_pubkey(secret)))


def _re_signed(row: dict, secret: int) -> str:
    """The same terms, signed by somebody else's key: what a POST could claim."""
    return _leg_of(secret, pays=row["owner"])


def _row(secret: int, created: float | None = None) -> dict:
    """A whole, honest listing row, built without asking the chain for anything."""
    _pub, address = _address_of(secret)
    fee = funding.swap_fee(RATE)
    paid = PIECE["value"] + COIN - fee
    now = created if created is not None else time.time()
    return {
        "id": "listed-" + format(secret & 0xFFFFFF, "x"),
        "network": NETWORK, "owner": address, "leg": _leg_of(secret),
        "input": {"txid": PIECE["txid"], "vout": PIECE["vout"],
                  "value": PIECE["value"]},
        "coin": None, "payload": "",
        "output": {"value": paid, "script": p2pkh_script(address).hex()},
        "fee": fee, "price": COIN, "what": "a piece", "status": "open",
        "created": now, "expires": now + LISTED_FOR, "spent_by": None,
    }


def _named_row(secret: int, naming: bytes) -> dict:
    """The same, for a leg that NAMES the piece it sells: two inputs, two digests.

    `_row`'s mirror in the other shape. Nothing here asks a chain either, so the
    drift a listing page is exposed to -- a column disagreeing with the bytes it
    is a column for -- can be shown without one.
    """
    _pub, address = _address_of(secret)
    script = p2pkh_script(address)
    fee = funding.swap_fee(RATE, op_return_script(naming))
    paid = PIECE["value"] + SPARE["value"] + COIN - fee
    outputs = [(0, op_return_script(naming)), (paid, script)]
    digests = [funding.sighash([PIECE, SPARE], outputs, n, script,
                               sighash_type=funding.SINGLE_ANYONECANPAY)
               for n in (0, 1)]
    unsigned = funding.build_raw_tx([(PIECE["txid"], PIECE["vout"]),
                                     (SPARE["txid"], SPARE["vout"])], outputs)
    signed = unsigned
    for n in (1, 0):          # descending: `_paste` counts blank inputs from the front
        signed = _paste(signed, n,
                        push(_sign(secret, digests[n],
                                   funding.SINGLE_ANYONECANPAY))
                        + push(_pubkey(secret)))
    now = time.time()
    return {
        "id": "named-" + format(secret & 0xFFFFFF, "x"),
        "network": NETWORK, "owner": address, "leg": signed,
        "input": {"txid": PIECE["txid"], "vout": PIECE["vout"],
                  "value": PIECE["value"]},
        "coin": {"txid": SPARE["txid"], "vout": SPARE["vout"],
                 "value": SPARE["value"]},
        "payload": naming.hex(),
        "output": {"value": paid, "script": script.hex()},
        "fee": fee, "price": COIN, "what": "a named piece", "status": "open",
        "created": now, "expires": now + LISTED_FOR, "spent_by": None,
    }


def test_a_row_cannot_drift_from_the_bytes_it_names(regtest):
    """The three columns a named listing adds, each moved by one, each refused.

    A coin listing has four numbers to keep honest and all four are arithmetic,
    which is why `check_leg` can derive the price. A listing that names a piece
    gains a fifth that is not arithmetic at all -- the payload -- and a second
    outpoint that has to be the very one its signature stands on. Those are the
    columns a page renders in words, so the drift is proven here rather than
    assumed impossible: an UPDATE that wrote one and not the other is the whole
    of what a listing page would then be lying about.
    """
    rpc = regtest.rpc
    row = _named_row(SELLER, NAMING)
    listings.check_leg(rpc, row)          # the honest one, so the rest mean something

    with pytest.raises(listings.ListingError) as renamed:
        listings.check_leg(rpc, {**row, "payload": _named(b"another-piece" * 3).hex()})
    assert "are not the bytes this listing says it sells" in (
        str(renamed.value)), str(renamed.value)

    with pytest.raises(listings.ListingError) as quiet:
        listings.check_leg(rpc, {**row, "payload": ""})
    assert "this row says it carries none" in str(quiet.value), str(quiet.value)

    with pytest.raises(listings.ListingError) as unbacked:
        listings.check_leg(rpc, {**row, "coin": None})
    assert "names no coin beside it" in str(unbacked.value), str(unbacked.value)

    with pytest.raises(listings.ListingError) as elsewhere:
        listings.check_leg(rpc, {**row, "coin": {"txid": "ee" * 32, "vout": 7,
                                                 "value": row["coin"]["value"]}})
    assert "second input is not" in str(elsewhere.value), str(elsewhere.value)


# --- the page that says what a listing is -------------------------------------
#
# Everything above is the book and the bytes. This is the words, which are the
# part a seller acts on. The page is read here with no node at all -- which is
# both the only way to render it without a chain and the state where its copy
# matters most: a node whose daemon is down knows nothing about any piece on it,
# and must not say one sold.

from test_web import app_state, client                                  # noqa: E402


def test_the_page_says_spending_the_piece_is_the_only_cancel(client):
    """No withdraw button, and the page says why rather than leaving it out.

    A seller who reads a listings page and finds no way to take something off
    it will conclude the node is holding their piece. It is not: it is holding a
    signature the seller's own browser sent, and the one thing a signature
    cannot do is unmake itself. That is the sentence the page owes.

    The prose is matched with its whitespace folded, because it is prose: it is
    wrapped in the template for the browser's sake, and a test that reads it as
    one line is the only honest way to say what has to be said.
    """
    app, state = client
    state.listings.add(_row(SELLER))
    words = " ".join(app.get("/listings").text.split())
    assert "no withdraw button" in words, words[:2000]
    assert "That is cancelling it" in words
    assert "a block spends the piece" in words
    assert "no lock time in a leg" in words, \
        "the expiry is not a deadline; the page has to say which is which"
    assert "1 coins" in words, "the price a signature commits to"


def test_a_page_that_could_not_ask_says_so_rather_than_that_a_piece_sold(client):
    """`piece_held` answers None for two different worlds, and only one of them
    is "spent". A page that reads the node being down as the piece being gone
    tells a seller their listing sold, and they go looking for coins that were
    never moved."""
    app, state = client
    row = _row(SELLER)
    state.listings.add(row)
    words = " ".join(app.get("/listings").text.split())
    assert "could not ask" in words
    assert "is done" not in words, "nothing was learned about this piece"
    assert state.listings.get(row["id"])["status"] == "open", \
        "a page that cannot read the chain has no business changing the row"


def test_the_page_stops_advertising_what_is_past_its_date(client):
    """The expiry is this node's promise about its own front page, and nothing
    else. So it arrives by the page being read, and it files the row without
    unsaying anything: no unlock, because nothing was locked, and the piece is
    still the seller's to sell again the same minute."""
    app, state = client
    row = _row(SELLER, created=time.time() - LISTED_FOR - 10)
    state.listings.add(row)
    body = app.get("/listings").text
    assert PIECE["txid"][:16] not in body, "an expired listing is still advertised"
    assert state.listings.get(row["id"])["status"] == "expired"


def test_the_listing_page_has_nothing_to_press(client):
    """Not an oversight: a control on this page could only ever lie.

    The one thing that takes a piece off the market is a block spending what the
    leg was made from, so a withdraw button would be a button that changes a row
    and changes nothing else -- worse than no button, because the signature
    stays live and the seller believes it does not. `listings.py` uses the word
    "withdrawn" in its own prose for exactly that gap: a row this node deleted
    is withdrawn, and the piece is not. What must never exist is a STATUS by
    that name, and a page that offers to set one.
    """
    app, state = client
    state.listings.add(_row(SELLER))
    body = app.get("/listings").text
    assert "<form" not in body and "csrf_token" not in body, \
        "a listings page that can be pressed is one that can lie"
    assert "status='withdrawn'" not in inspect.getsource(listings)
