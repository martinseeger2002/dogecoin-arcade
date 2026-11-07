"""The few pieces of Taproot (BIP-340/341) and DigiByte script a DigiDollar mint needs, in plain Python.

Everything here is public arithmetic: no key is ever in this module. The collateral and DD outputs are built exactly
as DigiByte Core 9.26.7 builds them (src/digidollar/scripts.cpp, txbuilder.cpp), so a node checks them the same way."""
from __future__ import annotations

import hashlib

P = 2 ** 256 - 2 ** 32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)

# DigiByte's opcodes for DigiDollar (src/script/script.h), and the plain ones a vault's paths use.
OP_0, OP_RETURN, OP_DROP, OP_VERIFY, OP_NOT, OP_CHECKSIG, OP_CLTV = 0x00, 0x6A, 0x75, 0x69, 0x91, 0xAC, 0xB1
OP_DIGIDOLLAR, OP_DDVERIFY, OP_CHECKCOLLATERAL = 0xBB, 0xBC, 0xBE
#: The vault's internal key: a point with no known private key, so the collateral moves only by a script path, after
#: its lock (scripts.h COLLATERAL_NUMS_POINT_BYTES).
NUMS = bytes.fromhex("50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0")


def _sha(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def tagged(tag: str, data: bytes) -> bytes:
    t = _sha(tag.encode())
    return _sha(t + t + data)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % P == 0:
        return None
    if a == b:
        m = 3 * a[0] * a[0] * pow(2 * a[1], -1, P) % P
    else:
        m = (b[1] - a[1]) * pow(b[0] - a[0], -1, P) % P
    x = (m * m - a[0] - b[0]) % P
    return x, (m * (a[0] - x) - a[1]) % P


def _mul(k: int, point):
    out = None
    while k:
        if k & 1:
            out = _add(out, point)
        point, k = _add(point, point), k >> 1
    return out


def lift_x(xonly: bytes):
    x = int.from_bytes(xonly, "big")
    y2 = (pow(x, 3, P) + 7) % P
    y = pow(y2, (P + 1) // 4, P)
    if x >= P or y * y % P != y2:
        raise ValueError("that is not a key on secp256k1")
    return x, y if y % 2 == 0 else P - y


def tweaked(xonly: bytes, merkle_root: bytes = b"") -> tuple[bytes, int]:
    """(the output key, the parity of its y) of a Taproot output with this internal key and script tree (BIP-341
    taproot_tweak_pubkey). The parity goes in a script path's control block."""
    t = int.from_bytes(tagged("TapTweak", xonly + merkle_root), "big")
    if t >= N:
        raise ValueError("tweak out of range")
    q = _add(lift_x(xonly), _mul(t, G))
    return q[0].to_bytes(32, "big"), q[1] & 1


def tweak(xonly: bytes, merkle_root: bytes = b"") -> bytes:
    """The output key alone."""
    return tweaked(xonly, merkle_root)[0]


def compact(n: int) -> bytes:
    return bytes([n]) if n < 0xFD else b"\xfd" + n.to_bytes(2, "little")


def leaf(script: bytes) -> bytes:
    return tagged("TapLeaf", b"\xc0" + compact(len(script)) + script)


def branch(a: bytes, b: bytes) -> bytes:
    return tagged("TapBranch", min(a, b) + max(a, b))


def p2tr(output_key: bytes) -> bytes:
    return b"\x51\x20" + output_key


# --- script numbers and pushes, as Bitcoin Core's CScript writes them --------------------------------------------

def num(n: int) -> bytes:
    """CScriptNum::serialize: little-endian, sign in the top bit, nothing for zero."""
    if n == 0:
        return b""
    out, neg, n = bytearray(), n < 0, abs(n)
    while n:
        out.append(n & 0xFF)
        n >>= 8
    if out[-1] & 0x80:
        out.append(0x80 if neg else 0)
    elif neg:
        out[-1] |= 0x80
    return bytes(out)


def push(data: bytes) -> bytes:
    """`CScript << vector`: the bytes as data, never as OP_N, even when short."""
    if len(data) < 0x4C:
        return bytes([len(data)]) + data
    if len(data) <= 0xFF:
        return b"\x4c" + bytes([len(data)]) + data
    return b"\x4d" + len(data).to_bytes(2, "little") + data


def pushes(script: bytes) -> list:
    """A script's items: the bytes of each push, and each opcode as an int. Enough to read an OP_RETURN record."""
    out, at = [], 0
    while at < len(script):
        op = script[at]
        at += 1
        if 1 <= op <= 0x4B:
            size = op
        elif op in (0x4C, 0x4D):
            width = 1 if op == 0x4C else 2
            size, at = int.from_bytes(script[at:at + width], "little"), at + width
        else:
            out.append(op)
            continue
        if at + size > len(script):
            raise ValueError("that script ends inside a push")
        out.append(script[at:at + size])
        at += size
    return out


def read_num(data) -> int:
    """CScriptNum, the other way: a push's bytes (or OP_0, OP_1 to OP_16) as a number."""
    if isinstance(data, int):
        if data == OP_0:
            return 0
        if 0x51 <= data <= 0x60:
            return data - 0x50
        raise ValueError(f"opcode {data:#x} is not a number")
    if not data:
        return 0
    n = int.from_bytes(data, "little")
    if data[-1] & 0x80:
        return -(n & ~(0x80 << (8 * (len(data) - 1))))
    return n


def push_int(n: int) -> bytes:
    """`CScript << int64_t`: OP_0, OP_1 to OP_16 and OP_1NEGATE where they fit, else the number as data."""
    if n == 0:
        return bytes([OP_0])
    if n == -1 or 1 <= n <= 16:
        return bytes([0x50 + n])
    return push(num(n))


B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def base58check(body: bytes) -> str:
    """`body` and its four check bytes (sha256d), in base58."""
    whole = body + _sha(_sha(body))[:4]
    n, out = int.from_bytes(whole, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(whole) - len(whole.lstrip(b"\0"))) + out


def unbase58check(text: str) -> bytes:
    """The body base58check text holds, or ValueError when it is not one."""
    n = 0
    for ch in text:
        at = B58.find(ch)
        if at < 0:
            raise ValueError("not base58")
        n = n * 58 + at
    whole = n.to_bytes((n.bit_length() + 7) // 8, "big")
    whole = b"\0" * (len(text) - len(text.lstrip("1"))) + whole
    if len(whole) < 5 or _sha(_sha(whole[:-4]))[:4] != whole[-4:]:
        raise ValueError("its check bytes are wrong")
    return whole[:-4]
