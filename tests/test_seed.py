"""Seed phrases, against the official vectors rather than our own reading.

The point of BIP39 is that a phrase works in somebody else's wallet years
from now. A test that only checks this code against itself would pass just
as happily on a scheme of our own invention, which is the one outcome that
must not slip through — so the vectors and the wordlist are the published
ones, carried in the repository beside these tests.
"""

import hashlib
import json
import pathlib

import pytest

from arcade import seed

VECTORS = json.loads(
    (pathlib.Path(__file__).with_name("bip39-vectors.json")).read_text()
)["english"]


_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_decode(text: str) -> bytes:
    """Enough base58 to read a published xprv. Not for production use: the
    application's own encoder lives in arcade/script.py."""
    number = 0
    for character in text:
        number = number * 58 + _ALPHABET.index(character)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * pad + raw


def test_the_wordlist_is_the_published_one():
    """Not "a list of 2048 words" — THE list. A phrase built from anything
    else is a phrase no other wallet can read, and nothing would say so
    until somebody tried to restore it."""
    raw = seed.WORDLIST_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == \
        "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"
    assert len(seed.WORDS) == 2048
    assert list(seed.WORDS) == sorted(seed.WORDS), "sorted, as the spec says"
    assert len(set(seed.WORDS)) == 2048, "and every word once"
    assert len({word[:4] for word in seed.WORDS}) == 2048, \
        "four letters identify a word, which is what makes them writable"


@pytest.mark.parametrize("entropy,phrase,expected_seed,xprv", VECTORS)
def test_the_official_vectors(entropy, phrase, expected_seed, xprv):
    """Every published English vector, all four fields.

    The fourth is the master key serialised as an xprv, which pins our BIP32
    root as well as the phrase: a seed that matched but a root that did not
    would be a wallet that restores everywhere except in this software.
    """
    assert seed.from_entropy(bytes.fromhex(entropy)) == phrase
    assert seed.to_seed(phrase, "TREZOR").hex() == expected_seed
    assert seed.entropy_of(phrase).hex() == entropy

    key, chain = seed.master(bytes.fromhex(expected_seed))
    raw = _base58_decode(xprv)
    # xprv layout: version4 depth1 fingerprint4 childnumber4 chaincode32
    #              0x00 + key32, then a four-byte checksum.
    assert raw[13:45] == chain, "chain code"
    assert raw[46:78] == key, "private key"


def test_a_generated_phrase_is_twenty_four_words_and_valid():
    phrase = seed.generate()
    assert len(phrase.split()) == 24
    assert seed.validate(phrase) == phrase
    assert len(seed.entropy_of(phrase)) == 32


def test_every_allowed_length_round_trips():
    for strength in seed.STRENGTHS:
        phrase = seed.generate(strength)
        assert len(seed.entropy_of(phrase)) * 8 == strength


def test_two_phrases_are_never_the_same():
    assert len({seed.generate() for _ in range(20)}) == 20


def test_a_swapped_word_is_caught_by_the_checksum():
    """The whole reason a phrase carries one: a single mistake should be a
    message, not an empty wallet."""
    words = seed.generate().split()
    words[3], words[4] = words[4], words[3]
    with pytest.raises(seed.SeedError) as complaint:
        seed.validate(" ".join(words))
    assert "checksum" in str(complaint.value)


def test_a_word_that_is_not_in_the_list_is_named():
    """Somebody typing their wallet back in is owed the actual problem."""
    words = seed.generate().split()
    words[2] = "bananas"
    with pytest.raises(seed.SeedError) as complaint:
        seed.validate(" ".join(words))
    assert "bananas" in str(complaint.value)


def test_the_wrong_number_of_words_says_how_many_there_are():
    with pytest.raises(seed.SeedError) as complaint:
        seed.validate("abandon abandon about")
    assert "this is 3" in str(complaint.value)


def test_spacing_and_case_do_not_change_a_phrase():
    """A phrase copied out of a document must open the same wallet."""
    phrase = seed.generate()
    messy = "  " + phrase.upper().replace(" ", "   ") + "\n"
    assert seed.validate(messy) == phrase
    assert seed.to_seed(messy) == seed.to_seed(phrase)


def test_a_passphrase_opens_a_different_wallet_silently():
    """Which is exactly why nothing asks for one by default."""
    phrase = seed.generate()
    assert seed.to_seed(phrase) != seed.to_seed(phrase, "holiday")


def test_the_messaging_identity_follows_the_phrase():
    """A phrase is a backup of the whole wallet, not only of its coins."""
    phrase = seed.generate()
    assert seed.messaging_key(phrase) == seed.messaging_key(phrase)
    assert len(seed.messaging_key(phrase)) == 32
    assert seed.messaging_key(phrase) != seed.messaging_key(seed.generate())
    # And it is not simply the seed handed over.
    assert seed.messaging_key(phrase) not in seed.to_seed(phrase)


def test_normal_derivation_is_refused_rather_than_approximated():
    """A non-hardened child needs secp256k1, which this application does not
    carry. Refusing loudly beats deriving something subtly wrong."""
    key, chain = seed.master(seed.to_seed(seed.generate()))
    with pytest.raises(seed.SeedError) as complaint:
        seed.child(key, chain, 0)
    assert "hardened" in str(complaint.value)


def test_a_path_is_deterministic_and_branch_dependent():
    material = seed.to_seed(seed.generate())
    assert seed.path(material, 44, 1, 0) == seed.path(material, 44, 1, 0)
    assert seed.path(material, 44, 1, 0) != seed.path(material, 44, 1, 1)
