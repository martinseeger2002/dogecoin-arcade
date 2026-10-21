"""An inscription published straight through a node: no offer, no browser.

`arcade.publish` is the interface's inscription (same plan, same outputs, same
funding arithmetic) with the key on this side and the node's wallet naming the
coins. Put through a real regtest node, read back by the real indexer, and asked
again to prove nothing is ever sent twice.
"""

import json
import secrets

import pytest

from arcade import inscribe
from arcade import publish
from arcade.ledger import LedgerIndex

from test_end_to_end import chain                    # noqa: F401  (fixture)
from test_inscription_e2e import inscribing, _sync   # noqa: F401  (fixture)


@pytest.fixture
def publisher(tmp_path, inscribing):                 # noqa: F811
    """A key this node cannot sign for, 60 coins on its address, a publisher."""
    node, alice, bob, params, follower, path = inscribing
    key = publish.Key(secrets.randbelow(publish.N - 1) + 1)
    address = key.address(params)
    node.rpc.call("sendtoaddress", address, 60)
    node.generate(1)
    pub = publish.Publisher(node.rpc, params, key, tmp_path / "journal")
    return pub, node, params, follower, path, address


def test_a_small_file_goes_in_one_transaction(publisher):
    pub, node, params, follower, path, address = publisher
    content = b"a small file that fits one transaction"
    out = pub.inscribe(content, "text/plain", '{"name": "Note"}', "note.txt")
    assert out["chunks"] == 1 and out["txids"] == [out["id"]] and not out["resumed"]

    _sync(node, follower)
    index = LedgerIndex(path, params, rpc_factory=lambda: node.rpc)
    rows = index.inscriptions()
    assert len(rows) == 1
    assert rows[0]["txid"] == out["id"]
    assert rows[0]["owner"] == rows[0]["creator"] == address, "the key's address, not the node's"
    assert rows[0]["json"] == '{"name": "Note"}'
    kind, body = index.inscription_content(out["id"])
    assert kind == "text/plain" and body == content


def test_a_big_file_splits_waits_a_block_and_lands(publisher):
    pub, node, params, follower, path, address = publisher
    content = bytes(range(256)) * 80              # 20,480 bytes -> 3 pieces
    assert inscribe.plan(content, "image/png").chunks == 3

    out = pub.inscribe(content, "image/png", "", "sunrise.png",
                       while_waiting=lambda: node.generate(1))
    assert out["chunks"] == 3 and len(out["txids"]) == 3 and out["id"] == out["txids"][0]

    _sync(node, follower)
    index = LedgerIndex(path, params, rpc_factory=lambda: node.rpc)
    rows = index.inscriptions()
    assert len(rows) == 1, f"one file, one inscription (mempool {len(node.rpc.call('getrawmempool'))})"
    assert rows[0]["txid"] == out["id"], "named by the piece carrying the manifest"
    assert rows[0]["chunks"] == 3 and rows[0]["creator"] == address
    kind, body = index.inscription_content(out["id"])
    assert kind == "image/png" and body == content, "byte for byte, off the chain"


def test_asked_again_nothing_goes_out_twice(publisher, tmp_path):
    pub, node, params, follower, path, address = publisher
    content = b"z" * 20_000
    first = pub.inscribe(content, "text/plain", "", "big.txt", while_waiting=lambda: node.generate(1))
    node.generate(1)
    before = node.rpc.call("getrawmempool")

    again = pub.inscribe(content, "text/plain", "", "big.txt")
    assert again["resumed"] and again["txids"] == first["txids"]
    assert node.rpc.call("getrawmempool") == before, "a file already sent sends nothing"

    # The journal lost two pieces (a stop between the broadcast and the write):
    # the split's outputs are spent, so the pieces are found, not paid for again.
    journal = tmp_path / "journal" / (__import__("hashlib").sha256(content).hexdigest() + ".json")
    data = json.loads(journal.read_text())
    data["pieces"] = {"0": data["pieces"]["0"]}
    data["txids"] = []
    journal.write_text(json.dumps(data))
    third = pub.inscribe(content, "text/plain", "", "big.txt")
    assert third["txids"] == first["txids"]
    assert node.rpc.call("getrawmempool") == before


def test_a_piece_whose_coin_another_sender_spent_goes_out_on_another_coin(publisher, tmp_path):
    """2026-10-19: the arcade on the same key spent part 5's first coin for its mesh announcement while the
    Doginal Frogs went out, and that announcement was filed as the piece -- the part never assembled. A spend
    that does not carry the piece is not the piece: it goes out again, paid from another coin."""
    from arcade import funding
    pub, node, params, follower, path, address = publisher
    content = bytes(range(256)) * 80                  # 3 pieces
    stolen = []

    def meanwhile():
        if not stolen:
            split = json.loads(next((tmp_path / "journal").glob("*.json")).read_text())["split"]
            coin = {"txid": split, "vout": 0, "value": int(round(float(
                node.rpc.call("gettxout", split, 0, True)["value"]) * 100_000_000)), "address": address}
            other = publish.payload_outputs(params, address, pub.key.pubkey, b"somebody else's message")
            stolen.append(pub._send(funding.build_one(params, address, coin, other, pub.rate, "elsewhere")))
        node.generate(1)

    out = pub.inscribe(content, "image/png", "", "frogs.png", while_waiting=meanwhile)
    assert stolen and stolen[0] not in out["txids"], "the other sender's transaction is not a piece"

    _sync(node, follower)
    index = LedgerIndex(path, params, rpc_factory=lambda: node.rpc)
    rows = index.inscriptions()
    assert [r["txid"] for r in rows] == [out["id"]]
    assert index.inscription_content(out["id"]) == ("image/png", content), "it assembles, byte for byte"


def test_a_file_too_small_to_pay_for_is_refused_before_anything_is_sent(publisher):
    pub, node, params, follower, path, address = publisher
    poor = publish.Publisher(node.rpc, params, publish.Key(7), pub.journal_home)
    with pytest.raises(publish.PublishError, match="not enough confirmed coins"):
        poor.inscribe(b"anything", "text/plain", "", "x.txt")
    assert not node.rpc.call("getrawmempool")


def test_the_arcade_index_may_name_the_coins_and_nothing_else(publisher, tmp_path):
    """`--index`: the coin list from the arcade's ledger index (read-only); the
    build, the signature and the broadcast stay here."""
    from arcade import utxos
    from arcade.ledger import LedgerIndex
    pub, node, params, follower, path, address = publisher
    index = LedgerIndex(path, params, rpc_factory=lambda: node.rpc)
    import contextlib
    with contextlib.closing(index.open()) as db:
        utxos.watch(db, address, node.rpc.get_block_count() - 1, "test")   # from the block that funded it
    follower.sync_once()                                                     # the follower indexes its coin
    via_index = publish.Publisher(node.rpc, params, pub.key, tmp_path / "journal2", index=path)
    assert isinstance(via_index.coins, publish.IndexCoins)
    assert sum(c["value"] for c in via_index.coins.unspent()) == 60 * 100_000_000

    out = via_index.inscribe(b"from the index", "text/plain", "", "idx.txt")
    assert out["id"] in node.rpc.call("getrawmempool"), "broadcast through the node, not the arcade"
    _sync(node, follower)
    assert index.inscriptions()[-1]["txid"] == out["id"]

    stranger = publish.Publisher(node.rpc, params, publish.Key(11), tmp_path / "j3", index=path)
    with pytest.raises(publish.PublishError, match="does not watch"):
        stranger.inscribe(b"x", "text/plain", "", "x.txt")
