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


def test_posts_read_downward_in_time(store):
    for index, when in enumerate([300, 100, 200]):
        store.add_group_post("test", "main", f"tx{index}", 1, when, "nA", "", str(when))

    assert [r["text"] for r in store.group_posts("test", "main")] == ["100", "200", "300"]


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
