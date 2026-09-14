"""Node-to-node messages: one arcade talking to another.

The same sealed envelope as a private message -- so the same guarantees, and
the same testnet-only rule -- with its own type, so machine traffic never lands
in a human's conversation, and its own cursor, so a program can resume a queue
exactly where it stopped.
"""

import pytest

from arcade.messaging import api
from arcade.messaging.envelope import (
    TYPE_API, TYPE_SINGLE, EnvelopeError, Header, seal_message)
from arcade.messaging.keys import Identity
from arcade.messaging.store import MessageStore


@pytest.fixture
def pair():
    return Identity.generate(), Identity.generate()


def test_a_message_is_sealed_to_one_node_and_signed_by_ours(pair):
    alice, bob = pair
    payload = api.seal(alice, bob.public_bytes, b'{"cmd":"ping"}')

    sender, body = api.open_payload(bob, payload)
    assert body == b'{"cmd":"ping"}'
    assert sender == alice.public_bytes, "who sent it is authenticated, not claimed"

    with pytest.raises(EnvelopeError, match="not addressed to us"):
        api.open_payload(Identity.generate(), payload)


def test_a_human_message_is_not_an_api_message(pair):
    """And the reverse. The type is what keeps the two channels apart, and it
    is inside the authenticated header, so it cannot be switched in flight."""
    alice, bob = pair
    human = seal_message(alice, bob.public_bytes, Header(type=TYPE_SINGLE), b"hello")
    with pytest.raises(EnvelopeError, match="not an API message"):
        api.open_payload(bob, human)


def test_the_type_is_authenticated_not_merely_declared(pair):
    """Flipping the byte on the wire must not turn one into the other: the
    header is copied inside the ciphertext and compared on open."""
    alice, bob = pair
    payload = bytearray(api.seal(alice, bob.public_bytes, b"x"))
    assert payload[5] == TYPE_API
    payload[5] = TYPE_SINGLE
    with pytest.raises(EnvelopeError):
        api.open_payload(bob, bytes(payload))


def test_it_is_one_transaction_and_says_so(pair):
    alice, bob = pair
    assert api.MAX_API_PAYLOAD > 7_000, "a command has plenty of room"
    api.seal(alice, bob.public_bytes, b"x" * api.MAX_API_PAYLOAD)
    with pytest.raises(api.ApiMessageError, match="one transaction"):
        api.seal(alice, bob.public_bytes, b"x" * (api.MAX_API_PAYLOAD + 1))


def test_nothing_is_not_a_message(pair):
    alice, bob = pair
    with pytest.raises(api.ApiMessageError, match="nothing to send"):
        api.seal(alice, bob.public_bytes, b"")


def test_the_body_is_bytes_and_json_is_a_convenience(pair):
    """The channel carries bytes. A caller that wants to send something that is
    not JSON is entitled to, and gets None rather than an exception."""
    message = api.ApiMessage(id=1, txid="t", height=1, block_time=0,
                             sender_pubkey=b"\x01" * 32, sender_address="nA",
                             body=b'{"a": [1, 2]}')
    assert message.json() == {"a": [1, 2]}
    assert message.text == '{"a": [1, 2]}'

    other = api.ApiMessage(id=2, txid="t", height=1, block_time=0,
                           sender_pubkey=b"\x01" * 32, sender_address="nA",
                           body=b"\x00\x01 not json")
    assert other.json() is None


# --- the queue ----------------------------------------------------------------


def test_a_program_can_resume_exactly_where_it_stopped(tmp_path):
    """A cursor, not a timestamp: two messages can share a block time, and a
    queue read twice must not deliver the same work twice."""
    store = MessageStore(tmp_path / "m.sqlite")
    for n in range(5):
        store.add_api_message("test", f"tx{n}", 100 + n, 1000, "nThem",
                              b"\x02" * 32, "me", f'{{"n":{n}}}'.encode())

    first = store.api_messages("me", "test", limit=2)
    assert [r["txid"] for r in first] == ["tx0", "tx1"]

    after = first[-1]["id"]
    rest = store.api_messages("me", "test", after_id=after)
    assert [r["txid"] for r in rest] == ["tx2", "tx3", "tx4"]
    assert store.api_messages("me", "test", after_id=rest[-1]["id"]) == []
    store.close()


def test_read_and_unread_are_separate_from_the_cursor(tmp_path):
    store = MessageStore(tmp_path / "m.sqlite")
    for n in range(3):
        store.add_api_message("test", f"tx{n}", 1, 0, "nThem", b"\x02" * 32,
                              "me", b"{}")
    rows = store.api_messages("me", "test")
    assert store.mark_api_read([rows[0]["id"], rows[1]["id"]]) == 2
    assert store.mark_api_read([rows[0]["id"]]) == 0, "marking twice changes nothing"
    assert len(store.api_messages("me", "test", unread_only=True)) == 1
    assert len(store.api_messages("me", "test")) == 3, "the cursor still sees all"
    store.close()


def test_one_node_does_not_read_anothers_queue(tmp_path):
    store = MessageStore(tmp_path / "m.sqlite")
    store.add_api_message("test", "tx1", 1, 0, "nThem", b"\x02" * 32, "me", b"{}")
    store.add_api_message("test", "tx2", 1, 0, "nThem", b"\x02" * 32, "somebody", b"{}")
    assert [r["txid"] for r in store.api_messages("me", "test")] == ["tx1"]
    store.close()


def test_the_same_transaction_is_never_queued_twice(tmp_path):
    """A rescan reads the chain again and must not redeliver work."""
    store = MessageStore(tmp_path / "m.sqlite")
    for _ in range(3):
        store.add_api_message("test", "tx1", 1, 0, "nThem", b"\x02" * 32, "me", b"{}")
    assert len(store.api_messages("me", "test")) == 1
    store.close()


def test_machine_traffic_stays_out_of_the_conversation():
    """The scanner routes on the envelope type. A chat full of machine chatter
    the reader cannot act on is worse than no chat at all."""
    import inspect

    from arcade.messaging import scanner

    source = inspect.getsource(scanner.Scanner.open_pending)
    assert "add_api_message" in source
    assert source.index("TYPE_API") < source.index("_store_message"), (
        "an API message must be routed away before it reaches the thread")


# --- are we speaking the same API? ---------------------------------------------


def test_every_message_says_what_api_it_speaks(pair):
    """Two nodes that disagree about the API is the failure this catches, and
    catching it at read time beats the alternative: a command that means one
    thing here and another there, acted on in good faith."""
    alice, bob = pair
    payload = api.seal(alice, bob.public_bytes, b'{"cmd":"ping"}')

    sender, protocol, fingerprint, body = api.open_stamped(bob, payload)
    assert sender == alice.public_bytes
    assert protocol == api.PROTOCOL
    assert fingerprint == api.api_fingerprint()
    assert api.compatible(protocol, fingerprint)
    assert body == b'{"cmd":"ping"}', "the stamp is not part of the message"


def test_a_different_api_is_visible_rather_than_silent(pair):
    alice, bob = pair
    assert not api.compatible(api.PROTOCOL, bytes(4))
    assert not api.compatible(99, api.api_fingerprint())


def test_the_fingerprint_follows_the_api_not_the_build():
    """It changes when the API changes and not when something unrelated does:
    two nodes on different commits that speak the same API are compatible, and
    saying so is more useful than insisting on identical builds."""
    same = api.api_fingerprint(["da_send", "da_inbox"])
    assert same == api.api_fingerprint(["da_inbox", "da_send"]), "order is not API"
    assert same != api.api_fingerprint(["da_send", "da_inbox", "da_trade"])
    assert len(api.api_fingerprint()) == 4


def test_the_stamp_cannot_be_edited_in_flight(pair):
    """It rides inside the ciphertext. A stamp on the outside could be changed
    by anyone who relayed the transaction."""
    alice, bob = pair
    payload = api.seal(alice, bob.public_bytes, b"x")
    # Every byte of the sealed part is covered by the MAC; flip one.
    broken = bytearray(payload)
    broken[-1] ^= 0xFF
    with pytest.raises(EnvelopeError):
        api.open_stamped(bob, bytes(broken))


def test_a_message_from_before_the_stamp_still_reads(pair):
    """Refusing to hand over bytes that opened perfectly well would be the
    worse failure. Protocol 0 means "unstamped, decide for yourself"."""
    alice, bob = pair
    from arcade.messaging.envelope import seal_message
    raw = seal_message(alice, bob.public_bytes, Header(type=TYPE_API), b"older")
    sender, protocol, fingerprint, body = api.open_stamped(bob, raw)
    assert (protocol, fingerprint, body) == (0, b"", b"older")
    assert not api.compatible(protocol, fingerprint)


def test_the_inbox_reports_compatibility(tmp_path):
    store = MessageStore(tmp_path / "m.sqlite")
    store.add_api_message("test", "tx1", 1, 0, "nThem", b"\x02" * 32, "me",
                          b"{}", protocol=api.PROTOCOL,
                          fingerprint=api.api_fingerprint())
    store.add_api_message("test", "tx2", 1, 0, "nThem", b"\x02" * 32, "me",
                          b"{}", protocol=api.PROTOCOL, fingerprint=bytes(4))
    rows = store.api_messages("me", "test")
    assert api.compatible(rows[0]["protocol"], bytes(rows[0]["fingerprint"]))
    assert not api.compatible(rows[1]["protocol"], bytes(rows[1]["fingerprint"]))
    store.close()
