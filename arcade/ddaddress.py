"""DigiDollar addresses for the wallet: an account's own, and an @name's (2026-11-06).

SadFrog: "make it so that everyone can have a digidollar test and digidollar address along side their wallet receives.
and add them to send. I should, now that i have 100 digidollar test be able to send it to @apple".

A DigiDollar address is base58check of a two-byte version ("DD" mainnet, "TD" test chain) and the Taproot output key
of the owner's coin key (DigiByte Core CDigiDollarAddress; the DigiDollar module's `dd_address`). So it needs the
payee's PUBLIC KEY, not an address hash, and an @name gives only the hash of its Pepecoin testnet address.

The key is on the chain already: every name was claimed by a transaction its address signed, and a pay-to-key-hash
input carries the key itself. Test chains share one coin key (m/44'/1'/0'/0/0, coins.js coinPath), so the key that
signed the claim on the Pepecoin test chain is the payee's key on the DigiByte test chain too. A key read so is used
only when its hash IS the name's address, so a transaction with someone else's inputs can never redirect a payment.
Mainnet keys (coin type 3) are not read this way: nothing on the name chain spends with them.

On mainnet (2026-11-08, SadFrog: names should resolve there too) an account publishes its mainnet coin key itself, in
its profile: the key announcement's ANNOUNCE_TAG_DD_KEY section (messaging/envelope.py), which the Wallet adds to every
announcement once the account has a DigiByte mainnet wallet. A record is used only when `proved` says so: the key signed
a proof naming the network, the name's address and itself (so nobody can publish somebody else's key), the announcement
transaction was signed by the name's own address (so nobody can publish a key for somebody else's name), and the key's
hash is the address that announcement published for the network, when it published one.
"""
from __future__ import annotations

from . import taproot
from .ddlib import ddtx
from .script import b58decode, hash160

#: The address versions are the DigiDollar module's (ddlib.ddtx, the module's own file).
VERSIONS = ddtx.DD_VERSIONS
#: The chains whose coin key is the name chain's own (coin type 1): an @name resolves to an address only on these.
BY_NAME = ("dgbtest",)
#: The chains an @name resolves on only through a key its owner published and proved (ANNOUNCE_TAG_DD_KEY).
PROVED = ("dgb",)
#: What a proof signs, before the network, the name's address hash and the key (proof_message). The protocol's name
#: stays DogecoinArcade on every face (COLLAB-NOTES, "What must keep the name").
PROOF_TAG = b"DogecoinArcade DigiDollar key\n"


def address(network: str, pubkey: bytes) -> str:
    """The DigiDollar address of a compressed coin key on `network`."""
    if network not in VERSIONS:
        raise ValueError(f"{network} has no DigiDollar addresses")
    return ddtx.dd_address(network, taproot.xonly(pubkey))


def _check(body: bytes) -> bytes:
    import hashlib
    return hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]


def network_of(text: str) -> str | None:
    """Which chain a typed DigiDollar address is for ("dgb" / "dgbtest"), or None if it is not one."""
    try:
        raw = b58decode(str(text).strip())
    except Exception:
        return None
    if len(raw) != 38 or _check(raw[:-4]) != raw[-4:]:
        return None
    return next((net for net, v in VERSIONS.items() if raw[:2] == v), None)


def key_in(tx: dict, key_hash: bytes) -> bytes | None:
    """The compressed key a decoded transaction's inputs show for `key_hash` (a pay-to-key-hash input's last push), or
    None. Only a key whose hash160 is `key_hash` is returned."""
    for vin in tx.get("vin") or []:
        parts = str((vin.get("scriptSig") or {}).get("asm", "")).split()
        if len(parts) < 2:
            continue
        try:
            key = bytes.fromhex(parts[-1])
        except ValueError:
            continue
        if len(key) == 33 and key[0] in (2, 3) and hash160(key) == key_hash:
            return key
    return None


def proof_message(network: str, name_hash: bytes, key: bytes) -> bytes:
    """The 32 bytes a published coin key signs (BIP-340): sha256 of PROOF_TAG, the network, a newline, the hash160 of
    the name chain address it is published for, and the 33-byte compressed key. coins.js `ddKeyProof` makes the same."""
    import hashlib
    return hashlib.sha256(PROOF_TAG + network.encode("ascii") + b"\n" + bytes(name_hash) + bytes(key)).digest()


def proof_ok(network: str, name_hash: bytes, key: bytes, sig: bytes) -> bool:
    """Whether `sig` is `key`'s own BIP-340 signature over proof_message: the key's holder put it there for that name."""
    if len(key) != 33 or key[0] not in (2, 3) or len(sig) != 64 or len(name_hash) != 20:
        return False
    try:
        return taproot.verify(taproot.xonly(key), proof_message(network, name_hash, key), sig)
    except Exception:                                      # noqa: BLE001 -- a key off the curve proves nothing
        return False


def signed_by(tx: dict, key_hash: bytes) -> bool:
    """Whether a decoded transaction has a pay-to-key-hash input signed by the key whose hash is `key_hash`, under
    SIGHASH_ALL, so that key's holder signed every output, the announcement among them. The chain checked the
    signature itself when it took the transaction."""
    for vin in tx.get("vin") or []:
        parts = str((vin.get("scriptSig") or {}).get("asm", "")).split()
        if len(parts) != 2 or not parts[0].endswith("[ALL]"):
            continue
        try:
            key = bytes.fromhex(parts[1])
        except ValueError:
            continue
        if len(key) == 33 and key[0] in (2, 3) and hash160(key) == key_hash:
            return True
    return False


def proved(network: str, name_hash: bytes, record: dict, tx: dict, published: bytes | None = None) -> bytes | None:
    """The coin key a published record proves for the name whose address hash is `name_hash`, or None.

    `record` is {"key": hex, "sig": hex} as the store keeps it, `tx` the decoded announcement that carried it, and
    `published` the hash160 of the address that announcement gave for `network`, if it gave one. All must hold:
    the key's proof, the name's own signature on the transaction, and the key being that published address's."""
    try:
        key, sig = bytes.fromhex(str(record.get("key", ""))), bytes.fromhex(str(record.get("sig", "")))
    except (AttributeError, ValueError):
        return None
    if not proof_ok(network, name_hash, key, sig):
        return None
    if published is not None and hash160(key) != published:
        return None
    if not signed_by(tx, name_hash):
        return None
    return key
