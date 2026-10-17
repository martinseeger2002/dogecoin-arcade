"""Orders on the chain (arcade/trade_orders.py) and the mesh heartbeat that decides who is
online (arcade/mesh/node.py last_seen/online).

2026-10-13: a facilitating node's orders "should be visible to other node operators
as long as that node is online. They can be inscribed like any other order the only
difference is that the node operator is the one who is doing the listing."
"""

import json
import pathlib
import secrets
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                               # noqa: F401,E402

from arcade import trade_orders as O                                  # noqa: E402

COIN = 100_000_000
LISTER, OTHER = "nListerAddressXXXXXXXXXXXXXXXXXXXXX", "nSomebodyElseXXXXXXXXXXXXXXXXXXXXX"


def _order(oid="order-0001", node=None, expires=None, **over):
    return {"id": oid, "give": {"network": "doge", "kind": "coin", "asset": "", "amount": 2 * COIN},
            "get": {"network": "main", "kind": "coin", "asset": "", "amount": 3 * COIN},
            "trade_key": "02" + secrets.token_hex(32), "msg_key": secrets.token_hex(32),
            "node": node or secrets.token_hex(32), "expires": expires or int(time.time()) + 3600,
            **over}


def test_an_order_is_checked_field_by_field():
    good = O.check_order(_order())
    assert good["give"]["network"] == "doge" and good["get"]["amount"] == 3 * COIN
    for bad in (_order(oid="x"), _order(expires=int(time.time()) - 1),
                _order(expires=int(time.time()) + O.MAX_EXPIRY + 60),
                _order(trade_key="zz"), _order(node="00"),
                _order(give={"network": "mars", "kind": "coin", "asset": "", "amount": 1}),
                _order(get={"network": "doge", "kind": "coin", "asset": "", "amount": 1})):
        with pytest.raises(O.OrderError):
            O.check_order(bad)


def test_a_batch_files_its_orders_and_only_the_lister_cancels(tmp_path):
    book = O.OrderBook(tmp_path / "orders.sqlite")
    one, two = _order("order-0001"), _order("order-0002")
    assert book.file_batch(LISTER, "test", "aa" * 32, O.batch_json([one, two])) == 2
    everyone = lambda node: True
    assert {o["id"] for o in book.visible(everyone)} == {"order-0001", "order-0002"}
    book.file_batch(OTHER, "test", "bb" * 32, O.batch_json([], cancels=["order-0001"]))
    assert {o["id"] for o in book.visible(everyone)} == {"order-0001", "order-0002"}, \
        "somebody else cannot cancel your order"
    book.file_batch(LISTER, "test", "cc" * 32, O.batch_json([], cancels=["order-0001"]))
    assert [o["id"] for o in book.visible(everyone)] == ["order-0002"]
    assert book.visible(everyone)[0]["lister"] == LISTER, "listed by whoever inscribed it"


def test_an_order_shows_only_while_its_node_is_online_and_until_it_expires(tmp_path):
    book = O.OrderBook(tmp_path / "orders.sqlite")
    up, down = secrets.token_hex(32), secrets.token_hex(32)
    book.file_batch(LISTER, "test", "aa" * 32, O.batch_json(
        [_order("order-0001", node=up), _order("order-0002", node=down)]))
    shown = book.visible(lambda node: node == up)
    assert [o["id"] for o in shown] == ["order-0001"]
    assert book.visible(lambda node: True, now=time.time() + 7200) == [], "expired"


def test_orders_are_read_off_the_chain_where_they_were_inscribed(client, monkeypatch, tmp_path):
    import arcade.ledger as L
    app, state = client
    index = state.token_index(state.messaging)
    bodies = {"d1" * 32: O.batch_json([_order("order-0001")]),
              "d2" * 32: b"not an order batch",
              "d3" * 32: O.batch_json([_order("order-0002")], cancels=["order-0001"])}
    with index.open() as db:
        for n, txid in enumerate(bodies):
            db.conn.execute(
                "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,block_height,position,"
                "content_type,content_len,sha256,json,chunks,content) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (txid, 900 + n, LISTER, LISTER, 1, 0, O.CONTENT_TYPE, 10, "ab" * 32, "{}", 1, b""))
        db.conn.commit()
    monkeypatch.setattr(L.LedgerIndex, "inscription_content",
                        lambda self, key: (O.CONTENT_TYPE, bodies[key]) if key in bodies else None)
    book = O.OrderBook(tmp_path / "orders.sqlite")
    assert O.import_announced(book, index, "regtest") == 2
    assert [o["id"] for o in book.visible(lambda n: True)] == ["order-0002"]
    assert O.import_announced(book, index, "regtest") == 0, "a cursor: each batch is read once"


def test_the_mesh_heartbeat_says_who_is_online():
    import nacl.signing
    from arcade.mesh.node import MeshNode, SUB_EVERY
    me = MeshNode(nacl.signing.SigningKey.generate(), "arcade-test")
    other = "ab" * 32
    assert me.online(me.node_id), "a node is online to itself"
    assert not me.online(other) and me.last_seen(other) is None
    me._heard[other] = time.time()
    assert me.online(other)
    assert not me.online(other, now=time.time() + 3.5 * SUB_EVERY + 1), "silent too long: offline"
