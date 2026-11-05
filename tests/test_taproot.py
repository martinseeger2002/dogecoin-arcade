"""Taproot for the user's wallet (arcade/taproot.py): BIP-340 signatures, and which outputs are a key's to sign.
The digest is checked by a real DigiByte Core in test_digidollar_regtest.py, and the browser's against it in
test_lite_schnorr_browser.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_module_host import _key  # noqa: E402

from arcade import taproot as tp  # noqa: E402


def schnorr(secret: int, message: bytes, aux: bytes = bytes(32)) -> bytes:
    """BIP-340 signing, as the wallet does it (coins.js signSchnorr), for the tests only: the arcade never signs."""
    point = tp.mul(secret)
    d = secret if point[1] % 2 == 0 else tp.N - secret
    px = point[0].to_bytes(32, "big")
    t = (d ^ int.from_bytes(tp.tagged("BIP0340/aux", aux), "big")).to_bytes(32, "big")
    k0 = int.from_bytes(tp.tagged("BIP0340/nonce", t + px + message), "big") % tp.N
    r = tp.mul(k0)
    k = k0 if r[1] % 2 == 0 else tp.N - k0
    e = int.from_bytes(tp.tagged("BIP0340/challenge", r[0].to_bytes(32, "big") + px + message), "big") % tp.N
    return r[0].to_bytes(32, "big") + ((k + e * d) % tp.N).to_bytes(32, "big")


def tweaked_secret(secret: int) -> int:
    """The secret of a key's key-path output (BIP-341 taproot_tweak_seckey, no script tree)."""
    point = tp.mul(secret)
    d = secret if point[1] % 2 == 0 else tp.N - secret
    return (d + int.from_bytes(tp.tagged("TapTweak", point[0].to_bytes(32, "big")), "big")) % tp.N


def test_bip340s_first_vector_signs_and_verifies():
    sig = schnorr(3, bytes(32))
    pub = tp.mul(3)[0].to_bytes(32, "big")
    assert pub.hex().upper() == "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9"
    assert sig.hex().upper() == ("E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA8215"
                                 "25F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0")
    assert tp.verify(pub, bytes(32), sig) and not tp.verify(pub, b"\x01" * 32, sig)


def keypair() -> tuple[bytes, int]:
    import secrets
    secret = secrets.randbelow(tp.N - 1) + 1
    x, y = tp.mul(secret)
    return bytes([2 + (y & 1)]) + x.to_bytes(32, "big"), secret


def test_a_key_path_output_is_the_keys_and_its_tweaked_secret_signs_for_it():
    pub, secret = keypair()
    script = tp.p2tr(tp.tweak(tp.xonly(pub))[0])
    assert tp.key_path_mine(script, pub)
    other, _ = _key()
    assert not tp.key_path_mine(script, other)
    sig = schnorr(tweaked_secret(secret), b"\x07" * 32)
    assert tp.verify(script[2:], b"\x07" * 32, sig)


def test_a_script_path_is_the_keys_only_where_its_leaf_ends_in_that_key_alone():
    pub, _ = _key()
    other, _ = _key()
    nums = bytes.fromhex("50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0")
    mine = b"\x02\x10\x27\xb1\x75\x20" + tp.xonly(pub) + b"\xac"
    theirs = b"\x20" + tp.xonly(other) + b"\xac"
    root = tp.branch(tp.leaf_hash(mine), tp.leaf_hash(theirs))
    key, parity = tp.tweak(nums, root)
    script = tp.p2tr(key)
    control = bytes([0xC0 | parity]) + nums + tp.leaf_hash(theirs)
    assert tp.leaf_mine(script, mine, control, pub)
    assert not tp.leaf_mine(script, mine, control, other), "a leaf for this key is not another's to sign"
    assert not tp.leaf_mine(script, theirs, bytes([0xC0 | parity]) + nums + tp.leaf_hash(mine), pub)
    assert not tp.leaf_mine(script, mine, bytes([0xC0 | (parity ^ 1)]) + nums + tp.leaf_hash(theirs), pub)
    assert not tp.leaf_mine(tp.p2tr(bytes(32)), mine, control, pub), "a control block that does not open this coin"
