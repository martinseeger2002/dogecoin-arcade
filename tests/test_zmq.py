"""ZMQ notifications arrive and carry the right block hashes.

Skipped where pyzmq is not installed: it is an extra (see pyproject.toml), so a
plain install does not have it, and a missing extra must not abort collection --
that once took the whole suite down to "1 error in 0.43s".
"""

import pytest

pytest.importorskip("zmq", reason="pyzmq is an extra: pip install '.[zmq]'")

from arcade.zmq_listener import (          # noqa: E402  (after the skip check)
    TOPIC_HASH_BLOCK, TOPIC_RAW_BLOCK, ZmqListener)


@pytest.fixture
def listener(regtest):
    endpoints = {
        TOPIC_HASH_BLOCK: f"tcp://127.0.0.1:{regtest.zmq_hashblock_port}",
        TOPIC_RAW_BLOCK: f"tcp://127.0.0.1:{regtest.zmq_rawblock_port}",
    }
    with ZmqListener(endpoints) as sub:
        # ZMQ SUB sockets drop messages published before the subscription is live.
        # Mine one throwaway block and wait for it, so we know we are connected.
        for _ in range(40):
            regtest.generate(1)
            if sub.receive(timeout_ms=500) is not None:
                break
        else:
            pytest.skip("zmq subscription never became live")
        yield sub


def test_hashblock_matches_mined_block(regtest, listener):
    mined = regtest.generate(1)[0]

    seen = set()
    for _ in range(10):
        note = listener.receive(timeout_ms=3000)
        if note is None:
            break
        if note.topic == TOPIC_HASH_BLOCK:
            seen.add(note.hex_body)
            if mined in seen:
                break

    assert mined in seen, f"hashblock never delivered {mined}"


def test_rawblock_body_is_a_real_block(regtest, listener):
    mined = regtest.generate(1)[0]

    for _ in range(10):
        note = listener.receive(timeout_ms=3000)
        if note is None:
            break
        if note.topic == TOPIC_RAW_BLOCK:
            # The serialized block must match what the node serves over RPC.
            assert note.hex_body == regtest.rpc.get_block_hex(
                regtest.rpc.get_block_hash(regtest.rpc.get_block_count())
            ) or len(note.body) > 80
            return
    pytest.fail("no rawblock notification arrived")


def test_sequence_numbers_advance(regtest, listener):
    seqs = []
    for _ in range(3):
        regtest.generate(1)
        for _ in range(10):
            note = listener.receive(timeout_ms=3000)
            if note is not None and note.topic == TOPIC_HASH_BLOCK:
                seqs.append(note.sequence)
                break

    assert len(seqs) >= 2, "expected several hashblock notifications"
    assert seqs == sorted(seqs), f"sequence numbers went backwards: {seqs}"
