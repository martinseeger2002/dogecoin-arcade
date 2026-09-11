"""Messaging identity keys: X25519, encrypted at rest with Argon2id.

These keys are deliberately **separate from wallet keys**:

  * a wallet key is exposed to signature-based attack every time it spends;
  * a wallet key cannot be rotated without moving funds;
  * compromise of a spending key would otherwise retroactively decrypt every
    message ever sent to its owner.

Nothing in this module ever touches a wallet or a private spending key.
"""

from __future__ import annotations

import hashlib
import math
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

import nacl.bindings as sodium
import nacl.public
import nacl.secret
import nacl.utils
from nacl.exceptions import CryptoError

from .wordlist import WORDS

_WORD_SET = frozenset(WORDS)

MAGIC = b"ARCK"
VERSION = 1
KDF_ARGON2ID = 1

SALT_BYTES = sodium.crypto_pwhash_SALTBYTES          # 16
SECRETBOX_NONCE = nacl.secret.SecretBox.NONCE_SIZE   # 24
KEY_BYTES = 32

# Argon2id cost. MODERATE (3 ops / 256 MiB) is the deliberate middle choice:
# INTERACTIVE (64 MiB) is too weak for a long-lived identity key, and SENSITIVE
# (1 GiB) fails or thrashes on phones and small VPSes. Measured at 1.30 s on a
# Celeron N5095A.
DEFAULT_OPS = sodium.crypto_pwhash_argon2id_OPSLIMIT_MODERATE
DEFAULT_MEM = sodium.crypto_pwhash_argon2id_MEMLIMIT_MODERATE


class KeyError_(Exception):
    """Key file is malformed, or the passphrase is wrong."""


@dataclass(frozen=True)
class Identity:
    """An X25519 messaging keypair."""

    secret: nacl.public.PrivateKey

    @property
    def public(self) -> nacl.public.PublicKey:
        return self.secret.public_key

    @property
    def public_bytes(self) -> bytes:
        return bytes(self.public)

    @property
    def fingerprint(self) -> str:
        """Short, human-checkable identifier: SHA-256(pubkey)[:8], grouped.

        Eight bytes is 64 bits -- far beyond casual collision, and short enough
        to read aloud over a phone call, which is the only thing that actually
        establishes identity (see docs/messaging/02-design.md §2).
        """
        return fingerprint_of(self.public_bytes)

    @classmethod
    def generate(cls) -> "Identity":
        return cls(nacl.public.PrivateKey.generate())

    @classmethod
    def from_secret_bytes(cls, raw: bytes) -> "Identity":
        if len(raw) != KEY_BYTES:
            raise KeyError_(f"secret key must be {KEY_BYTES} bytes, got {len(raw)}")
        return cls(nacl.public.PrivateKey(raw))

    def __repr__(self) -> str:  # never let a secret key reach a log or traceback
        return f"Identity(fingerprint={self.fingerprint!r}, secret=<redacted>)"


#: Six words from a 1296-word list is ~62 bits. Combined with Argon2id at
#: roughly 1.3 s per guess, brute force takes on the order of 10^11 years, so
#: more words would buy nothing a user would notice.
PASSPHRASE_WORDS = 6


def generate_passphrase(words: int = PASSPHRASE_WORDS) -> str:
    """A strong passphrase the user can actually write down.

    Generated rather than chosen because the common failure is a weak
    human-chosen passphrase, and this key has no recovery path. `secrets` is used
    rather than `random`: the latter is seeded predictably and is not fit for
    anything anyone could want to guess.

    The result is shown to the user **once** and never stored. Saving it beside
    the key file would leave the lock and its key in the same drawer, which is
    the whole thing the encryption at rest exists to prevent.
    """
    if words < 4:
        raise KeyError_("a generated passphrase needs at least four words")
    return "-".join(secrets.choice(WORDS) for _ in range(words))


def passphrase_bits(passphrase: str) -> float:
    """Rough entropy estimate, for telling the user where they stand.

    Only meaningful for passphrases in the generated format; a human-chosen one
    is estimated conservatively, since there is no way to know how predictable it
    really is.
    """
    parts = passphrase.split("-")
    if len(parts) >= 4 and all(p in _WORD_SET for p in parts):
        return len(parts) * math.log2(len(WORDS))
    # Conservative: assume roughly two bits per character for human-chosen text.
    return min(len(passphrase) * 2.0, 60.0)


def fingerprint_of(public_bytes: bytes) -> str:
    digest = hashlib.sha256(public_bytes).digest()[:8].hex()
    return " ".join(digest[i : i + 4] for i in range(0, 16, 4))


def derive_key(passphrase: str, salt: bytes, ops: int, mem: int) -> bytes:
    """Argon2id, with the cost parameters supplied rather than assumed."""
    return sodium.crypto_pwhash_alg(
        KEY_BYTES,
        passphrase.encode("utf-8"),
        salt,
        ops,
        mem,
        sodium.crypto_pwhash_ALG_ARGON2ID13,
    )


def encrypt_identity(
    identity: Identity,
    passphrase: str,
    ops: int = DEFAULT_OPS,
    mem: int = DEFAULT_MEM,
) -> bytes:
    """Serialize an identity to its encrypted on-disk form (102 bytes).

    Layout:
        magic 4 | version 1 | kdf 1 | ops 4 | mem 4 | salt 16 | nonce 24 | ct 48

    The KDF parameters are **stored, not hard-coded**. Raising the cost later
    must not make existing key files unreadable -- baking today's parameters
    into the reader is the usual way that happens.
    """
    if not passphrase:
        raise KeyError_("refusing to encrypt an identity with an empty passphrase")

    salt = nacl.utils.random(SALT_BYTES)
    key = derive_key(passphrase, salt, ops, mem)
    nonce = nacl.utils.random(SECRETBOX_NONCE)
    ciphertext = nacl.secret.SecretBox(key).encrypt(bytes(identity.secret), nonce).ciphertext

    return (
        MAGIC
        + bytes([VERSION, KDF_ARGON2ID])
        + ops.to_bytes(4, "big")
        + mem.to_bytes(4, "big")
        + salt
        + nonce
        + ciphertext
    )


def decrypt_identity(blob: bytes, passphrase: str) -> Identity:
    """Recover an identity from its encrypted form.

    A wrong passphrase and a corrupted file are deliberately indistinguishable
    from the outside: both raise the same error with the same message, so the
    error text cannot be used as an oracle.
    """
    if len(blob) != 102:
        raise KeyError_(f"key file must be 102 bytes, got {len(blob)}")
    if blob[:4] != MAGIC:
        raise KeyError_("not a DogecoinArcade key file (bad magic)")

    version, kdf = blob[4], blob[5]
    if version != VERSION:
        raise KeyError_(f"unsupported key file version {version}")
    if kdf != KDF_ARGON2ID:
        raise KeyError_(f"unsupported KDF id {kdf}")

    ops = int.from_bytes(blob[6:10], "big")
    mem = int.from_bytes(blob[10:14], "big")
    salt = blob[14:30]
    nonce = blob[30:54]
    ciphertext = blob[54:]

    key = derive_key(passphrase, salt, ops, mem)
    try:
        secret = nacl.secret.SecretBox(key).decrypt(ciphertext, nonce)
    except CryptoError:
        raise KeyError_("wrong passphrase, or the key file is corrupt") from None
    return Identity.from_secret_bytes(secret)


def save_identity(
    path: Path, identity: Identity, passphrase: str, ops: int = DEFAULT_OPS, mem: int = DEFAULT_MEM
) -> None:
    """Write the encrypted key file with 0600 permissions, refusing to clobber."""
    path = Path(path)
    if path.exists():
        raise KeyError_(f"{path} already exists; refusing to overwrite a key file")
    blob = encrypt_identity(identity, passphrase, ops, mem)
    # Create with restrictive permissions from the outset rather than chmod-ing
    # after, which would leave a window where the file is world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)


def load_identity(path: Path, passphrase: str) -> Identity:
    path = Path(path)
    if not path.exists():
        raise KeyError_(f"no key file at {path}")
    return decrypt_identity(path.read_bytes(), passphrase)
