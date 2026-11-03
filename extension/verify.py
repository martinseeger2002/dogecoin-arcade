"""Checking what an Arcade Wallet signed, for any site, in Python. Nothing here asks a server.

    from verify import verify_sign_in, verify_message
    verify_sign_in(origin, nonce, pubkey_hex, signature_hex)     # arcade_signIn; needs PyNaCl
    verify_message(chain, address, message, signature_base64)    # arcade_signMessage; the standard library only

verify.js is the same in JavaScript. A sign-in is the arcade's login message signed with the account's Ed25519 key:
"DogecoinArcade login\\n<your origin>\\n<your nonce>". Issue the nonce yourself (32-128 hex), use it once, and check
the origin is yours. A message is Core's `signmessage`: any of the chain's nodes checks it with `verifymessage` too.
RIPEMD-160 comes from hashlib, which on OpenSSL 3 needs its legacy provider.
"""
from __future__ import annotations

import base64
import hashlib
import re

#: Each chain's P2PKH version byte and its Core's message words (extension/chains.js; a test holds them equal).
CHAINS = {
    "main": (56, "Pepecoin Signed Message:\n"), "test": (113, "Pepecoin Signed Message:\n"),
    "regtest": (111, "Pepecoin Signed Message:\n"), "doge": (30, "Dogecoin Signed Message:\n"),
    "ltc": (48, "Litecoin Signed Message:\n"), "ltctest": (111, "Litecoin Signed Message:\n"),
    "bel": (25, "Bells Signed Message:\n"), "lky": (47, "Luckycoin Signed Message:\n"),
}

_P = 2 ** 256 - 2 ** 32 - 977
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def verify_sign_in(origin: str, nonce: str, pubkey: str, signature: str) -> bool:
    """Is `signature` (hex) the account `pubkey`'s (hex) sign-in to `origin` with `nonce`?"""
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey
    if not re.fullmatch(r"[0-9a-f]{32,128}", str(nonce)):
        return False
    try:
        VerifyKey(bytes.fromhex(pubkey)).verify(f"DogecoinArcade login\n{origin}\n{nonce}".encode(),
                                                bytes.fromhex(signature))
        return True
    except (BadSignatureError, ValueError):
        return False


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % _P == 0:
        return None
    lam = (3 * a[0] * a[0] * pow(2 * a[1], -1, _P)) if a == b else (b[1] - a[1]) * pow(b[0] - a[0], -1, _P)
    x = (lam * lam - a[0] - b[0]) % _P
    return x, (lam * (a[0] - x) - a[1]) % _P


def _mul(k: int, point):
    out = None
    while k:
        if k & 1:
            out = _add(out, point)
        point, k = _add(point, point), k >> 1
    return out


def _sha256d(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def _varstr(b: bytes) -> bytes:
    return (bytes([len(b)]) if len(b) < 0xFD else b"\xfd" + len(b).to_bytes(2, "little")) + b


def _key_hash(chain: str, address: str) -> bytes | None:
    n = 0
    for ch in str(address):
        if ch not in _B58:
            return None
        n = n * 58 + _B58.index(ch)
    raw = b"\0" * (len(address) - len(address.lstrip("1"))) + n.to_bytes((n.bit_length() + 7) // 8, "big")
    if len(raw) != 25 or raw[0] != CHAINS[chain][0] or _sha256d(raw[:21])[:4] != raw[21:]:
        return None
    return raw[1:21]


def verify_message(chain: str, address: str, message: str, signature: str) -> bool:
    """Did the key of `address` (a P2PKH address on `chain`) sign `message`? `signature` is the base64 Core gives."""
    if chain not in CHAINS:
        return False
    want = _key_hash(chain, address)
    try:
        raw = base64.b64decode(signature, validate=True)
    except ValueError:
        return False
    if want is None or len(raw) != 65 or not 27 <= raw[0] <= 34:
        return False
    flag = raw[0] - 27
    r, s = int.from_bytes(raw[1:33], "big"), int.from_bytes(raw[33:], "big")
    x = r + ((flag & 3) // 2) * _N
    if not (0 < r < _N and 0 < s < _N) or x >= _P:
        return False
    y = pow((x * x * x + 7) % _P, (_P + 1) // 4, _P)
    if (y * y - x * x * x - 7) % _P:
        return False
    if (y & 1) != (flag & 1):
        y = _P - y
    e = int.from_bytes(_sha256d(_varstr(CHAINS[chain][1].encode()) + _varstr(message.encode())), "big")
    q = _mul(pow(r, -1, _N), _add(_mul(s, (x, y)), _mul((-e) % _N, _G)))
    if q is None:
        return False
    key = bytes([2 + (q[1] & 1)]) + q[0].to_bytes(32, "big") if flag >= 4 \
        else b"\x04" + q[0].to_bytes(32, "big") + q[1].to_bytes(32, "big")
    return hashlib.new("ripemd160", hashlib.sha256(key).digest()).digest() == want
