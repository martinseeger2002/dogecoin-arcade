"""The ledger index: the product's token indexer, on a real regtest chain."""

import contextlib
import dataclasses

import pytest

from arcade import payload as P
from arcade.ledger import AmountError, LedgerIndex, format_amount, parse_amount
from arcade.regtest import RegtestNode
from test_end_to_end import chain, send_class_c  # noqa: F401  (fixture re-export)


@pytest.fixture
def ledger(tmp_path, chain):
    node, alice, bob = chain
    params, index = _index_on(node, tmp_path)
    return node, alice, bob, params, index


def _index_on(node, home):
    """An index that starts reading at the block this node is on right now."""
    params = dataclasses.replace(
        node.params,
        activation_height=node.rpc.get_block_count() + 1,
        marker_address=node.rpc.call("getnewaddress"),
    )
    return params, LedgerIndex(home / "ledger.sqlite", params,
                               lambda: contextlib.nullcontext(node.rpc))


@pytest.fixture
def alone():
    """A chain of this test's own, for a thing a chain cannot survive.

    Two of the tests below change what the chain *is* rather than what an
    index knows: one mines a message type no version here can read, and the
    engine stops at that block forever -- not a state an index can be reset
    out of, because the block stays. The other orphans a block and takes the
    chain back. On the session's node either one is a booby trap for every
    file that indexes that chain afterwards; a full-suite run lost seven of
    them to the first one alone, all of them saying "the index stopped".
    """
    with RegtestNode() as node:
        node.generate(200)                    # past coinbase maturity
        alice = node.rpc.call("getnewaddress")
        node.rpc.call("sendtoaddress", alice, 1000)
        node.generate(1)
        yield node, alice


def create_fixed(node, alice, name="Fixed", amount=1_000 * 10**8, divisible=True):
    msg = P.IssuanceFixed(
        ecosystem=1, property_type=2 if divisible else 1, previous_property_id=0,
        category="c", subcategory="s", name=name, url="", data="", amount=amount,
    )
    return send_class_c(node, alice, msg.encode(), change_to=alice)


def test_sync_indexes_a_token_and_reports_where_it_stands(ledger):
    node, alice, bob, params, index = ledger
    tip = node.rpc.get_block_count()
    status = index.status(node_tip=tip)
    assert status["indexed_height"] is None
    assert status["behind"] == 0 and status["current"], "nothing to read below the start"

    txid = create_fixed(node, alice, name="Ledgered")
    node.generate(1)
    tip = node.rpc.get_block_count()
    assert index.status(node_tip=tip)["behind"] == 1

    result = index.sync()
    assert result is not None and result.connected == [tip]
    status = index.status(node_tip=tip)
    assert status["indexed_height"] == tip and status["current"]
    assert index.stopped is None

    props = index.properties()
    assert [p["name"] for p in props] == ["Ledgered"]
    prop = props[0]
    assert prop["issuer"] == alice and prop["divisible"]
    assert prop["total_display"] == "1,000"
    assert prop["holder_count"] == 1
    assert index.holders(prop["property_id"]) == [
        {"address": alice, "balance": 1_000 * 10**8, "display": "1,000"}]
    assert index.balance(alice, prop["property_id"]) == 1_000 * 10**8

    history = index.history(property_id=prop["property_id"])
    assert [h["txid"] for h in history] == [txid]
    assert history[0]["type_name"] == "create (fixed supply)"
    assert history[0]["amount_display"] == "1,000"
    assert history[0]["valid"] == 1


def test_a_send_shows_up_for_both_addresses(ledger):
    node, alice, bob, params, index = ledger
    create_fixed(node, alice, name="Moving", amount=500, divisible=False)
    node.generate(1)
    index.sync()
    pid = index.properties()[0]["property_id"]

    txid = send_class_c(node, alice, P.SimpleSend(property_id=pid, amount=120).encode(),
                        recipient=bob, change_to=alice)
    node.generate(1)
    index.sync()

    held = {(b["address"], b["balance"]) for b in index.balances([alice, bob])}
    assert held == {(alice, 380), (bob, 120)}
    assert index.holders(pid)[0]["address"] == alice
    entry = index.history(address=bob)[0]
    assert entry["txid"] == txid and entry["type_name"] == "send"
    assert entry["amount_display"] == "120" and entry["name"] == "Moving"
    assert entry["reference"] == bob
    assert index.transaction(txid)["property_id"] == pid


def test_an_unimplemented_message_stops_the_index_and_says_so(alone, tmp_path):
    """Hard rule #2, surfaced: the stop names the block and the type."""
    node, alice = alone
    params, index = _index_on(node, tmp_path)
    create_fixed(node, alice, name="Before")
    node.generate(1)
    assert index.sync() is not None

    # Version 0, type 999: nothing this version has heard of.
    send_class_c(node, alice, bytes([0, 0, 3, 231]), change_to=alice)
    node.generate(1)
    bad = node.rpc.get_block_count()

    assert index.sync() is None
    assert index.stopped is not None
    assert index.stopped.height == bad
    assert "message type 999" in index.stopped.reason
    status = index.status(node_tip=bad)
    assert not status["current"] and status["indexed_height"] == bad - 1
    # Nothing after the stop is trusted: the token before it is still there,
    # and the index does not move past the block.
    assert [p["name"] for p in index.properties()] == ["Before"]
    node.generate(1)
    assert index.sync() is None
    assert index.status()["indexed_height"] == bad - 1


def test_a_reorg_is_unwound(alone, tmp_path):
    node, alice = alone
    params, index = _index_on(node, tmp_path)
    create_fixed(node, alice, name="Kept")
    node.generate(1)
    index.sync()
    create_fixed(node, alice, name="Orphaned")
    orphan = node.generate(1)[0]
    index.sync()
    assert [p["name"] for p in index.properties()] == ["Kept", "Orphaned"]

    node.invalidate(orphan)
    node.generate(2)
    result = index.sync()
    assert result is not None and result.reorged
    names = [p["name"] for p in index.properties()]
    # The orphaned issuance is back in the mempool and gets mined again, so it
    # may reappear -- what must not happen is a duplicate or a missing "Kept".
    assert names[0] == "Kept" and names.count("Orphaned") <= 1


def test_index_without_a_start_block_is_disabled(tmp_path, chain):
    node, alice, bob = chain
    params = dataclasses.replace(node.params, activation_height=None)
    index = LedgerIndex(tmp_path / "x.sqlite", params,
                        lambda: contextlib.nullcontext(node.rpc))
    assert not index.enabled
    assert index.sync() is None and index.stopped is None
    assert index.status(node_tip=5)["behind"] is None


# --- amounts, no node needed --------------------------------------------------


@pytest.mark.parametrize("text, divisible, units", [
    ("1", True, 10**8),
    ("0.5", True, 5 * 10**7),
    ("0.00000001", True, 1),
    ("1,000.25", True, 100_025_000_000),
    ("7", False, 7),
    ("1,000", False, 1000),
])
def test_parse_amount(text, divisible, units):
    assert parse_amount(text, divisible) == units
    assert parse_amount(format_amount(units, divisible), divisible) == units


@pytest.mark.parametrize("text, divisible", [
    ("", True), ("abc", True), ("0", True), ("-1", False),
    ("0.000000001", True), ("1.5", False), ("99999999999999999999", False),
])
def test_parse_amount_refuses(text, divisible):
    with pytest.raises(AmountError):
        parse_amount(text, divisible)


def test_format_amount():
    assert format_amount(10**8, True) == "1"
    assert format_amount(123_456_789, True) == "1.23456789"
    assert format_amount(150_000_000, True) == "1.5"
    assert format_amount(0, True) == "0"
    assert format_amount(1_234_567, False) == "1,234,567"
