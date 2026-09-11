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
            # An encrypted wallet is the one case where a passphrase still
            # exists: the node's own, which is the user's choice and nothing to
            # do with messaging. Worth saying plainly, because otherwise this
            # looks like the passphrase that was removed coming back.
            raise DerivationError(
                "this wallet is encrypted and locked, so the node will not sign "
                "with it. Messaging needs the wallet unlocked -- run "
                "`walletpassphrase \"<your wallet passphrase>\" 600` against this "
                "node, using its own command-line tool."
            ) from None
        raise DerivationError(f"could not sign with {address}: {exc}") from None

    try:
        raw = base64.b64decode(signature, validate=True)
    except Exception:
        raise DerivationError("the wallet returned a signature that is not base64") from None

    if len(raw) < 32:
        raise DerivationError(f"signature is only {len(raw)} bytes; expected 65")

    return Identity.from_secret_bytes(_hkdf_sha256(raw, HKDF_INFO))


#: The wallet account the identity address is filed under. Accounts live inside
#: wallet.dat, which is what lets a restore find the address again with no help
#: from any local database.
IDENTITY_ACCOUNT = "arcade-identity"

#: Where the chosen address is pinned, per network.
ADDRESS_META = "identity_address:{network}"


def resolve_identity_address(rpc: RpcClient, store, network: str) -> str:
    """The address this installation derives its identity from. Pinned, not computed.

    One implementation, used by both the web interface and the CLI, because two
    that computed it separately did diverge: the CLI recomputed the choice on
    every call while the web pinned it, so the same wallet could answer as two
    different people depending on which half you asked.

    The choice is made once and recorded. It is *not* recomputed afterwards,
    because the inputs are not stable:

    - `getaccountaddress` returns a **fresh** address as soon as the current one
      has been used, so calling it again can add an address to the account.
    - "Sort and take the first" is then not stable either: a newly added address
      that sorts earlier silently becomes the identity. a test machine caught this happening
      between two commands with no user action at all -- the same silent-change
      bug the sorting was introduced to prevent, wearing a different hat.

    So sorting survives only as the tiebreak on first setup, where the set really
    is fixed, and everything after that reads the pin.
    """
    key = ADDRESS_META.format(network=network)
    pinned = store.get_meta(key)
    if pinned:
        # Trust the pin, but not blindly: a wallet restored from a different
        # backup may not hold this address at all, and deriving from an address
        # the wallet cannot sign for fails confusingly deeper in.
        if _wallet_can_sign(rpc, pinned):
            _file_under_account(rpc, pinned)
            return pinned
        raise DerivationError(
            f"the identity address {pinned} is not in this wallet. If you have "
            f"restored a different wallet, this installation's messages belong to "
            f"the old one; if you have restored the right wallet, wait for it to "
            f"finish loading and try again."
        )

    # Deliberately not wrapped in `except Exception: pass`. It was, and that is
    # a great deal of consequence for a swallowed error: one transient RPC
    # failure here would fall through to `getaccountaddress` and silently mint a
    # *new* identity, orphaning every message the old one ever received. Failing
    # is the safe outcome; the next attempt succeeds and nothing was created.
    try:
        existing = rpc.call("getaddressesbyaccount", IDENTITY_ACCOUNT) or []
    except Exception as exc:
        raise DerivationError(
            f"could not read the wallet's identity account: {exc}. Nothing has "
            f"been changed -- try again once the node is responding."
        ) from None

    address = sorted(existing)[0] if existing else \
        rpc.call("getaccountaddress", IDENTITY_ACCOUNT)

    _file_under_account(rpc, address)
    store.set_meta(key, address)
    return address


def _file_under_account(rpc: RpcClient, address: str) -> None:
    """Keep the address in the account, so wallet.dat alone can find it again."""
    try:
        rpc.call("setaccount", address, IDENTITY_ACCOUNT)
    except Exception:
        pass          # cosmetic: the pin in the store is what this run relies on


def _wallet_can_sign(rpc: RpcClient, address: str) -> bool:
    try:
        info = rpc.call("validateaddress", address) or {}
    except Exception:
        return False
    return bool(info.get("ismine"))


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
