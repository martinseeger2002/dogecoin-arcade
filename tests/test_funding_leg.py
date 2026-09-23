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
from arcade.txbuild import build_raw_tx, p2pkh_script, push

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


def _key(rpc, db, secret: int) -> tuple[int, str]:
    """An address the node has no key for, funded, and seen by the index."""
    pubkey = _pubkey(secret)
    address = b58check_encode(PARAMS.pubkeyhash_version, hash160(pubkey))
    assert not rpc.call("validateaddress", address).get("ismine"), \
        "the whole point is that this node cannot sign for it"
    rpc.call("generate", 101)
    rpc.call("sendtoaddress", address, 5.0)
    height = rpc.call("getblockcount") + 1
    rpc.call("generate", 1)
    utxos.watch(db, address, 0)
    block = rpc.call("getblock", rpc.call("getblockhash", height), 2)
    state = StateDB(db)
    with state.block_context(height=height, block_hash=block["hash"],
                             prev_hash=block["previousblockhash"],
                             block_time=block["time"],
                             tx_count=len(block["tx"]), processed_at=0):
        utxos.on_block(state, height, block, PARAMS, {address})
    return pubkey, address


def test_a_leg_is_one_coin_in_and_one_payment_out():
    """No node: this is arithmetic about what a listing reserves.

    The seller's output is its own coin back plus the price less the fee it
    reserved. That number is fixed the moment it signs, so getting it wrong is
    not a mistake a later step can correct.
    """
    piece = {"txid": "ab" * 32, "vout": 1, "value": 3 * COIN, "address": SELLER}
    leg = funding.build_leg(PARAMS, SELLER, piece, coins=1 * COIN, rate=RATE)

    assert leg.input["txid"] == piece["txid"] and leg.input["vout"] == 1
    assert leg.output[1] == p2pkh_script(SELLER), "it pays the seller, not the node"
    assert leg.paid == 3 * COIN + 1 * COIN - leg.fee
    assert leg.output[0] == leg.paid
    assert leg.fee == funding.swap_fee(RATE), "the reservation is the swap's real cost"
    assert leg.sighash_type == funding.SINGLE_ANYONECANPAY == 0x83

    # One input and one output, because `SINGLE` commits to the output standing
    # at the signed input's own index and this must not be left to chance.
    raw = bytes.fromhex(leg.raw)
    assert raw[4] == 1, "one input"
    assert raw[5 + BLANK_INPUT] == 1, "one output, at index 0, the one it signed"
    assert leg.sighash == funding.sighash(
        [piece], [(leg.paid, p2pkh_script(SELLER))], 0,
        p2pkh_script(SELLER), sighash_type=funding.SINGLE_ANYONECANPAY).hex()


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
    assert leg.sighash == mine.hex()

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
    signed = _sign(secret, bytes.fromhex(leg.sighash),
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
        [leg.output, (back, p2pkh_script(buyer))])
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
               and out["scriptPubKey"]["hex"] == leg.output[1].hex()
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
    signed = _sign(secret, bytes.fromhex(leg.sighash),
                   funding.SINGLE_ANYONECANPAY)

    buyer = rpc.call("getnewaddress")
    theirs = [u for u in rpc.call("listunspent", 1, 9999999)
              if u["address"] != seller and not u.get("spend")][0]
    theirs_value = int(round(theirs["amount"] * COIN))
    # The fee stays exactly what it was in the passing test: the only thing
    # wrong with this transaction is the signature.
    back = theirs_value - price - 10_000
    assert back > 546

    grown = (leg.paid + 10_000, leg.output[1])
    finished = build_raw_tx(
        [(piece["txid"], piece["vout"]), (theirs["txid"], theirs["vout"])],
        [grown, (back, p2pkh_script(buyer))])
    half = _paste(finished, 0, push(signed) + push(pubkey))
    done = rpc.call("signrawtransaction", half)

    with pytest.raises(RpcError) as refused:
        rpc.call("sendrawtransaction", done["hex"])
    assert "bad-sign" in str(refused.value) or "16:" in str(refused.value), (
        f"the network took a leg whose payment had been changed: {refused.value}")
