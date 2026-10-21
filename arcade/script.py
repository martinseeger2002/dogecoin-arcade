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
    WITNESS_V0_KEYHASH = "witness_v0_keyhash"
    WITNESS_V0_SCRIPTHASH = "witness_v0_scripthash"
    WITNESS_V1_TAPROOT = "witness_v1_taproot"
    WITNESS_UNKNOWN = "witness_unknown"


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

    # Native segwit, only where the chain has a bech32 prefix (Litecoin and
    # its testnet; Pepecoin and Dogecoin have none, so their outputs never read
    # as witness): OP_0/OP_1..OP_16 then one 2-40 byte push, nothing else. The
    # P2SH-nested form is an ordinary P2SH output and is already caught above.
    if params.bech32_hrp:
        program = witness_program(script)
        if program is not None:
            version, prog = program
            if version == 0 and len(prog) == 20:
                kind = OutputType.WITNESS_V0_KEYHASH
            elif version == 0 and len(prog) == 32:
                kind = OutputType.WITNESS_V0_SCRIPTHASH
            elif version == 1 and len(prog) == 32:
                kind = OutputType.WITNESS_V1_TAPROOT
            else:
                kind = OutputType.WITNESS_UNKNOWN
            has_address = kind is not OutputType.WITNESS_UNKNOWN or version
            addr = segwit_encode(params.bech32_hrp, version, prog) if has_address else None
            return kind, addr, None, (), 0

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


def output_type(script, params: Params) -> "OutputType | None":
    """What kind of output a node's answer describes, or None if it says nothing.

    The same script arrives in whichever shape the daemon's version uses: an
    object with the hex inside it, which is what a `vout` row carries, or the
    hex on its own, which is what an older node's `listunspent` rows carry. A
    caller deciding whether an output may be SPENT has to ask in both, and the
    callers that must are the ones choosing transaction inputs -- after a
    transaction is built, the rule below has already been applied by the reader,
    and by then the transaction is on the chain (D-171).

    None means the node described nothing, not that the output is odd. Callers
    read that as allowed: refusing coins because their node was silent would
    report a funded wallet as empty, which is the wrong direction to be wrong in.
    """
    hex_text = (script.get("hex", "") if isinstance(script, dict)
                else script if isinstance(script, str) else "")
    if not hex_text:
        return None
    try:
        return classify_script(bytes.fromhex(hex_text), params)[0]
    except ValueError:          # not hex at all: the node said something odd
        return None



# --- bech32 and segwit addresses (BIP173 / BIP350) ---------------------------
#
# Added for the Electrum-style Litecoin paths (2026-10-19, tbuuol's side): the
# arcade's legacy P2PKH address stays as it is, and three more are derived
# beside it the way other wallets do -- legacy (m/44'), P2SH-nested segwit
# (m/49') and native segwit (m/84'). This module owns the ENCODING half so the
# indexer can recognise segwit outputs and the backup page can show the
# addresses; the KEY derivation stays in the browser (coins.js), where the
# curve is, exactly as the legacy coin keys already do.
#
# The implementation is BIP173's own reference, kept close to it on purpose so
# it can be read against the standard. `tests/test_segwit_addr.py` holds it to
# the published bitcoin vectors (bc1/tb1), which fix the checksum and the
# 8-to-5-bit packing; the Litecoin HRPs ("ltc", "tltc") are the same algorithm
# with a different prefix fed into that same checksum.

BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

#: The constant the checksum is built to. 1 is bech32 (witness v0, BIP173);
#: 0x2bc830a3 is bech32m (witness v1+, BIP350). A v0 program decoded under the
#: bech32m constant -- or the reverse -- fails, which is the whole point of the
#: two constants, so a Taproot address is never read as a P2WPKH one.
BECH32_CONST = 1
BECH32M_CONST = 0x2BC830A3


def _bech32_polymod(values: list[int]) -> int:
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_create_checksum(hrp: str, data: list[int], const: int) -> list[int]:
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ const
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _bech32_verify_checksum(hrp: str, data: list[int]) -> int | None:
    """Which spec (BECH32_CONST or BECH32M_CONST) this string checks under, or
    None if it checks under neither."""
    check = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    if check == BECH32_CONST:
        return BECH32_CONST
    if check == BECH32M_CONST:
        return BECH32M_CONST
    return None


def bech32_encode(hrp: str, data: list[int], const: int = BECH32_CONST) -> str:
    """hrp + '1' + the data (5-bit groups) + a six-symbol checksum."""
    combined = data + _bech32_create_checksum(hrp, data, const)
    return hrp + "1" + "".join(BECH32_CHARSET[d] for d in combined)


def bech32_decode(bech: str) -> tuple[str | None, list[int] | None, int | None]:
    """(hrp, 5-bit data, spec const), or (None, None, None). Mixed case, a
    missing separator, an out-of-range symbol or a bad checksum all refuse."""
    if any(ord(x) < 33 or ord(x) > 126 for x in bech):
        return (None, None, None)
    if bech.lower() != bech and bech.upper() != bech:
        return (None, None, None)
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech) or len(bech) > 90:
        return (None, None, None)
    if not all(x in BECH32_CHARSET for x in bech[pos + 1:]):
        return (None, None, None)
    hrp = bech[:pos]
    data = [BECH32_CHARSET.find(x) for x in bech[pos + 1:]]
    const = _bech32_verify_checksum(hrp, data)
    if const is None:
        return (None, None, None)
    return (hrp, data[:-6], const)


def convertbits(data, frombits: int, tobits: int, pad: bool = True) -> list[int] | None:
    """Repack a byte string into groups of `tobits` bits (BIP173). Used to turn
    a witness program's 8-bit bytes into the 5-bit symbols bech32 carries."""
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def segwit_encode(hrp: str, witver: int, program: bytes) -> str:
    """The segwit address for a witness version and program. Version 0 is
    bech32; version 1 and up is bech32m (BIP350)."""
    const = BECH32_CONST if witver == 0 else BECH32M_CONST
    five = convertbits(program, 8, 5)
    if five is None:
        raise ScriptError("cannot pack that witness program")
    return bech32_encode(hrp, [witver] + five, const)


def segwit_decode(hrp: str, addr: str) -> tuple[int | None, bytes | None]:
    """(witness version, program) for a segwit address under this HRP, or
    (None, None). Enforces the version/spec pairing and the length rules."""
    got_hrp, data, const = bech32_decode(addr)
    if got_hrp != hrp or data is None or len(data) < 1:
        return (None, None)
    decoded = convertbits(data[1:], 5, 8, False)
    if decoded is None or len(decoded) < 2 or len(decoded) > 40:
        return (None, None)
    witver = data[0]
    if witver > 16:
        return (None, None)
    if witver == 0 and len(decoded) not in (20, 32):
        return (None, None)
    want = BECH32_CONST if witver == 0 else BECH32M_CONST
    if const != want:
        return (None, None)
    return (witver, bytes(decoded))


# --- segwit scriptPubKeys and the three address kinds -------------------------


def p2wpkh_script(keyhash: bytes) -> bytes:
    """A native-segwit v0 output: OP_0 <20-byte hash160 of the pubkey>."""
    if len(keyhash) != 20:
        raise ScriptError("a P2WPKH program is a 20-byte hash")
    return bytes([OP_0, 20]) + keyhash


def p2sh_p2wpkh_redeem(keyhash: bytes) -> bytes:
    """The redeemScript a P2SH-nested segwit output commits to: the P2WPKH
    program itself (0x00 0x14 <20>). The address is this script's P2SH."""
    return p2wpkh_script(keyhash)


def p2wpkh_address(params: "Params", keyhash: bytes) -> str:
    """The native-segwit (bech32) address for a key hash, e.g. ltc1q... /
    tltc1q.... Refuses on a chain with no bech32 HRP."""
    if not params.bech32_hrp:
        raise ScriptError(f"{params.name} has no bech32 prefix")
    return segwit_encode(params.bech32_hrp, 0, keyhash)


def p2sh_p2wpkh_address(params: "Params", keyhash: bytes) -> str:
    """The P2SH-nested-segwit address for a key hash: base58check of the
    script hash, in this chain's script-hash version (M... on LTC, Q/2... on
    LTC testnet). Opens in any wallet that speaks BIP49."""
    return b58check_encode(params.scripthash_version, hash160(p2sh_p2wpkh_redeem(keyhash)))


def witness_program(script: bytes) -> "tuple[int, bytes] | None":
    """A segwit output's (version, program), or None. OP_0 (version 0) or
    OP_1..OP_16 (versions 1-16), then a single 2-40 byte push that is the whole
    rest of the script -- the same shape rawchain's reader used before this
    module became the one home for it."""
    if (4 <= len(script) <= 42 and (script[0] == 0 or OP_1 <= script[0] <= OP_16)
            and script[1] == len(script) - 2):
        return (0 if script[0] == 0 else script[0] - 0x50), script[2:]
    return None
