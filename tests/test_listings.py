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

import time
from types import SimpleNamespace

import pytest

from arcade import fees, funding, listings, utxos
from arcade.script import b58check_encode, hash160
from arcade.txbuild import build_raw_tx, p2pkh_script, push

from test_funding import COIN, PARAMS, _pubkey, _sign, db
from test_funding_leg import _coin, _index, _key, _paste

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


@pytest.fixture
def book(tmp_path):
    return listings.Listings(tmp_path / "listings.sqlite")


def _listed(rpc, db, book, secret=SELLER, price=COIN, rate=RATE,
            amount=5.0) -> tuple[dict, bytes, str]:
    """A piece put up for sale by a wallet whose key this node never had."""
    pubkey, address = _key(rpc, db, secret, amount)
    piece = _coin(db, address)
    leg = funding.build_leg(PARAMS, address, piece, coins=price, rate=rate)
    signature = _sign(secret, bytes.fromhex(leg.sighash),
                      funding.SINGLE_ANYONECANPAY).hex()
    listing = book.from_leg(rpc, leg, signature, pubkey, NETWORK, address,
                            price, LISTED_FOR)
    return listing, pubkey, address


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
    piece = int(listing["input"]["value"])
    fee = funding.price(1, [(0, p2pkh_script(seller))], RATE, change=True)
    away = funding.build(db, PARAMS, seller,
                         [(piece - fee, p2pkh_script(seller))], rate=RATE,
                         what="withdrawing it")
    gone = funding.assemble(away, [_sign(SELLER, bytes.fromhex(h)).hex()
                                   for h in away.sighashes], _pubkey(SELLER))
    txid = rpc.call("sendrawtransaction", gone)
    rpc.call("generate", 1)
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

    piece = int(listing["input"]["value"])
    fee = funding.price(1, [(0, p2pkh_script(seller))], RATE, change=True)
    away = funding.build(db, PARAMS, seller, [(piece - fee,
                                               p2pkh_script(seller))],
                         rate=RATE, what="withdrawing it")
    gone = funding.assemble(away, [_sign(SELLER, bytes.fromhex(h)).hex()
                                   for h in away.sighashes], _pubkey(SELLER))
    rpc.call("sendrawtransaction", gone)
    rpc.call("generate", 1)

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


def test_a_leg_that_is_not_one_in_and_one_out_is_refused(regtest):
    """Not a style rule. `SINGLE` binds an output to its input's INDEX, so a leg
    with a second input, or a second output, is a signature over a transaction
    nobody described to the signer."""
    rpc = regtest.rpc
    row = _row(SELLER)
    wider = build_raw_tx([(PIECE["txid"], PIECE["vout"]),
                          (PIECE["txid"], 1)],
                         [(row["output"]["value"],
                           bytes.fromhex(row["output"]["script"]))])
    with pytest.raises(listings.ListingError) as refused:
        listings.check_leg(rpc, {**row, "leg": _script_into(wider, row["leg"])})
    assert "one coin in and one payment out" in str(refused.value), (
        str(refused.value))

    paid = int(row["output"]["value"])
    taller = build_raw_tx([(PIECE["txid"], PIECE["vout"])],
                          [(paid // 2, bytes.fromhex(row["output"]["script"])),
                           (paid - paid // 2, p2pkh_script(row["owner"]))])
    with pytest.raises(listings.ListingError) as refused:
        listings.check_leg(rpc, {**row, "leg": _script_into(taller, row["leg"])})
    assert "one coin in and one payment out" in str(refused.value), (
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
        "output": {"value": paid, "script": p2pkh_script(address).hex()},
        "fee": fee, "price": COIN, "what": "a piece", "status": "open",
        "created": now, "expires": now + LISTED_FOR, "spent_by": None,
    }
