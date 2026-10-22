"""Sending a DRC-20 (Dogecoin) or PRC-20 (Pepecoin) token, the way the chain's indexers count it (2026-10-22:
"I want the ability to send a PRC 20 token in the same way I can send any other token" ... "Our transfer and send
protocol needs to match that of Pepe block and Nintondo").

There is no one-step send in these standards: our index (arcade/doginal_index.py, the rules wonky-ord keeps), pepeblocks
and nintondo all count the transfer the BRC-20 way -- the frgy that reached @apple on 2026-10-22 is the example
(67f2cc49...: five notes of 10,000 inscribed on the deployer's own address, then sent in one transaction):

1. the commit -- the sender's coins pay a P2SH coin whose redeem script lets the envelope ride in its spender;
2. the reveal -- spends it with the note in its scriptSig,
   `{"p":"prc-20","op":"transfer","tick":"frgy","amt":"10000"}`, landing it on the SENDER'S own address as a
   POSTAGE coin (its first koinu): that amount is set aside from the sender's balance ("transferable");
3. the send -- an ordinary transaction moves that coin, whole, to the recipient as output 0: the note's first move
   takes the amount from the sender to the recipient. (Into the fee, it goes back to the sender.)

The envelope is byte for byte what the explorers read (arcade/ltc_ord.py writes the Doginals format: "ord", the part
count, the type, then the parts; the frgy note is the same bytes). The note's text is not covered by any signature --
that is the Doginals format, for every wallet -- so the page shows the exact note before anything is signed, and the
amount is checked against a complete copy of the index first.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from . import fees, ltc_ord
from .funding import FundingError, Unsigned, sighash
from .script import b58check_encode, hash160
from .txbuild import build_raw_tx, p2pkh_script, push

#: The note's content type, as the frgy notes carry it.
NOTE_TYPE = "text/plain;charset=utf-8"
#: What the note's coin holds: 0.001 of the coin, as every note to @apple did.
POSTAGE = 100_000
#: The protocol word in a note, by chain.
PROTOCOL = {"doge": "drc-20", "main": "prc-20"}


def note(protocol: str, tick: str, amount: str) -> bytes:
    """The transfer note, compact JSON in the key order the explorers' own notes use."""
    return json.dumps({"p": protocol, "op": "transfer", "tick": tick, "amt": amount},
                      separators=(",", ":")).encode()


def amount_text(amount: str, decimals: int) -> tuple[str, int]:
    """(the amount as the note says it, in units) -- refused when it is not a plain positive number with at most
    `decimals` places. The text is normalised (no exponent, no trailing zeros) so the note reads as typed."""
    try:
        d = Decimal(str(amount).strip())
    except InvalidOperation:
        raise FundingError("that is not an amount") from None
    if not d.is_finite() or d <= 0:
        raise FundingError("the amount must be above zero")
    if -d.as_tuple().exponent > decimals:
        raise FundingError(f"this token has {decimals} decimal places at most")
    text = format(d.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text, int(d.scaleb(decimals))


def envelope(content: bytes, pubkey: bytes) -> tuple[list[bytes], bytes]:
    """(the envelope's pushes, the redeem script) for one note: ltc_ord's Doginals-format writer, which every chain
    of this family reads the same way."""
    try:
        return ltc_ord.plan(NOTE_TYPE, content, pubkey)
    except ltc_ord.InscribeError as exc:
        raise FundingError(str(exc)) from None


def commit_script(redeem: bytes) -> bytes:
    return b"\xa9\x14" + hash160(redeem) + b"\x87"


def reveal_fee(params, pushes: list[bytes], redeem: bytes, rate: int, outputs: list | None = None) -> int:
    """The reveal's fee: its size known exactly but for the signature (73 at most), one sigop, and the soft-dust fee
    of its outputs where the chain has one (the POSTAGE output, or `outputs`)."""
    script_sig = sum(len(p) for p in pushes) + 74 + len(push(redeem))
    outputs = list(outputs) if outputs else [(POSTAGE, b"\x76\xa9\x14" + b"\x00" * 20 + b"\x88\xac")]
    # version, input count, the outpoint, the scriptSig's length and itself, sequence, output count, the outputs
    # (value, length, script), locktime
    size = 4 + 1 + 36 + (1 if script_sig < 253 else 3) + script_sig + 4 + 1 + \
        sum(8 + 1 + len(script) for _v, script in outputs) + 4
    soft = fees.soft_dust_fee(outputs) if getattr(params, "soft_dust", True) else 0
    return fees.fee_for(size, 1, fees.rate_for(params, rate)) + soft


def build_reveal(params, commit: dict, pushes: list[bytes], redeem: bytes, address: str = "",
                 what: str = "", outputs: list | None = None) -> Unsigned:
    """The reveal, unsigned: the commit coin ({txid, vout, value}) in, the note's POSTAGE coin to `address` out (or
    `outputs`, the note on the first one's first koinu: a swap's lock, token20_swap), the rest the fee. The browser
    signs over `redeem` (coins.js `noteKey` checks it is its own key's); the node puts the envelope in front of the
    signature (funding.assemble, `envelope`)."""
    value = int(commit["value"])
    outputs = list(outputs) if outputs else [(POSTAGE, p2pkh_script(address))]
    if value <= sum(v for v, _ in outputs):
        raise FundingError("the commit coin does not cover the note's coin")
    given = [{"txid": str(commit["txid"]), "vout": int(commit["vout"]), "value": value,
              "address": b58check_encode(params.scripthash_version, hash160(redeem)),
              "redeem": redeem.hex(), "envelope": b"".join(pushes).hex()}]
    raw = build_raw_tx([(given[0]["txid"], given[0]["vout"])], outputs)
    return Unsigned(raw=raw, inputs=given, outputs=outputs, sighashes=[sighash(given, outputs, 0, redeem).hex()],
                    fee=value - sum(v for v, _ in outputs), change=0, what=what)
