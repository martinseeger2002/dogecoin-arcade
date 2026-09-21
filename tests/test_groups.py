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
    """The flag may only be asked for from the public-post paths.

    Checked by walking the syntax tree for the ENCLOSING FUNCTION of each call,
    rather than by looking at a window of characters around the line. The
    window version passed and failed for the wrong reasons: it searched for
    "group" within 900 characters, which a long docstring could satisfy by
    accident and a legitimate new call site could miss by being far from its
    own route decorator -- which is what happened when the public resume path
    was added. It also used source.index(line), which finds the FIRST
    occurrence of a line's text, so two identical call lines checked the same
    context twice.
    """
    import ast
    import pathlib

    # Public posts and inscriptions: both are plain, public data on a chain
    # anyone can read. Nothing sealed may pass the flag, which is the rule this
    # enforces -- not "only posts may", which was only ever true because posts
    # were the only public thing there was.
    allowed = ("group", "post", "inscrib")
    for name in ("arcade/messaging/cli.py", "arcade/web/app.py"):
        tree = ast.parse(pathlib.Path(name).read_text())

        # Innermost enclosing function for every node. ast.walk is
        # breadth-first, so shallower functions are visited first and plain
        # assignment lets the deepest one win -- with setdefault, create_app()
        # claimed every route nested inside it and nothing could be attributed.
        holder: dict[int, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    holder[id(child)] = node.name

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            asks = any(kw.arg == "public_only"
                       and isinstance(kw.value, ast.Constant)
                       and kw.value.value is True
                       for kw in node.keywords)
            if not asks:
                continue
            where = holder.get(id(node), "<module>")
            assert any(word in where.lower() for word in allowed), (
                f"{name}: public_only=True asked for in {where}(), which is not "
                f"a public-post path -- nothing sealed may pass this flag"
            )


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


def test_the_budget_is_exact_and_one_byte_over_changes_the_carriage():
    """Over the budget is not refused -- it moves to Class B and costs dust.

    It used to raise, from inside `build`, which runs before `plan` can choose a
    carriage. That made plan's own documented fallback ("three cases, cheapest
    first") unreachable: every text post over the Class C budget failed, with an
    error ending "attaching a file lifts the limit, but costs far more". The operator
    hit it with no file attached and read it as a file-size error.
    """
    room = G.max_text_bytes("main", "robin")

    at = G.plan(G.GroupPost("main", "robin", "x" * room))
    assert at.class_c, "at the budget it must still be the one cheap output"
    assert at.transactions == 1

    over = G.plan(G.GroupPost("main", "robin", "x" * (room + 1)))
    assert not over.class_c, "one byte over moves it into multisig outputs"
    assert over.transactions == 1, "but it is still a single transaction"


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
    """The budget is bytes, so emoji cross it far sooner than the count suggests.

    The composer's textarea used to carry maxlength="{room}", which counts UTF-16
    CHARACTERS. One emoji is four bytes, so a post well inside the character cap
    was well over the byte budget, walked past the browser's limit, and was
    refused by the server. What must not happen is the post being carried in a
    Class C output it does not fit in.
    """
    room = G.max_text_bytes("main", "")
    text = "🐸" * room
    assert len(text) < len(text.encode()), "the premise: characters are not bytes"

    planned = G.plan(G.GroupPost("main", "", text))
    assert not planned.class_c, "it does not fit one OP_RETURN and must not claim to"


def test_a_post_over_the_budget_still_fits_one_op_return_when_short_enough():
    """The cheap path is not lost -- only the refusal is."""
    planned = G.plan(G.GroupPost("main", "Big Chief Energy", "hello"))
    assert planned.class_c and planned.transactions == 1


def test_a_nickname_eats_the_budget_but_no_longer_costs_the_post():
    """This is what made the old refusal constant rather than an edge case."""
    assert G.max_text_bytes("main", "Big Chief Energy") < 50
    text = "Right on everything looks good. Except for when I went to send one."
    assert len(text.encode()) > G.max_text_bytes("main", "Big Chief Energy")
    planned = G.plan(G.GroupPost("main", "Big Chief Energy", text))
    assert planned.transactions == 1 and not planned.class_c


def test_the_text_ceiling_that_remains_is_the_header_field():
    """65,535 is a real limit -- the length is a uint16 on the wire."""
    with pytest.raises(G.GroupError):
        G.build(G.GroupPost("main", "", "x" * 70_000))


@pytest.mark.parametrize("post,reason", [
    (G.GroupPost("main", "", ""), "empty text and no file"),
    (G.GroupPost("main", "", "   "), "whitespace only and no file"),
    (G.GroupPost("x" * 40, "", "hello"), "channel too long"),
    (G.GroupPost("main", "y" * 40, "hello"), "nickname too long"),
])
def test_bad_posts_are_refused(post, reason):
    with pytest.raises(G.GroupError):
        G.build(post)


def test_a_picture_needs_no_words():
    """A picture is a post. The private side has always allowed one with
    nothing typed; the board refused it and told the user to write something,
    for a post that was already complete. Reported from a phone."""
    jpeg = b"\xff\xd8\xff" + bytes(range(256)) * 6
    post = G.GroupPost("main", "the operator", "", file_name="photo.jpg",
                       file_type="image/jpeg", file_data=jpeg)

    plan = G.plan(post)
    back = G.parse(plan.payloads[0])
    assert back.text == ""
    assert back.file_name == "photo.jpg"
    assert back.file_type == "image/jpeg"
    assert back.file_data == jpeg, "the picture is the whole of the post"
    assert back.channel == "main" and back.nickname == "the operator"


def test_nothing_at_all_is_still_refused():
    """Neither words nor a picture is not a post."""
    with pytest.raises(G.GroupError, match="write something, or choose a picture"):
        G.build(G.GroupPost("main", "", "   "))


def test_a_picture_with_words_still_carries_both():
    jpeg = b"\xff\xd8" * 500
    back = G.parse(G.plan(G.GroupPost("main", "M", "look at this",
                                      file_name="a.jpg", file_type="image/jpeg",
                                      file_data=jpeg)).payloads[0])
    assert back.text == "look at this" and back.file_data == jpeg


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


def test_a_channel_reads_downward_like_a_conversation(store):
    """Presented beside a private thread, it should behave like one."""
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


# The board's own page is gone: a post belongs to whoever wrote it, and the
# feed is the only place it lives (D-147). What is tested here now is the
# FORMAT and the STORE -- how a post is encoded, chunked, stored and read
# back -- which the feed uses unchanged. The page tests moved to
# test_feed_web.py, and the ones that tested channels went with the channels.


def _post(state, channel, text, when, **file):
    with state.store() as store:
        store.add_group_post("regtest", channel, f"tx{when}", 1, when, "nA",
                             "somebody", text, **file)


def test_an_injected_chunk_cannot_block_a_real_post(tmp_path):
    """A message id is in the clear on the chain, so anyone can claim one.

    One injected chunk with an unused countdown would otherwise make the real
    post look permanently incomplete: the reassembler would see a higher maximum
    and wait forever for links that were never sent. Grouping by sender means the
    injected chunk forms its own group and simply fails.
    """
    from arcade.config import NETWORKS
    from arcade.messaging.scanner import Scanner
    from arcade.messaging.store import MessageStore

    original = _png(G.MAX_CLASS_B_PAYLOAD + 500)
    plan = G.plan(G.GroupPost("art", "m", "real", "x.png", "image/png", original))
    assert plan.transactions == 2

    store = MessageStore(tmp_path / "m.sqlite")
    scanner = Scanner.__new__(Scanner)
    scanner.params = NETWORKS["regtest"]
    scanner.store = store
    scanner.identity = None
    scanner.public_only = True

    # A stranger claims the same message id with a countdown nobody sent.
    msg_id = plan.msg_id
    store.add_group_chunk("regtest", msg_id, 7, "tx-forged", 1, 1, "nAttacker",
                          b"nonsense")

    for index, payload in enumerate(plan.payloads):
        _, countdown, piece = G.parse_chunk(payload)
        store.add_group_chunk("regtest", msg_id, countdown, f"tx{index}",
                              100 + index, 1000 + index, "nHonest", piece)
        scanner._assemble_group(msg_id, 100 + index, 1000 + index)

    (row,) = store.group_posts("regtest", "art")
    assert bytes(store.group_post_file(row["id"])["file_data"]) == original


def test_a_text_only_class_b_post_reads_back():
    """A combination that did not exist before: text only, carried Class B.

    Text posts were always Class C and file posts always Class B, so "Class B
    with no file marker" is new on the wire. Nothing in the reader needed
    changing, but that is worth proving rather than assuming.
    """
    text = "Right on everything looks good. Except for when I went to send one."
    planned = G.plan(G.GroupPost("main", "Big Chief Energy", text))
    assert not planned.class_c and planned.transactions == 1

    back = G.parse(planned.payloads[0])
    assert back.text == text
    assert back.channel == "main"
    assert back.nickname == "Big Chief Energy"
    assert not back.has_file, "no file was posted, so none must be read back"


def test_an_emoji_post_reads_back_intact():
    """The bytes that crossed the budget must survive the crossing."""
    text = "🐸" * 30
    planned = G.plan(G.GroupPost("main", "", text))
    assert not planned.class_c
    assert G.parse(planned.payloads[0]).text == text


def test_a_chunked_text_only_post_reassembles():
    text = "long post. " * 900
    planned = G.plan(G.GroupPost("main", "", text))
    assert planned.transactions > 1

    joined = b"".join(G.parse_chunk(piece)[2] for piece in planned.payloads)
    assert G.parse(joined).text == text
