"""An offer is readable the moment it is broadcast, not when its block lands.

Danny offered for Goofball #100 and neither wallet showed anything: his said he
had made no offers, and the holder's said nothing had been offered. It was in
the mempool for four minutes and confirmed at block 1,490,734, at which point
both of them showed it. That wait is the whole of what this removes.

The rule it must not break: the ledger is built from blocks and only from
blocks. What is read here is read fresh on every call and written down nowhere,
so an offer that never confirms leaves nothing behind, and two nodes still agree
about the chain even when their pools differ.
"""

from pathlib import Path

import pytest

from arcade import inscriptions as I
from arcade import payload as P
from arcade.config import NETWORKS
from arcade.encoding import encode_class_c
from arcade.ledger import LedgerIndex
from arcade.txbuild import op_return_script

PARAMS = NETWORKS["regtest"]
ITEM = "a1" * 32
HOLDER = "mHolderAddressThatHoldsThePiece"
BUYER = "mBuyerAddressWithSomethingToSpend"


def an_offer(item_txid: str = ITEM, amount: int = 150_000_000) -> bytes:
    """The bytes a Make offer puts in an OP_RETURN."""
    take = I.Leg(kind=I.LEG_COINS, amount=amount)
    return P.AnyData(data=I.Offer(txid=bytes.fromhex(item_txid), take=take).encode()).encode()


class Node:
    """A node with one transaction in its pool and nothing else to say."""

    def __init__(self, pool: dict[str, dict]):
        self.pool = pool
        self.asked: list[str] = []

    def call(self, method, *args, **kwargs):
        self.asked.append(method)
        if method == "getrawmempool":
            return list(self.pool)
        if method == "getrawtransaction":
            found = self.pool.get(args[0])
            if found is None:
                raise RuntimeError("No such mempool transaction")
            return found
        raise AssertionError(f"unexpected call {method}")


def a_transaction(txid: str, payload: bytes, sender_txid: str = "b2" * 32) -> dict:
    """A Class C transaction carrying `payload`, spending one input."""
    return {
        "txid": txid,
        "vin": [{"txid": sender_txid, "vout": 0}],
        "vout": [
            {"n": 0, "value": 0,
             "scriptPubKey": {"hex": op_return_script(encode_class_c(payload)).hex()}},
            {"n": 1, "value": 0.001,
             "scriptPubKey": {"hex": "76a914" + "11" * 20 + "88ac",
                              "addresses": [HOLDER]}},
        ],
    }


@pytest.fixture
def index(tmp_path, monkeypatch):
    """An index holding one inscription, over a node we control."""
    node = Node({})

    class Rpc:
        def __enter__(self):
            return node

        def __exit__(self, *exc):
            return False

    made = LedgerIndex(tmp_path / "ledger.sqlite", PARAMS, lambda: Rpc())
    with made.open() as db:
        db.conn.execute(
            "INSERT INTO inscription(txid,number,creator,owner,block_height,position,"
            "content_type,content_len,sha256,json,chunks,content) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (ITEM, 100, HOLDER, HOLDER, 500, 0, "image/png", 10, "cd" * 32, "", 1, None))
        db.conn.commit()
    made.node = node
    # Every input resolves to the buyer, which makes the buyer the sender.
    monkeypatch.setattr("arcade.indexer.PrevOutCache.lookup",
                        lambda self, txid, n: _prevout())
    return made


def _prevout():
    from arcade.tx import PrevOut
    from arcade.script import OutputType
    return PrevOut(address=BUYER, value=200_000_000, type=OutputType.PUBKEYHASH)


def test_an_offer_in_the_pool_is_an_offer(index):
    index.node.pool = {"c3" * 32: a_transaction("c3" * 32, an_offer())}
    pending = index.pending_offers()
    assert len(pending) == 1
    offer = pending[0]
    assert offer["inscription"] == ITEM
    assert offer["buyer"] == BUYER
    assert offer["owner"] == HOLDER
    assert offer["number"] == 100
    assert offer["take_amount"] == 150_000_000
    assert offer["block_height"] == 0 and offer["pending"], "not in a block, and says so"


def test_nothing_is_written_down(index):
    """The ledger is built from blocks. A pool is not a block."""
    index.node.pool = {"c3" * 32: a_transaction("c3" * 32, an_offer())}
    index.pending_offers()
    with index.open() as db:
        assert db.conn.execute("SELECT COUNT(*) FROM nft_offer").fetchone()[0] == 0


def test_the_refusals_are_the_ones_the_block_path_makes(index):
    """A pool offer is checked exactly as an indexed one is: same rules, same
    reasons. An offer of nothing cannot even be encoded -- `Leg` refuses a
    zero amount -- so the two that can arrive are these."""
    index.node.pool = {"c4" * 32: a_transaction("c4" * 32, an_offer(item_txid="ff" * 32))}
    assert index.pending_offers() == [], "no such inscription on this chain"

    with index.open() as db:            # the holder offering for their own
        db.conn.execute("UPDATE inscription SET owner=? WHERE txid=?", (BUYER, ITEM))
        db.conn.commit()
    index.node.pool = {"c5" * 32: a_transaction("c5" * 32, an_offer())}
    assert index.pending_offers() == [], "that one is already yours"

    with pytest.raises(I.InscriptionError):
        an_offer(amount=0)


def test_the_holder_can_answer_before_the_block(index):
    """`offer()` is what an answer is checked against (D-049), so it has to
    find one that has not confirmed yet -- otherwise accepting an offer you
    can see says there is no such offer."""
    txid = "c3" * 32
    index.node.pool = {txid: a_transaction(txid, an_offer())}
    found = index.offer(txid)
    assert found is not None and found["inscription"] == ITEM
    assert index.offer("d4" * 32) is None, "and no others"


def test_a_transaction_is_read_once(index):
    """Every page load asks the pool; only a new transaction costs a fetch."""
    txid = "c3" * 32
    index.node.pool = {txid: a_transaction(txid, an_offer())}
    index.pending_offers()
    index.node.asked.clear()
    index.pending_offers()
    assert index.node.asked.count("getrawtransaction") == 0
    assert index.node.asked.count("getrawmempool") == 1


def test_a_pool_that_empties_is_forgotten(index):
    """An offer that confirms leaves the pool; the row it had must go with it,
    or the Exchange shows it twice -- once pending, once in its block."""
    txid = "c3" * 32
    index.node.pool = {txid: a_transaction(txid, an_offer())}
    assert index.pending_offers()
    index.node.pool = {}
    assert index.pending_offers() == []


def test_a_node_that_is_not_there_is_not_an_error(index, monkeypatch):
    """A badge and a tab both call this. Neither is worth a traceback."""
    def broken():
        raise OSError("connection refused")

    monkeypatch.setattr(index, "_rpc", broken)
    assert index.pending_offers() == []
