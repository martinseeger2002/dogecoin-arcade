"""One swap order book across arcades, over the mesh (arcade/book_relay.py; 2026-10-19: "They should share the
same order books ... They should work across Dogecoin arcades" and "Books should be updated as instantaneously as
possible. So no two users can fill the same order")."""
import json
import pathlib
import secrets
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from arcade import trade_orders as O                            # noqa: E402
from arcade.book_relay import BookRelay                         # noqa: E402
from test_talk_relay import _mesh, _until                       # noqa: E402

COIN = 100_000_000
LISTER = "nListerAddressXXXXXXXXXXXXXXXXXXXXX"


def _order(node, oid=None):
    return {"id": oid or f"ord-{secrets.token_hex(8)}",
            "give": {"network": "main", "kind": "coin", "asset": "", "amount": 2 * COIN},
            "get": {"network": "doge", "kind": "coin", "asset": "", "amount": 3 * COIN},
            "trade_key": "02" + secrets.token_hex(32), "msg_key": secrets.token_hex(32),
            "node": node, "expires": int(time.time()) + 3600}


def _state(book, oid):
    with book._open() as conn:
        row = conn.execute("SELECT cancelled FROM trade_order WHERE id=?", (oid,)).fetchone()
    return None if row is None else {0: "live", 1: "gone", 2: "taking"}[row["cancelled"]]


def _pump(*relays):
    for r in relays:
        r.tick()


def test_an_order_its_taking_and_its_cancel_reach_the_other_arcade(tmp_path):
    a = _mesh()
    b = _mesh(peers=[("127.0.0.1", a.node.listen_port)])
    try:
        assert _until(lambda: a.node.links and b.node.links), "the two arcades link"
        book_a, book_b = O.OrderBook(tmp_path / "a.sqlite"), O.OrderBook(tmp_path / "b.sqlite")
        ra, rb = BookRelay(lambda: a, book_a, lambda: None), BookRelay(lambda: b, book_b, lambda: None)
        _pump(ra, rb)
        assert _until(lambda: (_pump(ra, rb), a.node.room_live("arcade-book") and b.node.room_live("arcade-book"))[1])
        time.sleep(1.0)                                         # each knows the other's member (HERE)
        order = _order(a.node_id)
        book_a.file_batch(LISTER, "main", "node-x", json.dumps({"arcade": "orders", "v": 1,
                                                                "orders": [order]}).encode())
        assert _until(lambda: (_pump(ra, rb), _state(book_b, order["id"]) == "live")[1]), "B's book has A's order"
        book_a.set_taking(order["id"], True)
        assert _until(lambda: (_pump(ra, rb), _state(book_b, order["id"]) == "taking")[1]), "off B's book at once"
        book_a.set_taking(order["id"], False)
        assert _until(lambda: (_pump(ra, rb), _state(book_b, order["id"]) == "live")[1]), "back when it falls through"
        book_a.cancel_id(order["id"])
        assert _until(lambda: (_pump(ra, rb), _state(book_b, order["id"]) == "gone")[1]), "and gone when withdrawn"
    finally:
        a.stop(); b.stop()


def test_an_arcade_speaks_only_for_its_own_orders(tmp_path):
    """What arrives from node X is filed only for orders whose home is X: nobody cancels or takes another's."""
    book = O.OrderBook(tmp_path / "b.sqlite")
    theirs, other = "aa" * 32, "bb" * 32
    order = _order(other)
    book.file_batch(LISTER, "main", "chain", json.dumps({"arcade": "orders", "v": 1, "orders": [order]}).encode())
    book.apply_from(theirs, [{"id": order["id"], "lister": LISTER, "network": "main", "body": order, "state": "gone"}])
    book.apply_from(theirs, [], full=True)
    assert _state(book, order["id"]) == "live", "X cannot cancel an order whose home is Y"
    forged = _order(other)
    book.apply_from(theirs, [{"id": forged["id"], "lister": LISTER, "network": "main", "body": forged,
                              "state": "live"}])
    assert _state(book, forged["id"]) is None, "nor file one in Y's name"


def test_a_full_statement_drops_what_its_home_no_longer_has(tmp_path):
    book = O.OrderBook(tmp_path / "b.sqlite")
    home = "cc" * 32
    keep, drop = _order(home), _order(home)
    rows = [{"id": o["id"], "lister": LISTER, "network": "main", "body": o, "state": "live"} for o in (keep, drop)]
    book.apply_from(home, rows)
    book.apply_from(home, rows[:1], full=True)
    assert _state(book, keep["id"]) == "live" and _state(book, drop["id"]) == "gone"


def test_a_borrowed_chain_is_asked_of_the_fastest_sharer():
    """2026-10-19: "whichever node has the fastest ping should be the one that that arcade uses". One failure
    lately does not send a fast sharer behind a slow one; three do."""
    from arcade.mesh.coreshare import CoreShare

    class Node:
        links = {"fast": 1, "slow": 1}

        def rtts(self):
            return {"fast": 40.0, "slow": 900.0}
    share = CoreShare.__new__(CoreShare)
    share.node, share.offers, share.failures = Node(), {"fast": {"doge"}, "slow": {"doge"}}, {"fast": 1}
    assert share.sharers("doge") == ["fast", "slow"]
    share.failures["fast"] = 3
    assert share.sharers("doge") == ["slow", "fast"]
