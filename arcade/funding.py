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
import time
from dataclasses import dataclass, field
from typing import Any

from . import fees, utxos
from .config import Params
from .script import (b58check_decode, hash160, p2sh_p2wpkh_address,
                     p2sh_p2wpkh_redeem, p2wpkh_script as _p2wpkh_script,
                     segwit_decode)
from .txbuild import build_raw_tx, op_return_script, p2pkh_script, push, time_field, varint

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
#: ALL|ANYONECANPAY: what a pre-signed OFFER's inputs sign (2026-09-27:
#: an accepted offer completes the moment it is accepted, buyer online or not).
#: ALL, so every output -- the swap's bytes, the seller's payment, the buyer's
#: change -- is fixed by the buyer's signature and nobody can redirect a coin;
#: ANYONECANPAY, so the seller can add the one input the trade still needs.
ALL_ANYONECANPAY = SIGHASH_ALL | SIGHASH_ANYONECANPAY

#: The seller's side of a pre-signed offer is one coin of exactly this much,
#: paid straight back to them in the output the buyer fixed. It has to be an
#: amount known when the buyer signs, because the engine reads a coin price as
#: what the seller NETS (`Engine._check_leg`: outputs to the seller minus the
#: seller's inputs), and the buyer's signature covers every output. At the dust
#: limit it is the smallest coin that is not itself charged the soft-dust fee.
EXACT_SELLER_COIN = fees.DUST_LIMIT

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
    #: A Peercoin-style chain's transaction time (Params.tx_time, Kotia): in `raw`, in every sighash, and
    #: written again by `assemble`. None on every other chain.
    ntime: int | None = None

    def as_json(self) -> dict:
        return {
            "raw": self.raw,
            # `script` only for a bare-multisig input (a Class B payload output,
            # swept back by `build_dust_sweep`): the browser signs over it and
            # checks its own key is in it (coins.js verifyOffer).
            "inputs": [{"txid": i["txid"], "vout": i["vout"],
                        "value": i["value"], "address": i["address"],
                        **({"script": i["script"]} if i.get("multisig") else {}),
                        **({"redeem": i["redeem"]} if i.get("redeem") else {}),
                        # A DRC-20 / PRC-20 note's reveal (token20_send): the envelope the node puts in front of the
                        # signature, so the browser can show the note it is signing for (coins.js reads it back).
                        **({"envelope": i["envelope"]} if i.get("envelope") else {}),
                        **({"segwit": i["segwit"]} if i.get("segwit") else {})}
                       for i in self.inputs],
            "sighashes": self.sighashes,
            "signed_from": self.signed_from,
            # Whose key makes the signatures this is waiting for -- the thing an
            # offer used to leave unsaid while it named the address that signs
            # (a tester). It is read off the type, never off `signed_from`,
            # which is an index into `inputs` and stays one: an `Unsigned` is
            # unsigned and waiting for the reader's own key, since the node that
            # built it holds neither that key nor those coins. A node's own
            # signature is announced by `swap.public`, and nothing hashes this.
            "signed_by": "account",
            "fee": self.fee,
            "change": self.change,
            "what": self.what,
        }


#: Set by the app when this node has Dogecoin (arcade/doginals.Doginals): choose() never hands out a coin carrying a Doginal
#: to an ordinary send. GUARD_APPLIES says which addresses it watches (Dogecoin's, version 30: "D...").
GUARD = None
GUARD_APPLIES = lambda address: str(address).startswith("D")   # noqa: E731
#: More guards, each (applies(address), guard): the same chain reading for Pepecoin MAINNET, where ord envelopes sit on small
#: coins too (2026-10-15: "do the same for small .01 pep txs. Not made by the Arcade" -- the arcade's own coins are
#: vouched clean when it broadcasts them, doginals.vouch_broadcast).
GUARDS: list = []
#: The guard by chain: "doge" (Doginals), "main" (Pepinals) -- for a lock that holds one (2026-10-17).
GUARD_BY_NET: dict = {}


def guard_on(network: str):
    """The inscription guard for a chain, by name: Dogecoin's falls back on GUARD."""
    return GUARD_BY_NET.get(network) or (GUARD if network == "doge" else None)


def guard_for(address: str):
    """The inscription guard that watches `address`, or None."""
    if GUARD is not None and GUARD_APPLIES(address):
        return GUARD
    for applies, guard in GUARDS:
        if applies(address):
            return guard
    return None


#: Outpoints of DRC-20 / PRC-20 notes this arcade revealed and has not seen in a block yet (token20 routes add them as
#: each reveal goes out): `choose` never pays with one. In memory: after a restart the guard reads them from the chain.
NOTE_COINS: set = set()


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
    listed = utxos.unspent(db, address)
    available = [u for u in listed
                 if (u["txid"], u["vout"]) not in exclude]
    guard = guard_for(address)       # coins carrying an inscription stay put (arcade/doginals.py): Dogecoin and PEP mainnet
    if guard is not None:
        available = guard.spendable(available)
    # An `extra` coin the index also lists is the same coin twice. Once a block
    # has read a change output it stands in both places at once -- the note this
    # node made of it, and the row the scanner wrote -- and until the note ages
    # out the two would be offered as two inputs, which is a transaction the
    # chain refuses outright. Where the index agrees the coin exists, the index
    # is what gets used.
    outpoints = {(u["txid"], u["vout"]) for u in listed}
    for coin in (extra or []):
        out = (coin["txid"], coin["vout"])
        if out in exclude or out in outpoints:
            continue
        # A DRC-20 / PRC-20 note this arcade just revealed onto the address (token20_send / token20_swap), unconfirmed
        # and read by no index yet: never taken to pay (it would carry its tokens into a fee). Named by outpoint, so
        # every other coin on its way pays as before (2026-10-22: a rule on every small coin kept a take's own back).
        if out in NOTE_COINS:
            continue
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


def price(inputs: int, outputs: list, rate: int, change: bool = False, params=None) -> int:
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
    if getattr(params, "tx_time", False):
        raw += 4                                    # a Peercoin-style chain's nTime (Kotia)
    sigops = 0
    for _value, script in outputs:
        raw += 8 + len(varint(len(script))) + len(script)
        sigops += fees.legacy_sigops(script)
    if change:
        raw += BYTES_PER_CHANGE
        sigops += 1
    # The soft-dust fee is the Dogecoin family's; a chain without it (Litecoin) adds nothing.
    soft = fees.soft_dust_fee(outputs) if getattr(params, "soft_dust", True) else 0
    return fees.fee_for(raw, sigops, rate) + soft


def sighash(raw_inputs: list[dict], outputs: list[tuple[int, bytes]],
            index: int, script: bytes, version: int = 1,
            locktime: int = 0, sighash_type: int = SIGHASH_ALL,
            ntime: int | None = None) -> bytes:
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
    output; at index 1 it is NOT index 0's shape with one more output beside
    it, and a digest that assumed so is a signature no node will ever agree
    with. `build_leg` asks for both of them at once, and its shape -- the piece
    at input 0 standing over the payload, a coin of the seller's at input 1
    standing over the payment -- is that rule rather than a preference. Settled
    against a real pepecoind rather than against the algorithm's prose -- see
    `test_funding_leg.py`, "which output a signature has to be standing over".

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
    elif sighash_type == ALL_ANYONECANPAY:
        # This input alone, and every output: the other inputs are anybody's.
        inputs, signed_at, outs = raw_inputs[index:index + 1], 0, outputs
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
            f"this builds a sighash for SIGHASH_ALL, ALL|ANYONECANPAY and "
            f"SINGLE|ANYONECANPAY, not for {sighash_type:#04x}")

    # A Peercoin-style chain (Kotia) signs its `nTime` too, right after the version (kotiacash interpreter.cpp).
    raw = version.to_bytes(4, "little") + time_field(ntime) + varint(len(inputs))
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


def untimed(params: Params, what: str) -> None:
    """Refuse a builder that does not write `nTime` yet on a chain whose transactions need one (Params.tx_time,
    Kotia): only `build` does, so far. Without this the chain's Core refuses the bytes with a decode error."""
    if getattr(params, "tx_time", False):
        raise FundingError(f"a {what} is not open on {params.label} yet: only ordinary sends are")


def tx_time(params: Params) -> int | None:
    """The `nTime` a transaction built now carries on a Peercoin-style chain (Params.tx_time, Kotia), else None.

    Now, by this machine's clock: Kotia's Core takes a transaction no more than 15 seconds ahead of its own
    (main.cpp "time-too-new"), wants it no later than the block that holds it, and no earlier than any
    transaction whose coins it spends -- coins this arcade spends were made before now."""
    return int(time.time()) if getattr(params, "tx_time", False) else None


def build(db, params: Params, address: str, payload_outputs: list,
          rate: int, what: str = "", dust: int = 0,
          exclude=frozenset(), extra: list | None = None,
          keyhash: bytes | None = None) -> Unsigned:
    """An unsigned transaction paying `payload_outputs` from `address`.

    `keyhash` is for a P2SH-nested segwit sender only (see `spend_for`).

    Change goes back to the sender, always: a Class B payload's obfuscation
    is seeded with "largest input by sum", so a transaction whose change
    wandered elsewhere would be read as having a different sender and the
    payload would be unreadable by everyone (docs/multi-user.md §3).
    """
    spend = sum(value for value, _ in payload_outputs)
    rate = fees.rate_for(params, rate)              # this chain's own minimum (2026-10-18)
    # Priced as though there will be a payment back to the sender, since
    # that is what the finished transaction carries whenever anything is left
    # over. The guess picks the coins; the fee is recomputed once their count
    # is known. Told to the caller as the number it actually is.
    guess = price(1, payload_outputs, rate, change=True, params=params)
    chosen = choose(db, address, spend + guess, exclude=exclude, extra=extra)
    fee = price(len(chosen), payload_outputs, rate, change=True, params=params)
    if sum(c["value"] for c in chosen) < spend + fee:
        chosen = choose(db, address, spend + fee, exclude=exclude, extra=extra)
        fee = price(len(chosen), payload_outputs, rate, change=True, params=params)

    total = sum(c["value"] for c in chosen)
    change = total - spend - fee
    rule = spend_for(address, params, keyhash)
    outputs = list(payload_outputs)
    if change >= max(dust, fees.dust_limit(params)):
        outputs.append((change, rule["change_script"]))
    else:
        # Too small to be worth an output: it goes to the miner rather than
        # creating a coin nobody can afford to spend. Below fees.DUST_LIMIT it
        # would also cost the dust fee, and without it no peer relays the
        # transaction at all.
        fee += max(0, change)
        change = 0

    ntime = tx_time(params)
    raw = build_raw_tx([(c["txid"], c["vout"]) for c in chosen], outputs, ntime=ntime)
    hashes = []
    for n, coin in enumerate(chosen):
        if rule["kind"] in ("p2wpkh", "p2sh-p2wpkh"):
            coin["segwit"] = rule["kind"]
            hashes.append(sighash_witness(chosen, outputs, n, rule["script_code"],
                                          int(coin["value"])).hex())
        else:
            hashes.append(sighash(chosen, outputs, n, rule["script_code"], ntime=ntime).hex())
    return Unsigned(raw=raw, inputs=chosen, outputs=outputs, sighashes=hashes,
                    fee=fee, change=change, what=what, ntime=ntime)


def build_one(params: Params, address: str, coin: dict,
              payload_outputs: list, rate: int, what: str = "",
              dust: int = 0) -> Unsigned:
    """One named coin paying these outputs, with no coin hunting at all.

    `build` searches the index for coins worth enough; here the funding is
    already decided -- the output of a split this node itself just built, which
    is what pays for one piece of a multi-transaction inscription
    (arcade/accountparts.py). Searching instead would be wrong twice over. The
    split's output is usually unconfirmed, so it is not in the index yet and
    `extra` would have to carry it; and if the index DID happen to hold some
    other coin worth enough, `choose` would take the smallest sufficient one and
    the piece would be paid for out of the wallet while its own output sat there
    unspent -- the same inscription, paid for twice, one of those payments
    stranded.

    A named coin is also what makes a piece buildable a week later: its input is
    an outpoint, not a coin picked out of wherever the balance happens to be
    today, so the transaction comes out identical after a restart.
    """
    untimed(params, "split piece")
    txid, vout = str(coin.get("txid") or ""), int(coin.get("vout", -1))
    value = int(coin.get("value", 0))
    if len(txid) != 64 or vout < 0 or value <= 0:
        raise FundingError(
            "that is not a coin this node can spend: a coin needs a 32-byte "
            "txid, an index, and an amount above nothing")
    given = {"txid": txid, "vout": vout, "value": value,
             "address": str(coin.get("address") or address)}

    spend = sum(value for value, _ in payload_outputs)
    rate = fees.rate_for(params, rate)              # this chain's own minimum (2026-10-18)
    fee = price(1, payload_outputs, rate, change=True, params=params)
    if value < spend + fee:
        raise FundingError(
            f"that piece costs {spend + fee} and the output meant to pay for it "
            f"holds {value}. Nothing has been paid for.")

    change = value - spend - fee
    outputs = list(payload_outputs)
    if change >= max(dust, fees.dust_limit(params)):
        # Back to the sender, as always: a Class B payload's obfuscation is
        # seeded with the largest input, and a piece whose change wandered off
        # would be read as somebody else's and never indexed.
        outputs.append((change, p2pkh_script(address)))
    else:
        fee += max(0, change)
        change = 0

    raw = build_raw_tx([(txid, vout)], outputs)
    script = p2pkh_script(address)
    return Unsigned(raw=raw, inputs=[given], outputs=outputs,
                    sighashes=[sighash([given], outputs, 0, script).hex()],
                    fee=fee, change=change, what=what)


def build_sweep(db, params: Params, address: str, coins: list[dict], rate: int,
                what: str = "", exclude=frozenset(), extra: list | None = None) -> Unsigned:
    """Spend exactly these coins of `address` back to `address`.

    Cancelling a listing (2026-09-26: one "Sell", with "Cancel listing"):
    a leg signed SINGLE|ANYONECANPAY stays good until a block spends the coins it
    signed, and spending them is the only way to take it back. The listing's two
    coins are small, so when they cannot pay the fee and still leave a coin worth
    keeping, one more coin of the account's is added to pay for it.
    """
    untimed(params, "sweep")
    given = []
    for coin in coins:
        txid, vout = str(coin.get("txid") or ""), int(coin.get("vout", -1))
        if len(txid) != 64 or vout < 0:
            raise FundingError("that is not a coin this account holds")
        given.append({"txid": txid, "vout": vout, "value": int(coin.get("value", 0)),
                      "address": address})
    if not given:
        raise FundingError("there is nothing to spend")
    taken = {(c["txid"], c["vout"]) for c in given}
    total = sum(c["value"] for c in given)
    rate, least = fees.rate_for(params, rate), fees.dust_limit(params)     # this chain's own (2026-10-18)
    fee = price(len(given), [], rate, change=True, params=params)
    if total - fee < least:
        more = choose(db, address, fee + least - total + price(1, [], rate, params=params),
                      exclude=frozenset(exclude) | taken, extra=extra)
        given += [dict(c, address=address) for c in more]
        total = sum(c["value"] for c in given)
        fee = price(len(given), [], rate, change=True, params=params)
    change = total - fee
    if change < least:
        raise FundingError("this account does not hold enough to pay the fee")
    outputs = [(change, p2pkh_script(address))]
    raw = build_raw_tx([(c["txid"], c["vout"]) for c in given], outputs)
    script = p2pkh_script(address)
    return Unsigned(raw=raw, inputs=given, outputs=outputs,
                    sighashes=[sighash(given, outputs, n, script).hex()
                               for n in range(len(given))],
                    fee=fee, change=change, what=what)


def build_pool_close(address: str, coins: list[dict], back_to: str, rate: int,
                     payload: bytes = b"", what: str = "", redeem: bytes = b"") -> Unsigned:
    """Close a prize pool: every coin of the pool's own address, and the
    tokens it still holds, back to the account in one transaction.

    `payload` is the Class C token send of what the pool still holds, or
    nothing. The pool's address is the only input address, so it is the
    send's sender, and `back_to` is the only other output, so it is the
    reference that receives the tokens (tx.determine_reference). Spending the
    coins also stills every lot signed over them (2026-09-29: "the pool should
    be able to be canceled and closed").
    """
    given = [{"txid": str(c["txid"]), "vout": int(c["vout"]), "value": int(c["value"]),
              "address": address, **({"redeem": bytes(redeem).hex()} if redeem else {})}
             for c in coins]
    if not given:
        raise FundingError("that pool holds no coins to close it with")
    outputs = [(0, op_return_script(payload))] if payload else []
    total = sum(c["value"] for c in given)
    fee = price(len(given), outputs, rate, change=True)
    back = total - fee
    if back < fees.DUST_LIMIT:
        raise FundingError("that pool's coins cannot pay the fee to close it")
    outputs.append((back, p2pkh_script(back_to)))
    raw = build_raw_tx([(c["txid"], c["vout"]) for c in given], outputs)
    script = bytes(redeem) or p2pkh_script(address)
    return Unsigned(raw=raw, inputs=given, outputs=outputs,
                    sighashes=[sighash(given, outputs, n, script).hex()
                               for n in range(len(given))],
                    fee=fee, change=0, what=what)


def build_bid(db, params: Params, buyer: str, seller: str, payload: bytes,
              price_sats: int, rate: int, what: str = "",
              exclude=frozenset(), extra: list | None = None) -> Unsigned:
    """The buyer's half of a pre-signed offer: every output of the finished
    swap, and the buyer's inputs signed ALL|ANYONECANPAY over them.

    Outputs, in the order the swap needs them:
      0  the swap's own bytes (the same payload a listing carries, `_ask_payload`)
      1  the seller, paid the price plus their own EXACT_SELLER_COIN back
      2  the buyer's change, when there is any worth keeping

    Inputs are the buyer's alone here. At accept the seller's one coin of
    exactly EXACT_SELLER_COIN goes in FRONT of them -- the first input is the
    swap's sender, and the sender is the seller (`Engine._swap`) -- and signs
    SIGHASH_ALL over the finished transaction. ANYONECANPAY is what lets that
    input be added without touching the buyer's signatures; ALL is what stops
    anybody moving a coin the buyer did not agree to. The fee is priced for the
    FINISHED transaction, the seller's input included, and the buyer pays it.
    A token price pays the seller no coins, so output 1 is only their coin back.
    """
    untimed(params, "bid")
    payload_script = op_return_script(payload)
    seller_out = (int(price_sats) + EXACT_SELLER_COIN, p2pkh_script(seller))
    outputs = [(0, payload_script), seller_out]
    spend = int(price_sats)
    fee = price(2, outputs, rate, change=True)
    chosen = choose(db, buyer, spend + fee, exclude=exclude, extra=extra)
    for _ in range(3):
        fee = price(len(chosen) + 1, outputs, rate, change=True) \
              + fees.soft_dust_fee(outputs)
        if sum(c["value"] for c in chosen) >= spend + fee:
            break
        chosen = choose(db, buyer, spend + fee, exclude=exclude, extra=extra)
    total = sum(c["value"] for c in chosen)
    change = total - spend - fee
    if change < 0:
        raise FundingError(
            f"there is not enough here for this offer: it needs "
            f"{(spend + fee) / 100_000_000:.8f} and this address has "
            f"{total / 100_000_000:.8f} free")
    outs = list(outputs)
    if change >= fees.DUST_LIMIT:
        outs.append((change, p2pkh_script(buyer)))
    else:
        fee += change
        change = 0
    inputs = [dict(c, address=buyer) for c in chosen]
    raw = build_raw_tx([(c["txid"], c["vout"]) for c in inputs], outs)
    script = p2pkh_script(buyer)
    return Unsigned(raw=raw, inputs=inputs, outputs=outs,
                    sighashes=[sighash(inputs, outs, n, script,
                                       sighash_type=ALL_ANYONECANPAY).hex()
                               for n in range(len(inputs))],
                    fee=fee, change=change, what=what)


def bid_need(payload: bytes, price_sats: int, seller: str, rate: int) -> int:
    """What the coin set aside for a pre-signed offer must hold: the price, and
    the fee of the FINISHED swap (its one buyer input, the seller's one input,
    the swap's bytes and the seller's payment). No change: it is exact."""
    outputs = [(0, op_return_script(payload)),
               (int(price_sats) + EXACT_SELLER_COIN, p2pkh_script(seller))]
    return int(price_sats) + price(2, outputs, rate) + fees.soft_dust_fee(outputs)


def build_bid_on(coin: dict, buyer: str, seller: str, payload: bytes,
                 price_sats: int, what: str = "") -> Unsigned:
    """The buyer's half, built on the one coin the offer set aside for it:
    that coin in, the swap's bytes and the seller's payment out, signed
    ALL|ANYONECANPAY. Whatever the coin holds beyond the price is the fee."""
    value = int(coin["value"])
    outputs = [(0, op_return_script(payload)),
               (int(price_sats) + EXACT_SELLER_COIN, p2pkh_script(seller))]
    if value <= int(price_sats):
        raise FundingError("the coin set aside for this offer does not cover its price")
    inputs = [{"txid": str(coin["txid"]), "vout": int(coin["vout"]),
               "value": value, "address": buyer}]
    raw = build_raw_tx([(inputs[0]["txid"], inputs[0]["vout"])], outputs)
    return Unsigned(raw=raw, inputs=inputs, outputs=outputs,
                    sighashes=[sighash(inputs, outputs, 0, p2pkh_script(buyer),
                                       sighash_type=ALL_ANYONECANPAY).hex()],
                    fee=value - int(price_sats), change=0, what=what)


def complete_bid(seller_coin: dict, seller: str, bid_inputs: list[dict],
                 outputs: list[tuple[int, bytes]]) -> Unsigned:
    """The finished swap: the seller's one coin in front of the buyer's signed
    inputs, and the seller's SIGHASH_ALL digest over the whole of it."""
    value = int(seller_coin.get("value", 0))
    if value != EXACT_SELLER_COIN:
        raise FundingError(
            f"the seller's side of a pre-signed offer is one coin of exactly "
            f"{EXACT_SELLER_COIN} sats, and this one holds {value}")
    first = {"txid": str(seller_coin["txid"]), "vout": int(seller_coin["vout"]),
             "value": value, "address": seller}
    inputs = [first] + [dict(c) for c in bid_inputs]
    raw = build_raw_tx([(c["txid"], c["vout"]) for c in inputs], outputs)
    return Unsigned(raw=raw, inputs=inputs, outputs=list(outputs),
                    sighashes=[sighash(inputs, outputs, 0, p2pkh_script(seller)).hex()],
                    fee=0, change=0, what="complete the trade")


def assemble_bid(unsigned: Unsigned, seller_sig: str, seller_pubkey: bytes,
                 buyer_sigs: list[str], buyer_pubkey: bytes) -> str:
    """Input 0 the seller's (SIGHASH_ALL), every other input the buyer's
    (ALL|ANYONECANPAY, signed when the offer was made)."""
    if len(buyer_sigs) != len(unsigned.inputs) - 1:
        raise FundingError(f"{len(unsigned.inputs) - 1} buyer signatures were "
                           f"needed and {len(buyer_sigs)} are kept")
    raw = (1).to_bytes(4, "little") + varint(len(unsigned.inputs))
    for n, coin in enumerate(unsigned.inputs):
        if n == 0:
            script_sig = push(bytes.fromhex(seller_sig)) + push(seller_pubkey)
        else:
            script_sig = push(bytes.fromhex(buyer_sigs[n - 1])) + push(buyer_pubkey)
        raw += bytes.fromhex(coin["txid"])[::-1]
        raw += int(coin["vout"]).to_bytes(4, "little")
        raw += varint(len(script_sig)) + script_sig
        raw += b"\xff\xff\xff\xff"
    raw += varint(len(unsigned.outputs))
    for value, script in unsigned.outputs:
        raw += value.to_bytes(8, "little") + varint(len(script)) + script
    raw += (0).to_bytes(4, "little")
    return raw.hex()


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
    untimed(params, "trade")
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
    if change >= max(dust, fees.DUST_LIMIT):
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


#: Most payload outputs one sweep takes back: ~150 bytes each keeps a sweep
#: well under the 100 kB a relayed transaction may be.
DUST_SWEEP_MAX = 400


def build_dust_sweep(address: str, coins: list[dict], rate: int, what: str = "") -> Unsigned:
    """Spend an account's own Class B payload outputs back to its address.

    2026-09-28 (a tester: 54.50 coins in 5,450 of them, invisible):
    every Class B output an account writes is a bare 1-of-n multisig with the
    account's own key in it (`encoding.encode_class_b`), so it is still the
    account's money. Each input is signed over its own multisig script and
    unlocked with OP_0 <sig> (`assemble`), in the account's own browser.
    """
    given = [{"txid": str(c["txid"]), "vout": int(c["vout"]), "value": int(c["value"]),
              "address": address, "script": str(c["script"]), "multisig": True}
             for c in coins[:DUST_SWEEP_MAX]]
    if not given:
        raise FundingError("there is nothing to sweep back")
    total = sum(c["value"] for c in given)
    # Priced on the real size: an OP_0 <sig> input is ~115 bytes, not the 148 a
    # key-and-signature input takes (a tester, 2026-09-28: 17% of the sweep).
    size = 10 + len(given) * 115 + 34
    fee = max(fees.fee_for(size, 1, rate), fees.MIN_FEE_PER_KB // 10)
    change = total - fee
    if change < fees.DUST_LIMIT:
        raise FundingError("these outputs are worth less than the fee to sweep them")
    outputs = [(change, p2pkh_script(address))]
    raw = build_raw_tx([(c["txid"], c["vout"]) for c in given], outputs)
    return Unsigned(raw=raw, inputs=given, outputs=outputs,
                    sighashes=[sighash(given, outputs, n, bytes.fromhex(c["script"])).hex()
                               for n, c in enumerate(given)],
                    fee=fee, change=change, what=what)


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
    if any(coin.get("segwit") for coin in unsigned.inputs):
        return _assemble_witness(unsigned, signatures, pubkey)
    raw = (1).to_bytes(4, "little") + time_field(unsigned.ntime) + varint(len(unsigned.inputs))
    for n, coin in enumerate(unsigned.inputs):
        script_sig = b""
        if n >= first and coin.get("multisig"):
            # 1-of-n CHECKMULTISIG pops one item too many: the OP_0 is that item.
            script_sig = b"\x00" + push(bytes.fromhex(signatures[n - first]))
        elif n >= first and coin.get("envelope") and coin.get("redeem"):
            # A note's reveal (token20_send): the envelope, the signature, the redeem script it satisfies.
            script_sig = (bytes.fromhex(coin["envelope"]) + push(bytes.fromhex(signatures[n - first]))
                          + push(bytes.fromhex(coin["redeem"])))
        elif n >= first and coin.get("redeem"):
            # A refereed pool's coin, closed by its creator: the ELSE branch.
            script_sig = (push(bytes.fromhex(signatures[n - first])) + b"\x00"
                          + push(bytes.fromhex(coin["redeem"])))
        elif n >= first:
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
    """What a seller signs before it knows its buyer: its own coins in, and
    the one payment it will accept out.

    Not an `Unsigned`. An `Unsigned` is a transaction waiting for signatures
    and there is exactly one of those in flight; a `Leg` is half of one that
    does not exist yet, and the difference is the whole point -- the node is
    going to receive a signature over this and then build the rest of the
    transaction around it, so the type that says "this was signed first, on
    its own" has to be a different type.

    It is one input and one output when it commits to nothing but a payment,
    which is honest for a coin -- a coin has nothing else to promise. It is two
    of each the moment it names the thing being sold, because a `SINGLE`
    signature reaches the output standing at its own input's index and no
    other: an output that has to be committed to needs an input standing under
    it. So the piece is input 0 and signs the payload, and a coin of the
    seller's own is input 1 and signs the payment. `sighashes` is one digest
    per input, index for index with `inputs`, and a leg whose second signature
    is missing commits to a payment and calls it a sale.
    """

    raw: str
    #: The seller's own inputs, signing order first: the piece, then the coin
    #: behind the second signature when there is one.
    inputs: list[dict[str, Any]]
    #: Output 0 is the payload when the leg names a thing, and the payment when
    #: it does not. The payment is always the last of them.
    outputs: list[tuple[int, bytes]]
    #: One digest per input, in signing order.
    sighashes: list[str]
    fee: int
    #: What the seller's output pays, and to whom: its own coins back plus the
    #: price, less the fee it reserved. The buyer sees this number, and the
    #: paste checks the finished transaction still pays it.
    pays: str
    paid: int
    #: The bytes output 0 carries, or nothing. This is the reason for the
    #: two-input shape, so it is carried here and stored by `listings` rather
    #: than derived: what a buyer is shown about a listing is read out of these
    #: bytes, and a completion that changes them is refused.
    payload: bytes = b""
    #: The type the browser must sign with. Not a knob to turn: it is
    #: SINGLE|ANYONECANPAY or the leg would commit to coins the seller has
    #: never seen.
    sighash_type: int = SINGLE_ANYONECANPAY
    what: str = ""
    #: The redeem script, for a leg at a refereed pool's two-key address: what
    #: the browser checks its key is in, and signs over.
    redeem: bytes = b""

    def as_json(self) -> dict:
        return {
            "raw": self.raw,
            "inputs": [{"txid": i["txid"], "vout": i["vout"],
                        "value": i["value"], "address": i.get("address", "")}
                       for i in self.inputs],
            "outputs": [{"value": value, "script": script.hex()}
                        for value, script in self.outputs],
            "sighashes": self.sighashes,
            "sighash_type": self.sighash_type,
            # The same disclosure an offer carries. A leg's signatures are made
            # by whoever made the leg, in their own tab -- this node completes
            # legs and never signs one -- so from here they are always the
            # account's, and a reader should not have to infer it from there
            # being a leg at all.
            "signed_by": "account",
            "fee": self.fee,
            "pays": self.pays,
            "paid": self.paid,
            "payload": self.payload.hex(),
            "what": self.what,
            **({"redeem": self.redeem.hex()} if self.redeem else {}),
        }


#: What the finished swap costs the seller to reserve against: the leg's own
#: input or two, the buyer's input, the payment to the seller, the buyer's
#: change, and the OP_RETURN naming what changed hands. The buyer's script is
#: not known when a leg is signed, so both of its outputs are priced as a
#: P2PKH by shape.
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


def swap_fee(rate: int, payload_script: bytes = b"") -> int:
    """What the finished swap will cost, as closely as a listing can know it.

    This is the seller's CONTRIBUTION to the fee, not the fee. The seller's
    output is fixed the moment it signs, so whatever it reserves comes out of
    that output and cannot be adjusted afterwards; what the finished transaction
    actually pays is settled by whoever completes the listing, since that is the
    side that knows the real size. `listings.paste_leg` is what keeps the two
    honest with each other -- it refuses a finished swap whose total falls under
    what a block asks, which is the only way a quiet-market listing fails loudly
    instead of relaying forever. What the completion does with the reservation
    is hand it back: a swap's coin leg is checked against what the receiving
    side NETS (`tx.paid_to` is outputs minus inputs at that address), so keeping
    it would leave the seller a little under its own price and the index would
    call the trade invalid. Reserving more than the swap costs therefore buys a
    listing nothing -- it is money held back for a block that never arrives, and
    returned the moment one does.

    `payload_script` is the OP_RETURN output as it will really be written, or
    nothing for a leg that promises only a payment. It decides the count of
    inputs as well as their size, because the two shapes of leg are different
    transactions: naming a thing costs the seller one extra input -- the coin
    behind the signature that output 0 needs -- on top of the buyer's.
    """
    outputs = [(0, P2PKH_SHAPE), (0, P2PKH_SHAPE)]
    if payload_script:
        outputs.append((0, payload_script))
        return price(SWAP_INPUTS + 1, outputs, rate)
    return price(SWAP_INPUTS, outputs, rate)


def build_leg(params: Params, address: str, piece: dict, coins: int,
              rate: int, what: str = "", payload: bytes | None = None,
              coin: dict | None = None, redeem: bytes = b"",
              pool_pays: int = 0) -> Leg:
    """The transaction a seller signs to LIST: its own coins in, one payment
    out, and -- when it names the thing it is selling -- the bytes that say so.

    The other half of `build_partial`, and its mirror in one respect that
    matters: `build_partial` is what a BUYER signs when it already knows what
    it is buying, and this is what a SELLER signs when it cannot yet know who
    is buying. A listing has to promise the piece before a buyer exists, so
    the signature has to be over as little of a transaction as can possibly be
    honest -- the seller's own inputs, and the outputs it is willing to accept.
    That is what `SINGLE|ANYONECANPAY` buys, and the shape below is that rule
    followed out rather than a layout anyone picked: `SINGLE` commits to the
    output standing at the signed input's index, so every output worth
    committing to needs an input standing under it. The piece is input 0 and
    signs the payload; `coin` is input 1 and signs the payment.

    `payload` is what output 0 will carry, the OP_RETURN data, and it is the
    reason a leg that sells a thing looks different from a leg that sells a
    coin: without it there is one input and one output, and the payment is
    every atom of what the signature reaches. That is honest for a coin, which
    has nothing else to promise, and it is the whole reason
    `test_a_leg_that_signs_its_payment_only_cannot_name_the_piece` ends the way
    it does. An asset surface that built one would be advertising a piece its
    seller's signature says nothing about.

    `coin` is the seller's own -- a second outpoint at `address`, named by the
    caller the same way `piece` is, because this function looks nothing up.
    Its value is not needed for anything except the arithmetic: it is there to
    be the second input, and it comes back to the seller inside `paid`.

    The fee is reserved here, at listing time, because the seller's output is
    fixed the moment it signs and cannot be adjusted later. It is a reservation
    and not a payment: the buyer pays what the finished transaction really costs
    out of its own change and hands this back to the seller, because the engine
    reads a coin leg as what the seller NETS -- and `paste_leg` still refuses the
    transaction if the whole of it does not add up to what a block asks for.
    """
    untimed(params, "listing")
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

    payload = bytes(payload or b"")
    if payload and coin is None:
        raise FundingError(
            "a leg that names what it sells needs a second coin of the "
            "seller's to stand under its payment. One input signs one output, "
            "and that output has to be the payment, so the bytes naming the "
            "piece would be sitting in the transaction signed by nobody")
    if coin is not None and not payload:
        raise FundingError(
            f"{str(coin.get('txid') or '')[:16]}… was given as a second input "
            f"to a leg that names nothing, and a second input exists only to "
            f"carry the second signature a payload needs")

    inputs = [{"txid": txid, "vout": vout, "value": value, "address": address}]
    if coin is not None:
        ctxid = str(coin.get("txid") or "")
        cvout, cvalue = int(coin.get("vout", -1)), int(coin.get("value", 0))
        if len(ctxid) != 64 or cvout < 0 or cvalue <= 0:
            raise FundingError("that is not a coin to sign a second output "
                               "with: it needs a 32-byte txid, an index, and an "
                               "amount above nothing")
        if (ctxid, cvout) == (txid, vout):
            raise FundingError(
                f"{ctxid[:16]}…:{cvout} was handed as both the piece and the "
                f"coin behind the second signature. A transaction that spends "
                f"one outpoint twice is invalid, and this one would also be "
                f"signing the same coin over two different outputs")
        owner = str(coin.get("address") or "")
        if owner and owner != address:
            raise FundingError(f"{ctxid[:16]}…:{cvout} belongs to {owner}, not "
                               f"to {address}, so this wallet cannot sign a "
                               f"second input out of it")
        inputs.append({"txid": ctxid, "vout": cvout, "value": cvalue,
                       "address": address})

    payload_script = op_return_script(payload) if payload else b""
    fee = swap_fee(rate, payload_script)
    held = sum(spent["value"] for spent in inputs)
    paid = held + coins - fee
    if pool_pays:
        # A lot that pays its own claim (a prize pool with "fee": "pool",
        # 2026-10-04): it keeps back `pool_pays` for the block and for the
        # claimer's receiving output, which the referee's ALL signature fixes,
        # so the claimer adds no coin of its own and reserves nothing to repay.
        fee, paid = 0, held + coins - int(pool_pays)
    if paid <= 0:
        raise FundingError(
            f"the price ({coins} sats) does not cover what a block costs "
            f"({fee} sats) plus keeping what is being sold ({held} sats), so "
            f"there is no output this seller could sign")

    script = p2pkh_script(address)
    outputs = ([(0, payload_script)] if payload else []) + [(paid, script)]
    raw = build_raw_tx([(c["txid"], c["vout"]) for c in inputs], outputs)
    # A leg at a script-hash address (a refereed prize pool, 2026-09-29) signs
    # over the script its coins are locked by: the redeem script, not the
    # address's own 23 bytes.
    code = bytes(redeem) or script
    digests = [sighash(inputs, outputs, n, code,
                       sighash_type=SINGLE_ANYONECANPAY).hex()
               for n in range(len(inputs))]
    return Leg(raw=raw, inputs=inputs, outputs=outputs, sighashes=digests,
               fee=fee, pays=address, paid=paid, payload=payload, what=what,
               redeem=bytes(redeem))


def build_doginal_send(db, params: Params, address: str, coin: dict, inscription: str, to: str,
                       rate: int, exclude=frozenset(), extra: list | None = None) -> Unsigned:
    """Send one Doginal on purpose (2026-10-14), the way ord-dogecoin's wallet does it
    (src/subcommand/wallet/transaction_builder.rs): the inscribed coin is input 0 and its WHOLE value goes to the recipient
    as output 0, so the inscription's first koinu is the recipient output's first koinu; the fee comes from other, clean
    coins (GUARD.spendable) added after it; change last, back to the sender. Refused unless the chain says this coin holds
    exactly this inscription at offset 0 -- a coin with two inscriptions, or one sitting further in, would need an
    alignment output first, and sending it plainly could hand the wrong piece away or burn it to fees."""
    untimed(params, "doginal send")
    return _doginal_first(db, address, coin, inscription, p2pkh_script(to), [], rate, exclude, extra,
                          what=f"send Doginal {inscription} to {to}", params=params)


def build_doginal_lock(db, params: Params, address: str, coin: dict, inscription: str, lock_script: bytes,
                       reserve: int, rate: int, exclude=frozenset(), extra: list | None = None) -> Unsigned:
    """A Doginal into a swap's lock ([Claude 2026-10-15]; the operator: "use the atomic swap feature we have to trade doginals").
    The send above with the lock as recipient: the inscribed coin input 0 and whole to the lock as output 0 (the inscription
    at offset 0 of the lock), then `reserve` clean coin to the SAME lock as output 1 -- what pays the claim or the refund
    later without touching the Doginal's own koinu (htlc.doginal_out) -- then change. Every coin after input 0 is clean."""
    untimed(params, "doginal lock")
    return _doginal_first(db, address, coin, inscription, bytes(lock_script), [(int(reserve), bytes(lock_script))],
                          rate, exclude, extra, what=f"lock Doginal {inscription} for a swap", params=params)


def build_note_send(db, params: Params, address: str, note_coin: dict, to: str, rate: int,
                    exclude=frozenset(), extra: list | None = None, what: str = "") -> Unsigned:
    """A DRC-20 / PRC-20 transfer note's send (token20_send, step 3): the note's coin -- the reveal this same send
    built a moment ago, not yet in any block, so no index has read it -- as input 0, whole to `to` as output 0, as a
    Doginal is sent; clean coins pay the fee; change last."""
    untimed(params, "note send")
    return _doginal_first(db, address, note_coin, "", p2pkh_script(to), [], rate, exclude, extra, what=what,
                          vouched=True, params=params)


def _doginal_first(db, address: str, coin: dict, inscription: str, first: bytes, more: list, rate: int,
                   exclude, extra, what: str, vouched: bool = False, params: Params | None = None) -> Unsigned:
    """The inscribed coin as input 0, whole to `first` as output 0; `more` outputs after it; clean coins pay for them and
    the fee; change last. `vouched`: the coin is one this node just built (a note's reveal) and is taken as it is.
    Priced on the chain `params` describes, as `build` is (2026-10-25: Bellinals; at Dogecoin's 0.01 a kB a Bells send
    overpaid a hundred times): its own minimum rate, its own soft-dust rule, its own least change."""
    guard = guard_for(address) or GUARD          # Doginals on Dogecoin, Pepinals on Pepecoin mainnet
    if guard is None:
        raise FundingError("inscriptions are not checked on this chain here: nothing that carries one is sent")
    if not vouched:
        from .doginals import exactly
        bad = exactly(guard.held(coin["txid"]).get(int(coin["vout"])), inscription)
        if bad:
            raise FundingError(bad)
    if not vouched and not any((u["txid"], u["vout"]) == (coin["txid"], int(coin["vout"])) for u in utxos.unspent(db, address)) \
            and not any((c["txid"], c["vout"]) == (coin["txid"], int(coin["vout"])) for c in (extra or [])):
        raise FundingError("that coin is not this address's to spend")
    piece = (int(coin["value"]), first)
    paid = [piece] + list(more)
    owed = sum(v for v, _ in more)
    keep_out = set(exclude) | {(coin["txid"], int(coin["vout"]))}
    rate = fees.rate_for(params, rate)              # this chain's own minimum
    fee = price(2, paid, rate, change=True, params=params)
    fees_in = choose(db, address, owed + fee, exclude=keep_out, extra=extra)
    fee = price(1 + len(fees_in), paid, rate, change=True, params=params)
    if sum(c["value"] for c in fees_in) < owed + fee:
        fees_in = choose(db, address, owed + fee, exclude=keep_out, extra=extra)
        fee = price(1 + len(fees_in), paid, rate, change=True, params=params)
    chosen = [dict(coin, vout=int(coin["vout"]))] + fees_in
    change = sum(c["value"] for c in fees_in) - owed - fee
    outputs = list(paid)
    if change >= fees.dust_limit(params):
        outputs.append((change, p2pkh_script(address)))
    else:
        fee += max(0, change)
        change = 0
    raw = build_raw_tx([(c["txid"], c["vout"]) for c in chosen], outputs)
    script = p2pkh_script(address)
    hashes = [sighash(chosen, outputs, n, script).hex() for n in range(len(chosen))]
    return Unsigned(raw=raw, inputs=chosen, outputs=outputs, sighashes=hashes, fee=fee, change=change, what=what)


# --- segwit v0 spending: BIP143 sighash and witness serialisation ------------
#
# 2026-10-19 (tbuuol's side), layer 3a. The legacy `sighash` above does not
# commit to the amount being spent; BIP143 does, and a segwit input can be
# signed no other way. This is the node's half for a segwit input: the digest
# the browser signs, and the serialisation that carries the signature in the
# witness instead of the scriptSig.
#
# Held to BIP143's own worked example (the native-P2WPKH vector) in
# tests/test_bip143.py, byte for byte -- the same way the legacy sighash is
# settled against pepecoind. The browser mirror is coins.js `sighashWitness`.
# Only SIGHASH_ALL and ALL|ANYONECANPAY are built, as above; a type this does
# not mean to implement is refused rather than computed.


def _dsha256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def p2sh_script(scripthash: bytes) -> bytes:
    """A P2SH output: OP_HASH160 <20-byte script hash> OP_EQUAL."""
    return bytes([0xA9, 0x14]) + scripthash + bytes([0x87])


def witness_scriptcode(keyhash: bytes) -> bytes:
    """The BIP143 scriptCode of a P2WPKH input (and of a P2SH-nested one): the
    ordinary P2PKH script of the 20-byte key hash. Not length-prefixed; the
    preimage adds the length."""
    if len(keyhash) != 20:
        raise FundingError("a P2WPKH scriptCode is built from a 20-byte hash")
    return bytes([0x76, 0xA9, 0x14]) + keyhash + bytes([0x88, 0xAC])


def sighash_witness(raw_inputs: list[dict], outputs: list[tuple[int, bytes]],
                    index: int, script_code: bytes, amount: int,
                    version: int = 1, locktime: int = 0,
                    sighash_type: int = SIGHASH_ALL) -> bytes:
    """The 32 bytes a segwit v0 input must sign (BIP143).

    Unlike the legacy preimage this commits to `amount` (the value of the coin
    being spent), which is why every segwit input needs its value known and
    why a signature cannot be lifted onto a coin of a different size.
    """
    if sighash_type not in (SIGHASH_ALL, ALL_ANYONECANPAY):
        raise FundingError(
            f"this builds a segwit sighash for SIGHASH_ALL and "
            f"ALL|ANYONECANPAY, not for {sighash_type:#04x}")
    anyone = bool(sighash_type & SIGHASH_ANYONECANPAY)

    def outpoint(coin: dict) -> bytes:
        return bytes.fromhex(coin["txid"])[::-1] + int(coin["vout"]).to_bytes(4, "little")

    def sequence(coin: dict) -> bytes:
        return int(coin.get("sequence", 0xFFFFFFFF)).to_bytes(4, "little")

    zero = b"\x00" * 32
    hash_prevouts = zero if anyone else _dsha256(b"".join(outpoint(c) for c in raw_inputs))
    # SIGHASH_ALL (and its ANYONECANPAY form only when not ANYONECANPAY) commits
    # to every sequence; ANYONECANPAY commits to none.
    hash_sequence = zero if anyone else _dsha256(b"".join(sequence(c) for c in raw_inputs))
    serialised_outputs = b"".join(
        (value & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little") + varint(len(spk)) + spk
        for value, spk in outputs)
    hash_outputs = _dsha256(serialised_outputs)

    this = raw_inputs[index]
    pre = version.to_bytes(4, "little")
    pre += hash_prevouts + hash_sequence
    pre += outpoint(this)
    pre += varint(len(script_code)) + script_code
    pre += int(amount).to_bytes(8, "little")
    pre += sequence(this)
    pre += hash_outputs
    pre += locktime.to_bytes(4, "little")
    pre += sighash_type.to_bytes(4, "little")
    return _dsha256(pre)


def _witness_stack(items: list[bytes]) -> bytes:
    out = varint(len(items))
    for item in items:
        out += varint(len(item)) + item
    return out


def serialize_segwit(inputs: list[dict], outputs: list[tuple[int, bytes]],
                     locktime: int = 0, version: int = 1) -> str:
    """A finished transaction, with a witness section when any input carries
    one (BIP144 marker 0x00 and flag 0x01). Each input: `txid`, `vout`,
    optional `script_sig` (bytes; empty for native segwit, the pushed
    redeemScript for a P2SH-nested one), optional `sequence`, and optional
    `witness` (a list of byte items). With no witness anywhere this is the same
    bytes as the legacy serialisation."""
    has_witness = any(coin.get("witness") for coin in inputs)
    raw = version.to_bytes(4, "little")
    if has_witness:
        raw += b"\x00\x01"                       # marker, flag
    raw += varint(len(inputs))
    for coin in inputs:
        script_sig = coin.get("script_sig") or b""
        if isinstance(script_sig, str):
            script_sig = bytes.fromhex(script_sig)
        raw += bytes.fromhex(coin["txid"])[::-1]
        raw += int(coin["vout"]).to_bytes(4, "little")
        raw += varint(len(script_sig)) + script_sig
        raw += int(coin.get("sequence", 0xFFFFFFFF)).to_bytes(4, "little")
    raw += varint(len(outputs))
    for value, spk in outputs:
        raw += int(value).to_bytes(8, "little") + varint(len(spk)) + spk
    if has_witness:
        for coin in inputs:
            raw += _witness_stack(coin.get("witness") or [])
    raw += locktime.to_bytes(4, "little")
    return raw.hex()


def segwit_txid(inputs: list[dict], outputs: list[tuple[int, bytes]],
                locktime: int = 0, version: int = 1) -> str:
    """The txid (not the wtxid): double-SHA of the serialisation with the
    witness and the marker/flag stripped, in display (reversed) order."""
    stripped = [{"txid": c["txid"], "vout": c["vout"],
                 "script_sig": c.get("script_sig") or b"",
                 "sequence": c.get("sequence", 0xFFFFFFFF)} for c in inputs]
    raw = bytes.fromhex(serialize_segwit(stripped, outputs, locktime, version))
    return _dsha256(raw)[::-1].hex()


def spend_for(address: str, params: Params, keyhash: bytes | None = None) -> dict:
    """How to spend a coin at `address`: the scriptCode its sighash is taken
    over, the script its change comes back on, and whether it is a native
    segwit (P2WPKH) input or a legacy P2PKH one.

    Native segwit is read straight from the bech32 address -- the witness
    program IS the key hash -- so the node builds it without a pubkey, the same
    way it builds legacy from a base58 address. P2SH-nested segwit (BIP49)
    hides the key hash behind a script hash, so the caller names it as
    `keyhash` (2026-10-20: the account registered it with its pubkey); it is
    used only when the address really is that key's P2SH-P2WPKH. The input
    signs the same BIP143 digest as native segwit, and its change goes back
    to the same P2SH address.
    """
    if keyhash is not None:
        if len(keyhash) != 20 or p2sh_p2wpkh_address(params, keyhash) != address:
            raise FundingError("that is not the P2SH-segwit address of this key")
        return {"kind": "p2sh-p2wpkh",
                "script_code": witness_scriptcode(keyhash),
                "change_script": p2sh_script(hash160(p2sh_p2wpkh_redeem(keyhash)))}
    if params.bech32_hrp:
        version, program = segwit_decode(params.bech32_hrp, address)
        if version == 0 and program is not None and len(program) == 20:
            return {"kind": "p2wpkh",
                    "script_code": witness_scriptcode(program),
                    "change_script": _p2wpkh_script(program)}
    return {"kind": "p2pkh", "script_code": p2pkh_script(address),
            "change_script": p2pkh_script(address)}


def _assemble_witness(unsigned: "Unsigned", signatures: list[str], pubkey: bytes) -> str:
    """`assemble` for a transaction with native-segwit inputs: the signature
    rides in the witness, not the scriptSig. Inputs before `signed_from`, and
    any legacy/multisig/pool input mixed in, keep the scriptSig shapes `assemble`
    builds; a P2WPKH input gets an empty scriptSig and a [sig, pubkey] witness."""
    first = unsigned.signed_from
    ins = []
    for n, coin in enumerate(unsigned.inputs):
        item = {"txid": coin["txid"], "vout": coin["vout"],
                "sequence": 0xFFFFFFFF, "script_sig": b"", "witness": []}
        if n >= first:
            sig = bytes.fromhex(signatures[n - first])
            if coin.get("segwit") == "p2wpkh":
                item["witness"] = [sig, pubkey]
            elif coin.get("segwit") == "p2sh-p2wpkh":
                # BIP49: the scriptSig only pushes the redeemScript (the P2WPKH
                # program of this pubkey); the signature rides in the witness.
                item["script_sig"] = push(p2sh_p2wpkh_redeem(hash160(pubkey)))
                item["witness"] = [sig, pubkey]
            elif coin.get("multisig"):
                item["script_sig"] = b"\x00" + push(sig)
            elif coin.get("redeem"):
                item["script_sig"] = (push(sig) + b"\x00"
                                      + push(bytes.fromhex(coin["redeem"])))
            else:
                item["script_sig"] = push(sig) + push(pubkey)
        ins.append(item)
    return serialize_segwit(ins, unsigned.outputs, locktime=0, version=1)
