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
