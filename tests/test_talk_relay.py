"""Swap messages over the mesh (arcade/talk_relay.py; 2026-10-17: "Maybe the swap messages
should travel through the game server rather than the blockchain?")."""
import os
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from arcade import talk_relay as T                             # noqa: E402
from arcade.mesh.node import MAX_PAYLOAD                       # noqa: E402

KEY = "ab" * 32


def test_a_message_goes_in_pieces_the_mesh_carries_and_comes_back_whole():
    payload = os.urandom(1300)
    frames = T.pieces(payload)
    assert len(frames) == 3 and all(len(f) <= MAX_PAYLOAD for f in frames)
    relay = T.TalkRelay(lambda: None)
    for f in reversed(frames):                                  # any order
        relay._piece(KEY, f, time.time())
    got, cursor = relay.read(KEY)
    assert [bytes.fromhex(p) for _s, p in got] == [payload] and cursor == 1
    assert relay.read(KEY, after=cursor) == ([], cursor), "read from the cursor on"
    relay._piece(KEY, b"junk", time.time())
    assert relay.read(KEY, after=cursor)[0] == []


def test_nobody_reachable_means_the_chain_carries_it():
    assert T.TalkRelay(lambda: None).send(KEY, b"x") is False


def _mesh(peers=None):
    import nacl.signing
    from arcade.mesh.service import MeshService
    return MeshService("arcade-test", nacl.signing.SigningKey.generate(), listen_host="127.0.0.1",
                       listen_port=0, verify=lambda *a: True, peers=peers or []).start()


def _until(check, most=15.0):
    end = time.time() + most
    while time.time() < end:
        if check():
            return True
        time.sleep(0.1)
    return False


def test_two_nodes_carry_a_sealed_message_from_one_page_to_the_other():
    """The page on node B holds its rooms; node A's page speaks to it; B's page reads it whole."""
    a = _mesh()
    b = _mesh(peers=[("127.0.0.1", a.node.listen_port)])
    try:
        assert _until(lambda: a.node.links and b.node.links), "the two nodes link"
        ra, rb = T.TalkRelay(lambda: a), T.TalkRelay(lambda: b)
        rb.hold(KEY)
        assert _until(lambda: ra.live(KEY)), "A hears B's page is open"
        payload = os.urandom(900)
        assert ra.send(KEY, payload) is True, "carried by the mesh, not the chain"
        assert _until(lambda: rb.read(KEY)[0]), "B heard it"
        assert bytes.fromhex(rb.read(KEY)[0][0][1]) == payload
        rb.expire(now=time.time() + T.ALIVE_FOR + 1)
        assert _until(lambda: not ra.live(KEY)), "B's page closed: A falls back to the chain"
        assert ra.send(KEY, b"later") is False
    finally:
        a.stop(); b.stop()
