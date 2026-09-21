"""Seed phrases: twenty-four words that are a wallet.

BIP39, not something of our own. The point of a seed phrase is that it works
somewhere else -- a person who writes down twelve or twenty-four words
expects to type them into another wallet one day and find their coins, and a
scheme of our own invention breaks that promise quietly, years later, when
nobody is left to explain it. So: the official English wordlist, the
official checksum, the official derivation, checked against the official
test vectors (`tests/test_seed.py`).

There is already a six-word passphrase generator in `messaging/wordlist.py`.
It is not this and does not become this: it protects a key FILE on one
machine, has no checksum and no standard behind it. This is the wallet
itself.

**What this module does and does not do.** It owns the phrase, the seed, and
everything that can be derived with a hash function: the BIP32 master key,
hardened children, and the messaging identity. It does NOT derive coin
addresses or sign anything, because both need secp256k1 point arithmetic and
this application deliberately depends on two packages. In the design the
phrase was written for (docs/multi-user.md), the coin keys and the signing
live in the browser, where a library for that is ordinary -- and the node
never holds them anyway.

Hardened derivation only, for the same reason: `CKDpriv` for a hardened
child is HMAC-SHA512 over the private key, which is stdlib, while a
non-hardened child needs the public point. Every path this application wants
is hardened up to the account level, and what happens below that is the
browser's business.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import unicodedata
from pathlib import Path

#: The official BIP39 English list, vendored rather than fetched: a wallet
#: whose restore depends on a website is a wallet that stops restoring.
#: sha256 2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda,
#: 2048 words, sorted, unique in their first four letters -- all four
#: properties are asserted in the tests, so a corrupted copy is a failure
#: rather than a phrase nobody else can read.
WORDLIST_PATH = Path(__file__).with_name("bip39-english.txt")

WORDS: tuple[str, ...] = tuple(
    WORDLIST_PATH.read_text(encoding="utf-8").split())

_INDEX = {word: number for number, word in enumerate(WORDS)}

#: Twenty-four words. Twelve is standard and fine; this is the default
#: because the difference in what somebody has to write down is small and
#: the difference in what it protects is not.
DEFAULT_STRENGTH = 256

#: What BIP39 allows: 128 to 256 bits, in steps of 32.
STRENGTHS = (128, 160, 192, 224, 256)


class SeedError(Exception):
    """The phrase is not one anybody could restore a wallet from."""


def generate(strength: int = DEFAULT_STRENGTH) -> str:
    """A new phrase, from the system's own randomness.

    `secrets` rather than `random`: this is the only copy of somebody's
    wallet that will ever exist.
    """
    if strength not in STRENGTHS:
        raise SeedError(f"a seed is {', '.join(map(str, STRENGTHS))} bits, not "
                        f"{strength}")
    return from_entropy(secrets.token_bytes(strength // 8))


def from_entropy(entropy: bytes) -> str:
    """The phrase for exactly these bytes. The BIP39 construction:

    checksum = first (bits/32) bits of sha256(entropy), appended to the
    entropy; the result is read eleven bits at a time, each group naming a
    word.
    """
    if len(entropy) * 8 not in STRENGTHS:
        raise SeedError(f"{len(entropy)} bytes is not a seed length")
    checksum_bits = len(entropy) * 8 // 32
    digest = hashlib.sha256(entropy).digest()
    bits = (int.from_bytes(entropy, "big") << checksum_bits) | (
        digest[0] >> (8 - checksum_bits))
    total = len(entropy) * 8 + checksum_bits
    return " ".join(
        WORDS[(bits >> shift) & 0x7FF]
        for shift in range(total - 11, -1, -11))


def normalise(phrase: str) -> str:
    """The phrase as it is meant to be compared and hashed.

    NFKD and single spaces, because that is what the specification hashes.
    A phrase that differs only in spacing or in how an accent was typed is
    the same phrase, and must open the same wallet.
    """
    return " ".join(unicodedata.normalize("NFKD", phrase or "").lower().split())


def validate(phrase: str) -> str:
    """Return the phrase, normalised, or say exactly what is wrong with it.

    Every failure here is somebody typing their wallet back in, which is the
    worst moment to be unhelpful: the message names the problem and, for a
    word that is not in the list, names the word.
    """
    said = normalise(phrase)
    if not said:
        raise SeedError("write the words in, separated by spaces.")
    words = said.split()
    if len(words) not in (12, 15, 18, 21, 24):
        raise SeedError(
            f"a seed phrase is 12, 15, 18, 21 or 24 words; this is "
            f"{len(words)}.")
    unknown = [word for word in words if word not in _INDEX]
    if unknown:
        raise SeedError(
            f"not a word from the list: {', '.join(unknown[:3])}"
            f"{'…' if len(unknown) > 3 else ''}. Every word comes from the "
            f"standard 2048, and the first four letters are enough to "
            f"identify one.")
    bits = 0
    for word in words:
        bits = (bits << 11) | _INDEX[word]
    total = len(words) * 11
    checksum_bits = total // 33
    entropy_bits = total - checksum_bits
    entropy = (bits >> checksum_bits).to_bytes(entropy_bits // 8, "big")
    said_checksum = bits & ((1 << checksum_bits) - 1)
    digest = hashlib.sha256(entropy).digest()
    if said_checksum != digest[0] >> (8 - checksum_bits):
        raise SeedError(
            "those are all real words, but not in an order this can be. "
            "A seed phrase carries a checksum, so a single mistyped or "
            "swapped word is caught here rather than opening an empty "
            "wallet. Check the order against what you wrote down.")
    return said


def entropy_of(phrase: str) -> bytes:
    """The bytes behind a phrase, once it is known to be valid."""
    words = validate(phrase).split()
    bits = 0
    for word in words:
        bits = (bits << 11) | _INDEX[word]
    total = len(words) * 11
    checksum_bits = total // 33
    return (bits >> checksum_bits).to_bytes((total - checksum_bits) // 8, "big")


def to_seed(phrase: str, passphrase: str = "") -> bytes:
    """The 64-byte seed, as every other wallet computes it.

    PBKDF2-HMAC-SHA512, 2048 iterations, salt "mnemonic" plus an optional
    passphrase. The passphrase is part of the WALLET, not a password for
    this machine: a different one opens a different wallet, silently and
    for ever, which is why nothing in this application asks for one by
    default.
    """
    return hashlib.pbkdf2_hmac(
        "sha512",
        validate(phrase).encode("utf-8"),
        ("mnemonic" + unicodedata.normalize("NFKD", passphrase or "")).encode("utf-8"),
        2048,
    )


# --- BIP32, the hardened half -------------------------------------------------

HARDENED = 0x8000_0000


def master(seed: bytes) -> tuple[bytes, bytes]:
    """(key, chain code) for the root, from a 64-byte seed."""
    if len(seed) < 16:
        raise SeedError("that is not a seed")
    digest = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    return digest[:32], digest[32:]


def child(key: bytes, chain: bytes, index: int) -> tuple[bytes, bytes]:
    """One hardened child. Non-hardened is deliberately not implemented.

    A hardened child is HMAC-SHA512 over 0x00 || key || index, which needs
    no elliptic curve; a normal child needs the public point, and that is
    the browser's job in this design. Refusing loudly beats deriving
    something subtly wrong.
    """
    if index < HARDENED:
        raise SeedError(
            "only hardened derivation is available here: a normal child "
            "needs secp256k1, which is the browser's half of this design "
            "(docs/multi-user.md)")
    data = b"\x00" + key + index.to_bytes(4, "big")
    digest = hmac.new(chain, data, hashlib.sha512).digest()
    # The proper BIP32 step adds the left half to the parent key modulo the
    # curve order. Without curve arithmetic that addition cannot be done
    # here -- so this is NOT a BIP32 key and must never be used as one for
    # coins. It is a deterministic tree for keys that are not secp256k1,
    # which is what the messaging identity needs, and the name says so.
    return digest[:32], digest[32:]


def path(seed: bytes, *indexes: int) -> tuple[bytes, bytes]:
    """Walk a hardened path from a seed."""
    key, chain = master(seed)
    for index in indexes:
        key, chain = child(key, chain, index if index >= HARDENED
                           else index + HARDENED)
    return key, chain


#: The arcade's own branch of the tree, deliberately outside the coin space.
#:
#: BIP44 spends under m/44'/<coin>'/<account>', so m/44'/1'/0' is the testnet
#: account key of every ordinary wallet -- and `child()` above is NOT real
#: BIP32 (no curve addition), so a key derived here at a BIP44 path would be
#: a different 32 bytes wearing a path that says it spends coins. Two things
#: called the same name is how a wallet loses money quietly years later.
#:
#: So: purpose 24946', which is 0x6172, "ar". No BIP-43 purpose in use is
#: near it, the coin keys stay on the standard path where another wallet can
#: find them, and nothing derived here can ever be confused for one of them.
ARCADE_PURPOSE = 24946

#: The X25519 key that reads somebody's messages.
MESSAGING_BRANCH = (ARCADE_PURPOSE, 0, 0)

#: The Ed25519 key that holds a seat on a node (arcade/accounts.py). Its own
#: branch because it is used differently from every other key here: it signs
#: a challenge from a server on demand, many times a day, with no human
#: confirmation. A key used that way must not also be the key that reads
#: messages or spends coins.
LOGIN_BRANCH = (ARCADE_PURPOSE, 1, 0)


def messaging_key(phrase: str, passphrase: str = "") -> bytes:
    """The 32 bytes this wallet's messaging identity is built from.

    Same phrase, same identity, on any machine -- which is what makes a
    phrase a backup of the WHOLE wallet rather than only of its coins.
    """
    key, _ = path(to_seed(phrase, passphrase), *MESSAGING_BRANCH)
    return key


def login_key(phrase: str, passphrase: str = "") -> bytes:
    """The Ed25519 seed this wallet signs into a node with.

    32 bytes, which is exactly what an Ed25519 private key is -- PyNaCl's
    `SigningKey` takes it as it stands, and the browser wraps it in the
    fixed PKCS#8 prefix that `crypto.subtle.importKey` expects. The public
    half is the account id in `arcade/accounts.py`.

    Derived rather than generated, so a seat is part of what the phrase
    restores: the same twenty-four words open the same account on any node
    that has a space, which is the portability the whole design rests on.
    """
    key, _ = path(to_seed(phrase, passphrase), *LOGIN_BRANCH)
    return key


def login_pubkey(phrase: str, passphrase: str = "") -> str:
    """The hex public key a node knows this wallet by."""
    from nacl.signing import SigningKey
    return SigningKey(login_key(phrase, passphrase)).verify_key.encode().hex()
