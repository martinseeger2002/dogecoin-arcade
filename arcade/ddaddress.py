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
Mainnet keys (coin type 3) are not read this way: a mainnet DigiDollar is paid to a DD... address as typed.
"""
from __future__ import annotations

from . import taproot
from .ddlib import ddtx
from .script import b58decode, hash160

#: The address versions are the DigiDollar module's (ddlib.ddtx, the module's own file).
VERSIONS = ddtx.DD_VERSIONS
#: The chains whose coin key is the name chain's own (coin type 1): an @name resolves to an address only on these.
BY_NAME = ("dgbtest",)


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
