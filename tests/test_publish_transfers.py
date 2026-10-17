"""Transfers straight through a node (D-195): a token send, a grant, an NFT.

A game's bank pays players this way (examples/rpc_bank): the key here,
the coins from the node, the bytes from the arcade's own builders, the
broadcast through the node -- and the real ledger index reading the block to
say who holds what afterwards. Then the two promises the Bank depends on: an
intent asked for twice is paid once, and a transaction that does not read back
as meant is never sent.
"""

import contextlib
import dataclasses
import json
import secrets

import pytest

from arcade import fees, publish
from arcade.ledger import LedgerIndex
from arcade.tokens import issuance_payload
from test_end_to_end import chain                    # noqa: F401  (fixture)


@pytest.fixture
def bank(tmp_path, chain):                           # noqa: F811
    """A key the node cannot sign for, its coins split so transfers can each
    take a confirmed one, a managed token and a fixed one it issued, and an
    index following from before any of it."""
    node, alice, bob = chain
    params = dataclasses.replace(node.params, activation_height=node.rpc.get_block_count() + 1,
                                 marker_address=node.rpc.call("getnewaddress"))
    index = LedgerIndex(tmp_path / "ledger.sqlite", params, lambda: contextlib.nullcontext(node.rpc))
    key = publish.Key(secrets.randbelow(publish.N - 1) + 1)
    pub = publish.Publisher(node.rpc, params, key, tmp_path / "journal")
    node.rpc.call("sendtoaddress", pub.address, 50)
    node.generate(1)
    pub.split(12, fees.COIN, "test split")
    node.generate(1)

    def issue(**kw):
        body = issuance_payload(**kw)
        outputs = publish.payload_outputs(params, pub.address, key.pubkey, body, wrap=False)
        pub._send(publish.build(pub.address, pub.coins.confirmed(), outputs, pub.rate, "issue"))
        sync()

    def sync():
        node.generate(1)
        assert index.sync() is not None, index.stopped

    issue(name="GOLD", divisible=False, managed=False, amount=1000)
    issue(name="ORE", divisible=False, managed=True, amount=None)
    gold, ore = (next(p["property_id"] for p in index.properties() if p["name"] == n) for n in ("GOLD", "ORE"))
    return node, params, index, pub, sync, gold, ore, bob


def test_a_token_send_a_grant_and_an_nft_each_land_where_meant(bank):
    node, params, index, pub, sync, gold, ore, bob = bank

    sent = pub.transfer_token(gold, 30, bob, "job 1: 30 GOLD to bob")
    granted = pub.grant_token(ore, 7, bob, "job 2: 7 ORE to bob")
    piece = pub.inscribe(b"a sword", "text/plain", '{"name": "Sword #1"}', "sword.txt")["id"]
    sync()
    moved = pub.transfer_nft(piece, bob, "job 3: the sword to bob")
    assert moved["txid"] in node.rpc.call("getrawmempool"), "broadcast through the node"
    sync()

    assert index.balance(bob, gold) == 30 and index.balance(pub.address, gold) == 970
    assert index.balance(bob, ore) == 7 and index.property(ore)["total_tokens"] == 7
    for txid in (sent["txid"], granted["txid"]):
        assert index.transaction(txid)["valid"] == 1, index.transaction(txid)
    row = index.inscription(piece)
    assert row["owner"] == bob and row["creator"] == pub.address


def test_the_same_intent_is_paid_once_and_lost_bytes_go_out_again_unchanged(bank):
    node, params, index, pub, sync, gold, ore, bob = bank
    first = pub.transfer_token(gold, 5, bob, "job 9")
    again = pub.transfer_token(gold, 5, bob, "job 9")
    assert again["resumed"] and again["txid"] == first["txid"]
    assert node.rpc.call("getrawmempool").count(first["txid"]) == 1

    # A process that journaled and then lost the node before the broadcast
    # (no answer at all, so nobody knows): a new publisher reading the same
    # journal sends exactly those bytes, once.
    dry = pub.transfer_token(gold, 6, bob, "job 10", dry=True)
    assert dry["dry"] and pub.transfers.get(publish.intent_key("job 10")) is None, "a dry run journals nothing"
    real_call = pub.rpc.call

    def lost(method, *args):
        if method == "sendrawtransaction":
            raise publish.RpcTransportError("connection reset")
        return real_call(method, *args)

    pub.rpc.call = lost
    try:
        with pytest.raises(publish.RpcTransportError):
            pub.transfer_token(gold, 6, bob, "job 10")
    finally:
        pub.rpc.call = real_call
    line = pub.transfers.get(publish.intent_key("job 10"))
    assert line and line["txid"] not in node.rpc.call("getrawmempool")
    resumed = publish.Publisher(node.rpc, params, pub.key, pub.journal_home).transfer_token(gold, 6, bob, "job 10")
    assert resumed["resumed"] and resumed["txid"] == line["txid"]
    sync()
    assert publish.Publisher(node.rpc, params, pub.key, pub.journal_home).transfer_token(gold, 6, bob, "job 10")["resumed"]
    assert index.balance(bob, gold) == 11, "two intents, two payments, never three"

    lines = [json.loads(x) for x in (pub.journal_home / "transfers.jsonl").read_text().splitlines()]
    assert [x.get("intent") for x in lines if x.get("intent", "").startswith("job")] == ["job 9", "job 10"]


def test_a_refused_broadcast_leaves_the_intent_free_and_nothing_sent(bank, monkeypatch):
    node, params, index, pub, sync, gold, ore, bob = bank
    real_call = pub.rpc.call

    def refusing(method, *args):
        if method == "sendrawtransaction":
            raise publish.RpcError(-26, "min relay fee not met", method)
        return real_call(method, *args)

    monkeypatch.setattr(pub.rpc, "call", refusing)
    with pytest.raises(publish.PublishError, match="Nothing went out"):
        pub.transfer_token(gold, 3, bob, "job 20")
    monkeypatch.setattr(pub.rpc, "call", real_call)
    assert pub.transfers.get(publish.intent_key("job 20")) is None
    reread = publish.Publisher(node.rpc, params, pub.key, pub.journal_home)
    assert reread.transfers.get(publish.intent_key("job 20")) is None, "the refusal is in the journal too"
    ok = reread.transfer_token(gold, 3, bob, "job 20")
    sync()
    assert index.balance(bob, gold) == 3 and not ok["resumed"]


def test_a_transaction_that_does_not_read_back_as_meant_is_never_sent(bank, monkeypatch):
    node, params, index, pub, sync, gold, ore, bob = bank
    carol = node.rpc.call("getnewaddress")
    # The reader says it pays carol: nothing is journaled, nothing broadcast.
    import arcade.tx as txlib
    original = txlib.extract

    def misread(*a, **kw):
        got = original(*a, **kw)
        return dataclasses.replace(got, reference=carol)

    monkeypatch.setattr(txlib, "extract", misread)
    before = node.rpc.call("getrawmempool")
    with pytest.raises(publish.PublishError, match="refusing to send: it reads as paying"):
        pub.transfer_token(gold, 4, bob, "job 30")
    assert node.rpc.call("getrawmempool") == before
    assert pub.transfers.get(publish.intent_key("job 30")) is None


def test_refusals_before_anything_is_built(bank):
    node, params, index, pub, sync, gold, ore, bob = bank
    with pytest.raises(publish.PublishError, match="needs an intent"):
        pub.transfer_token(gold, 1, bob, " ")
    with pytest.raises(publish.PublishError, match="own"):
        pub.transfer_token(gold, 1, pub.address, "job 40")
    with pytest.raises(publish.PublishError, match="not a regtest address"):
        pub.transfer_token(gold, 1, publish.Key(5).address(publish.TESTNET), "job 41")
    with pytest.raises(publish.PublishError, match="not an inscription id"):
        pub.transfer_nft("abc", bob, "job 42")
    assert not node.rpc.call("getrawmempool")


def test_small_coins_are_swept_into_one_and_a_waiting_split_is_left_alone(bank):
    node, params, index, pub, sync, gold, ore, bob = bank
    crumbs = pub.split(25, 2 * fees.COIN // 100, "test crumbs")          # 25 coins of 0.02
    waiting = pub.split(3, 2 * fees.COIN // 100, "an inscription's split")
    (pub.journal_home / "abc.json").write_text(json.dumps({"split": waiting["txid"], "txids": []}))
    node.generate(1)
    assert pub.consolidate(5 * fees.COIN // 100, "sweep 1", min_inputs=30) is None, "fewer than asked: nothing"
    out = pub.consolidate(5 * fees.COIN // 100, "sweep 1", min_inputs=20)
    assert out["inputs"] == 25 and out["txid"] in node.rpc.call("getrawmempool")
    assert out["value"] == 25 * 2 * fees.COIN // 100 - out["fee"]
    assert pub.consolidate(5 * fees.COIN // 100, "sweep 1")["resumed"], "the same intent sweeps once"
    node.generate(1)
    left = {(c["txid"]) for c in pub.coins.confirmed() if c["value"] < 5 * fees.COIN // 100}
    assert crumbs["txid"] not in left and waiting["txid"] in left, "the inscription's pieces are still there"
    sent = pub.transfer_token(gold, 2, bob, "after the sweep")
    sync()
    assert index.balance(bob, gold) == 2 and sent["txid"]
