"""Taproot (BIP-340/341) for coins a user's wallet signs with Schnorr: whether an output is the user's to sign, and the
digest a Taproot input signs. The arcade signs nothing with it: the user's wallet does (coins.js signSchnorr).

Two ways a user's key spends a Taproot output, both checked here against the coin's script on the chain:
- **key path:** the output key is the user's key tweaked with no script tree (BIP-86's TapTweak(P)), and the witness
  is one signature by that tweaked key. DigiDollar's own outputs are this.
- **script path:** the coin commits to a script tree, and the input reveals one leaf with its control block. The leaf
  must end in `<the user's x-only key> OP_CHECKSIG`, so the signature the wallet makes is one that leaf asks of the
  user and nothing else. A DigiDollar vault's normal path is this.

Plain arithmetic on secp256k1 in Python; no secret ever passes through it."""
from __future__ import annotations

import hashlib

P = 2 ** 256 - 2 ** 32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)
LEAF_VERSION = 0xC0
#: SIGHASH_DEFAULT: every input and output, and a 64-byte signature with no sighash byte.
DEFAULT = 0x00


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


def mul(k: int, point=G):
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


def xonly(pubkey: bytes) -> bytes:
    """A compressed public key's 32-byte x-only form (BIP-340)."""
    if len(pubkey) != 33 or pubkey[0] not in (2, 3):
        raise ValueError("a compressed public key is 33 bytes, starting 02 or 03")
    return pubkey[1:]


def tweak(internal: bytes, merkle_root: bytes = b"") -> tuple[bytes, int]:
    """(the output key, the parity of its y) for this internal key and script tree (BIP-341 taproot_tweak_pubkey)."""
    t = int.from_bytes(tagged("TapTweak", internal + merkle_root), "big")
    if t >= N:
        raise ValueError("tweak out of range")
    q = _add(lift_x(internal), mul(t))
    return q[0].to_bytes(32, "big"), q[1] & 1


def p2tr(output_key: bytes) -> bytes:
    return b"\x51\x20" + output_key


def _compact(n: int) -> bytes:
    return bytes([n]) if n < 0xFD else b"\xfd" + n.to_bytes(2, "little")


def leaf_hash(script: bytes, version: int = LEAF_VERSION) -> bytes:
    return tagged("TapLeaf", bytes([version]) + _compact(len(script)) + script)


def branch(a: bytes, b: bytes) -> bytes:
    return tagged("TapBranch", min(a, b) + max(a, b))


def key_path_mine(script: bytes, pubkey: bytes) -> bool:
    """Whether `script` is the key-path output of this user's key: P2TR to TapTweak(P), no script tree."""
    return script == p2tr(tweak(xonly(pubkey))[0])


def leaf_mine(script: bytes, leaf: bytes, control: bytes, pubkey: bytes) -> bool:
    """Whether revealing `leaf` with `control` spends the P2TR `script`, and the leaf asks this user's key alone to
    sign: it ends in a push of the user's x-only key and OP_CHECKSIG, and has no other OP_CHECKSIG family opcode
    after it. The control block's parity and path are checked as a node checks them (BIP-341)."""
    if len(script) != 34 or script[:2] != b"\x51\x20" or not leaf.endswith(b"\x20" + xonly(pubkey) + b"\xac"):
        return False
    if len(control) < 33 or (len(control) - 33) % 32 or (control[0] & 0xFE) != LEAF_VERSION:
        return False
    node = leaf_hash(leaf, control[0] & 0xFE)
    for at in range(33, len(control), 32):
        node = branch(node, control[at:at + 32])
    try:
        key, parity = tweak(control[1:33], node)
    except ValueError:
        return False
    return key == script[2:] and parity == control[0] & 1


def sighash(inputs: list[dict], outputs: list[tuple[int, bytes]], index: int, *, version: int, locktime: int,
            leaf: bytes | None = None) -> bytes:
    """The digest input `index` signs under SIGHASH_DEFAULT (BIP-341 SigMsg, then TapSighash). Every input carries
    `txid`, `vout`, `value`, `spent` (its script) and optionally `sequence`; `leaf` for a script-path spend."""
    def sha(parts) -> bytes:
        return _sha(b"".join(parts))
    msg = bytes([0, DEFAULT]) + version.to_bytes(4, "little") + locktime.to_bytes(4, "little")
    msg += sha(bytes.fromhex(c["txid"])[::-1] + int(c["vout"]).to_bytes(4, "little") for c in inputs)
    msg += sha(int(c["value"]).to_bytes(8, "little") for c in inputs)
    msg += sha(_compact(len(c["spent"])) + c["spent"] for c in inputs)
    msg += sha(int(c.get("sequence", 0xFFFFFFFF)).to_bytes(4, "little") for c in inputs)
    msg += sha(int(v).to_bytes(8, "little") + _compact(len(s)) + s for v, s in outputs)
    msg += bytes([2 if leaf is not None else 0]) + index.to_bytes(4, "little")
    if leaf is not None:
        msg += leaf_hash(leaf) + b"\x00" + b"\xff\xff\xff\xff"
    return tagged("TapSighash", msg)


def verify(pubkey32: bytes, message: bytes, signature: bytes) -> bool:
    """BIP-340 verification, for the tests and for checking what a wallet sent back before it is broadcast."""
    if len(signature) != 64:
        return False
    try:
        point = lift_x(pubkey32)
    except ValueError:
        return False
    r, s = int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
    if r >= P or s >= N:
        return False
    e = int.from_bytes(tagged("BIP0340/challenge", signature[:32] + pubkey32 + message), "big") % N
    found = _add(mul(s), mul(N - e, point))
    return found is not None and found[1] % 2 == 0 and found[0] == r
