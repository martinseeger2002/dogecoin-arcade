"""Telling other nodes that a release exists.

Automatic updates poll, and a poll is a compromise: often enough to matter,
rare enough not to hammer the site. A consensus rule starting at a height does
not care about that compromise -- the machines that have not looked yet are
exactly the ones that will read the block wrong.

So a release is announced on the public board. The notice carries no authority
and is not meant to: it names a revision, a node that sees one looks NOW
instead of in six hours, and what actually gets installed is still decided by
the signature on the manifest. The worst a forged notice can do is make
somebody fetch a manifest they would have fetched anyway.
"""

import pytest

from arcade import release


def test_a_notice_says_one_thing():
    assert release.notice("32c54dc") == "arcade-release 32c54dc"
    assert release.revision_in("arcade-release 32c54dc") == "32c54dc"
    assert release.revision_in(release.notice("ABCDEF0")) == "abcdef0"


def test_anything_else_is_not_a_notice():
    """A public board is a public board; most of what is on it is people."""
    for text in ("", "hello everyone", "arcade-release", "arcade-release zzz",
                 "arcade-release 32c", "arcade-release 32c54dc extra",
                 "please run arcade-release 32c54dc", None):
        assert release.revision_in(text) == "", text


def test_the_channel_and_the_tag_are_named_once():
    """Both ends compare against the same two constants, so they cannot
    drift: the node that announces checks that it holds the tag, and every
    node that reads checks the sender against the same tag."""
    assert release.RELEASE_CHANNEL == "releases"
    assert release.RELEASE_TAG == "bigchiefenergy"


def test_only_the_tag_holder_is_believed(tmp_path):
    """Anybody may post on a public board. Only one address is announcing
    releases, and it is the one that published the tag -- read from the chain
    rather than pinned, so it moves when its holder moves it."""
    from arcade.messaging.store import MessageStore

    store = MessageStore(tmp_path / "m.sqlite")
    assert store.address_for_tag(release.RELEASE_TAG) == "", "nobody yet"

    store.add_key_announcement("aa" * 32, "nChief", b"\x01" * 32, "fp", 100, 1700,
                               tag=release.RELEASE_TAG)
    assert store.address_for_tag(release.RELEASE_TAG) == "nChief"
    assert store.address_for_tag("@" + release.RELEASE_TAG) == "nChief", "with or without the @"
    assert store.address_for_tag("somebodyelse") == ""

    # A tag moves when its holder says so, and the newest announcement wins.
    store.add_key_announcement("bb" * 32, "nChiefToo", b"\x02" * 32, "fp2", 200, 1800,
                               tag=release.RELEASE_TAG)
    assert store.address_for_tag(release.RELEASE_TAG) == "nChiefToo"
