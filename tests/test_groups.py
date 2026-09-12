"""Public group posts, and the boundary they are allowed to cross.

Everything else in this application is sealed to one recipient. A group post is
deliberately the opposite: plain text on the chain, readable by anyone, for as
long as the chain exists. Because it carries no key material it may run on
mainnet, where it spends real coins -- and that exception has to stay exactly as
narrow as it is, which is most of what these tests are for.
"""

import pytest

from arcade.config import NETWORKS, MainnetRefused
from arcade.messaging import group as G
from arcade.messaging.envelope import EnvelopeError, TYPE_GROUP, Header
from arcade.messaging.sender import MessageSender


class FakeRpc:
    def call(self, *args, **kwargs):
        raise AssertionError("no RPC expected")


# --- the D-010 boundary -------------------------------------------------------


def test_encrypted_messaging_is_still_refused_on_mainnet():
    """D-010 is unchanged. This is the test that says so."""
    for network in ("main", "doge-main"):
        with pytest.raises(MainnetRefused):
            MessageSender(FakeRpc(), NETWORKS[network])


def test_a_public_sender_is_allowed_on_mainnet():
    for network in ("main", "doge-main"):
        sender = MessageSender(FakeRpc(), NETWORKS[network], public_only=True)
        assert sender.public_only


def test_the_exception_must_be_asked_for_explicitly():
    """Opt-in, and named for what it permits rather than what it disables."""
    assert MessageSender(FakeRpc(), NETWORKS["test"]).public_only is False


def test_no_encrypted_call_site_passes_public_only():
    """A grep-style guard: the flag must never appear near sealed sending."""
    import pathlib

    for name in ("arcade/messaging/cli.py", "arcade/web/app.py"):
        source = pathlib.Path(name).read_text()
        for line in source.splitlines():
            if "public_only=True" in line:
                # Only the group-post path may ask for it.
                assert "group" in source[max(0, source.index(line) - 900):
                                         source.index(line) + 200].lower(), line


# --- the format ---------------------------------------------------------------


def test_a_post_round_trips():
    raw = G.build(G.GroupPost("trading", "robin", "Anyone selling RBIT?"))
    got = G.parse(raw)
    assert got.channel == "trading"
    assert got.nickname == "robin"
    assert got.text == "Anyone selling RBIT?"


def test_a_post_carries_no_ciphertext_length():
    """`clen` exists to unpad a sealed box. Nothing here is sealed."""
    assert Header(type=TYPE_GROUP).length == 6
    assert len(Header(type=TYPE_GROUP).encode()) == 6


def test_the_size_limit_is_exact():
    room = G.max_text_bytes("main", "robin")
    G.build(G.GroupPost("main", "robin", "x" * room))
    with pytest.raises(G.GroupError):
        G.build(G.GroupPost("main", "robin", "x" * (room + 1)))


def test_a_longer_channel_name_leaves_less_room():
    """Channel and nickname are paid for in the same bytes as the message."""
    assert G.max_text_bytes("a", "") > G.max_text_bytes("a" * 20, "")
    assert G.max_text_bytes("main", "") > G.max_text_bytes("main", "a-long-name")


def test_a_post_fits_in_one_op_return():
    from arcade.encoding import max_class_c_payload
    from arcade.payload import AnyData

    raw = G.build(G.GroupPost("main", "robin", "x" * G.max_text_bytes("main", "robin")))
    assert len(AnyData(data=raw).encode()) <= max_class_c_payload()


def test_unicode_is_measured_in_bytes_not_characters():
    """A limit counted in characters would overflow the output on emoji."""
    room = G.max_text_bytes("main", "")
    with pytest.raises(G.GroupError):
        G.build(G.GroupPost("main", "", "🐸" * room))


@pytest.mark.parametrize("post,reason", [
    (G.GroupPost("main", "", ""), "empty text"),
    (G.GroupPost("main", "", "   "), "whitespace only"),
    (G.GroupPost("x" * 40, "", "hello"), "channel too long"),
    (G.GroupPost("main", "y" * 40, "hello"), "nickname too long"),
])
def test_bad_posts_are_refused(post, reason):
    with pytest.raises(G.GroupError):
        G.build(post)


def test_an_empty_channel_lands_in_the_default_room():
    """A channel of spaces is not a channel. It should not be unnameable either."""
    assert G.parse(G.build(G.GroupPost("", "", "hello"))).channel == G.DEFAULT_CHANNEL
    assert G.parse(G.build(G.GroupPost("   ", "", "hello"))).channel == G.DEFAULT_CHANNEL


def test_a_sealed_message_is_not_mistaken_for_a_post():
    from arcade.messaging.envelope import TYPE_SINGLE

    assert not G.is_group_payload(Header(type=TYPE_SINGLE).encode() + b"sealed")


def test_a_truncated_post_does_not_raise_something_unexpected():
    raw = G.build(G.GroupPost("main", "robin", "hello there"))
    with pytest.raises((EnvelopeError, IndexError)):
        G.parse(raw[:7])


def test_class_b_padding_is_stripped():
    """A post carried as Class B arrives NUL-padded to a 30-byte boundary."""
    raw = G.build(G.GroupPost("main", "", "padded"))
    assert G.parse(raw + b"\x00" * 17).text == "padded"


# --- storage ------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    from arcade.messaging.store import MessageStore
    return MessageStore(tmp_path / "m.sqlite")


def test_the_same_channel_on_two_chains_is_two_rooms(store):
    """Same name, different chain, different cost -- and different conversation."""
    store.add_group_post("test", "main", "tx1", 1, 10, "nA", "alice", "on testnet")
    store.add_group_post("main", "main", "tx2", 1, 10, "PB", "bob", "on mainnet")

    assert [r["text"] for r in store.group_posts("test", "main")] == ["on testnet"]
    assert [r["text"] for r in store.group_posts("main", "main")] == ["on mainnet"]


def test_the_newest_post_comes_first(store):
    """A feed, not a conversation: you drop in, and the unseen thing is newest."""
    for index, when in enumerate([300, 100, 200]):
        store.add_group_post("test", "main", f"tx{index}", 1, when, "nA", "", str(when))

    assert [r["text"] for r in store.group_posts("test", "main")] == ["300", "200", "100"]


def test_rescanning_does_not_duplicate_posts(store):
    for _ in range(3):
        store.add_group_post("test", "main", "tx1", 1, 10, "nA", "alice", "once")
    assert len(store.group_posts("test", "main")) == 1


def test_a_post_gains_its_height_when_the_scan_finds_it(store):
    """Found live: the optimistic row blocked the confirmation forever.

    A post this machine makes is recorded at broadcast with height 0 so it shows
    immediately. INSERT OR IGNORE then discarded the scanned row, so the real
    height never landed and the post read "pending" for good.
    """
    store.add_group_post("test", "main", "tx1", 0, 1000, "nMe", "me", "hello",
                         mine=True)
    store.add_group_post("test", "main", "tx1", 1_482_900, 2000, "nMe", "me",
                         "hello", mine=False)

    (row,) = store.group_posts("test", "main")
    assert row["height"] == 1_482_900
    assert row["block_time"] == 2000


def test_a_rescan_cannot_forget_that_a_post_is_mine(store):
    """The chain cannot say who ran the command. Only this machine knows."""
    store.add_group_post("test", "main", "tx1", 0, 1000, "nMe", "me", "hi", mine=True)
    store.add_group_post("test", "main", "tx1", 500, 2000, "nMe", "me", "hi", mine=False)

    assert store.group_posts("test", "main")[0]["mine"] == 1


def test_a_confirmed_post_is_not_reset_by_a_later_optimistic_row(store):
    store.add_group_post("test", "main", "tx1", 500, 2000, "nMe", "", "hi")
    store.add_group_post("test", "main", "tx1", 0, 1000, "nMe", "", "hi")

    assert store.group_posts("test", "main")[0]["height"] == 500


# --- chunked public posts -----------------------------------------------------
# A post carrying a file outgrows one transaction quickly. Unlike a private
# message these chunks are NOT sealed, so rejoining them needs no key and no
# identity -- a node with no wallet can read them. That is the whole reason they
# needed a type of their own rather than reusing the sealed chunk path.


def _png(size):
    return b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * (size // 256 + 1)


def test_a_text_post_is_still_one_cheap_op_return():
    plan = G.plan(G.GroupPost("main", "robin", "just talking"))
    assert plan.transactions == 1 and plan.class_c


def test_a_small_file_is_one_transaction_but_not_class_c():
    """An OP_RETURN cannot hold a file at all."""
    plan = G.plan(G.GroupPost("art", "m", "look", "x.png", "image/png", _png(1000)))
    assert plan.transactions == 1 and not plan.class_c


def test_a_large_file_chunks():
    plan = G.plan(G.GroupPost("art", "m", "look", "x.png", "image/png", _png(40_000)))
    assert plan.transactions > 1
    assert plan.msg_id and len(plan.msg_id) == 8


def test_chunks_are_split_evenly():
    """Same reason as private messages: a greedy split makes the first the largest."""
    plan = G.plan(G.GroupPost("art", "m", "look", "x.png", "image/png", _png(40_000)))
    sizes = [len(p) for p in plan.payloads]
    assert max(sizes) - min(sizes) <= 20, sizes


def test_the_final_chunk_carries_countdown_zero():
    plan = G.plan(G.GroupPost("art", "m", "look", "x.png", "image/png", _png(40_000)))
    countdowns = [G.parse_chunk(p)[1] for p in plan.payloads]
    assert countdowns[-1] == 0
    assert countdowns == list(range(len(countdowns) - 1, -1, -1))


def test_every_chunk_shares_one_message_id():
    plan = G.plan(G.GroupPost("art", "m", "look", "x.png", "image/png", _png(40_000)))
    ids = {G.parse_chunk(p)[0] for p in plan.payloads}
    assert len(ids) == 1


def test_rejoining_the_chunks_reproduces_the_post():
    original = _png(40_000)
    plan = G.plan(G.GroupPost("art", "m", "look", "x.png", "image/png", original))
    joined = b"".join(G.parse_chunk(p)[2] for p in plan.payloads)
    got = G.parse(joined)
    assert got.file_data == original
    assert got.channel == "art" and got.text == "look"


def test_a_chunk_is_not_mistaken_for_a_whole_post():
    plan = G.plan(G.GroupPost("art", "m", "look", "x.png", "image/png", _png(40_000)))
    assert G.is_group_chunk_payload(plan.payloads[0])
    assert not G.is_group_payload(plan.payloads[0])


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0), (1, 2, 0), (2, 0, 1)])
def test_chunks_reassemble_in_any_arrival_order(tmp_path, order):
    """Blocks are scanned in whatever order the chain gives them."""
    from arcade.config import NETWORKS
    from arcade.messaging.scanner import Scanner
    from arcade.messaging.store import MessageStore

    original = _png(G.MAX_CLASS_B_PAYLOAD * 2 + 500)
    plan = G.plan(G.GroupPost("art", "m", "look", "x.png", "image/png", original))
    assert plan.transactions == 3, "fixture must produce exactly three chunks"

    store = MessageStore(tmp_path / f"m{order}.sqlite")
    scanner = Scanner.__new__(Scanner)
    scanner.params = NETWORKS["regtest"]
    scanner.store = store
    scanner.identity = None
    scanner.public_only = True

    for index in order:
        msg_id, countdown, piece = G.parse_chunk(plan.payloads[index])
        store.add_group_chunk("regtest", msg_id, countdown, f"tx{index}",
                              100 + index, 1000 + index, "nA", piece)
        scanner._assemble_group(msg_id, 100 + index, 1000 + index)

    (row,) = store.group_posts("regtest", "art")
    assert bytes(store.group_post_file(row["id"])["file_data"]) == original


def test_a_lone_final_chunk_is_not_treated_as_complete(tmp_path):
    """The bug this guard exists for, and it destroyed data.

    A chunk with countdown 0 satisfies every naive completeness check -- "the
    highest countdown is 0, so there is one chunk, and we have one" -- while
    being the *last* piece of a post whose start has not arrived. The parse then
    failed and the error path deleted the stored chunks, so each chunk was
    destroyed as it arrived and the post could never complete.
    """
    from arcade.config import NETWORKS
    from arcade.messaging.scanner import Scanner
    from arcade.messaging.store import MessageStore

    plan = G.plan(G.GroupPost("art", "m", "look", "x.png", "image/png",
                              _png(G.MAX_CLASS_B_PAYLOAD * 2)))
    store = MessageStore(tmp_path / "lone.sqlite")
    scanner = Scanner.__new__(Scanner)
    scanner.params = NETWORKS["regtest"]
    scanner.store = store
    scanner.identity = None
    scanner.public_only = True

    msg_id, countdown, piece = G.parse_chunk(plan.payloads[-1])
    assert countdown == 0
    store.add_group_chunk("regtest", msg_id, countdown, "tx", 1, 1, "nA", piece)

    assert scanner._assemble_group(msg_id, 1, 1) is False
    assert store.group_posts("regtest", "art") == []
    assert len(store.group_chunks("regtest", msg_id)) == 1, (
        "an incomplete chain must be kept, not discarded"
    )


# --- the feed ------------------------------------------------------------------
# The public channel reads as a feed rather than a conversation: composer at the
# top, newest first, images shown without a click, audio and video as players
# that wait to be started.


@pytest.fixture
def feed_client(tmp_path, no_nodes):
    from pathlib import Path
    from fastapi.testclient import TestClient
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    state = AppState(
        home=tmp_path,
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                               datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=Path("/nonexistent")),
    )
    return TestClient(create_app(state)), state


def _post(state, channel, text, when, **file):
    with state.store() as store:
        store.add_group_post("regtest", channel, f"tx{when}", 1, when, "nA",
                             "somebody", text, **file)


def test_the_composer_comes_before_the_feed(feed_client):
    """What you post drops down into the feed underneath it."""
    app, state = feed_client
    _post(state, "main", "a post", 100)

    body = app.get("/groups?channel=main").text
    assert body.index('id="postform"') < body.index('class="card post')


def test_the_newest_post_is_rendered_first(feed_client):
    import re

    app, state = feed_client
    _post(state, "main", "first thing", 100)
    _post(state, "main", "second thing", 200)

    body = app.get("/groups?channel=main").text
    # Match the rendered posts, not the whole document: a bare substring search
    # finds "older" inside the composer's own placeholder text.
    rendered = re.findall(r'class="post-text">([^<]+)', body)
    assert rendered == ["second thing", "first thing"]


def test_an_image_is_shown_without_a_click(feed_client):
    app, state = feed_client
    _post(state, "main", "look", 100, file_name="a.png", file_type="image/png",
          file_data=b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    body = app.get("/groups?channel=main").text
    assert '<img class="post-media"' in body


def test_audio_is_a_player_that_waits_to_be_started(feed_client):
    app, state = feed_client
    _post(state, "main", "listen", 100, file_name="a.wav", file_type="audio/wav",
          file_data=b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 32)

    body = app.get("/groups?channel=main").text
    assert 'audio class="post-media" controls' in body
    assert 'preload="none"' in body


def test_video_is_a_player_that_waits_to_be_started(feed_client):
    app, state = feed_client
    _post(state, "main", "watch", 100, file_name="a.mp4", file_type="video/mp4",
          file_data=b"\x00\x00\x00\x20ftypisom" + b"\x00" * 64)

    body = app.get("/groups?channel=main").text
    assert 'video class="post-media" controls' in body
    assert 'preload="metadata"' in body


def test_an_unsafe_file_gets_no_player_at_all(feed_client):
    """A feed that rendered whatever it was handed would be the whole problem."""
    app, state = feed_client
    _post(state, "main", "careful", 100, file_name="x.svg", file_type="image/svg+xml",
          file_data=b"<svg onload='alert(1)'></svg>" + b" " * 40)

    body = app.get("/groups?channel=main").text
    # The class name also appears in the stylesheet, so look for the elements.
    for element in ('<img class="post-media"', '<video class="post-media"',
                    '<audio class="post-media"'):
        assert element not in body
    assert "x.svg" in body, "it should still be offered as a download"


def test_starting_a_channel_needs_no_creation_step(feed_client):
    """A channel is a name. Posting to an unused one starts it."""
    app, state = feed_client

    body = app.get("/groups?channel=brand-new").text
    assert "#brand-new" in body
    assert "is empty" in body

    _post(state, "brand-new", "first ever", 100)
    assert "first ever" in app.get("/groups?channel=brand-new").text


def test_the_channel_control_is_above_the_feed(feed_client):
    """It was at the bottom, labelled 'Go to channel', which hid that you can
    invent one."""
    app, state = feed_client
    _post(state, "main", "a post", 100)

    body = app.get("/groups?channel=main").text
    assert body.index('class="card chanbar"') < body.index('id="postform"')
    assert "a new one starts it" in body
