"""An inscription on a real chain: built, broadcast, mined, indexed, sent.

Everything else about inscriptions is tested against payloads made in memory.
This is the one that puts them through a node: the same transaction builder the
interface uses, real coins, real blocks, and the real indexer reading them back.
"""

import dataclasses

import pytest

from arcade import inscribe
from arcade import inscriptions as I
from arcade import payload as P
from arcade.chain import ChainFollower
from arcade.db import Database
from arcade.indexer import ArcadeHandler
from arcade.ledger import LedgerIndex
from arcade.messaging.sender import MessageSender
from arcade.state import install_schema
from arcade.tokens import TokenSender

from test_end_to_end import chain                    # noqa: F401  (fixture)


@pytest.fixture
def inscribing(tmp_path, chain):                     # noqa: F811
    """A node, a funded address, and a follower watching from now."""
    node, alice, bob = chain
    marker = node.rpc.call("getnewaddress")
    params = dataclasses.replace(
        node.params,
        activation_height=node.rpc.get_block_count() + 1,
        marker_address=marker,
    )
    path = tmp_path / "inscriptions.sqlite"
    db = Database(path)
    install_schema(db)
    follower = ChainFollower(node.rpc, db, params,
                             handler=ArcadeHandler(node.rpc, params))
    yield node, alice, bob, params, follower, path
    db.close()


def _sync(node, follower, blocks: int = 8):
    """Mine until the mempool is empty, then index.

    A regtest block is only as full as the miner made it, and these
    transactions are 14 KB each: one block does not always take them all. A
    test that mined once and then asserted was testing the block template.
    """
    for _ in range(blocks):
        node.generate(1)
        follower.sync_once()
        if not node.rpc.call("getrawmempool"):
            return


def _put(node, params, address, payloads):
    """Broadcast every piece without waiting for a block between them.

    `send_all` waits for the previous transaction to confirm when it cannot
    find an independent confirmed output -- correct on a live chain and a hang
    on regtest, where nothing mines unless this test says so. Splitting first
    and mining that split gives every piece its own output, which is also what
    the interface arranges before a real inscription goes out.
    """
    sender = MessageSender(node.rpc, params, public_only=True)
    if len(payloads) > 1:
        # Sized like a real inscription's pieces, not like a message's: a
        # message chunk fits in 2 coins and an inscription chunk does not.
        each = inscribe.piece_size(inscribe.Plan(
            payloads=payloads, estimate=inscribe.estimate(len(payloads) * 7000),
            content_type="", json="", content_len=0))
        node.rpc.call("sendrawtransaction",
                      sender.split_outputs(address, len(payloads) + 2, each).hex)
        node.generate(1)
    return sender.send_all(address, payloads)


def test_a_file_goes_on_the_chain_and_comes_back(inscribing):
    """The whole round trip: plan, broadcast, mine, index, read it back."""
    node, alice, _, params, follower, path = inscribing

    content = bytes(range(256)) * 80              # 20,480 bytes -> 3 chunks
    plan = inscribe.plan(content, "image/png", '{"name": "Sunrise"}')
    assert plan.chunks == 3

    txids = _put(node, params, alice, plan.payloads)
    assert len(txids) == plan.chunks

    _sync(node, follower)
    index = LedgerIndex(path, params, rpc_factory=lambda: node.rpc)
    rows = index.inscriptions()
    assert len(rows) == 1, (
        f"one file, one inscription; mempool still holds "
        f"{len(node.rpc.call('getrawmempool'))}")

    row = rows[0]
    assert row["number"] == 0
    assert row["owner"] == row["creator"] == alice
    assert row["content_type"] == "image/png"
    assert row["content_len"] == len(content)
    assert row["chunks"] == plan.chunks
    assert row["txid"] == txids[0], "named by the transaction carrying the manifest"

    kind, body = index.inscription_content(row["txid"])
    assert kind == "image/png"
    assert body == content, "byte for byte, off the chain"
    assert index.inscription(0)["txid"] == row["txid"], "and by number"


def test_the_json_field_survives_the_round_trip(inscribing):
    node, alice, _, params, follower, path = inscribing
    written = '{"collection": "first", "traits": {"hat": true}}'

    plan = inscribe.plan(b"a small file", "text/plain", written)
    _put(node, params, alice, plan.payloads)
    _sync(node, follower)

    index = LedgerIndex(path, params, rpc_factory=lambda: node.rpc)
    assert index.inscriptions()[0]["json"] == written


def test_half_an_inscription_is_not_an_inscription(inscribing):
    """Until the last piece lands there is nothing: the pieces are on chain,
    paid for, and waiting."""
    node, alice, _, params, follower, path = inscribing

    plan = inscribe.plan(b"y" * 20_000, "text/plain")
    assert plan.chunks > 1
    _put(node, params, alice, plan.payloads[:-1])
    _sync(node, follower)

    index = LedgerIndex(path, params, rpc_factory=lambda: node.rpc)
    assert index.inscriptions() == []
    waiting = index.unfinished_inscriptions()
    assert len(waiting) == 1
    assert waiting[0]["have"] == plan.chunks - 1
    assert waiting[0]["expected"] == plan.chunks

    # Finish it and it exists.
    _put(node, params, alice, plan.payloads[-1:])
    _sync(node, follower)
    assert len(index.inscriptions()) == 1
    assert index.unfinished_inscriptions() == []


def test_an_inscription_can_be_sent_to_somebody_else(inscribing):
    """Built with the same TokenSender the interface uses, so this is the
    transfer that page actually makes."""
    node, alice, bob, params, follower, path = inscribing

    plan = inscribe.plan(b"a thing worth owning", "text/plain")
    _put(node, params, alice, plan.payloads)
    _sync(node, follower)

    index = LedgerIndex(path, params, rpc_factory=lambda: node.rpc)
    row = index.inscriptions()[0]
    assert row["owner"] == alice

    payload = P.AnyData(data=I.Transfer(txid=bytes.fromhex(row["txid"])).encode()).encode()
    tokens = TokenSender(node.rpc, params)
    prepared = tokens.prepare(alice, payload, bob)
    assert prepared.reference == bob, "the reference output is who gets it"
    tokens.broadcast(prepared)
    _sync(node, follower)

    assert index.inscription(row["txid"])["owner"] == bob
    assert [r["txid"] for r in index.inscriptions(owner=bob)] == [row["txid"]]
    assert index.inscriptions(owner=alice) == []
    assert index.inscription(row["txid"])["creator"] == alice, "who made it does not move"


def test_only_the_owner_can_send_it_on_a_real_chain(inscribing):
    node, alice, bob, params, follower, path = inscribing

    plan = inscribe.plan(b"mine", "text/plain")
    _put(node, params, alice, plan.payloads)
    _sync(node, follower)
    index = LedgerIndex(path, params, rpc_factory=lambda: node.rpc)
    row = index.inscriptions()[0]

    # Bob tries to send alice's inscription to himself. He has to name somebody
    # else as the reference -- a transaction cannot pay itself -- so he names a
    # third address he also controls, which is the closest a thief gets.
    elsewhere = node.rpc.call("getnewaddress")
    payload = P.AnyData(data=I.Transfer(txid=bytes.fromhex(row["txid"])).encode()).encode()
    tokens = TokenSender(node.rpc, params)
    node.rpc.call("sendtoaddress", bob, 5)
    _sync(node, follower)
    tokens.broadcast(tokens.prepare(bob, payload, elsewhere))
    _sync(node, follower)

    assert index.inscription(row["txid"])["owner"] == alice, "it did not move"


def test_a_transfer_costs_one_small_transaction(inscribing):
    """Moving one must never cost what making one cost: the payload is 38
    bytes, so it fits an OP_RETURN."""
    node, alice, bob, params, follower, path = inscribing

    plan = inscribe.plan(b"x" * 9000, "text/plain")
    MessageSender(node.rpc, params, public_only=True).ensure_outputs(alice, plan.chunks)
    _put(node, params, alice, plan.payloads)
    _sync(node, follower)
    index = LedgerIndex(path, params, rpc_factory=lambda: node.rpc)
    row = index.inscriptions()[0]

    payload = P.AnyData(data=I.Transfer(txid=bytes.fromhex(row["txid"])).encode()).encode()
    prepared = TokenSender(node.rpc, params).prepare(alice, payload, bob)
    assert prepared.encoding_class == "C", "an OP_RETURN, not a Class B pile"
    assert prepared.size < 1000, prepared.size


def test_a_reorg_is_proven_at_the_engine_rather_than_here():
    """Deliberately not done against the shared node.

    `invalidateblock` rewinds a chain that every other test in the module is
    still using, and the assertion it buys is already made precisely in
    tests/test_inscription_ledger.py, where a block can be rolled back without
    a node having an opinion about it.
    """
    import pathlib as _p

    source = _p.Path("tests/test_inscription_ledger.py").read_text()
    assert "def test_a_reorg_takes_it_all_back" in source
    assert "rollback_block" in source


def test_a_hashlips_set_is_filed_as_a_collection_from_the_chain(inscribing):
    """Two items with HashLips JSON, no other hint: the index reads the set
    from what is on the chain, so every node files it the same way."""
    import json

    node, alice, _, params, follower, path = inscribing
    for edition in (2, 1):                        # out of order on purpose
        item = {"name": f"Doge Punks #{edition}", "edition": edition,
                "attributes": [{"trait_type": "Hat", "value": "Cap"}]}
        plan = inscribe.plan(bytes([edition]) * 40, "image/png",
                             json.dumps(item, separators=(",", ":")))
        _put(node, params, alice, plan.payloads)
        # A block between them: the second would otherwise wait for the
        # first's change to confirm, and nothing mines here unasked.
        _sync(node, follower)

    index = LedgerIndex(path, params, rpc_factory=lambda: node.rpc)
    sets = index.collections()
    assert [(s["creator"], s["collection"], s["count"]) for s in sets] == [
        (alice, "Doge Punks", 2)]
    items = index.collection_items(alice, "Doge Punks")
    assert [i["edition"] for i in items] == [1, 2], "by edition, not by arrival"
    assert items[0]["number"] == 1, "edition 1 arrived second"
    assert index.collection_traits(alice, "Doge Punks") == {"Hat": {"Cap": 2}}
