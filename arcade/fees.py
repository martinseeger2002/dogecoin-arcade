"""Fees priced the way the node's miner prices them.

A node fills a block from its mempool by fee rate (miner.cpp:494) -- but the
size it divides by is not the transaction's bytes. It is the mempool's
*virtual* size, max(bytes, sigops x 20) (txmempool.cpp:71-73,
policy/policy.cpp:215-218; -bytespersigop, policy.h:58), and a bare
CHECKMULTISIG output counts twenty sigops whatever its real length
(script.cpp:156-173, MAX_PUBKEYS_PER_MULTISIG). Every Class B transaction
this project sends is mostly bare multisig outputs, so to the miner each
105-byte output is 400 bytes, and a 100-output piece is 39 KB, not 11.

Priced by its bytes at 0.01 PEP/kB, such a piece pays 0.11 PEP: under a third
of what -blockmintxfee (0.01 PEP/kB, policy.h:23,32) asks of 39 KB. Relay
takes it (-minrelaytxfee is a tenth of that, validation.h:59), so it sits in
every mempool; the fee part of block assembly takes none of it ("everything
else we might consider has a lower fee rate"); and the only door left is the
priority area (miner.cpp:559), which admits one transaction and stops
(miner.cpp:613, AllowFree) because our inputs are fresh coins. Seen on
testnet on 2026-09-14: 76 pieces waiting, one confirmed per block, and a
collection job that died of -26 too-long-mempool-chain (the ancestor limit
is 101 KB, validation.h:76 -- virtual again, so two pieces). Every default
node prices the same way: this is not one miner's policy, it is what a
Class B transaction costs.

`fund` is fundrawtransaction priced for the virtual size; `fee_for`
estimates ahead of time. Nothing here is pepecoin-specific: Dogecoin's node
counts the same way, with its own minimum.
"""

from __future__ import annotations

import math
from typing import Any

COIN = 100_000_000

#: -blockmintxfee, the least a miner's block assembler asks per virtual kB
#: (policy.h:23 RECOMMENDED_MIN_TX_FEE, policy.h:32). Also what the wallet
#: pays per raw kB when it prices a transaction itself (-fallbackfee).
MIN_FEE_PER_KB = COIN // 100

#: The soft dust limit (policy.h:70 DEFAULT_DUST_LIMIT = RECOMMENDED_MIN_TX_FEE).
#: Every output below it must pay this much again in fee (pepecoin-fees.cpp:80,
#: GetPepecoinDustFee) or peers will not relay the transaction. Our own node takes
#: it anyway -- sendrawtransaction skips that check -- so it sits in our mempool
#: "waiting for its block" and never reaches a miner. Seen live 2026-09-25: a 0.01
#: coin paying a 0.00235 fee left 0.00765 of change, and seven posts stuck.
DUST_LIMIT = COIN // 100

def soft_dust_fee(outputs) -> int:
    """What Pepecoin adds to the fee for outputs below the soft dust limit.

    GetPepecoinDustFee charges DUST_LIMIT again for every spendable output worth
    less than DUST_LIMIT. A zero-value OP_RETURN is unspendable and is not
    counted. Our own node accepts a transaction that skips this, so it is priced
    here or it waits for a block for ever: the answered-offer swap of
    2026-09-26 returned a 0.00589 fee reservation to its seller and stuck.
    """
    extra = 0
    for value, script in outputs:
        script = bytes(script)
        if script[:1] == b"\x6a":
            continue
        if 0 < int(value) < DUST_LIMIT:
            extra += DUST_LIMIT
    return extra


#: -bytespersigop (policy.h:58): what one sigop weighs against the bytes.
BYTES_PER_SIGOP = 20

#: What a bare CHECKMULTISIG counts, keys or no keys (script.cpp:173).
SIGOPS_PER_MULTISIG = 20

#: A P2PKH scriptSig once signed: push, 72-byte signature, push, 33-byte key
#: -- what the node itself allows for when it funds (sign.cpp:415).
SCRIPTSIG_BYTES = 107

_OP_PUSHDATA1, _OP_PUSHDATA2, _OP_PUSHDATA4 = 0x4C, 0x4D, 0x4E
_OP_CHECKSIG, _OP_CHECKSIGVERIFY = 0xAC, 0xAD
_OP_CHECKMULTISIG, _OP_CHECKMULTISIGVERIFY = 0xAE, 0xAF


def legacy_sigops(script: bytes) -> int:
    """Sigops as a block counts them (script.cpp GetSigOpCount, fAccurate false)."""
    n, i = 0, 0
    while i < len(script):
        op = script[i]
        i += 1
        if op <= 0x4B:
            i += op
        elif op == _OP_PUSHDATA1:
            i += 1 + (script[i] if i < len(script) else 0)
        elif op == _OP_PUSHDATA2:
            i += 2 + (int.from_bytes(script[i:i + 2], "little") if i + 1 < len(script) else 0)
        elif op == _OP_PUSHDATA4:
            i += 4 + (int.from_bytes(script[i:i + 4], "little") if i + 3 < len(script) else 0)
        elif op in (_OP_CHECKSIG, _OP_CHECKSIGVERIFY):
            n += 1
        elif op in (_OP_CHECKMULTISIG, _OP_CHECKMULTISIGVERIFY):
            n += SIGOPS_PER_MULTISIG
    return n


def sigops_of(decoded: dict) -> int:
    """The sigops of a decoded transaction: its outputs' scripts and its
    inputs' (a signed P2PKH input has none; pushes are not sigops)."""
    total = 0
    for out in decoded.get("vout", []):
        total += legacy_sigops(bytes.fromhex(out.get("scriptPubKey", {}).get("hex", "")))
    for inp in decoded.get("vin", []):
        total += legacy_sigops(bytes.fromhex(inp.get("scriptSig", {}).get("hex", "")))
    return total


def virtual_size(raw_bytes: int, sigops: int) -> int:
    """What the mempool and the miner take the transaction's size to be."""
    return max(int(raw_bytes), int(sigops) * BYTES_PER_SIGOP)


def fee_for(raw_bytes: int, sigops: int, per_kb: int = MIN_FEE_PER_KB) -> int:
    """Satoshis that put the transaction at `per_kb` on its virtual size."""
    return math.ceil(virtual_size(raw_bytes, sigops) * per_kb / 1000)


def fund(rpc: Any, raw_hex: str, options: dict | None = None,
         per_kb: int = MIN_FEE_PER_KB) -> dict:
    """fundrawtransaction, paying `per_kb` on the virtual size.

    The node prices what it funds by the bytes it expects once signed, and
    knows nothing of sigops until it builds a block. So: fund, count, and if
    the fee falls short of the virtual size, fund again at the rate per raw
    kB that lands on it. A transaction with no multisig outputs is priced
    exactly as before; one that is mostly multisig pays about 3.5x.

    Returns what fundrawtransaction returns (`hex`, `fee` in coins,
    `changepos`), plus `sigops` and `vsize` for the record.
    """
    opts = dict(options or {})
    funded = rpc.call("fundrawtransaction", raw_hex, opts) if opts \
        else rpc.call("fundrawtransaction", raw_hex)
    for _ in range(5):
        if not funded or "hex" not in funded:
            return funded
        decoded = rpc.call("decoderawtransaction", funded["hex"])
        sigops = sigops_of(decoded)
        raw = len(funded["hex"]) // 2 + SCRIPTSIG_BYTES * len(decoded.get("vin", []))
        needed = fee_for(raw, sigops, per_kb)
        paid = int(round(float(funded.get("fee", 0)) * COIN))
        if paid >= needed:
            return dict(funded, sigops=sigops, vsize=virtual_size(raw, sigops))
        # Per raw kB, so that the node's own sizing arrives at `needed`; a
        # hair over, since its estimate of the signed size may be under mine.
        rate_per_kb = math.ceil(needed * 1000 / raw * 1.01)
        if opts.get("feeRate") is not None and rate_per_kb <= opts["feeRate"] * COIN:
            rate_per_kb = math.ceil(opts["feeRate"] * COIN * 1.05)
        opts["feeRate"] = rate_per_kb / COIN
        funded = rpc.call("fundrawtransaction", raw_hex, opts)
    raise ValueError(f"could not fund the transaction at {per_kb / COIN:.8f}/kB "
                     f"of its virtual size ({virtual_size(raw, sigops):,} bytes)")
