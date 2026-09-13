"""Token transactions built through the wallet, read back by the ledger index.

The whole path the /tokens pages use: build with `TokenSender`, broadcast,
mine, and confirm the `LedgerIndex` reads what was meant -- with the sender
and recipient the engine derives, not the ones the builder intended.
"""

import contextlib
import dataclasses

import pytest

from arcade import payload as P
from arcade.ledger import LedgerIndex
from arcade.tokens import (
    TokenError, TokenSender, change_issuer_payload, grant_payload, issuance_payload,
    revoke_payload, send_payload,
)
from test_end_to_end import chain  # noqa: F401  (fixture re-export)


@pytest.fixture
def setup(tmp_path, chain):
    node, alice, bob = chain
    params = dataclasses.replace(
        node.params,
        activation_height=node.rpc.get_block_count() + 1,
        marker_address=node.rpc.call("getnewaddress"),
    )
    index = LedgerIndex(tmp_path / "ledger.sqlite", params,
                        lambda: contextlib.nullcontext(node.rpc))
    return node, alice, bob, params, index, TokenSender(node.rpc, params)


def mine_and_sync(node, index):
    node.generate(1)
    assert index.sync() is not None, index.stopped


def test_create_send_grant_revoke_and_hand_over(setup):
    node, alice, bob, params, index, sender = setup

    # Create: fixed supply, Class C, no recipient.
    prepared = sender.prepare(alice, issuance_payload(
        name="Arcade Token", divisible=True, managed=False, amount=1_000 * 10**8))
    assert prepared.encoding_class == "C"
    assert prepared.sender == alice and prepared.reference is None
    assert prepared.dust_sats == 0 and prepared.fee_sats > 0
    kinds = [o["where"] for o in prepared.outputs]
    assert "token data (OP_RETURN)" in kinds
    assert any(o["is_change"] for o in prepared.outputs), "change must return to the sender"
    txid = sender.broadcast(prepared)
    mine_and_sync(node, index)
    (prop,) = index.properties()
    assert prop["name"] == "Arcade Token" and prop["issuer"] == alice
    assert prop["creation_txid"] == txid
    assert index.balance(alice, prop["property_id"]) == 1_000 * 10**8
    pid = prop["property_id"]

    # Send: the recipient output is what the reference rule lands on.
    prepared = sender.prepare(alice, send_payload(pid, 250 * 10**8), reference=bob)
    assert prepared.reference == bob and prepared.dust_sats == 1_000_000
    assert [o["where"] for o in prepared.outputs if o["is_recipient"]] == [bob]
    sender.broadcast(prepared)
    mine_and_sync(node, index)
    assert index.balance(alice, pid) == 750 * 10**8
    assert index.balance(bob, pid) == 250 * 10**8
    assert index.transaction(prepared.txid)["valid"] == 1

    # Managed supply: created empty, then granted -- once to bob, once to self.
    sender.broadcast(sender.prepare(alice, issuance_payload(
        name="Points", divisible=False, managed=True, amount=None)))
    mine_and_sync(node, index)
    managed = [p for p in index.properties() if p["name"] == "Points"][0]
    assert managed["managed"] and managed["total_tokens"] == 0
    mid = managed["property_id"]

    sender.broadcast(sender.prepare(alice, grant_payload(mid, 40, "welcome"), reference=bob))
    sender.broadcast(sender.prepare(alice, grant_payload(mid, 2)))
    mine_and_sync(node, index)
    assert index.balance(bob, mid) == 40
    assert index.balance(alice, mid) == 2
    assert index.property(mid)["total_tokens"] == 42

    # Revoke from the sender's own holding.
    sender.broadcast(sender.prepare(alice, revoke_payload(mid, 1)))
    mine_and_sync(node, index)
    assert index.balance(alice, mid) == 1
    assert index.property(mid)["total_tokens"] == 41

    # Hand the token to bob; alice can no longer grant it.
    sender.broadcast(sender.prepare(alice, change_issuer_payload(mid), reference=bob))
    mine_and_sync(node, index)
    assert index.property(mid)["issuer"] == bob
    sender.broadcast(sender.prepare(alice, grant_payload(mid, 5)))
    mine_and_sync(node, index)
    refused = index.history(property_id=mid)[0]
    assert refused["valid"] == 0 and "not the issuer" in refused["invalid_reason"]
    assert index.property(mid)["total_tokens"] == 41

    types = [h["type_name"] for h in index.history(property_id=mid)]
    assert types == ["grant", "change issuer", "revoke", "grant", "grant",
                     "create (managed supply)"]


def test_a_long_name_goes_class_b_and_reads_back(setup):
    node, alice, bob, params, index, sender = setup
    name = "A token whose name, category and description together are far too long"
    payload = issuance_payload(name=name, divisible=False, managed=False, amount=10,
                               category="Collectibles", subcategory="Arcade cabinets",
                               url="https://example.invalid/token",
                               data="Something said at length about it.")
    prepared = sender.prepare(alice, payload)
    assert prepared.encoding_class == "B"
    assert prepared.dust_sats >= 2 * 1_000_000        # marker plus at least one data output
    rows = {o["where"]: o for o in prepared.outputs}
    data = [o for o in prepared.outputs if "multisig" in o["where"]]
    assert data, "Class B carries the payload in multisig outputs"
    # The wallet's decoder lists the sender's key inside each multisig output;
    # the confirm screen once called every one of them "change back to you".
    assert not any(o["is_change"] for o in data)
    assert f"{params.marker_address} (Class B marker)" in rows
    assert [o for o in prepared.outputs if o["is_change"]] == [rows[alice]]
    sender.broadcast(prepared)
    mine_and_sync(node, index)
    (prop,) = index.properties()
    assert prop["name"] == name and prop["url"] == "https://example.invalid/token"
    assert index.balance(alice, prop["property_id"]) == 10
    assert index.transaction(prepared.txid)["encoding_class"] == "B"


def test_refusals_happen_before_anything_is_spent(setup):
    node, alice, bob, params, index, sender = setup
    with pytest.raises(TokenError, match="sending address itself"):
        sender.prepare(alice, send_payload(3, 1), reference=alice)
    with pytest.raises(TokenError, match="unreadable"):
        sender.prepare(alice, b"\x00\x00\x03\xe7")
    empty = node.rpc.call("getnewaddress")
    with pytest.raises(TokenError, match="holds 0.00000000"):
        sender.prepare(empty, send_payload(3, 1), reference=bob)
    with pytest.raises(TokenError, match="name is needed"):
        issuance_payload(name="  ", divisible=True, managed=False, amount=1)
    with pytest.raises(TokenError, match="supply"):
        issuance_payload(name="x", divisible=True, managed=False, amount=0)
    with pytest.raises(TokenError, match="NUL"):
        issuance_payload(name="a\x00b", divisible=True, managed=False, amount=1)
