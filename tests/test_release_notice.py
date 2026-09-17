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
    # Whatever the publishing node actually holds. It read "bigchiefenergy"
    # while the publisher held "notbigchiefenergy", so the check never passed
    # and the notice path was inert -- a pin at a name nobody has is not a
    # pin, it is an off switch nobody can see (D-085).
    assert release.RELEASE_TAG == "notbigchiefenergy"


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


def test_a_wallet_finds_its_tag_on_the_chain_that_holds_tags(tmp_path, monkeypatch):
    """`my_tag` asked the LEDGER chain. Tags are claimed on the chain the
    messages are on (D-032), so a wallet holding one was told it had none --
    and the node that publishes releases never announced one, because the
    check that gates announcing is "do I hold the release tag" (D-085)."""
    from arcade.web.state import AppState, ChainContext

    class Index:
        def __init__(self, tags):
            self.tags = tags

        def tag_of(self, address):
            return self.tags.get(address)

    state = AppState(
        home=tmp_path,
        messaging=ChainContext(network="regtest", role="messaging", label="T"),
        ledger=ChainContext(network="main", role="ledger", label="M"))
    indexes = {"regtest": Index({"nMe": "notbigchiefenergy"}), "main": Index({})}
    monkeypatch.setattr(type(state), "token_index",
                        lambda self, chain: indexes[chain.network])
    monkeypatch.setattr(type(state), "derived_address",
                        property(lambda self: "nMe"))
    monkeypatch.setattr(type(state), "token_chains",
                        property(lambda self: [state.ledger, state.messaging]))
    assert state.my_tag() == "notbigchiefenergy", \
        "the messaging chain, even when the ledger chain is listed first"


def test_both_sides_ask_the_same_table_who_holds_the_tag(tmp_path, monkeypatch):
    """The announce side read the tag table; the receive side read the tag an
    ANNOUNCEMENT states. They disagree the moment somebody renames -- an
    announcement is a statement by the key holder and goes stale -- so the
    publisher announced and every receiver answered "nobody holds that"
    before looking at a post.

    Invisible from the publishing machine, because the half that works is the
    half it runs. A test machine found it by reporting that its node did nothing
    (D-086).
    """
    from arcade.web.state import AppState, ChainContext
    from arcade.web.watcher import BlockWatcher

    class Index:
        def address_of(self, tag):
            return "nPublisher" if tag == release.RELEASE_TAG else None

    state = AppState(
        home=tmp_path,
        messaging=ChainContext(network="regtest", role="messaging", label="T"),
        ledger=ChainContext(network="main", role="ledger", label="M"))
    monkeypatch.setattr(type(state), "token_index", lambda self, chain: Index())
    monkeypatch.setattr(type(state), "token_chains",
                        property(lambda self: [state.ledger, state.messaging]))

    watcher = BlockWatcher(state)
    assert watcher._release_publisher() == "nPublisher", \
        "the claim on the chain, not what an announcement says about it"

    # And an announcement carrying a stale name cannot make it disagree,
    # because it is not consulted at all.
    from arcade.messaging.store import MessageStore
    store = MessageStore(tmp_path / "m.sqlite")
    store.add_key_announcement("aa" * 32, "nPublisher", b"\x01" * 32, "fp", 100,
                               1700, tag="theoldname")
    assert watcher._release_publisher() == "nPublisher"
