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

from . import utxos
from .config import Params
from .script import b58check_decode
from .txbuild import build_raw_tx, p2pkh_script, push, varint

#: What a transaction of this shape costs, at the node's own relay fee. Not
#: a guess dressed as a fact: it is recomputed from the real size once the
#: inputs are chosen, and the caller is told both.
BYTES_PER_INPUT = 148          # outpoint + a P2PKH scriptSig + sequence
BYTES_PER_OUTPUT = 34
BYTES_OVERHEAD = 10

SIGHASH_ALL = 1


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

    def as_json(self) -> dict:
        return {
            "raw": self.raw,
            "inputs": [{"txid": i["txid"], "vout": i["vout"],
                        "value": i["value"], "address": i["address"]}
                       for i in self.inputs],
            "sighashes": self.sighashes,
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


def estimate(inputs: int, outputs: int, rate: int) -> int:
    """The fee for a transaction of this shape, in satoshis."""
    size = BYTES_OVERHEAD + inputs * BYTES_PER_INPUT + outputs * BYTES_PER_OUTPUT
    return max(1, (size * rate) // 1000)


def sighash(raw_inputs: list[dict], outputs: list[tuple[int, bytes]],
            index: int, script: bytes, version: int = 1,
            locktime: int = 0) -> bytes:
    """The 32 bytes input `index` must sign, for SIGHASH_ALL.

    The legacy algorithm: serialise the transaction with every scriptSig
    empty except this one, which carries the script being spent, append the
    sighash type as four little-endian bytes, and hash it twice.

    Written here rather than asked of the node because the node cannot be
    asked -- `signrawtransaction` needs the key. It is a serialisation with
    published test vectors and no secret in it, and a mistake in it makes a
    transaction the network refuses rather than a key that leaks.
    """
    raw = version.to_bytes(4, "little") + varint(len(raw_inputs))
    for n, coin in enumerate(raw_inputs):
        raw += bytes.fromhex(coin["txid"])[::-1]
        raw += int(coin["vout"]).to_bytes(4, "little")
        if n == index:
            raw += varint(len(script)) + script
        else:
            raw += varint(0)
        raw += b"\xff\xff\xff\xff"
    raw += varint(len(outputs))
    for value, script_out in outputs:
        raw += value.to_bytes(8, "little") + varint(len(script_out)) + script_out
    raw += locktime.to_bytes(4, "little")
    raw += SIGHASH_ALL.to_bytes(4, "little")
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
    # One guess to pick coins, then the real number once their count is
    # known. Told to the caller as the number it actually is.
    guess = estimate(1, len(payload_outputs) + 1, rate)
    chosen = choose(db, address, spend + guess, exclude=exclude, extra=extra)
    fee = estimate(len(chosen), len(payload_outputs) + 1, rate)
    if sum(c["value"] for c in chosen) < spend + fee:
        chosen = choose(db, address, spend + fee, exclude=exclude, extra=extra)
        fee = estimate(len(chosen), len(payload_outputs) + 1, rate)

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


def assemble(unsigned: Unsigned, signatures: list[str], pubkey: bytes) -> str:
    """Put the signatures in and hand back a broadcastable transaction.

    The node does this rather than the browser so that what goes out is
    built from what the node offered: the browser returns signatures over
    the bytes it was given, and nothing else it says is used.
    """
    if len(signatures) != len(unsigned.inputs):
        raise FundingError(
            f"{len(unsigned.inputs)} signatures were needed and "
            f"{len(signatures)} came back")
    raw = (1).to_bytes(4, "little") + varint(len(unsigned.inputs))
    for n, coin in enumerate(unsigned.inputs):
        script_sig = push(bytes.fromhex(signatures[n])) + push(pubkey)
        raw += bytes.fromhex(coin["txid"])[::-1]
        raw += int(coin["vout"]).to_bytes(4, "little")
        raw += varint(len(script_sig)) + script_sig
        raw += b"\xff\xff\xff\xff"
    raw += varint(len(unsigned.outputs))
    for value, script in unsigned.outputs:
        raw += value.to_bytes(8, "little") + varint(len(script)) + script
    raw += (0).to_bytes(4, "little")
    return raw.hex()
