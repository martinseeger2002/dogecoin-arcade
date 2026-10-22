"""Wallets an account imports: the node's half (plan item 18, 2026-10-20).

"We should be able to import any with private key or 12 word seed phrase, even if that wallet has not been used in
Dogecoin arcade." The key or the phrase never comes here. The browser derives the wallet's address, seals the secret
in a second record under the account's password (accounts.ImportedWallets), and tells this node only the ADDRESS --
with a signature by that address's key over the account and the address, so an account can register an address only
when it holds the key. The node then watches the address (utxos.watch), reads back the coins it already held, and
builds unsigned sends from it, exactly as it does for the account's own address. Only the browser can sign.

What lives here is what the routes in web/app.py share and the tests hold still: the proof, the address a key makes
for each kind, the per-account list kept in the node's settings, and how a wait is said.
"""

from __future__ import annotations

import hashlib
import json
import time

from .script import b58check_encode, hash160, p2sh_p2wpkh_address, p2wpkh_address

#: How many imported addresses one account may have on one chain (the operator's design, 2026-10-21).
CAP_PER_CHAIN = 20
#: How many imports (registrations) one account may make in a day, every chain together: each can start a read-back
#: of the chain, and an account that imported and removed wallets in a loop would keep every other import waiting.
PER_DAY = 40

#: The address kinds an import may be: P2PKH on every chain; on a test chain with SegWit (L test), its two SegWit
#: kinds too, the same ones the account's own words offer there (wallet.js ADDRESS_KINDS).
KINDS = ("p2pkh", "p2wpkh", "p2sh-p2wpkh")

#: The longest name a wallet may have.
NAME_MAX = 40


class ImportRefused(ValueError):
    """An import this node will not take, said so a person can act on it."""


def setting_key(network: str, account: str) -> str:
    """Where one account's imported addresses on one chain are listed (a JSON list in the node's settings)."""
    return f"extra_address:{network}:{(account or '').lower()}"


def name_key(network: str, account: str) -> str:
    """The account's MAIN wallet's name on one chain: not a secret, so a setting (an imported wallet's name is in its
    sealed record)."""
    return f"wallet_name:{network}:{(account or '').lower()}"


def entries(raw) -> list[dict]:
    """The listed addresses, from what the setting holds: a list of {"a": address, "k": kind, "p": pubkey hex,
    "t": when}. Anything else is read as nothing rather than trusted."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    out = []
    for one in raw if isinstance(raw, list) else []:
        if isinstance(one, dict) and isinstance(one.get("a"), str) and one.get("k") in KINDS:
            out.append({"a": one["a"], "k": one["k"], "p": str(one.get("p") or ""), "t": int(one.get("t") or 0)})
    return out


def proof_message(account: str, network: str, address: str, pubkey: str) -> bytes:
    """What the imported key signs to register its address for one account (coins.js `importProof` makes the same
    bytes). Naming the account makes a proof useless to anybody else; naming the chain, useless on another chain."""
    return (f"DogecoinArcade wallet import\n{(account or '').lower()}\n{network}\n{address}\n"
            f"{(pubkey or '').lower()}").encode()


def proof_digest(account: str, network: str, address: str, pubkey: str) -> bytes:
    """The 32 bytes the key signs: SHA-256 twice over the message, as a transaction's hash is."""
    return hashlib.sha256(hashlib.sha256(proof_message(account, network, address, pubkey)).digest()).digest()


def address_of(params, pubkey: bytes, kind: str) -> str:
    """The address `pubkey` makes on this chain, of this kind."""
    if kind == "p2pkh":
        if len(pubkey) not in (33, 65):
            raise ImportRefused("that is not a public key")
        return b58check_encode(params.pubkeyhash_version, hash160(pubkey))
    if kind not in KINDS:
        raise ImportRefused("that is not an address kind this arcade knows")
    if getattr(params, "real_coins", False) or not getattr(params, "bech32_hrp", ""):
        raise ImportRefused("SegWit wallets are on test chains with SegWit only, for now")
    if len(pubkey) != 33:
        raise ImportRefused("a SegWit address is made from a compressed public key")
    if kind == "p2wpkh":
        return p2wpkh_address(params, hash160(pubkey))
    return p2sh_p2wpkh_address(params, hash160(pubkey))


def verify_signature(digest: bytes, signature: bytes, pubkey: bytes) -> bool:
    """A low-S DER signature by `pubkey` over `digest` (the check the Doginal market uses)."""
    from .doginal_market import verify_signature as check
    return check(digest, signature, pubkey)


def check_proof(params, account: str, network: str, address: str, pubkey_hex: str, kind: str,
                signature_hex: str) -> bytes:
    """The public key, once the address is that key's of that kind and the key signed the proof; else
    ImportRefused. This is what makes an imported address the account's own: nobody registers an address whose key
    they do not hold."""
    try:
        pubkey = bytes.fromhex(str(pubkey_hex or ""))
        signature = bytes.fromhex(str(signature_hex or ""))
    except ValueError:
        raise ImportRefused("the import's key proof is not readable; try again") from None
    if address_of(params, pubkey, kind) != address:
        raise ImportRefused("that address is not this key's")
    if not signature or not verify_signature(proof_digest(account, network, address, pubkey.hex()),
                                             signature, pubkey):
        raise ImportRefused("the key did not sign this import, so it was not taken")
    return pubkey


def clean_name(text) -> str:
    """A wallet's name as kept: one line, trimmed, at most NAME_MAX characters."""
    return " ".join(str(text or "").split())[:NAME_MAX]


def say_wait(seconds: float | None) -> str:
    """How long, said the way a person reads it: "about 25 minutes", "about 2 hours", "a few minutes"."""
    if seconds is None:
        return "a few minutes"
    minutes = int(round(max(0.0, float(seconds)) / 60.0))
    if minutes <= 2:
        return "a few minutes"
    if minutes < 55:
        return f"about {minutes} minutes"
    hours = minutes / 60.0
    if hours < 1.5:
        return "about an hour"
    if hours < 36:
        return f"about {int(round(hours))} hours"
    return f"about {int(round(hours / 24.0))} days"


def recent(stamps, now: float | None = None) -> list[int]:
    """The registration times still inside the last day."""
    now = time.time() if now is None else now
    return [int(t) for t in (stamps if isinstance(stamps, list) else []) if isinstance(t, (int, float))
            and now - float(t) < 86400]


# --- the record, signed by the account, so it can travel between arcades (2026-10-21: "an imported wallet
# needs to be automatically broadcast to other arcades that you are signed in with") --------------------------------
#
# Every saved version of an account's sealed record of imported wallets carries a version number (1, 2, 3 ... one
# more each change) and a signature by the ACCOUNT'S LOGIN KEY (Ed25519, from its words: signin.js LOGIN_BRANCH, the
# key /auth/login checks) over `record_digest`. The browser makes it where the words are open (wallet.js
# changeImports); this node checks it on every save (POST /account/imported), and an arcade that is handed the record
# by another (mesh/importshare.py, GET /signin/{tag}/imported) checks it with the pubkey of ITS OWN vault row for the
# account, never one the message names. So no arcade can make, change or roll back another account's record: only the
# holder of the words can sign a version, and a version no higher than the one kept is never taken.

RECORD_PREFIX = b"DogecoinArcade imported wallets\n"
#: What a merge signs (the record's other copies it folded in, which this node may then drop).
MERGED_PREFIX = b"DogecoinArcade imported wallets merged\n"
#: What an imported address's key signs to have another arcade tell this one its coins.
COINS_PREFIX = b"DogecoinArcade imported coins\n"
#: How far a coins proof's minute may be from this node's clock, either way.
COINS_MINUTES = 5


def blob_hash(blob: str) -> str:
    """SHA-256 of the sealed record's text exactly as kept (UTF-8), as hex: what a version's signature names."""
    return hashlib.sha256(str(blob or "").encode()).hexdigest()


def record_digest(pubkey: str, version: int, blob_sha256_hex: str) -> bytes:
    """The 32 bytes the account's login key signs for one version of its record:
    sha256(b"DogecoinArcade imported wallets\\n" + pubkey hex + "\\n" + str(version) + "\\n" + sha256(blob) hex)
    (wallet.js `recordDigest` makes the same bytes)."""
    return hashlib.sha256(RECORD_PREFIX + (pubkey or "").lower().encode() + b"\n" + str(int(version)).encode()
                          + b"\n" + str(blob_sha256_hex or "").lower().encode()).digest()


def merged_digest(pubkey: str, version: int, digests) -> bytes:
    """What the login key signs to say the record's version `version` folds in those other copies (their blob
    hashes): sha256(MERGED_PREFIX + pubkey + "\\n" + str(version) + "\\n" + the hashes sorted, comma-joined)."""
    joined = ",".join(sorted(str(d).lower() for d in digests))
    return hashlib.sha256(MERGED_PREFIX + (pubkey or "").lower().encode() + b"\n" + str(int(version)).encode()
                          + b"\n" + joined.encode()).digest()


def _ed25519_ok(pubkey_hex: str, digest: bytes, signature_hex: str) -> bool:
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey
    try:
        key = bytes.fromhex(str(pubkey_hex or ""))
        signature = bytes.fromhex(str(signature_hex or ""))
        if len(key) != 32 or len(signature) != 64:
            return False
        VerifyKey(key).verify(digest, signature)
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False


def check_record(pubkey: str, version, blob: str, signature: str) -> int:
    """The version, once `signature` is the account `pubkey`'s login key's over this version of this blob; else
    ImportRefused. `pubkey` must come from the checker's own records (the session, or its own vault row)."""
    try:
        version = int(version)
    except (TypeError, ValueError):
        raise ImportRefused("the record's version is not a number") from None
    if version < 1:
        raise ImportRefused("a signed record's version starts at 1")
    if not _ed25519_ok(pubkey, record_digest(pubkey, version, blob_hash(blob)), signature):
        raise ImportRefused("your account's key did not sign this record, so it was not taken")
    return version


def check_merged(pubkey: str, version: int, digests, signature: str) -> None:
    if not _ed25519_ok(pubkey, merged_digest(pubkey, version, digests), signature):
        raise ImportRefused("your account's key did not sign that merge")


def account_hash(pubkey: str) -> str:
    """How arcades name an account to each other over the mesh: not its pubkey, so a list of them says nothing to an
    arcade that does not already hold that account (it can only match the ones it has)."""
    return hashlib.sha256(b"DogecoinArcade imported account\n" + (pubkey or "").lower().encode()).hexdigest()[:32]


def coins_digest(tag: str, network: str, address: str, asker_host: str, minute: int) -> bytes:
    """What an imported address's key signs so one named arcade may ask another for its coins, this minute:
    sha256(b"DogecoinArcade imported coins\\n" + tag + "\\n" + network + "\\n" + address + "\\n" + asker host + "\\n"
    + str(unix minute)). The tag, the chain, the asker and the minute are in it, so it is good for nothing else."""
    return hashlib.sha256(COINS_PREFIX + f"{(tag or '').lower()}\n{network}\n{address}\n{(asker_host or '').lower()}\n"
                                         f"{int(minute)}".encode()).digest()


def check_coins_proof(params, tag: str, network: str, address: str, asker_host: str, minute, pubkey_hex: str,
                      kind: str, signature_hex: str, now: float | None = None) -> bytes:
    """The public key, once the address is that key's and the key signed this ask within COINS_MINUTES of now."""
    try:
        minute = int(minute)
    except (TypeError, ValueError):
        raise ImportRefused("the proof's minute is not a number") from None
    here = int((time.time() if now is None else now) // 60)
    if abs(here - minute) > COINS_MINUTES:
        raise ImportRefused("that proof is not from the last few minutes")
    try:
        pubkey = bytes.fromhex(str(pubkey_hex or ""))
        signature = bytes.fromhex(str(signature_hex or ""))
    except ValueError:
        raise ImportRefused("the proof is not readable") from None
    if address_of(params, pubkey, kind) != address:
        raise ImportRefused("that address is not this key's")
    if not signature or not verify_signature(coins_digest(tag, network, address, asker_host, minute), signature,
                                             pubkey):
        raise ImportRefused("the address's key did not sign this")
    return pubkey
