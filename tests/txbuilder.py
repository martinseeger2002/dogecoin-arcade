"""A minimal raw-transaction builder, for tests only.

The real builder lands in M7, where it must also handle fee estimation, change
and PSBT. This is the smallest thing that can put a genuine Arcade transaction on
a regtest chain so the indexer can be tested against real blocks rather than
hand-written JSON.
"""

from __future__ import annotations

from arcade.script import OP_CHECKMULTISIG, OP_RETURN, b58check_decode


def varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xFFFFFFFF:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


def push(data: bytes) -> bytes:
    if len(data) < 0x4C:
        return bytes([len(data)]) + data
    if len(data) <= 0xFF:
        return b"\x4c" + bytes([len(data)]) + data
    return b"\x4d" + len(data).to_bytes(2, "little") + data


def p2pkh_script(address: str) -> bytes:
    _, hash160 = b58check_decode(address)
    return b"\x76\xa9" + push(hash160) + b"\x88\xac"


def op_return_script(data: bytes) -> bytes:
    return bytes([OP_RETURN]) + push(data)


def multisig_script(pubkeys: list[bytes], required: int = 1) -> bytes:
    """OP_m <key>... OP_n OP_CHECKMULTISIG"""
    body = b"".join(push(key) for key in pubkeys)
    return bytes([0x50 + required]) + body + bytes([0x50 + len(pubkeys)]) + bytes([OP_CHECKMULTISIG])


def build_raw_tx(
    inputs: list[tuple[str, int]],
    outputs: list[tuple[int, bytes]],
    version: int = 1,
    locktime: int = 0,
) -> str:
    """Serialize an unsigned transaction. `outputs` is [(value_sats, script)]."""
    raw = version.to_bytes(4, "little")
    raw += varint(len(inputs))
    for txid, index in inputs:
        raw += bytes.fromhex(txid)[::-1]      # txids are displayed big-endian
        raw += index.to_bytes(4, "little")
        raw += varint(0)                       # empty scriptSig; the wallet signs
        raw += b"\xff\xff\xff\xff"             # sequence
    raw += varint(len(outputs))
    for value, script in outputs:
        raw += value.to_bytes(8, "little")
        raw += varint(len(script)) + script
    raw += locktime.to_bytes(4, "little")
    return raw.hex()


def sign_and_send(node, raw_hex: str) -> str:
    """Sign with the node's wallet and broadcast. Returns the txid."""
    try:
        signed = node.rpc.call("signrawtransactionwithwallet", raw_hex)
    except Exception:
        signed = node.rpc.call("signrawtransaction", raw_hex)
    if not signed.get("complete"):
        raise RuntimeError(f"signing failed: {signed.get('errors')}")
    return node.rpc.call("sendrawtransaction", signed["hex"])
