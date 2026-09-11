"""Bitcoin-family script and address handling.

Only what Arcade needs: recognising the handful of output types Omni cares about,
extracting destinations, and base58check encoding. Arcade never builds spending
scripts and never touches private keys.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from .config import Params

B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Opcodes we need to recognise.
OP_0 = 0x00
OP_PUSHDATA1 = 0x4C
OP_PUSHDATA2 = 0x4D
OP_PUSHDATA4 = 0x4E
OP_1 = 0x51
OP_16 = 0x60
OP_RETURN = 0x6A
OP_DUP = 0x76
OP_EQUAL = 0x87
OP_EQUALVERIFY = 0x88
OP_HASH160 = 0xA9
OP_CHECKSIG = 0xAC
OP_CHECKMULTISIG = 0xAE


class OutputType(Enum):
    """The output types Omni distinguishes (omnicore.cpp:GetEncodingClass)."""

    NONSTANDARD = "nonstandard"
    PUBKEYHASH = "pubkeyhash"
    SCRIPTHASH = "scripthash"
    MULTISIG = "multisig"
    NULL_DATA = "nulldata"
    PUBKEY = "pubkey"


class ScriptError(Exception):
    """A script could not be parsed."""


# --- base58check --------------------------------------------------------------


def b58encode(data: bytes) -> str:
    value = int.from_bytes(data, "big")
    out = bytearray()
    while value:
        value, rem = divmod(value, 58)
        out.append(B58_ALPHABET[rem])
    # Each leading zero byte becomes a leading '1'.
    for byte in data:
        if byte != 0:
            break
        out.append(B58_ALPHABET[0])
    return bytes(reversed(out)).decode("ascii")


def b58decode(text: str) -> bytes:
    value = 0
    for char in text:
        index = B58_ALPHABET.find(char.encode("ascii"))
        if index == -1:
            raise ScriptError(f"invalid base58 character {char!r}")
        value = value * 58 + index
    size = (value.bit_length() + 7) // 8
    out = value.to_bytes(size, "big")
    pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * pad + out


def _checksum(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]


def b58check_encode(version: int, payload: bytes) -> str:
    body = bytes([version]) + payload
    return b58encode(body + _checksum(body))


def b58check_decode(address: str) -> tuple[int, bytes]:
    raw = b58decode(address)
    if len(raw) < 5:
        raise ScriptError(f"address {address!r} is too short")
    body, checksum = raw[:-4], raw[-4:]
    if _checksum(body) != checksum:
        raise ScriptError(f"bad checksum in address {address!r}")
    return body[0], body[1:]


def address_to_bytes(address: str) -> bytes:
    """Return the 21-byte version||hash160 form used by payload types 185/186.

    Mirrors omnicore/src/omnicore/createpayload.cpp:31-45 -- base58 decode, then
    truncate the 4-byte checksum.
    """
    version, payload = b58check_decode(address)
    if len(payload) != 20:
        raise ScriptError(f"address {address!r} does not contain a 20-byte hash")
    return bytes([version]) + payload


def hash160(data: bytes) -> bytes:
    return hashlib.new("ripemd160", hashlib.sha256(data).digest()).digest()


# --- script parsing -----------------------------------------------------------


def iter_pushes(script: bytes) -> list[bytes]:
    """Return every data element pushed by `script`, ignoring other opcodes.

    Equivalent to Omni's GetScriptPushes. Malformed scripts yield what could be
    read rather than raising: a truncated script is a normal thing to encounter
    on a public chain, not an exceptional one.
    """
    pushes: list[bytes] = []
    i = 0
    n = len(script)
    while i < n:
        op = script[i]
        i += 1
        if op == 0 or op > OP_PUSHDATA4:
            continue
        if op < OP_PUSHDATA1:
            size = op
        elif op == OP_PUSHDATA1:
            if i >= n:
                break
            size = script[i]
            i += 1
        elif op == OP_PUSHDATA2:
            if i + 2 > n:
                break
            size = int.from_bytes(script[i : i + 2], "little")
            i += 2
        else:
            if i + 4 > n:
                break
            size = int.from_bytes(script[i : i + 4], "little")
            i += 4
        if i + size > n:
            break
        pushes.append(script[i : i + size])
        i += size
    return pushes


@dataclass(frozen=True)
class ParsedOutput:
    """One transaction output, classified."""

    type: OutputType
    value: int
    address: str | None = None
    data: bytes | None = None           # NULL_DATA payload
    pubkeys: tuple[bytes, ...] = ()     # MULTISIG keys, in script order
    required: int = 0                   # MULTISIG threshold


def classify_script(script: bytes, params: Params) -> tuple[OutputType, str | None, bytes | None, tuple[bytes, ...], int]:
    """Classify a scriptPubKey.

    Returns (type, address, nulldata_payload, multisig_pubkeys, threshold).
    """
    n = len(script)

    # P2PKH: OP_DUP OP_HASH160 <20> OP_EQUALVERIFY OP_CHECKSIG
    if (
        n == 25
        and script[0] == OP_DUP
        and script[1] == OP_HASH160
        and script[2] == 20
        and script[23] == OP_EQUALVERIFY
        and script[24] == OP_CHECKSIG
    ):
        return (
            OutputType.PUBKEYHASH,
            b58check_encode(params.pubkeyhash_version, script[3:23]),
            None,
            (),
            0,
        )

    # P2SH: OP_HASH160 <20> OP_EQUAL
    if n == 23 and script[0] == OP_HASH160 and script[1] == 20 and script[22] == OP_EQUAL:
        return (
            OutputType.SCRIPTHASH,
            b58check_encode(params.scripthash_version, script[2:22]),
            None,
            (),
            0,
        )

    # OP_RETURN <data>
    if n >= 1 and script[0] == OP_RETURN:
        pushes = iter_pushes(script[1:])
        return OutputType.NULL_DATA, None, (pushes[0] if pushes else b""), (), 0

    # Bare multisig: OP_m <pubkey>... OP_n OP_CHECKMULTISIG
    if n >= 3 and script[-1] == OP_CHECKMULTISIG and OP_1 <= script[0] <= OP_16:
        required = script[0] - OP_1 + 1
        if OP_1 <= script[-2] <= OP_16:
            total = script[-2] - OP_1 + 1
            keys = tuple(iter_pushes(script[1:-2]))
            if len(keys) == total and all(len(k) in (33, 65) for k in keys):
                return OutputType.MULTISIG, None, None, keys, required

    # P2PK: <pubkey> OP_CHECKSIG
    if n in (35, 67) and script[-1] == OP_CHECKSIG:
        keys = iter_pushes(script[:-1])
        if len(keys) == 1 and len(keys[0]) in (33, 65):
            return (
                OutputType.PUBKEY,
                b58check_encode(params.pubkeyhash_version, hash160(keys[0])),
                None,
                (),
                0,
            )

    return OutputType.NONSTANDARD, None, None, (), 0


def parse_output(script_hex: str, value_sats: int, params: Params) -> ParsedOutput:
    script = bytes.fromhex(script_hex)
    kind, address, data, keys, required = classify_script(script, params)
    return ParsedOutput(
        type=kind, value=value_sats, address=address, data=data, pubkeys=keys, required=required
    )


# Input types Omni accepts. Anything else invalidates the transaction
# (omnicore/src/omnicore/rules.cpp:416-430 -- only TX_PUBKEYHASH and TX_SCRIPTHASH).
ALLOWED_INPUT_TYPES = frozenset({OutputType.PUBKEYHASH, OutputType.SCRIPTHASH})
