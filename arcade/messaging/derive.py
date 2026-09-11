"""Deriving a messaging identity from the node's wallet.

The passphrase problem
----------------------
A standalone identity needs a passphrase, and a passphrase can be forgotten --
after which every message ever sent to that identity is unreadable. That is a
poor thing to ask of someone who just wants to send a message.

The alternative: derive the X25519 key from something the wallet already holds
and already protects.

    signature = wallet.signmessage(address, "DogecoinArcade messaging identity v1")
    seed      = HKDF-SHA256(signature)
    identity  = X25519 keypair from that seed

This works because `signmessage` is **deterministic** -- Bitcoin-derived wallets
use RFC 6979, so the same address and message always produce the same signature.
Verified on Pepecoin: three calls, byte-identical results.

What this buys, and what it costs
---------------------------------
Buys: no passphrase to forget, and no separate backup. Restore the wallet and the
same messaging identity comes back, because the same key produces the same
signature produces the same seed.

Costs, stated plainly: **whoever can use the wallet can re-derive the identity**.
The wallet's own security becomes the messaging security. If the wallet is
unencrypted, so is the identity in practice. That is a real reduction against a
standalone key with its own passphrase, and it is the trade being made.

Why not use the wallet's secp256k1 key directly for encryption
--------------------------------------------------------------
Because a spending key is the wrong key. It is exposed to signature-based attack
every time it spends, it cannot be rotated without moving funds, and its
compromise would retroactively decrypt every message ever sent. Deriving a
separate X25519 key keeps encryption away from spending while still resting on
the wallet for custody.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from ..rpc import RpcClient
from .keys import Identity

#: Signed to produce the seed. Fixed, and versioned so a future scheme can
#: coexist. CONSENSUS-IRRELEVANT but IDENTITY-CRITICAL: changing it gives every
#: user a different key and orphans their messages.
DERIVATION_MESSAGE = "DogecoinArcade messaging identity v1"

#: Domain separation for the HKDF step, so the seed cannot collide with any
#: other use of the same signature.
HKDF_INFO = b"arcade-x25519-identity-v1"


class DerivationError(Exception):
    """The identity could not be derived from the wallet."""


def _hkdf_sha256(material: bytes, info: bytes, length: int = 32) -> bytes:
    """HKDF-SHA256 with an empty salt (RFC 5869).

    The signature is high-entropy already, so extraction adds little -- but it is
    the standard construction and costs nothing, and it gives domain separation
    through `info`.
    """
    prk = hmac.new(b"\x00" * 32, material, hashlib.sha256).digest()
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def derive_identity(rpc: RpcClient, address: str) -> Identity:
    """Derive the X25519 identity that belongs to `address`.

    Deterministic: the same wallet and address always give the same identity.
    """
    if not address:
        raise DerivationError("an address is required to derive an identity")

    try:
        signature = rpc.call("signmessage", address, DERIVATION_MESSAGE)
    except Exception as exc:
        message = str(exc)
        if "private key" in message.lower() or "not available" in message.lower():
            raise DerivationError(
                f"the wallet has no private key for {address}. Use an address the "
                "wallet owns."
            ) from None
        if "passphrase" in message.lower() or "locked" in message.lower():
            raise DerivationError(
                "the wallet is locked. Unlock it first: "
                "pepecoin-cli walletpassphrase \"<passphrase>\" 600"
            ) from None
        raise DerivationError(f"could not sign with {address}: {exc}") from None

    try:
        raw = base64.b64decode(signature, validate=True)
    except Exception:
        raise DerivationError("the wallet returned a signature that is not base64") from None

    if len(raw) < 32:
        raise DerivationError(f"signature is only {len(raw)} bytes; expected 65")

    return Identity.from_secret_bytes(_hkdf_sha256(raw, HKDF_INFO))


def verify_derivation(rpc: RpcClient, address: str, expected: Identity) -> bool:
    """Confirm an address still derives the identity we think it does.

    Worth checking before relying on a stored association: a wallet restored from
    a different backup, or a mistyped address, would silently produce a different
    key and a silently unreadable inbox.
    """
    try:
        return derive_identity(rpc, address).public_bytes == expected.public_bytes
    except DerivationError:
        return False
