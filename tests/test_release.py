"""Whether a release is one of ours.

The installer has always checked the archive against a sum published beside it,
and has always said in its own comments what that is worth: both halves come
from the same website, so whoever can replace one can replace the other. It
catches a truncated download and nothing else.

That was defensible while a person typed the update command and could look at
what they were installing. It stops being defensible the moment the machine
does it on its own, so a release is signed now, with a key that is not on the
website, and every installation carries the public half in its own code.
"""

import json

import pytest
from nacl.signing import SigningKey

from arcade import release


@pytest.fixture
def key():
    return SigningKey.generate()


def test_a_signed_release_verifies(key):
    manifest = release.sign(key.encode().hex(), "abc1234", "de" * 32)
    body = release.verify(manifest, key.verify_key.encode().hex())
    assert body["revision"] == "abc1234" and body["sha256"] == "de" * 32
    assert release.verify(json.dumps(manifest), key.verify_key.encode().hex()) == body


def test_changing_anything_breaks_it(key):
    """Every field is inside the signature, the hash most of all: the hash is
    what the download is checked against, so a hash that could be edited would
    make the signature decorative."""
    manifest = release.sign(key.encode().hex(), "abc1234", "de" * 32)
    trusted = key.verify_key.encode().hex()
    for field, value in (("sha256", "ff" * 32), ("revision", "bad0000"),
                         ("published", 1.0)):
        with pytest.raises(release.ReleaseError, match="not signed by the key"):
            release.verify({**manifest, field: value}, trusted)


def test_another_key_is_not_our_key(key):
    """The point of the whole exercise: the website can serve anything it
    likes, and without this key it is not a release."""
    attacker = SigningKey.generate()
    manifest = release.sign(attacker.encode().hex(), "abc1234", "de" * 32)
    with pytest.raises(release.ReleaseError, match="not signed by the key"):
        release.verify(manifest, key.verify_key.encode().hex())
    # Including when the manifest helpfully names the key it was signed with.
    assert manifest["public_key"] == attacker.verify_key.encode().hex()
    with pytest.raises(release.ReleaseError):
        release.verify(manifest, key.verify_key.encode().hex())


def test_it_fails_closed(key):
    """No manifest, no signature, no pinned key: install nothing."""
    trusted = key.verify_key.encode().hex()
    for bad in ("", "{", "[]", "null", json.dumps({"revision": "a"})):
        with pytest.raises(release.ReleaseError):
            release.verify(bad, trusted)
    manifest = release.sign(key.encode().hex(), "abc1234", "de" * 32)
    with pytest.raises(release.ReleaseError, match="no release signing key pinned"):
        release.verify(manifest, "")


def test_an_old_release_cannot_be_served_back(key):
    """A signed release stays signed for ever, so an attacker who can serve
    files but not sign them can still serve last month's -- the one whose bug
    they know. `published` is inside the signature, so it cannot be edited to
    look new."""
    old = release.sign(key.encode().hex(), "old1234", "aa" * 32, published=1000.0)
    new = release.sign(key.encode().hex(), "new1234", "bb" * 32, published=2000.0)
    assert release.is_newer(new, 1000.0)
    assert not release.is_newer(old, 2000.0)
    assert not release.is_newer(old, 1000.0), "the same release is not newer"
    assert release.is_newer(old, None), "a fresh installation has nothing to compare"


def test_the_pinned_key_is_a_real_key():
    """It ships in the code. A typo here is an installation that can never
    update again."""
    assert len(release.PUBLIC_KEY) == 64
    from nacl.signing import VerifyKey
    VerifyKey(bytes.fromhex(release.PUBLIC_KEY))


def test_the_signature_survives_a_round_trip_through_json(key):
    """The manifest is published as a file and read back as text. Floats are
    the usual way a signature over "whatever json.dumps did" stops verifying."""
    manifest = release.sign(key.encode().hex(), "abc1234", "de" * 32,
                            published=1789603795.7431774)
    written = json.dumps(manifest, sort_keys=True)
    assert release.verify(written, key.verify_key.encode().hex())
