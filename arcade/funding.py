"""Building a transaction for an address this node does not own.

`fundrawtransaction` and `signrawtransaction` are wallet calls: both need
keys the node holds. In this design it holds none of anybody's, so both
have to be replaced -- the first by choosing inputs from the UTXO index
(arcade/utxos.py), the second by asking the browser to sign
(docs/multi-user.md §5).

What this module does is the first half and the arithmetic of the second:
pick the coins, build the unsigned transaction, and work out **exactly
what bytes each input has to sign**. It never sees a private key and has
no way to use one.

The handshake it is the node's half of:

    browser                          node
       |  what I want to do  ------>  |  choose inputs, build, and say
       |                              |  in plain words what it does
       |  <-- unsigned + sighashes + what it costs
       |  shows it, asks, signs       |
       |  signatures  -------------->  |  assemble, CHECK it is what was
       |                              |  offered, broadcast
       |  <-- txid                    |

The check on the way back is not a formality. The node built the thing; if
it broadcast whatever came back instead of what it offered, a compromised
page could have the browser sign one transaction and the node publish
another. Both ends verify, and neither is trusted alone.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from . import fees, utxos
from .config import Params
from .script import b58check_decode
from .txbuild import build_raw_tx, p2pkh_script, push, varint

#: What an input costs once it is signed: the outpoint, the sequence number
#: and a P2PKH scriptSig. Not a guess dressed as a fact: it is recomputed
#: from the real outputs once they are chosen, and the caller is told both.
BYTES_PER_INPUT = 148
BYTES_OVERHEAD = 10

#: A payment back to the sender: eight bytes of value, one of length, and the
#: twenty-five of a P2PKH script.
BYTES_PER_CHANGE = 34

SIGHASH_ALL = 1

#: A seller signing ONE coin of its own, before it knows who the buyer will
#: be. `SINGLE` commits it to the output standing at its own input's index and
#: to no other output's CONTENTS; `ANYONECANPAY` commits it to its own input
#: and to no other input. Together they are the only way to hand out a
#: signature in advance without signing a stranger's coins, which is what a
#: listing has to do -- the piece is promised before anybody turns up to pay
#: for it.
SIGHASH_SINGLE = 3
SIGHASH_ANYONECANPAY = 0x80
SINGLE_ANYONECANPAY = SIGHASH_SINGLE | SIGHASH_ANYONECANPAY

#: An output with nothing in it, written the way the legacy preimage does:
#: `CTxOut::SetNull()`, value -1 and no scriptPubKey. A SINGLE signature at
#: index i puts one of these in every output slot before i, so the length of
#: the output list it signs is `i + 1` and no more. Only a preimage's
#: construction: no transaction has an output like this in it.
NULL_OUTPUT = (-1, b"")


class FundingError(Exception):
    """Not enough coins, or nothing to spend them on. The message is shown."""


@dataclass
class Unsigned:
    """A transaction waiting for somebody else's signatures."""

    raw: str
    inputs: list[dict[str, Any]]            # txid, vout, value, address
    outputs: list[tuple[int, bytes]]
    sighashes: list[str] = field(default_factory=list)
    fee: int = 0
    change: int = 0
    what: str = ""
    #: The first input this address signs. Zero everywhere except a trade,
    #: where the inputs before it are the counterparty's and stay unsigned
    #: until the counterparty signs them.
    signed_from: int = 0

    def as_json(self) -> dict:
        return {
            "raw": self.raw,
            "inputs": [{"txid": i["txid"], "vout": i["vout"],
                        "value": i["value"], "address": i["address"]}
                       for i in self.inputs],
            "sighashes": self.sighashes,
            "signed_from": self.signed_from,
            "fee": self.fee,
            "change": self.change,
            "what": self.what,
        }


def choose(db, address: str, target: int, exclude=frozenset(),
           extra: list | None = None) -> list[dict]:
    """Coins for `address` worth at least `target`.

    Smallest sufficient first, so a wallet that has been split does not
    break a large output to pay for a small thing and collapse back to
    having one output -- the same rule the messaging sender follows, and
    for the same reason.

    `exclude` and `extra` are what make two transactions in a row
    possible. The index only knows what is in a BLOCK, so between a
    broadcast and its block it still shows the coin that was just spent and
    does not show the change that came back. Building from that picks the
    same coin twice and the node answers `txn-mempool-conflict` -- which is
    what it did, the first time an account claimed a name and published a
    key without waiting a block in between.
    """
    available = [u for u in utxos.unspent(db, address)
                 if (u["txid"], u["vout"]) not in exclude]
    for coin in (extra or []):
        if (coin["txid"], coin["vout"]) not in exclude:
            available.append(dict(coin))
    big_enough = [u for u in available if u["value"] >= target]
    ordered = (sorted(big_enough, key=lambda u: u["value"]) if big_enough
               else sorted(available, key=lambda u: -u["value"]))
    chosen, total = [], 0
    for coin in ordered:
        chosen.append(coin)
        total += coin["value"]
        if total >= target:
            return chosen
    raise FundingError(
        f"there is not enough here: {total / 100_000_000:.8f} available and "
        f"{target / 100_000_000:.8f} needed, at this address.")


def price(inputs: int, outputs: list, rate: int, change: bool = False) -> int:
    """Satoshis that get a transaction with these outputs into a block.

    Priced on the mempool's *virtual* size, because that is the number a
    miner divides the fee by -- not on its bytes. A bare multisig output,
    which is what every Class B payload output is, is around 105 bytes and
    twenty sigops, so it weighs 400 bytes to whoever fills the block. The
    note at the top of arcade/fees.py is what that looked like on testnet
    when it was not being counted: seventy-six pieces waiting, one taken per
    block. Counting a flat 34 bytes for every output offered an account a
    fee that relay accepts and block assembly walks past, which is a worse
    surprise than being asked for the money up front.

    The inputs are counted at their signed size, since they are not built
    yet; `outputs` are the real scripts. `change` adds the payment back to
    the sender by shape rather than by script: a P2PKH output is 34 bytes
    and one sigop whoever the owner is, and working it out from the address
    here would mean decoding that address before anybody had said whether
    there are coins to spend at all.
    """
    raw = BYTES_OVERHEAD + inputs * BYTES_PER_INPUT
    sigops = 0
    for _value, script in outputs:
        raw += 8 + len(varint(len(script))) + len(script)
        sigops += fees.legacy_sigops(script)
    if change:
        raw += BYTES_PER_CHANGE
        sigops += 1
    return fees.fee_for(raw, sigops, rate)


def sighash(raw_inputs: list[dict], outputs: list[tuple[int, bytes]],
            index: int, script: bytes, version: int = 1,
            locktime: int = 0, sighash_type: int = SIGHASH_ALL) -> bytes:
    """The 32 bytes input `index` must sign.

    The legacy algorithm: serialise the transaction with every scriptSig
    empty except this one, which carries the script being spent, append the
    sighash type as four little-endian bytes, and hash it twice.

    Written here rather than asked of the node because the node cannot be
    asked -- `signrawtransaction` needs the key. It is a serialisation with
    published test vectors and no secret in it, and a mistake in it makes a
    transaction the network refuses rather than a key that leaks.

    `sighash_type` defaults to SIGHASH_ALL, which is every input and every
    output, and is what everything that pays a fee from its own coins uses.
    SINGLE|ANYONECANPAY is the other one this builds ever asks for, and it
    narrows the serialisation instead: the preimage carries this input alone,
    and an output list `index + 1` long -- each slot BEFORE this one's
    serialised empty, at value -1 and no script, and the output standing at
    this input's own index for real. At index 0 that is one input and one
    output, which is what `build_leg` builds and why it is built one-in and
    one-out; at index 1 it is NOT index 0's shape with one more output beside
    it, and a digest that assumed so is a signature no node will ever agree
    with. Settled against a real pepecoind rather than against the
    algorithm's prose -- see `test_funding_leg.py`, "which output a signature
    has to be standing over".

    A SINGLE type whose index runs past the end of the outputs is refused
    rather than computed. The legacy rule substitutes the constant
    0x0000...0001 for the output list in exactly that case, so a signature
    over that preimage is a signature over a fixed number -- good for any
    transaction whatever, which is the one thing a signature must never be.
    Nothing else is offered: NONE, and SINGLE without ANYONECANPAY, have no
    use here, and a builder that silently accepted a type it did not mean to
    implement would be worse than one that refuses.
    """
    if sighash_type == SIGHASH_ALL:
        inputs, signed_at, outs = raw_inputs, index, outputs
    elif sighash_type == SINGLE_ANYONECANPAY:
        if index >= len(outputs):
            raise FundingError(
                f"input {index} has no output {index} to commit to, and the "
                f"preimage for that is a constant, so a signature over it "
                f"would authorise every transaction there is")
        inputs, signed_at = raw_inputs[index:index + 1], 0
        outs = [NULL_OUTPUT] * index + outputs[index:index + 1]
    else:
        raise FundingError(
            f"this builds a sighash for SIGHASH_ALL and for "
            f"SINGLE|ANYONECANPAY, not for {sighash_type:#04x}")

    raw = version.to_bytes(4, "little") + varint(len(inputs))
    for n, coin in enumerate(inputs):
        raw += bytes.fromhex(coin["txid"])[::-1]
        raw += int(coin["vout"]).to_bytes(4, "little")
        if n == signed_at:
            raw += varint(len(script)) + script
        else:
            raw += varint(0)
        raw += b"\xff\xff\xff\xff"
    raw += varint(len(outs))
    for value, script_out in outs:
        # Masked rather than written straight, because the empty output a
        # SINGLE preimage pads its list with carries -1, which is all-ones on
        # the wire -- and an OverflowError here would be a builder that cannot
        # make the one digest a second input needs.
        raw += (value & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")
        raw += varint(len(script_out)) + script_out
    raw += locktime.to_bytes(4, "little")
    raw += sighash_type.to_bytes(4, "little")
    return hashlib.sha256(hashlib.sha256(raw).digest()).digest()


def build(db, params: Params, address: str, payload_outputs: list,
          rate: int, what: str = "", dust: int = 0,
          exclude=frozenset(), extra: list | None = None) -> Unsigned:
    """An unsigned transaction paying `payload_outputs` from `address`.

    Change goes back to the sender, always: a Class B payload's obfuscation
    is seeded with "largest input by sum", so a transaction whose change
    wandered elsewhere would be read as having a different sender and the
    payload would be unreadable by everyone (docs/multi-user.md §3).
    """
    spend = sum(value for value, _ in payload_outputs)
    # Priced as though there will be a payment back to the sender, since
    # that is what the finished transaction carries whenever anything is left
    # over. The guess picks the coins; the fee is recomputed once their count
    # is known. Told to the caller as the number it actually is.
    guess = price(1, payload_outputs, rate, change=True)
    chosen = choose(db, address, spend + guess, exclude=exclude, extra=extra)
    fee = price(len(chosen), payload_outputs, rate, change=True)
    if sum(c["value"] for c in chosen) < spend + fee:
        chosen = choose(db, address, spend + fee, exclude=exclude, extra=extra)
        fee = price(len(chosen), payload_outputs, rate, change=True)

    total = sum(c["value"] for c in chosen)
    change = total - spend - fee
    outputs = list(payload_outputs)
    if change > dust:
        outputs.append((change, p2pkh_script(address)))
    else:
        # Too small to be worth an output: it goes to the miner rather than
        # creating a coin nobody can afford to spend.
        fee += max(0, change)
        change = 0

    raw = build_raw_tx([(c["txid"], c["vout"]) for c in chosen], outputs)
    script = p2pkh_script(address)
    hashes = [sighash(chosen, outputs, n, script).hex()
              for n in range(len(chosen))]
    return Unsigned(raw=raw, inputs=chosen, outputs=outputs, sighashes=hashes,
                    fee=fee, change=change, what=what)


def build_partial(db, params: Params, address: str, foreign: list,
                  payload_outputs: list, rate: int, what: str = "",
                  dust: int = 0, exclude=frozenset(),
                  extra: list | None = None) -> Unsigned:
    """A trade: the counterparty's coins in front, this address paying the rest.

    `foreign` are outpoints somebody else owns and will sign -- the piece
    being bought, usually. They go first and stay unsigned, and the sighashes
    handed back are only for the inputs `address` must sign, so a browser is
    never asked for a signature over a coin that is not its own. Change comes
    back to `address`, as always, and `address` pays the whole fee: it is the
    one asking.

    This is what `swap.build` does with a wallet it holds the keys to
    (arcade/swap.py:1256), redone for a node that holds nobody's key. Until
    it existed an account could hand over coins and receive nothing: every
    route that buys anything ended at `door.py`, because `assemble` puts one
    pubkey in every scriptSig and there was no way to offer a transaction
    whose first input belonged to someone else.
    """
    given = []
    for piece in foreign:
        txid = str(piece.get("txid") or "")
        vout, value = int(piece.get("vout", -1)), int(piece.get("value", 0))
        if len(txid) != 64 or vout < 0 or value <= 0:
            raise FundingError("that is not something the node can put in a "
                               "transaction: a coin needs a 32-byte txid, an "
                               "index, and an amount above nothing")
        given.append({"txid": txid, "vout": vout, "value": value,
                      "address": str(piece.get("address") or "")})

    spend = sum(value for value, _ in payload_outputs)
    theirs = sum(g["value"] for g in given)
    # What has to come from this address, once the counterparty's coins and
    # the fee are accounted for. Priced with a payment back in it, as `build`
    # is: the guess picks the coins, then the fee is what it really is.
    #
    # The foreign outpoints are excluded from the choice even though they are
    # not this address's to spend: if a caller names one wrongly, the honest
    # answer is a refusal below, not a transaction that spends the same coin
    # twice -- once unsigned, once signed -- which the network rejects for
    # reasons nobody reading the offer would recognise.
    guess = price(len(given) + 1, payload_outputs, rate, change=True)
    asked = spend + guess - theirs
    if asked <= 0:
        raise FundingError("that needs nothing from this address, so there is "
                           "nothing here for it to sign")
    mine = exclude | {(g["txid"], g["vout"]) for g in given}
    chosen = choose(db, address, asked, exclude=mine, extra=extra)
    fee = price(len(given) + len(chosen), payload_outputs, rate, change=True)
    asked = spend + fee - theirs
    if sum(c["value"] for c in chosen) < asked:
        chosen = choose(db, address, asked, exclude=mine, extra=extra)
        fee = price(len(given) + len(chosen), payload_outputs, rate, change=True)

    total = theirs + sum(c["value"] for c in chosen)
    change = total - spend - fee
    outputs = list(payload_outputs)
    if change > dust:
        outputs.append((change, p2pkh_script(address)))
    else:
        fee += max(0, change)
        change = 0

    every = given + chosen
    raw = build_raw_tx([(c["txid"], c["vout"]) for c in every], outputs)
    script = p2pkh_script(address)
    hashes = [sighash(every, outputs, len(given) + n, script).hex()
              for n in range(len(chosen))]
    return Unsigned(raw=raw, inputs=every, outputs=outputs, sighashes=hashes,
                    fee=fee, change=change, what=what, signed_from=len(given))


def assemble(unsigned: Unsigned, signatures: list[str], pubkey: bytes) -> str:
    """Put the signatures in and hand back a transaction.

    The node does this rather than the browser so that what goes out is
    built from what the node offered: the browser returns signatures over
    the bytes it was given, and nothing else it says is used.

    Inputs before `unsigned.signed_from` are left with an empty scriptSig --
    they belong to a counterparty that has not signed yet, and the network
    refuses the result until it does. That is the point: the node cannot
    sign them and must not be able to.
    """
    first = unsigned.signed_from
    if len(signatures) != len(unsigned.inputs) - first:
        raise FundingError(
            f"{len(unsigned.inputs) - first} signatures were needed and "
            f"{len(signatures)} came back")
    raw = (1).to_bytes(4, "little") + varint(len(unsigned.inputs))
    for n, coin in enumerate(unsigned.inputs):
        script_sig = b""
        if n >= first:
            script_sig = push(bytes.fromhex(signatures[n - first])) + push(pubkey)
        raw += bytes.fromhex(coin["txid"])[::-1]
        raw += int(coin["vout"]).to_bytes(4, "little")
        raw += varint(len(script_sig)) + script_sig
        raw += b"\xff\xff\xff\xff"
    raw += varint(len(unsigned.outputs))
    for value, script in unsigned.outputs:
        raw += value.to_bytes(8, "little") + varint(len(script)) + script
    raw += (0).to_bytes(4, "little")
    return raw.hex()


@dataclass
class Leg:
    """What a seller signs before it knows its buyer: one coin in, one out.

    Not an `Unsigned`. An `Unsigned` is a transaction waiting for signatures
    and there is exactly one of those in flight; a `Leg` is half of one that
    does not exist yet, and the difference is the whole point -- the node is
    going to receive a signature over this and then build the rest of the
    transaction around it, so the type that says "this was signed first, on
    its own" has to be a different type.
    """

    raw: str
    input: dict[str, Any]
    output: tuple[int, bytes]
    sighash: str
    fee: int
    #: What the seller's output pays, and to whom: its own coin back plus the
    #: price, less the fee it reserved. The buyer sees this number, and the
    #: paste checks the finished transaction still pays it.
    pays: str
    paid: int
    #: The type the browser must sign with. Not a knob to turn: it is
    #: SINGLE|ANYONECANPAY or the leg would commit to coins the seller has
    #: never seen.
    sighash_type: int = SINGLE_ANYONECANPAY
    what: str = ""

    def as_json(self) -> dict:
        return {
            "raw": self.raw,
            "input": {"txid": self.input["txid"], "vout": self.input["vout"],
                      "value": self.input["value"],
                      "address": self.input.get("address", "")},
            "output": {"value": self.output[0],
                       "script": self.output[1].hex()},
            "sighash": self.sighash,
            "sighash_type": self.sighash_type,
            "fee": self.fee,
            "pays": self.pays,
            "paid": self.paid,
            "what": self.what,
        }


#: What the finished swap costs the seller to reserve against: its own signed
#: input, the buyer's input, the payment to the seller, the piece to the buyer,
#: and the OP_RETURN carrying the two legs. The buyer's script is not known
#: when a leg is signed, so this is an estimate of a P2PKH.
#:
#: An estimate, and deliberately generous rather than tight: a leg that
#: reserves too little is a listing whose transactions will not confirm, and a
#: leg that reserves too much costs the seller a few thousandths of a coin.
#: What makes it safe is not the number, it is `paste_leg` refusing to
#: complete a transaction whose real fee turns out to be below the floor.
SWAP_INPUTS = 2

#: A P2PKH script by shape, for pricing something whose owner is not known
#: yet: OP_DUP OP_HASH160 <20> OP_EQUALVERIFY OP_CHECKSIG. Twenty-five bytes
#: and one sigop whoever the address turns out to be, so an unknown buyer is
#: priced exactly rather than guessed at.
P2PKH_SHAPE = bytes([0x76, 0xa9, 0x14]) + b"\x00" * 20 + bytes([0x88, 0xac])


def swap_fee(rate: int, payload_bytes: int = 0) -> int:
    """What the finished swap will cost, as closely as a listing can know it.

    This is the seller's CONTRIBUTION to the fee, not the fee. The seller's
    output is fixed the moment it signs, so whatever it reserves comes out of
    that output and cannot be adjusted afterwards; what the finished transaction
    actually pays is settled by whoever completes the listing, since that is the
    side that knows the real size. `listings.paste_leg` is what keeps the two
    honest with each other -- it refuses a finished swap whose total falls under
    what a block asks, which is the only way a quiet-market listing fails loudly
    instead of relaying forever. Reserving more than the swap costs buys a
    listing nothing: it moves value from the seller to the buyer's change.
    """
    outputs = [(0, P2PKH_SHAPE), (0, P2PKH_SHAPE)]
    if payload_bytes:
        outputs.append((0, b"\x6a" + varint(payload_bytes) + b"\x00" * payload_bytes))
    return price(SWAP_INPUTS, outputs, rate)


def build_leg(params: Params, address: str, piece: dict, coins: int,
              rate: int, what: str = "", payload_bytes: int = 0) -> Leg:
    """The transaction a seller signs to LIST: that one coin in, that one payment out.

    The other half of `build_partial`, and its mirror in one respect that
    matters: `build_partial` is what a BUYER signs when it already knows what
    it is buying, and this is what a SELLER signs when it cannot yet know who
    is buying. A listing has to promise the piece before a buyer exists, so
    the signature has to be over as little of a transaction as can possibly be
    honest -- one input, the piece, and one output, the payment this seller
    will accept and nothing else. That is what `SINGLE|ANYONECANPAY` buys, and
    it only works because this transaction is one input wide: `SINGLE` commits
    to the output at the signed input's own index, and building it any other
    way would leave that rule to chance.

    The fee is reserved here, at listing time, because the seller's output is
    fixed the moment it signs and cannot be adjusted later: whatever the
    finished swap costs comes out of this reservation and out of the buyer's
    own inputs, and `paste_leg` refuses the transaction if the two do not add
    up to what a block asks for.
    """
    txid = str(piece.get("txid") or "")
    vout, value = int(piece.get("vout", -1)), int(piece.get("value", 0))
    if len(txid) != 64 or vout < 0 or value <= 0:
        raise FundingError("that is not something a listing can promise: a "
                           "piece needs a 32-byte txid, an index, and an "
                           "amount above nothing")
    holder = str(piece.get("address") or "")
    if holder and holder != address:
        raise FundingError(f"{txid[:16]}…:{vout} belongs to {holder}, not to "
                           f"{address}, so this wallet cannot sell it")
    if coins < 0:
        raise FundingError("a price below nothing is not a price")

    fee = swap_fee(rate, payload_bytes)
    paid = value + coins - fee
    if paid <= 0:
        raise FundingError(
            f"the price ({coins} sats) does not cover what a block costs "
            f"({fee} sats) plus keeping the coin ({value} sats), so there is "
            f"no output this seller could sign")

    script = p2pkh_script(address)
    outputs = [(paid, script)]
    raw = build_raw_tx([(txid, vout)], outputs)
    digest = sighash([{"txid": txid, "vout": vout, "value": value}], outputs,
                     0, script, sighash_type=SINGLE_ANYONECANPAY)
    return Leg(raw=raw, input={"txid": txid, "vout": vout, "value": value,
                               "address": address},
               output=outputs[0], sighash=digest.hex(), fee=fee,
               pays=address, paid=paid, what=what)
