"""Browser-listed swaps stand only while their lister's page is open (arcade/order_presence.py;
2026-10-17: "make sure that it is not listing listings that are not active")."""
import json
import pathlib
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from arcade import order_presence as P                        # noqa: E402
from arcade.mesh.node import room_id                          # noqa: E402


class Mesh:
    def __init__(self):
        self.rooms, self.left, self.remote = {}, [], set()

    def join(self, room, member, cred):
        tok = f"t{len(self.rooms)}"
        self.rooms[tok] = room
        return SimpleNamespace(token=tok)

    def touch(self, token):
        pass

    def leave(self, token):
        self.left.append(self.rooms.pop(token))

    def room_live(self, room):
        return room in self.rooms.values() or room in self.remote


def test_a_page_keeps_its_lister_live_and_silence_lets_go():
    mesh, bumps = Mesh(), []
    p = P.Presence(lambda: mesh, lambda: bumps.append(1))
    p.alive(["nA"])
    assert p.live("nA") and list(mesh.rooms.values()) == [P.room("nA")] and bumps == [1]
    p.alive(["nA"])
    assert len(mesh.rooms) == 1 and bumps == [1], "a beat keeps it; it does not join again"
    p.expire(now=time.time() + P.ALIVE_FOR - 1)
    assert p.live("nA")
    p.expire(now=time.time() + P.ALIVE_FOR + 1)
    assert not p.live("nA") and mesh.left == [P.room("nA")] and bumps == [1, 1]


def test_another_nodes_lister_is_live_while_the_mesh_says_its_room_is():
    mesh = Mesh()
    p = P.Presence(lambda: mesh, lambda: None)
    assert not p.live("nB")
    mesh.remote.add(P.room("nB"))
    assert p.live("nB")


def test_the_book_shows_a_tab_order_only_while_its_lister_is_live(tmp_path):
    from arcade.trade_orders import OrderBook
    from arcade import trade_orders as O
    from test_asset_pair import _order, _side
    book = OrderBook(tmp_path / "o.sqlite")
    orders = [{**_order("tab-0001", _side("main", 100), _side("doge", 5)), "tab": True},
              _order("node-0001", _side("main", 100), _side("doge", 5))]
    book.file_batch("nLister", "main", "ab" * 32, O.batch_json(orders))
    ids = lambda live: {o["id"] for o in book.visible(lambda n: True, tab_live=lambda a: live)}  # noqa: E731
    assert ids(True) == {"tab-0001", "node-0001"}
    assert ids(False) == {"node-0001"}, "the closed page's swap comes off; the node's own stays"
    assert book.tab_listers() == {"nLister"}
    assert {o["id"] for o in book.visible(lambda n: True, tab_live=lambda a: False,
                                          browser=lambda a: a == "nLister")} == set(), \
        "an older listing by one of this node's accounts needs its page open too"


def test_a_mesh_node_knows_a_room_live_here_or_elsewhere():
    from arcade.mesh.node import MeshNode
    n = MeshNode.__new__(MeshNode)
    n.local, n.routes = {}, {}
    assert not n.room_live("arcade-orders/nX")
    n.local[("arcade-orders/nX", "lister")] = object()
    assert n.room_live("arcade-orders/nX")
    n.local = {}
    n.routes[room_id("arcade-orders/nY")] = {"O": {"via": "p", "until": time.monotonic() + 30}}
    assert n.room_live("arcade-orders/nY")
    n.routes[room_id("arcade-orders/nY")]["O"]["until"] = time.monotonic() - 1
    assert not n.room_live("arcade-orders/nY")


def test_the_door_lets_a_page_say_it_is_here():
    from arcade.web import door
    assert door.public_path("/account/orders/alive", "POST")
