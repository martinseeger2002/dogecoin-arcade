"""End-to-end messaging across two regtest nodes.

Alice runs on one node, Bob on another. Everything travels over the real P2P
network between them: Bob never sees Alice's transaction except by receiving and
indexing a block. This is the test that exercises the pieces together --
publishing, sending, propagation, scanning, trial decryption, reassembly and
reading.
"""

import dataclasses

import pytest

from arcade.messaging.envelope import build_key_announcement
from arcade.messaging.keys import Identity
from arcade.messaging.scanner import Scanner
from arcade.messaging.sender import MessageSender, plan_message
from arcade.messaging.store import MessageStore
from arcade.regtest import RegtestNode


@pytest.fixture(scope="module")
def pair():
    """Two connected regtest nodes, both enforcing mainnet standardness."""
    alice_node = RegtestNode(allow_peers=True, require_standard=True)
    bob_node = RegtestNode(allow_peers=True, require_standard=True)
    try:
        alice_node.start()
        bob_node.start()
    except RuntimeError as exc:
        for n in (alice_node, bob_node):
            n.stop()
        pytest.skip(f"regtest nodes unavailable: {exc}")
    alice_node.connect_to(bob_node)
    alice_node.generate(200)
    alice_node.sync_with(bob_node)
    yield alice_node, bob_node
    alice_node.stop()
    bob_node.stop()


@pytest.fixture(scope="module")
def params(pair):
    alice_node, _ = pair
    marker = alice_node.rpc.call("getnewaddress")
    return dataclasses.replace(alice_node.params, activation_height=1, marker_address=marker)


@pytest.fixture(scope="module")
def alice_address(pair):
    alice_node, bob_node = pair
    addr = alice_node.rpc.call("getnewaddress")
    alice_node.rpc.call("sendtoaddress", addr, 5000)
    alice_node.generate(1)
    alice_node.sync_with(bob_node)
    return addr


@pytest.fixture(scope="module")
def identities():
    return Identity.generate(), Identity.generate()


@pytest.fixture
def bob_store(tmp_path):
    store = MessageStore(tmp_path / "bob.sqlite")
    yield store
    store.close()


def confirm(pair):
    alice_node, bob_node = pair
    alice_node.generate(1)
    alice_node.sync_with(bob_node)


def send(pair, params, address, payload):
    alice_node, _ = pair
    sender = MessageSender(alice_node.rpc, params)
    prepared = sender.prepare(address, payload)
    # The CLI shows this to the user and waits for approval; the test approves.
    assert prepared.txid and prepared.fee_sats > 0
    return sender.broadcast(prepared)


# --- key announcement ---------------------------------------------------------


def test_key_announcement_propagates_and_is_indexed(pair, params, alice_address,
                                                    identities, bob_store):
    alice_node, bob_node = pair
    alice_id, _ = identities

    sender = MessageSender(alice_node.rpc, params)
    prepared = sender.prepare(alice_address, build_key_announcement(alice_id.public_bytes),
                              class_c=True)
    sender.broadcast(prepared)
    confirm(pair)

    # Bob learns the key from HIS node, having never spoken to Alice.
    scanner = Scanner(bob_node.rpc, params, bob_store)
    result = scanner.scan()
    assert result.announcements >= 1

    row = bob_store.key_for(alice_address)
    assert row is not None, "Bob did not index Alice's key announcement"
    assert bytes(row["pubkey"]) == alice_id.public_bytes
    assert row["fingerprint"] == alice_id.fingerprint


# --- single-transaction message ----------------------------------------------


def test_short_message_end_to_end(pair, params, alice_address, identities, bob_store):
    alice_node, bob_node = pair
    alice_id, bob_id = identities
    body = b"Meet me at the usual place. Bring the thing."

    plan = plan_message(alice_id, bob_id.public_bytes, body)
    assert plan.transactions == 1
    send(pair, params, alice_address, plan.chunk_payloads[0])
    confirm(pair)

    scanner = Scanner(bob_node.rpc, params, bob_store, identity=bob_id)
    scanner.scan()

    inbox = bob_store.inbox(recipient_fp=bob_id.fingerprint)
    assert len(inbox) == 1, "Bob did not receive the message"
    message = inbox[0]
    assert message.body == body
    assert message.sender_addr == alice_address
    assert message.complete
    assert message.read_at is None

    bob_store.mark_read(message.id)
    assert bob_store.get_message(message.id).read_at is not None


def test_message_not_addressed_to_bob_is_not_decrypted(pair, params, alice_address,
                                                       identities, bob_store):
    """Trial decryption must not produce false positives."""
    alice_id, _ = identities
    carol = Identity.generate()
    _, bob_node = pair

    plan = plan_message(alice_id, carol.public_bytes, b"for carol only")
    send(pair, params, alice_address, plan.chunk_payloads[0])
    confirm(pair)

    scanner = Scanner(bob_node.rpc, params, bob_store, identity=identities[1])
    scanner.scan()

    bodies = [m.body for m in bob_store.inbox(recipient_fp=identities[1].fingerprint)]
    assert b"for carol only" not in bodies
    # Bob still stored it as a candidate: cheap, and needed if he later imports
    # another identity.
    assert bob_store.stats()["candidates"] >= 1


# --- multi-transaction message ------------------------------------------------


def test_chunked_message_end_to_end(pair, params, alice_address, identities, bob_store):
    alice_node, bob_node = pair
    alice_id, bob_id = identities
    body = ("Chunked message. " * 1200).encode()      # ~20 kB, several transactions

    plan = plan_message(alice_id, bob_id.public_bytes, body)
    assert plan.transactions > 1, "this test needs a multi-transaction message"

    sender = MessageSender(alice_node.rpc, params)
    for payload in plan.chunk_payloads:
        prepared = sender.prepare(alice_address, payload)
        sender.broadcast(prepared)
        confirm(pair)          # confirm between links so ancestor limits never bind

    scanner = Scanner(bob_node.rpc, params, bob_store, identity=bob_id)
    scanner.scan()

    received = [m for m in bob_store.inbox(recipient_fp=bob_id.fingerprint)
                if len(m.body) == len(body)]
    assert received, f"chunked message not reassembled ({plan.transactions} chunks)"
    assert received[0].body == body
    assert received[0].complete


def test_incomplete_chunked_message_is_not_surfaced(pair, params, alice_address,
                                                    identities, tmp_path):
    """A partial send must never appear as a readable message.

    Completion is self-describing: the final chunk carries countdown 0. An
    abandoned chain never reaches it, so the message stays invisible rather than
    appearing truncated.
    """
    alice_node, bob_node = pair
    alice_id, bob_id = identities
    store = MessageStore(tmp_path / "partial.sqlite")

    body = ("Never finished. " * 1200).encode()
    plan = plan_message(alice_id, bob_id.public_bytes, body)
    assert plan.transactions > 1

    sender = MessageSender(alice_node.rpc, params)
    for payload in plan.chunk_payloads[:-1]:          # deliberately omit the last
        sender.broadcast(sender.prepare(alice_address, payload))
        confirm(pair)

    scanner = Scanner(bob_node.rpc, params, store, identity=bob_id)
    scanner.scan()

    assert not [m for m in store.inbox(recipient_fp=bob_id.fingerprint)
                if m.body == body], "an incomplete message was surfaced as readable"
    store.close()


# --- scanning behaviour -------------------------------------------------------


def test_scanning_is_resumable(pair, params, tmp_path, identities):
    """A second scan must not redo work or duplicate rows."""
    _, bob_node = pair
    store = MessageStore(tmp_path / "resume.sqlite")

    scanner = Scanner(bob_node.rpc, params, store, identity=identities[1])
    first = scanner.scan()
    assert first.blocks > 0
    cursor = store.scan_cursor(params.name)
    assert cursor is not None

    second = scanner.scan()
    assert second.blocks == 0, "a second scan re-read blocks it had already seen"
    assert store.scan_cursor(params.name) == cursor
    store.close()


def test_scan_picks_up_where_it_left_off(pair, params, tmp_path, identities):
    _, bob_node = pair
    store = MessageStore(tmp_path / "incremental.sqlite")
    scanner = Scanner(bob_node.rpc, params, store, identity=identities[1])
    scanner.scan()
    height_before = store.scan_cursor(params.name)[0]

    confirm(pair)
    result = scanner.scan()
    assert result.blocks == 1, "should have scanned exactly the one new block"
    assert store.scan_cursor(params.name)[0] == height_before + 1
    store.close()


# --- broadcast is not the same question as readable ---------------------------

def test_readable_takes_a_block_per_chunk_even_when_broadcast_is_instant():
    """One chunk per block -- a floor, from a quiet chain.

    Testnet: two chunks spending different outputs of the same confirmed split,
    broadcast together, taking blocks 1,484,220 and 1,484,221 while the loser
    sat in a mempool beside a block 0.9% full. Regtest: three chunks, three
    blocks.

    But 292 of the surrounding 300 blocks hold nothing but a coinbase and no
    block held two transactions at all, so this cannot separate "large chunks do
    not share a block" from "nothing shares a block here". Kept because the
    error points the right way -- competition can only spread chunks further
    apart -- and because the alternative was the screen's old claim that they
    "all go at once", which is true of broadcasting and not of reading.
    """
    from arcade.messaging.sender import (
        estimate_readable_seconds, estimate_send_seconds)

    typical = 60.0
    # A well-split wallet: nothing waits to be broadcast.
    quick, _ = estimate_send_seconds(3, typical, 180.0, independent_outputs=8)
    assert quick == 0, "with enough outputs, broadcasting waits for nothing"

    # But the message is not readable for three blocks.
    assert estimate_readable_seconds(3, typical) == 180
    assert estimate_readable_seconds(1, typical) == 60, "one block, like anything"
    assert estimate_readable_seconds(0, typical) == 60, "never less than a block"

    # The two answers must not be confused: readable is never the shorter one.
    for chunks in (1, 2, 5, 20):
        broadcast, _ = estimate_send_seconds(chunks, typical, 180.0,
                                             independent_outputs=chunks + 4)
        assert estimate_readable_seconds(chunks, typical) >= broadcast


# --- the dust figure has to be a number, not a zero ---------------------------
#
# I added dust_sats and the confirm screens that quote it, and the value they
# quoted could only ever be zero. There are two PreparedTx constructions and my
# edit landed in the wrong one -- split_outputs() instead of prepare(), which is
# the function that builds every message and post. The edit was guarded by
# `assert s != before`, which proved that SOMETHING changed, not that the right
# thing did.
#
# BOXA caught it on the live instance: a 9,000-byte post offered as "Costs
# 0.08856 in total -- 0.08856 fee plus 0.00000 in dust" when the real dust was
# about 1.63. It also made the point that decides how this is tested: a check
# asserting the screen "shows a dust figure" passes on 0.00000. So these assert
# a POSITIVE number, and that prepare() is where it comes from.


def test_prepare_reports_the_dust_it_builds():
    """Class B carries the payload in outputs, so the dust is the real cost."""
    import ast
    import pathlib

    source = pathlib.Path("arcade/messaging/sender.py").read_text()
    tree = ast.parse(source)

    holder = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                holder[id(child)] = node.name

    setters = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "PreparedTx":
            setters[holder.get(id(node))] = any(
                kw.arg == "dust_sats" for kw in node.keywords)

    assert setters.get("prepare") is True, (
        "prepare() builds every message and post; without dust_sats there the "
        "confirm screens can only ever quote zero"
    )
    assert setters.get("split_outputs") is False, (
        "a split's outputs return to the same address as spendable change, so "
        "reporting them as dust would claim a split costs twelve coins"
    )


def test_a_class_b_send_costs_more_than_its_fee():
    """The arithmetic behind the screen, with no node involved."""
    from arcade.messaging.sender import OUTPUT_VALUE, PreparedTx

    # Six multisig outputs plus a dust recipient output, as a Class B message
    # of a few hundred bytes builds.
    built = [(OUTPUT_VALUE, b"script")] * 7
    prepared = PreparedTx(hex="", txid="", decoded={}, fee_sats=568_000,
                          size=989, outputs=9,
                          dust_sats=sum(v for v, _ in built))

    assert prepared.dust_sats > 0, "a Class B send always spends into outputs"
    assert prepared.dust_coins == 0.07
    assert prepared.total_sats > prepared.fee_sats, (
        "the total must exceed the fee, or the screen is quoting the wrong thing"
    )
    # And the case BOXA was actually shown: dust dominating the fee.
    assert prepared.dust_sats > prepared.fee_sats


def test_class_c_has_no_dust():
    """One OP_RETURN output carries no value, so the total is just the fee."""
    from arcade.messaging.sender import PreparedTx

    prepared = PreparedTx(hex="", txid="", decoded={}, fee_sats=56_800,
                          size=250, outputs=2, dust_sats=0)
    assert prepared.total_sats == prepared.fee_sats


# --- one definition of what a send costs --------------------------------------
#
# Only the FIRST transaction of a chunked send is ever prepared; the rest spend
# its change and cannot be built until it is broadcast. So its fee and dust have
# to be multiplied by the chunk count -- and the two composers did that
# differently. messages.html scaled both figures in Jinja; groups.html rendered
# the single prepared transaction unscaled, so a post was offered at exactly
# 1/count of its price, the FEE understated as well as the dust. BOXA caught it
# against a transaction it had already paid for, which is stronger evidence than
# posting another one and costs nothing.
#
# Cost is the fourth thing the two composers have disagreed about in a day,
# after the identity pin, funded_address and chunk sequencing. Hence one
# function, and a test shaped the way BOXA suggested: assert a two-chunk send
# costs about twice a one-chunk one, because asserting "a positive dust figure"
# passes on the understated number.


def _prepared(fee_sats, dust_sats):
    from arcade.messaging.sender import PreparedTx

    return PreparedTx(hex="", txid="", decoded={}, fee_sats=fee_sats,
                      size=8855, outputs=80, dust_sats=dust_sats)


def test_a_chunked_send_costs_its_chunk_count():
    from arcade.messaging.sender import send_cost

    one = send_cost(_prepared(8_856_000, 77_000_000), 1)
    two = send_cost(_prepared(8_856_000, 77_000_000), 2)

    assert two["total"] == pytest.approx(one["total"] * 2), (
        "a two-chunk send costs twice a one-chunk one; rendering the single "
        "prepared transaction unscaled is what halved a post's quoted price"
    )
    assert two["fee"] == pytest.approx(one["fee"] * 2), (
        "the fee is per transaction too, not just the dust"
    )
    assert two["dust"] == pytest.approx(one["dust"] * 2)
    assert two["transactions"] == 2


def test_the_quoted_cost_matches_a_post_actually_paid_for():
    """Ground truth: BOXA's two-chunk public post on testnet.

    It paid 1.71712 in total. The screen offered 0.85856.
    """
    from arcade.messaging.sender import send_cost

    quoted = send_cost(_prepared(8_856_000, 77_000_000), 2)
    assert quoted["total"] == pytest.approx(1.71712, abs=0.0001)


def test_a_single_transaction_send_is_not_scaled_away():
    from arcade.messaging.sender import send_cost

    cost = send_cost(_prepared(568_000, 4_000_000), 1)
    assert cost["total"] == pytest.approx(0.04568, abs=1e-9), (
        "this is BOXA's test-1 transaction, which it paid 0.04568 for"
    )
    # A count of zero must not zero the cost; something is always being sent.
    assert send_cost(_prepared(568_000, 4_000_000), 0)["total"] == cost["total"]


def test_no_template_quotes_a_single_transaction_as_the_price():
    """The bug was a template reading PER-TRANSACTION figures off `prepared`.

    A first version of this test only banned the literal 100000000, and passed
    with the bug restored -- `prepared.total_coins` divides by COIN in Python,
    so no satoshi conversion appears in the template at all. What has to be
    banned is reading the prepared transaction's own cost, because `prepared` is
    the FIRST chunk and the rest are not built yet.
    """
    import pathlib

    per_transaction = ("prepared.total_coins", "prepared.fee_coins",
                       "prepared.dust_coins", "prepared.fee_sats",
                       "prepared.dust_sats", "prepared[0].fee_sats",
                       "prepared[0].dust_sats", "prepared[0].fee_coins")

    # The private composer only. The feed has no confirm screen and no
    # chunked send: a post is one transaction by construction, and a file
    # attached to one becomes an inscription with its own accounting
    # (D-138, D-147).
    for name in ("messages.html",):
        source = (pathlib.Path("arcade/web/templates") / name).read_text()
        for attr in per_transaction:
            assert attr not in source, (
                f"{name} quotes {attr}, which is ONE chunk's cost. A chunked "
                f"send pays that per transaction -- go through send_cost()"
            )
        assert "100000000" not in source, (
            f"{name} is converting satoshis itself again"
        )
        assert "cost." in source, f"{name} should read the shared cost"
