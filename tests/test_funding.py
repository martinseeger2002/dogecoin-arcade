"""Spending from an address the node has no key for, proved on a real node.

This is the half of the design that cannot be checked by reading it. The
sighash algorithm is a serialisation, and a serialisation that is subtly
wrong produces a signature that verifies against nothing: the only honest
proof is that a real pepecoind accepts the transaction into its mempool.

So the key here is made in Python with a throwaway secp256k1
implementation used ONLY by this test -- the application never signs with
Python -- and what is asserted is what the node said.
"""

import hashlib

import pytest

from arcade import fees, funding, utxos
from arcade.config import NETWORKS
from arcade.db import Database, StateDB
from arcade.rpc import RpcError
from arcade.script import b58check_encode, hash160
from arcade.state import install_schema
from arcade.txbuild import multisig_script, p2pkh_script

PARAMS = NETWORKS["regtest"]
COIN = 100_000_000

# --- a minimal secp256k1, for the TEST only -----------------------------------
#
# The application signs in the browser with an audited library. This exists
# so the test can play the part of a browser without one, and is not
# imported by anything that ships.

P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0] and (p[1] + q[1]) % P == 0:
        return None
    if p == q:
        lam = (3 * p[0] * p[0]) * pow(2 * p[1], P - 2, P) % P
    else:
        lam = (q[1] - p[1]) * pow(q[0] - p[0], P - 2, P) % P
    x = (lam * lam - p[0] - q[0]) % P
    return (x, (lam * (p[0] - x) - p[1]) % P)


def _mul(k, point=G):
    out = None
    while k:
        if k & 1:
            out = _add(out, point)
        point = _add(point, point)
        k >>= 1
    return out


def _pubkey(secret: int) -> bytes:
    x, y = _mul(secret)
    return bytes([2 + (y & 1)]) + x.to_bytes(32, "big")


def _der(r: int, s: int) -> bytes:
    def integer(value):
        raw = value.to_bytes((value.bit_length() + 8) // 8 or 1, "big")
        return bytes([2, len(raw)]) + raw
    body = integer(r) + integer(s)
    return bytes([0x30, len(body)]) + body


def _sign(secret: int, digest: bytes,
          sighash_type: int = funding.SIGHASH_ALL) -> bytes:
    z = int.from_bytes(digest, "big")
    k = int.from_bytes(hashlib.sha256(digest + secret.to_bytes(32, "big")
                                      ).digest(), "big") % N
    while True:
        point = _mul(k)
        r = point[0] % N
        if r:
            s = (pow(k, N - 2, N) * (z + r * secret)) % N
            if s > N // 2:                       # low-S, as the network wants
                s = N - s
            if s:
                return _der(r, s) + bytes([sighash_type])
        k += 1


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "index.sqlite")
    install_schema(database)
    yield database
    database.close()


def test_a_transaction_signed_outside_the_node_is_accepted_by_it(regtest, db):
    """The whole design in one test: coins at an address the node has no
    key for, an unsigned transaction built from the index, signatures made
    somewhere else, and a real pepecoind taking it."""
    rpc = regtest.rpc
    secret = 0x1122334455667788112233445566778811223344556677881122334455667788
    pubkey = _pubkey(secret)
    ours = b58check_encode(PARAMS.pubkeyhash_version, hash160(pubkey))

    # The node does not know this address. It says so itself.
    assert not rpc.call("validateaddress", ours).get("ismine")

    # Pay it, and let the index see the block.
    rpc.call("generate", 101)
    funded = rpc.call("sendtoaddress", ours, 5.0)
    height = rpc.call("getblockcount") + 1
    rpc.call("generate", 1)
    utxos.watch(db, ours, 0)
    block = rpc.call("getblock", rpc.call("getblockhash", height), 2)
    state = StateDB(db)
    with state.block_context(height=height, block_hash=block["hash"],
                             prev_hash=block["previousblockhash"],
                             block_time=block["time"],
                             tx_count=len(block["tx"]), processed_at=0):
        utxos.on_block(state, height, block, PARAMS, {ours})
    assert utxos.balance(db, ours) == int(5.0 * COIN), "the index saw the coins"

    # Build something to send, for an address the node DOES own, so it can
    # be checked afterwards from the other side.
    theirs = rpc.call("getnewaddress")
    unsigned = funding.build(db, PARAMS, ours,
                             [(int(1.0 * COIN), p2pkh_script(theirs))],
                             rate=100_000, what="a test payment")
    assert unsigned.inputs and unsigned.sighashes
    assert len(unsigned.sighashes) == len(unsigned.inputs)
    assert unsigned.change > 0, "the rest comes back"

    # Sign it as a browser would: over the bytes we were handed, nothing else.
    signatures = [_sign(secret, bytes.fromhex(h)).hex()
                  for h in unsigned.sighashes]
    signed = funding.assemble(unsigned, signatures, pubkey)

    # And the only opinion that counts.
    accepted = rpc.call("sendrawtransaction", signed)
    assert len(accepted) == 64, accepted
    in_pool = rpc.call("getrawmempool")
    assert accepted in in_pool

    rpc.call("generate", 1)
    got = rpc.call("gettransaction", accepted)
    assert got["confirmations"] >= 1, "and a block took it"


def test_the_change_comes_back_to_the_sender(regtest, db):
    """A Class B payload's obfuscation is seeded with the largest input's
    address, so change that wandered elsewhere would make the payload
    unreadable by everyone."""
    rpc = regtest.rpc
    secret = 0x2233445566778899223344556677889922334455667788992233445566778899
    pubkey = _pubkey(secret)
    ours = b58check_encode(PARAMS.pubkeyhash_version, hash160(pubkey))
    rpc.call("generate", 101)
    rpc.call("sendtoaddress", ours, 3.0)
    height = rpc.call("getblockcount") + 1
    rpc.call("generate", 1)
    utxos.watch(db, ours, 0)
    block = rpc.call("getblock", rpc.call("getblockhash", height), 2)
    state = StateDB(db)
    with state.block_context(height=height, block_hash=block["hash"],
                             prev_hash=block["previousblockhash"],
                             block_time=block["time"],
                             tx_count=len(block["tx"]), processed_at=0):
        utxos.on_block(state, height, block, PARAMS, {ours})

    unsigned = funding.build(db, PARAMS, ours,
                             [(int(0.5 * COIN), p2pkh_script(rpc.call("getnewaddress")))],
                             rate=100_000)
    change = [value for value, script in unsigned.outputs
              if script == p2pkh_script(ours)]
    assert change and change[0] == unsigned.change


def test_it_refuses_rather_than_building_something_unpayable(db):
    utxos.watch(db, "nNobody", 0)
    with pytest.raises(funding.FundingError) as refused:
        funding.build(db, PARAMS, "nNobody",
                      [(100_000, b"\x6a")], rate=100_000)
    assert "not enough" in str(refused.value)


def test_the_signatures_have_to_match_the_inputs(db):
    unsigned = funding.Unsigned(raw="", inputs=[{"txid": "aa" * 32, "vout": 0,
                                                 "value": 1, "address": "x"}],
                                outputs=[])
    with pytest.raises(funding.FundingError) as refused:
        funding.assemble(unsigned, [], b"\x02" + b"\x11" * 32)
    assert "signatures were needed" in str(refused.value)


def test_an_offer_says_who_makes_its_signatures():
    """The disclosure a tester asked for (S19), and the shape it has to keep.

    An offer named the address that signs and left the reader to work out
    whether that address is an account's or a node's -- which is a guess, and
    the wrong guess is a tab signing over coins it does not hold. The guess is
    what the field is for. What it must not be is `signed_from` as a string:
    `coins.js` feeds that to `Number()` and walks the input list from it, so a
    label there turns into NaN coins and an offer that checks nobody.
    """
    offer = funding.Unsigned(raw="", inputs=[{"txid": "aa" * 32, "vout": 0,
                                              "value": 1, "address": "x"}],
                             outputs=[])
    leg = funding.Leg(raw="", inputs=[{"txid": "aa" * 32, "vout": 0, "value": 1,
                                       "address": "x"}],
                      outputs=[], sighashes=[], fee=0, pays="0.00000000", paid=0)
    assert offer.as_json()["signed_by"] == "account", \
        "an Unsigned is unsigned, and it is the reader's own key it waits for"
    assert leg.as_json()["signed_by"] == "account", \
        "a leg is signed by whoever made it, in their own tab"
    assert isinstance(offer.as_json()["signed_from"], int), \
        "the index stays an index; who signs is a second field beside it"


def test_the_fee_offered_is_the_one_a_block_would_take(regtest, db):
    """The fee an account is shown has to be the fee a block asks for.

    Until 2026-09-22 this module counted a flat 34 bytes for every output,
    which is what a payment back to the sender is and nothing like a payload
    output. A bare multisig is around a hundred bytes and twenty sigops, and
    where sigops x 20 is the larger number, that is the size a miner divides
    the fee by. So an offer to write something was offered at a fraction of
    its real cost: relay takes it, mempool holds it, and block assembly
    walks past it -- the seventy-six-pieces-one-per-block note in
    arcade/fees.py is that, seen from outside.

    Sized here by the node's own `decoderawtransaction`, so the assertion is
    against what the network says the transaction is and not against the
    arithmetic that set the fee.
    """
    rpc = regtest.rpc
    secret = 0x3344556677889900334455667788990033445566778899003344556677889900
    pubkey = _pubkey(secret)
    ours = b58check_encode(PARAMS.pubkeyhash_version, hash160(pubkey))
    utxos.watch(db, ours, 0)
    for n in (1, 2):
        # A txid is 32 bytes. Longer than that builds a transaction the node
        # will not even decode, which is the only way this was found.
        db.conn.execute(
            "INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) "
            "VALUES (?,?,?,?,?)", (("%02x" % n) * 32, 0, ours, 10 * COIN, 1))

    payment = funding.build(db, PARAMS, ours,
                            [(COIN, p2pkh_script(rpc.call("getnewaddress")))],
                            rate=100_000)
    piece = funding.build(db, PARAMS, ours,
                          [(10_000, multisig_script([pubkey, pubkey]))] * 4,
                          rate=100_000)

    signed = funding.assemble(piece, [_sign(secret, bytes.fromhex(h)).hex()
                                      for h in piece.sighashes], pubkey)
    decoded = rpc.call("decoderawtransaction", signed)
    raw = len(signed) // 2
    sigops = fees.sigops_of(decoded)
    assert fees.virtual_size(raw, sigops) > raw, "sigops, not bytes, decide this one"
    assert piece.fee >= fees.fee_for(raw, sigops, 100_000), (
        f"offered {piece.fee} for a transaction the node sizes at "
        f"{fees.virtual_size(raw, sigops)}")
    assert piece.fee > 3 * payment.fee, (
        "four payload outputs cost several times what four payments cost")


def test_an_account_signs_half_a_trade(regtest, db):
    """A trade has two signers, and the node holds the key of neither.

    `swap.build` already makes a half-signed transaction, but the half it
    leaves empty is the seller's, because the empty one is always whoever the
    node cannot sign for. This is the shape the other way round: the coin in
    front belongs to the node's own wallet, the coin behind it belongs to an
    account the node will never hold a key for. Three things have to hold and
    the node is asked to say each of them: the browser is offered a signature
    for its own input and not for the other one; while the front input is
    empty nothing will take the transaction; and `signrawtransaction` -- which
    is what countersigning a trade is, and is the only thing the node can do
    with a coin it owns -- finishes it without touching the half it was not
    given.
    """
    rpc = regtest.rpc
    secret = 0x4455667788990011445566778899001144556677889900114455667788990011
    pubkey = _pubkey(secret)
    ours = b58check_encode(PARAMS.pubkeyhash_version, hash160(pubkey))

    rpc.call("generate", 101)
    seller = rpc.call("getnewaddress")
    rpc.call("sendtoaddress", seller, 2.0)
    rpc.call("sendtoaddress", ours, 5.0)
    height = rpc.call("getblockcount") + 1
    rpc.call("generate", 1)
    utxos.watch(db, ours, 0)
    block = rpc.call("getblock", rpc.call("getblockhash", height), 2)
    state = StateDB(db)
    with state.block_context(height=height, block_hash=block["hash"],
                             prev_hash=block["previousblockhash"],
                             block_time=block["time"],
                             tx_count=len(block["tx"]), processed_at=0):
        utxos.on_block(state, height, block, PARAMS, {ours})
    assert utxos.balance(db, ours) == 5 * COIN

    sold = rpc.call("listunspent", 1, 9999999, [seller])[0]
    owed = int(round(sold["amount"] * COIN)) + COIN
    piece = rpc.call("getnewaddress")

    trade = funding.build_partial(
        db, PARAMS, ours,
        [{"txid": sold["txid"], "vout": sold["vout"],
          "value": int(round(sold["amount"] * COIN))}],
        [(owed, p2pkh_script(piece))], rate=100_000, what="a test trade")

    assert trade.signed_from == 1, "the bought piece goes first"
    assert len(trade.sighashes) == 1, "one input is ours, so one signature is asked for"
    assert trade.inputs[0]["txid"] == sold["txid"]
    assert trade.fee > 0 and trade.change > 0

    half = funding.assemble(trade, [_sign(secret, bytes.fromhex(h)).hex()
                                    for h in trade.sighashes], pubkey)
    with pytest.raises(RpcError):
        rpc.call("sendrawtransaction", half)

    signed = rpc.call("signrawtransaction", half)
    assert signed.get("complete") is True, (
        "the node signed the one input it owns and nobody else's")
    txid = rpc.call("sendrawtransaction", signed["hex"])
    rpc.call("generate", 1)
    landed = rpc.call("getrawtransaction", txid, 1)
    assert landed["confirmations"] >= 1, "and a block took it"
    assert {(f"{float(out['value']):.8f}", out["scriptPubKey"]["hex"])
            for out in landed["vout"]} == {
                (f"{value / COIN:.8f}", script.hex())
                for value, script in trade.outputs}, (
        "two signers, one transaction, and it paid what was offered")



# --- dust: change nobody would relay (2026-09-25) --------------------------------------


def _one_coin(address, value):
    return [{"txid": "ab" * 32, "vout": 4, "value": value, "address": address}]


def test_change_under_the_dust_limit_goes_to_the_fee(db):
    """Live: a 0.01 coin paying a 0.00235 fee kept 0.00765 as change. Pepecoin asks
    0.01 more for an output that small; peers refused it and seven posts waited for
    a block that never came. The change goes to the miner instead."""
    ours = b58check_encode(PARAMS.pubkeyhash_version, hash160(_pubkey(7)))
    utxos.watch(db, ours, 0)
    unsigned = funding.build(db, PARAMS, ours, [(0, b"\x6a\x04post")],
                             rate=fees.MIN_FEE_PER_KB,
                             extra=_one_coin(ours, fees.DUST_LIMIT))
    assert unsigned.change == 0
    assert len(unsigned.outputs) == 1, "no output under the dust limit"
    assert unsigned.fee == fees.DUST_LIMIT


def test_change_at_the_dust_limit_is_kept(db):
    ours = b58check_encode(PARAMS.pubkeyhash_version, hash160(_pubkey(7)))
    utxos.watch(db, ours, 0)
    unsigned = funding.build(db, PARAMS, ours, [(0, b"\x6a\x04post")],
                             rate=fees.MIN_FEE_PER_KB,
                             extra=_one_coin(ours, 5 * fees.DUST_LIMIT))
    assert unsigned.change >= fees.DUST_LIMIT
    assert all(v >= fees.DUST_LIMIT for v, s in unsigned.outputs if not s.startswith(b"\x6a"))
