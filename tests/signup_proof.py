"""Signing up as the browser does since 2026-11-08 (security review): the account key signs a fresh challenge, as a
sign-in does, and the coin key proves the address is its own (wallet_imports.check_proof). For tests that post
/signup themselves."""
import itertools

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from nacl.signing import SigningKey

from arcade import accounts, wallet_imports
from arcade.script import b58check_encode, hash160

ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_secrets = itertools.count(0x5160)


def coin_key(secret: int | None = None):
    """A secp256k1 key: (private key, compressed public key)."""
    priv = ec.derive_private_key(secret or next(_secrets), ec.SECP256K1())
    return priv, priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)


def coin_sign(priv, digest: bytes) -> bytes:
    r, s = utils.decode_dss_signature(priv.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256()))))
    return utils.encode_dss_signature(r, min(s, ORDER - s))


def signup(app, state, tag: str, *, key: SigningKey | None = None, coin=None, address: str | None = None,
           blob=None, headers=None, sign_address: bool = True, sign_key: bool = True, **extra):
    """POST /signup with both proofs. `address` other than the coin key's own is posted as given (and its proof is
    then the coin key's, which the arcade refuses); `sign_address` / `sign_key` False leave a proof out."""
    key = key or SigningKey.generate()
    priv, pub = coin or coin_key()
    chain = state.messaging
    own = b58check_encode(chain.params.pubkeyhash_version, hash160(pub))
    address = address or own
    pubkey = key.verify_key.encode().hex()
    body = {"tag": tag, "pubkey": pubkey, "address": address, "coin_pubkey": pub.hex(),
            "blob": blob if blob is not None else {"sealed": "x", "iterations": 600000}, **extra}
    if sign_address:
        digest = wallet_imports.proof_digest(pubkey, chain.network, address, pub.hex())
        body["address_signature"] = coin_sign(priv, digest).hex()
    if sign_key:
        challenge = app.get("/auth/challenge", headers=headers or {}).json()
        body["nonce"] = challenge["nonce"]
        body["signature"] = key.sign(accounts.login_message(challenge["origin"], challenge["nonce"])).signature.hex()
    return app.post("/signup", headers=headers or {}, json=body)


def address_of(state, coin) -> str:
    """The account-chain address of a coin key made by `coin_key`."""
    return b58check_encode(state.messaging.params.pubkeyhash_version, hash160(coin[1]))
