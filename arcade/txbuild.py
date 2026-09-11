"""Raw transaction construction.

Only what is needed to place our own outputs into a transaction. The node's
wallet then adds inputs and change (`fundrawtransaction`) and signs
(`signrawtransaction`). **This module never sees a private key.**

Why build raw rather than use `createrawtransaction`: that RPC only expresses
outputs as {address: amount} or {"data": hex}, and Class B needs bare multisig
scriptPubKeys, which it cannot represent.
"""

from __future__ import annotations

from .script import OP_CHECKMULTISIG, OP_RETURN, b58check_decode


def varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xFFFFFFFF:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


def push(data: bytes) -> bytes:
    """Minimal push encoding. Non-minimal pushes are non-standard."""
    if len(data) < 0x4C:
        return bytes([len(data)]) + data
    if len(data) <= 0xFF:
        return b"\x4c" + bytes([len(data)]) + data
    if len(data) <= 0xFFFF:
        return b"\x4d" + len(data).to_bytes(2, "little") + data
    return b"\x4e" + len(data).to_bytes(4, "little") + data


def p2pkh_script(address: str) -> bytes:
    _, h160 = b58check_decode(address)
    if len(h160) != 20:
        raise ValueError(f"address {address!r} does not contain a 20-byte hash")
    return b"\x76\xa9" + push(h160) + b"\x88\xac"


def op_return_script(data: bytes) -> bytes:
    return bytes([OP_RETURN]) + push(data)


def multisig_script(pubkeys: list[bytes], required: int = 1) -> bytes:
    """OP_m <key>... OP_n OP_CHECKMULTISIG.

    Only x-of-3 is standard (`policy/policy.cpp:41-49`), so `pubkeys` must hold
    at most three keys.
    """
    if not 1 <= required <= len(pubkeys) <= 3:
        raise ValueError(f"non-standard multisig: {required}-of-{len(pubkeys)}")
    body = b"".join(push(k) for k in pubkeys)
    return bytes([0x50 + required]) + body + bytes([0x50 + len(pubkeys)]) + bytes([OP_CHECKMULTISIG])


def build_raw_tx(
    inputs: list[tuple[str, int]],
    outputs: list[tuple[int, bytes]],
    version: int = 1,
    locktime: int = 0,
) -> str:
    """Serialize an unsigned transaction. `outputs` is [(value_sats, script)]."""
    raw = version.to_bytes(4, "little") + varint(len(inputs))
    for txid, index in inputs:
        raw += bytes.fromhex(txid)[::-1]          # txids display big-endian
        raw += index.to_bytes(4, "little")
        raw += varint(0)                           # empty scriptSig; the wallet signs
        raw += b"\xff\xff\xff\xff"
    raw += varint(len(outputs))
    for value, script in outputs:
        raw += value.to_bytes(8, "little") + varint(len(script)) + script
    raw += locktime.to_bytes(4, "little")
    return raw.hex()
