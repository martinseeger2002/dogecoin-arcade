"""A seller's leg: a signature handed out before the buyer exists.

The buying half of an account swap already works -- `funding.build_partial`
builds a transaction whose first input belongs to somebody else, and the
browser signs the rest. Selling is the opposite problem in time rather than in
shape: a listing has to promise a piece to whoever turns up, so the seller must
sign a transaction that does not contain a buyer yet. That is only honest under
SINGLE|ANYONECANPAY -- one input, the piece, and one output, the payment the
seller will accept -- and a preimage narrowed like that is exactly the kind of
serialisation that is easy to get subtly wrong, because the rule that ties the
output to the input's INDEX is invisible until it fails.

So this is proved the way `test_funding.py` proves the rest of the module: a
throwaway secp256k1 used only here plays the part of the browser, and a real
pepecoind decides whether the signature is worth anything. Where a test cannot
reach a node, it is because the claim is about the arithmetic rather than about
consensus -- and that is said in the open, not glossed.
"""

import pytest

from arcade import funding, utxos
from arcade.db import StateDB
from arcade.rpc import RpcError
from arcade.script import b58check_encode, hash160
from arcade.txbuild import build_raw_tx, op_return_script, p2pkh_script, push

from test_funding import COIN, PARAMS, _pubkey, _sign, db

RATE = 100_000


def _address(fill: int) -> str:
    """A real address for arithmetic that never gets spent to.

    A readable placeholder like "nSeller" is not one: base58 has no lowercase
    'l', and `p2pkh_script` says so before anything interesting can happen.
    """
    return b58check_encode(PARAMS.pubkeyhash_version, bytes([fill]) * 20)


#: Names are for reading and these are for hashing.
SELLER = _address(0x11)
BUYER = _address(0x22)
STRANGER = _address(0x33)


#: An unsigned input: 32 bytes of txid, 4 of index, one zero length byte for
#: the empty scriptSig, and 4 of sequence.
BLANK_INPUT = 41


def _paste(unsigned_raw: str, index: int, script_sig: bytes) -> str:
    """Put one finished scriptSig into an unsigned transaction.

    A stand-in for the combiner `funding.paste_leg` will be, written here so
    these tests say what the primitive has to support rather than quietly
    assuming it exists.
    """
    raw = bytes.fromhex(unsigned_raw)
    assert raw[4] < 0xfd, "these tests do not build a transaction this large"
    start = 5 + BLANK_INPUT * index
    assert raw[start + 36] == 0, "the input pasted into has to be unsigned"
    sequence = raw[start + 37:start + BLANK_INPUT]
    entry = (raw[start:start + 36] + bytes([len(script_sig)]) + script_sig
             + sequence)
    return (raw[:start] + entry + raw[start + BLANK_INPUT:]).hex()


def _coin(db, address: str) -> dict:
    """The coin to sell, out of the index.

    Not `listunspent`: that answers for addresses the wallet holds, and the
    whole point of a seller's leg is a coin this node has no key for. The
    index is what knows about those, which is how `funding` finds them too.
    """
    unspent = utxos.unspent(db, address)
    assert unspent, "nothing funded at that address"
    coin = unspent[0]
    return {"txid": coin["txid"], "vout": coin["vout"], "value": coin["value"],
            "address": address}


def _index(rpc, db, addresses: set[str]) -> None:
    """Mine the next block and connect it to the index, for those addresses."""
    height = rpc.call("getblockcount") + 1
    rpc.call("generate", 1)
    for address in addresses:
        utxos.watch(db, address, 0)
    block = rpc.call("getblock", rpc.call("getblockhash", height), 2)
    state = StateDB(db)
    with state.block_context(height=height, block_hash=block["hash"],
                             prev_hash=block["previousblockhash"],
                             block_time=block["time"],
                             tx_count=len(block["tx"]), processed_at=0):
        utxos.on_block(state, height, block, PARAMS, addresses)


def _key(rpc, db, secret: int, amount: float = 5.0) -> tuple[int, str]:
    """An address the node has no key for, funded, and seen by the index."""
    pubkey = _pubkey(secret)
    address = b58check_encode(PARAMS.pubkeyhash_version, hash160(pubkey))
    assert not rpc.call("validateaddress", address).get("ismine"), \
        "the whole point is that this node cannot sign for it"
    rpc.call("generate", 101)
    rpc.call("sendtoaddress", address, amount)
    _index(rpc, db, {address})
    return pubkey, address


def test_a_leg_is_one_coin_in_and_one_payment_out():
    """No node: this is arithmetic about what a listing reserves.

    The seller's output is its own coin back plus the price less the fee it
    reserved. That number is fixed the moment it signs, so getting it wrong is
    not a mistake a later step can correct.

    This is the shape a LEG FOR A COIN keeps: nothing but a payment to commit
    to, so nothing but a payment is committed to. What a leg that names a thing
    looks like is below, in "a leg that names a thing signs it twice".
    """
    piece = {"txid": "ab" * 32, "vout": 1, "value": 3 * COIN, "address": SELLER}
    leg = funding.build_leg(PARAMS, SELLER, piece, coins=1 * COIN, rate=RATE)

    assert [i["txid"] for i in leg.inputs] == [piece["txid"]]
    assert leg.inputs[0]["vout"] == 1
    assert leg.outputs[-1][1] == p2pkh_script(SELLER), "it pays the seller, not the node"
    assert leg.paid == 3 * COIN + 1 * COIN - leg.fee
    assert leg.outputs[-1][0] == leg.paid
    assert leg.payload == b"", "a coin has nothing to say beyond its payment"
    assert leg.fee == funding.swap_fee(RATE), "the reservation is the swap's real cost"
    assert leg.sighash_type == funding.SINGLE_ANYONECANPAY == 0x83

    # One input and one output, because `SINGLE` commits to the output standing
    # at the signed input's own index and this must not be left to chance.
    raw = bytes.fromhex(leg.raw)
    assert raw[4] == 1, "one input"
    assert raw[5 + BLANK_INPUT] == 1, "one output, at index 0, the one it signed"
    assert leg.sighashes == [funding.sighash(
        [piece], [(leg.paid, p2pkh_script(SELLER))], 0,
        p2pkh_script(SELLER), sighash_type=funding.SINGLE_ANYONECANPAY).hex()]


def test_the_leg_commits_to_its_payment_and_to_nothing_else():
    """The property the whole seller's half rests on, in the arithmetic.

    A signature over a leg must survive the buyer's parts being bolted on --
    otherwise there is nothing to hand out in advance -- and must not survive
    the payment changing. Both halves are asserted here, from the same digest.
    """
    piece = {"txid": "cd" * 32, "vout": 0, "value": 2 * COIN, "address": SELLER}
    script = p2pkh_script(SELLER)
    leg = funding.build_leg(PARAMS, SELLER, piece, coins=100_000, rate=RATE)
    paid = leg.paid
    mine = funding.sighash([piece], [(paid, script)], 0, script,
                           sighash_type=funding.SINGLE_ANYONECANPAY)
    assert leg.sighashes == [mine.hex()]

    buyer = {"txid": "ee" * 32, "vout": 3, "value": 4 * COIN, "address": BUYER}
    theirs = (3 * COIN, p2pkh_script(BUYER))

    # The buyer's input and the buyer's output, added after the signing: the
    # digest is unchanged, which is what lets a stranger complete it.
    after = funding.sighash([piece, buyer], [(paid, script), theirs], 0, script,
                            sighash_type=funding.SINGLE_ANYONECANPAY)
    assert after == mine, "the leg has to still be valid once a buyer turns up"

    # A thinner price, or a fatter one: refused by the signature, not by a
    # promise. This is the assertion that makes the pasted leg safe.
    for changed in (paid - 1, paid + 1):
        assert funding.sighash([piece], [(changed, script)], 0, script,
                               sighash_type=funding.SINGLE_ANYONECANPAY) != mine
    # And another seller's coin is not this coin.
    assert funding.sighash([{**piece, "txid": "ab" * 32}], [(paid, script)], 0,
                           script,
                           sighash_type=funding.SINGLE_ANYONECANPAY) != mine


def test_a_single_that_would_hash_a_constant_is_refused():
    """The edge case that must never be serialised.

    Legacy SINGLE with no output at the signed input's index hashes a fixed
    256-bit constant instead of an output list. A signature over a constant is
    good for every transaction, so the builder refuses before anybody is
    asked to sign it -- it does not compute it and hope.
    """
    piece = {"txid": "ab" * 32, "vout": 0, "value": COIN}
    script = p2pkh_script(SELLER)
    with pytest.raises(funding.FundingError) as refused:
        funding.sighash([piece, piece], [(COIN, script)], 1, script,
                        sighash_type=funding.SINGLE_ANYONECANPAY)
    assert "every transaction" in str(refused.value)


def test_a_sighash_type_this_does_not_build_is_refused():
    """Silently implementing the wrong type is the dangerous failure here."""
    piece = {"txid": "ab" * 32, "vout": 0, "value": COIN}
    script = p2pkh_script(SELLER)
    for wrong in (funding.SIGHASH_SINGLE, 0x02,
                  funding.SIGHASH_ALL | funding.SIGHASH_ANYONECANPAY):
        with pytest.raises(funding.FundingError) as refused:
            funding.sighash([piece], [(COIN, script)], 0, script,
                            sighash_type=wrong)
        assert "SINGLE|ANYONECANPAY" in str(refused.value)


def test_a_leg_prices_a_block_and_refuses_a_price_that_cannot_pay_one():
    piece = {"txid": "ab" * 32, "vout": 0, "value": 10_000, "address": SELLER}
    with pytest.raises(funding.FundingError) as refused:
        funding.build_leg(PARAMS, SELLER, piece, coins=1, rate=RATE)
    assert "no output this seller could sign" in str(refused.value)

    someone_else = {"txid": "ab" * 32, "vout": 0, "value": COIN,
                    "address": STRANGER}
    with pytest.raises(funding.FundingError) as refused:
        funding.build_leg(PARAMS, SELLER, someone_else, coins=COIN, rate=RATE)
    assert "cannot sell it" in str(refused.value)


def test_a_leg_signed_before_the_buyer_existed_unlocks_the_coin(regtest, db):
    """The claim, proved by a node rather than by this file's own arithmetic.

    A seller signs a one-in, one-out leg. A buyer turns up afterwards, its
    input and its output are appended, and the finished transaction goes to a
    real pepecoind. If the narrowed preimage is right the node takes it; if it
    is wrong the node says `bad-sign`. Nothing in this file's own hashing can
    settle it either way, which is why it is here.
    """
    rpc = regtest.rpc
    secret = 0x3344556677889900334455667788990033445566778899003344556677889900
    pubkey, seller = _key(rpc, db, secret)
    piece = _coin(db, seller)

    price = int(1.0 * COIN)
    leg = funding.build_leg(PARAMS, seller, piece, coins=price, rate=RATE)
    signed = _sign(secret, bytes.fromhex(leg.sighashes[0]),
                   funding.SINGLE_ANYONECANPAY)
    leg_sig = push(signed) + push(pubkey)

    # The buyer is the node's own wallet, so its half can be signed here and
    # the finished transaction needs no key that this test should not hold.
    buyer = rpc.call("getnewaddress")
    theirs = [u for u in rpc.call("listunspent", 1, 9999999)
              if u["address"] != seller and not u.get("spend")][0]
    theirs_value = int(round(theirs["amount"] * COIN))
    # Chosen so the whole fee is exactly what the leg reserved: the buyer puts
    # in the price and nothing else, and its change is the remainder.
    back = theirs_value - price
    assert back > 546, "the buyer's change has to be an output worth making"

    finished = build_raw_tx(
        [(piece["txid"], piece["vout"]), (theirs["txid"], theirs["vout"])],
        [leg.outputs[-1], (back, p2pkh_script(buyer))])
    half = _paste(finished, 0, leg_sig)

    done = rpc.call("signrawtransaction", half)
    assert done.get("complete") is True, (
        "the seller's leg was pasted in and the node signed only its own half")
    taken = rpc.call("sendrawtransaction", done["hex"])
    assert len(taken) == 64, taken

    rpc.call("generate", 1)
    got = rpc.call("gettransaction", taken)
    assert got["confirmations"] >= 1, "a block took a transaction the seller " \
        "signed before it knew this buyer existed"
    # And the seller was paid exactly what it had signed to be paid for: the
    # leg's output travelled all the way to the block untouched. Compared by
    # script and by satoshis, because a script is the address and a float is
    # not an amount.
    landed = rpc.call("getrawtransaction", taken, True)["vout"]
    assert any(int(round(out["value"] * COIN)) == leg.paid
               and out["scriptPubKey"]["hex"] == leg.outputs[-1][1].hex()
               for out in landed), \
        "the payment the seller signed is not the payment it got"


def test_a_leg_whose_payment_was_changed_after_signing_is_refused(regtest, db):
    """The same transaction, with the seller's output quietly grown.

    Without this, a completed swap would be safe only because nobody thought
    to change it. The signature has to be the thing that stops it.
    """
    rpc = regtest.rpc
    secret = 0x4455667788990011445566778899001144556677889900114455667788990011
    pubkey, seller = _key(rpc, db, secret)
    piece = _coin(db, seller)
    price = int(1.0 * COIN)

    leg = funding.build_leg(PARAMS, seller, piece, coins=price, rate=RATE)
    signed = _sign(secret, bytes.fromhex(leg.sighashes[0]),
                   funding.SINGLE_ANYONECANPAY)

    buyer = rpc.call("getnewaddress")
    theirs = [u for u in rpc.call("listunspent", 1, 9999999)
              if u["address"] != seller and not u.get("spend")][0]
    theirs_value = int(round(theirs["amount"] * COIN))
    # The fee stays exactly what it was in the passing test: the only thing
    # wrong with this transaction is the signature.
    back = theirs_value - price - 10_000
    assert back > 546

    grown = (leg.paid + 10_000, leg.outputs[-1][1])
    finished = build_raw_tx(
        [(piece["txid"], piece["vout"]), (theirs["txid"], theirs["vout"])],
        [grown, (back, p2pkh_script(buyer))])
    half = _paste(finished, 0, push(signed) + push(pubkey))
    done = rpc.call("signrawtransaction", half)

    with pytest.raises(RpcError) as refused:
        rpc.call("sendrawtransaction", done["hex"])
    assert "bad-sign" in str(refused.value) or "16:" in str(refused.value), (
        f"the network took a leg whose payment had been changed: {refused.value}")


# --- which output a signature has to be standing over -------------------------
#
# A leg that names its payment and nothing else is an order against every piece
# the seller holds: whoever completes it writes the payload, and the signature
# says nothing about what it says. Closing that needs a SECOND digest, over the
# output that carries the payload, which is only honest if a real node takes two
# signatures at two different input indices -- and takes the second one as
# covering the second output.
#
# What the node answered, and what these tests hold: a SINGLE preimage at index
# i carries the signed input ALONE and `i + 1` output slots, the ones before i
# written empty (value -1, no script) and only the one at i for real. At index 0
# that is what this file always built, so a digest for a second input is not
# index 0's shape with an output added beside it, and the code that thought
# otherwise produced a signature no node would ever take. `build_leg` now builds
# that second digest itself, and the tests below ask it rather than hand-rolling
# the digests -- which is the only way they catch the builder getting the shape
# wrong rather than the hasher.


def _named(what: bytes) -> bytes:
    """Payload DATA for `build_leg`, which takes the bytes and writes the output."""
    return b"arc-swap" + what


def _payload(what: bytes) -> bytes:
    """An OP_RETURN, standing in for a listing's payload.

    These bytes are not a valid arcade swap and that is deliberate. A
    pepecoind does not read them at all: it checks the signature over the
    output that carries them. The arcade index is what reads a payload, and it
    takes the two parties from the transaction's INPUTS
    (`state.Engine._swap`), not from any output -- so the only thing here that
    can be wrong is which output a signature stands over.
    """
    return op_return_script(_named(what))


def _two_coins(rpc, db, seller: str) -> list[dict]:
    """A second coin at the seller's own address, seen by the index.

    One `sendtoaddress` leaves exactly one: its change goes back into the
    node's wallet, not to the address it was paying, and a second input needs
    a second coin of the seller's.
    """
    rpc.call("sendtoaddress", seller, 2.0)
    _index(rpc, db, {seller})
    coins = utxos.unspent(db, seller)
    assert len(coins) > 1, "a two-input leg needs two coins of the seller's"
    return coins


def _buyer_side(rpc, seller: str, price: int, extra: int) -> tuple[str, dict, int]:
    """A buyer the node holds the key for, and the change it gets back.

    `extra` is what the buyer adds on top of the price, so the fee the finished
    transaction pays is the leg's reservation plus that much -- enough that
    nothing in these tests can fail for a reason other than a signature.
    """
    buyer = rpc.call("getnewaddress")
    theirs = [u for u in rpc.call("listunspent", 1, 9999999)
              if u["address"] != seller and not u.get("spend")][0]
    back = int(round(theirs["amount"] * COIN)) - price - extra
    assert back > 546, "the buyer's change has to be an output worth making"
    return buyer, theirs, back


def test_the_second_digest_commits_its_own_output_and_no_other():
    """What a digest at index 1 holds, and what it cannot, without a chain.

    The rule the node gave, stated where it can be checked in arithmetic: two
    output slots, the first of them empty. So the payment's signature is blind
    to the payload lying beside it, and the payload's is blind to the payment.
    Two signatures, each seeing one output, is the shape -- which is why a
    listing needs both and not one digest made over a longer output list.
    """
    piece = {"txid": "ab" * 32, "vout": 0, "value": 3 * COIN}
    spare = {"txid": "cd" * 32, "vout": 1, "value": COIN}
    payload = (0, _payload(b"the-piece" * 3))
    payment = (3 * COIN, p2pkh_script(SELLER))
    script = p2pkh_script(SELLER)
    second = funding.sighash([piece, spare], [payload, payment], 1, script,
                             sighash_type=funding.SINGLE_ANYONECANPAY)

    # What the payload says is nowhere in it. This is the exact blindness the
    # payload's own digest exists to close, in the other direction from the
    # test below that shows it on the wire.
    for naming in (b"another-piece" * 3, b"cheaper-piece" * 3):
        assert funding.sighash(
            [piece, spare], [(0, _payload(naming)), payment], 1, script,
            sighash_type=funding.SINGLE_ANYONECANPAY) == second

    # The payment is in it, coin and amount both.
    assert funding.sighash([piece, spare], [payload, (payment[0] + 1, payment[1])],
                           1, script,
                           sighash_type=funding.SINGLE_ANYONECANPAY) != second
    assert funding.sighash([piece, spare], [payload, (payment[0], p2pkh_script(BUYER))],
                           1, script,
                           sighash_type=funding.SINGLE_ANYONECANPAY) != second
    # And a second digest is not a first one with company: the same payment
    # signed from index 0 is a different number, because the list it stood in
    # was one output long instead of two.
    assert funding.sighash([piece], [payment], 0, script,
                           sighash_type=funding.SINGLE_ANYONECANPAY) != second


def test_two_seller_signatures_at_two_indices_are_both_taken_by_a_node(regtest, db):
    """Shape of a listing that says what it sells: two inputs, two digests.

    Input 0 stands at the payload, input 1 at the payment, and each input
    signs the output at its own index. Whether a node accepts a transaction
    whose two signatures were each made over a different one-input,
    one-output preimage is not something this file can settle by hashing.
    """
    rpc = regtest.rpc
    secret = 0x5566778899001122556677889900112255667788990011225566778899001122
    pubkey, seller = _key(rpc, db, secret)
    first, second = _two_coins(rpc, db, seller)[:2]
    price = int(1.0 * COIN)

    script = p2pkh_script(seller)
    paid = first["value"] + second["value"] + price - 60_000
    outputs = [(0, _payload(b"the-piece" * 3)), (paid, script)]
    digests = [funding.sighash([first, second], outputs, n, script,
                               sighash_type=funding.SINGLE_ANYONECANPAY)
               for n in (0, 1)]
    assert digests[0] != digests[1], \
        "one digest standing for two outputs is the mistake this is here to catch"
    sigs = [push(_sign(secret, digest, funding.SINGLE_ANYONECANPAY)) + push(pubkey)
            for digest in digests]

    buyer, theirs, back = _buyer_side(rpc, seller, price, 20_000)
    finished = build_raw_tx(
        [(first["txid"], first["vout"]), (second["txid"], second["vout"]),
         (theirs["txid"], theirs["vout"])],
        outputs + [(back, p2pkh_script(buyer))])
    # Descending, because `_paste` counts blank inputs from the front and the
    # first paste makes the transaction longer behind it.
    half = _paste(_paste(finished, 1, sigs[1]), 0, sigs[0])
    done = rpc.call("signrawtransaction", half)
    assert done.get("complete") is True, (
        "both seller signatures were pasted and only the buyer's half was left to sign")

    taken = rpc.call("sendrawtransaction", done["hex"])
    rpc.call("generate", 1)
    assert rpc.call("gettransaction", taken)["confirmations"] >= 1, (
        "a block refused a leg whose payload and payment were each pinned by "
        "their own signature")
    landed = rpc.call("getrawtransaction", taken, True)["vout"]
    assert [out["scriptPubKey"]["hex"] for out in landed] == \
        [outputs[0][1].hex(), script.hex(), p2pkh_script(buyer).hex()], \
        "the outputs travelled to the block in the order they were signed at"
    assert int(round(landed[1]["value"] * COIN)) == paid


def test_a_byte_changed_in_a_pinned_payload_is_refused_by_the_node(regtest, db):
    """The same transaction, with the payload's contents quietly changed.

    One byte of the payload, and the payment left exactly as it was: the only
    signature that can notice is the one standing at the payload's index. This
    is the whole reason for a second digest, asked of a node instead of
    asserted here.
    """
    rpc = regtest.rpc
    secret = 0x6677889900112233667788990011223366778899001122336677889900112233
    pubkey, seller = _key(rpc, db, secret)
    first, second = _two_coins(rpc, db, seller)[:2]
    price = int(1.0 * COIN)

    script = p2pkh_script(seller)
    paid = first["value"] + second["value"] + price - 60_000
    outputs = [(0, _payload(b"the-piece" * 3)), (paid, script)]
    digests = [funding.sighash([first, second], outputs, n, script,
                               sighash_type=funding.SINGLE_ANYONECANPAY)
               for n in (0, 1)]
    sigs = [push(_sign(secret, digest, funding.SINGLE_ANYONECANPAY)) + push(pubkey)
            for digest in digests]

    buyer, theirs, back = _buyer_side(rpc, seller, price, 20_000)
    pieces = [(first["txid"], first["vout"]), (second["txid"], second["vout"]),
              (theirs["txid"], theirs["vout"])]
    finished = build_raw_tx(pieces, outputs + [(back, p2pkh_script(buyer))])
    half = _paste(_paste(finished, 1, sigs[1]), 0, sigs[0])
    done = rpc.call("signrawtransaction", half)
    assert done.get("complete") is True, "the buyer's half signed onto the pasted pair"

    # One byte of the payload, same length, same everything else.
    payload = bytearray(outputs[0][1])
    payload[-1] ^= 0x01
    swapped = build_raw_tx(pieces, [(0, bytes(payload)), outputs[1]]
                           + [(back, p2pkh_script(buyer))])
    tampered = _paste(_paste(swapped, 1, sigs[1]), 0, sigs[0])
    done = rpc.call("signrawtransaction", tampered)
    with pytest.raises(RpcError) as refused:
        rpc.call("sendrawtransaction", done["hex"])
    assert "bad-sign" in str(refused.value) or "16:" in str(refused.value), (
        f"the network took a payload the seller never signed: {refused.value}")


def test_a_leg_that_signs_its_payment_only_cannot_name_the_piece(regtest, db):
    """The hole a second digest closes, shown rather than described.

    This is a leg built with no payload, which is what a COIN still uses and is
    honest for: one input, its own payment, and whatever the completer feels
    like writing. Two payloads that name two different pieces give the SAME
    digest, so one signature is good for both, and the node takes the
    transaction without ever having been shown either. A listing like that is not
    an order for one piece; it is an order for whichever piece the completer chose
    to write -- which is why `build_leg` refuses to make one for an asset, and why
    `listings` keeps the bytes in a column of their own.
    """
    rpc = regtest.rpc
    secret = 0x7788990011223344778899001122334477889900112233447788990011223344
    pubkey, seller = _key(rpc, db, secret)
    piece = _coin(db, seller)
    price = int(1.0 * COIN)

    leg = funding.build_leg(PARAMS, seller, piece, coins=price, rate=RATE)
    mine = funding.sighash([piece], [(leg.paid, leg.outputs[-1][1])], 0,
                           p2pkh_script(seller),
                           sighash_type=funding.SINGLE_ANYONECANPAY)
    assert leg.sighashes == [mine.hex()]

    # What the seller signed says nothing about either payload, and cannot:
    # `SINGLE` takes the output standing at the signed input's index alone, so
    # an output after it is invisible here. Both completions below are
    # authorised by the one signature above.
    for naming in (b"the-piece" * 3, b"another-piece" * 3):
        assert funding.sighash([piece], [(leg.paid, leg.outputs[-1][1]),
                                         (0, _payload(naming))], 0,
                               p2pkh_script(seller),
                               sighash_type=funding.SINGLE_ANYONECANPAY) == mine

    signed = push(_sign(secret, bytes.fromhex(leg.sighashes[0]),
                        funding.SINGLE_ANYONECANPAY)) + push(pubkey)
    buyer, theirs, back = _buyer_side(rpc, seller, price, 10_000)
    # The payload appended at index 1: an output no signature stands at, since
    # the one signature there is is over the output at index 0.
    finished = build_raw_tx(
        [(piece["txid"], piece["vout"]), (theirs["txid"], theirs["vout"])],
        [leg.outputs[-1], (0, _payload(b"the-piece" * 3)),
         (back, p2pkh_script(buyer))])
    taken = rpc.call("sendrawtransaction",
                    rpc.call("signrawtransaction", _paste(finished, 0, signed))["hex"])
    assert len(taken) == 64, (
        "the node was expected to take this one happily -- the point is that it "
        f"took a payload it was never shown: {taken}")


def test_a_leg_that_names_a_thing_is_two_inputs_and_two_digests():
    """No node: the shape `build_leg` builds for an asset, in the arithmetic.

    The piece signs the payload and a second coin of the seller's signs the
    payment, because a `SINGLE` digest reaches the output standing at its own
    input's index and no other -- so two outputs worth pinning need two inputs
    standing under them. The seller's second coin pays for nothing: it comes back
    inside the seller's own output, and its whole job is to be signed at index 1.
    """
    piece = {"txid": "ab" * 32, "vout": 0, "value": 3 * COIN, "address": SELLER}
    spare = {"txid": "cd" * 32, "vout": 1, "value": COIN, "address": SELLER}
    naming = _named(b"the-piece" * 3)
    leg = funding.build_leg(PARAMS, SELLER, piece, coins=COIN, rate=RATE,
                            payload=naming, coin=spare)
    script = p2pkh_script(SELLER)

    assert [(i["txid"], i["vout"]) for i in leg.inputs] == \
        [(piece["txid"], 0), (spare["txid"], 1)], \
        "the piece is input 0: it is the input that makes the seller the seller"
    assert leg.payload == naming, "the bytes come back out, not a hash of them"
    assert leg.outputs[0] == (0, op_return_script(naming)), \
        "what is being sold is output 0, where the first signature reaches"
    assert leg.outputs[-1] == (leg.paid, script), "and the payment at the other"
    assert leg.paid == 3 * COIN + COIN + COIN - leg.fee
    assert leg.fee == funding.swap_fee(RATE, op_return_script(naming)), \
        "the reservation is priced at the swap this leg becomes: three in, three out"

    raw = bytes.fromhex(leg.raw)
    assert raw[4] == 2, "two inputs"
    assert raw[5 + 2 * BLANK_INPUT] == 2, "two outputs, at the two indices they sign"
    assert leg.sighashes == [
        funding.sighash([piece, spare], leg.outputs, n, script,
                        sighash_type=funding.SINGLE_ANYONECANPAY).hex()
        for n in (0, 1)]
    assert leg.sighashes[0] != leg.sighashes[1], \
        "one digest covering both outputs is the mistake the shape exists to avoid"


def test_a_leg_cannot_name_a_thing_it_cannot_sign_twice():
    """Each way the two-input shape can be got wrong, refused where it is built.

    None of these need a chain, and none of them are hypothetical: they are the
    four ways a caller can ask for a leg that says it sells something while
    signing only what it is paid for it.
    """
    piece = {"txid": "ab" * 32, "vout": 0, "value": 3 * COIN, "address": SELLER}
    spare = {"txid": "cd" * 32, "vout": 1, "value": COIN, "address": SELLER}
    naming = _named(b"the-piece" * 3)

    with pytest.raises(funding.FundingError) as refused:
        funding.build_leg(PARAMS, SELLER, piece, coins=COIN, rate=RATE,
                          payload=naming)
    assert "signed by nobody" in str(refused.value), \
        "a payload with no second input is a payload nobody signed"

    with pytest.raises(funding.FundingError) as refused:
        funding.build_leg(PARAMS, SELLER, piece, coins=COIN, rate=RATE,
                          coin=spare)
    assert "names nothing" in str(refused.value), \
        "a second input buys a second signature, and buys nothing else"

    with pytest.raises(funding.FundingError) as refused:
        funding.build_leg(PARAMS, SELLER, piece, coins=COIN, rate=RATE,
                          payload=naming, coin=piece)
    assert "twice" in str(refused.value), \
        "the same outpoint cannot be both the piece and the coin under it"

    with pytest.raises(funding.FundingError) as refused:
        funding.build_leg(PARAMS, SELLER, piece, coins=COIN, rate=RATE,
                          payload=naming, coin={**spare, "address": STRANGER})
    assert "cannot sign a second input" in str(refused.value), \
        "an input the seller cannot sign is an input nobody signs"
