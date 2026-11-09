"""A profile (key announcement) speaks for the address it names only when that address signed it (2026-11-08).

The newest profile for an address is where its @name is paid on other chains (`_where_to_pay`) and the key its
messages are sealed to, so it has to come from that address. The scanner now keeps a profile that names an address
only when one of that address's coins is an input. Every real profile on our node (332) already was.
"""

import dataclasses

import pytest

from arcade.messaging.envelope import build_key_announcement
from arcade.messaging.keys import Identity
from arcade.messaging.scanner import Scanner
from arcade.messaging.sender import MessageSender
from arcade.messaging.store import MessageStore
from arcade.regtest import RegtestNode
from arcade.script import b58check_decode

ALICE_DOGE = b"\xa1" * 20
ALICE_DOGE_NEW = b"\xa2" * 20
MALLORY_DOGE = b"\x66" * 20


@pytest.fixture(scope="module")
def node():
    n = RegtestNode(require_standard=True)
    try:
        n.start()
    except RuntimeError as exc:
        n.stop()
        pytest.skip(f"regtest node unavailable: {exc}")
    n.generate(200)
    yield n
    n.stop()


@pytest.fixture(scope="module")
def params(node):
    return dataclasses.replace(node.params, activation_height=1, marker_address=node.rpc.call("getnewaddress"))


@pytest.fixture
def store(tmp_path):
    s = MessageStore(tmp_path / "s.sqlite")
    yield s
    s.close()


def _funded(node, count=1):
    """New addresses, each holding a coin, funded in ONE send: funding them one by one let the wallet spend the coin it
    had just given the first to fund the second."""
    addresses = [node.rpc.call("getnewaddress") for _ in range(count)]
    node.rpc.call("sendmany", "", {a: 500 for a in addresses})
    node.generate(1)
    return addresses if count > 1 else addresses[0]


def _announce(node, params, paid_from, payload, *, mine=True):
    sender = MessageSender(node.rpc, params)
    txid = sender.broadcast(sender.prepare(paid_from, payload, class_c=len(payload) <= 70))
    if mine:
        node.generate(1)
    return txid


def _profile(key, address, doge):
    """A profile naming `address`, with `doge` as its Dogecoin address -- the field a DOGE send to its @name pays.
    (Regtest encodes no Pepecoin main address; that one lives in the same row and is judged the same way.)"""
    return build_key_announcement(key, hash160=b58check_decode(address)[1], doge_hash160=doge, tag="alice")


def _doge(row):
    return b58check_decode(row["doge_address"])[1]


def test_a_profile_paid_for_by_somebody_else_does_not_speak_for_the_address(node, params, store):
    alice, mallory = _funded(node, 2)
    alice_key = Identity.generate().public_bytes
    _announce(node, params, alice, _profile(alice_key, alice, ALICE_DOGE))
    forged = _announce(node, params, mallory, _profile(Identity.generate().public_bytes, alice, MALLORY_DOGE))

    result = Scanner(node.rpc, params, store).scan()
    row = store.key_for(alice)
    assert _doge(row) == ALICE_DOGE, "a DOGE send to @alice still pays Alice"
    assert bytes(row["pubkey"]) == alice_key, "and a message to Alice is still sealed to Alice's key"
    assert any(forged in e and "did not sign" in e for e in result.errors), "the forgery is reported, not stored"


def test_one_waiting_in_the_pool_is_ignored_too(node, params, store):
    alice, mallory = _funded(node, 2)
    alice_key = Identity.generate().public_bytes
    _announce(node, params, alice, _profile(alice_key, alice, ALICE_DOGE))
    Scanner(node.rpc, params, store).scan()
    _announce(node, params, mallory, _profile(Identity.generate().public_bytes, alice, MALLORY_DOGE), mine=False)
    Scanner(node.rpc, params, store).scan()
    assert _doge(store.key_for(alice)) == ALICE_DOGE
    node.generate(1)


def test_the_address_itself_can_still_change_its_profile(node, params, store):
    alice = _funded(node)
    new_key = Identity.generate().public_bytes
    _announce(node, params, alice, _profile(Identity.generate().public_bytes, alice, ALICE_DOGE))
    _announce(node, params, alice, _profile(new_key, alice, ALICE_DOGE_NEW))
    Scanner(node.rpc, params, store).scan()
    row = store.key_for(alice)
    assert _doge(row) == ALICE_DOGE_NEW and bytes(row["pubkey"]) == new_key


def test_a_profile_that_names_no_address_is_its_payers_as_before(node, params, store):
    bob = _funded(node)
    key = Identity.generate().public_bytes
    _announce(node, params, bob, build_key_announcement(key))
    Scanner(node.rpc, params, store).scan()
    row = store.key_for(bob)
    assert row is not None and bytes(row["pubkey"]) == key
