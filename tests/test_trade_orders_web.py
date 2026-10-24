"""The Exchange's "Swaps between arcades" (2026-10-13, trustless-trading-plan.md phases 4-5):
orders every arcade inscribed, shown while their node is online, taken by this node's own
lock-script trader -- and only while the operator has node-facilitated trades on."""

import pathlib
import secrets
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                               # noqa: F401,E402

from arcade import trade_orders as O                                  # noqa: E402

COIN = 100_000_000
NODE = "ab" * 32


def _file(state, oid="order-0001", node=NODE):
    order = {"id": oid, "give": {"network": "main", "kind": "coin", "asset": "", "amount": 2 * COIN},
             "get": {"network": "regtest", "kind": "coin", "asset": "", "amount": 3 * COIN},
             "trade_key": "02" + secrets.token_hex(32), "msg_key": secrets.token_hex(32),
             "node": node, "expires": int(time.time()) + 3600}
    state.trade_orders.file_batch("nListerAddressXXXXXXXXXXXXXXXXXXXXX", "regtest", "aa" * 32,
                                  O.batch_json([order]))


class _Mesh:
    node_id = "cd" * 32

    def __init__(self, up):
        self.up = set(up)

    def online(self, node_id):
        return node_id in self.up


def test_other_arcades_orders_show_while_their_node_is_online(client, monkeypatch):
    app, state = client
    _file(state)
    monkeypatch.setattr(state, "mesh", _Mesh({NODE}), raising=False)
    page = app.get("/exchange?tab=tokens").text
    assert "Swaps listed now" in page and 'action="/exchange/trade-orders/take"' in page
    # One way to list any pair (2026-10-15): the Start-a-market box offers every chain's coin,
    # and the swap's market page has the operator's Post form.
    assert 'value="regtest-coin"' in page and 'value="main-coin"' in page
    assert 'action="/exchange/trade-orders/place"' in app.get("/exchange/swap/regtest-coin/main-coin").text, \
        "the operator may post one (node-facilitated trades on)"
    monkeypatch.setattr(state, "mesh", _Mesh(set()), raising=False)
    page = app.get("/exchange?tab=tokens").text
    assert 'name="order" value="order-0001"' not in page, "its arcade went offline: not shown"


def test_the_operator_posts_a_swap(client, monkeypatch):
    app, state = client
    monkeypatch.setattr(state, "mesh", None, raising=False)
    said = app.post("/exchange/trade-orders/stand", data={
        "give_network": "regtest", "give_amount": "3", "get_network": "main", "get_amount": "2",
        "csrf_token": state.csrf_token}, follow_redirects=False)
    assert said.status_code == 303
    standing = state.node_trader.standing()
    assert len(standing) == 1 and standing[0]["give"] == {"network": "regtest", "kind": "coin",
                                                          "asset": "", "amount": 3 * COIN, "fee_bps": 0}


@pytest.mark.node_trades_default
def test_with_the_switch_off_the_operator_still_takes_and_lists(client, monkeypatch):
    """2026-10-19: "the operator should be able to take orders even if operator facilitated trades are
    turned off. That ... should be for clients coming through the Web portal"."""
    app, state = client
    state.set_setting("node_trades", False)
    _file(state)
    monkeypatch.setattr(state, "mesh", _Mesh({NODE}), raising=False)
    page = app.get("/exchange?tab=tokens").text
    assert "Swaps listed now" in page and 'action="/exchange/trade-orders/take"' in page
    assert "Node-facilitated trades</a> is on" not in page
    assert state.node_trader.enabled(), "the node's own trader runs whatever the switch says"


def test_a_token_swap_is_named_by_its_token(client, monkeypatch):
    from test_account_tokens import _token
    app, state = client
    _token(state, property_id=150, name="Gold")
    order = {"id": "order-gold1", "give": {"network": "regtest", "kind": "token", "asset": "150",
                                           "amount": 40 * COIN},
             "get": {"network": "main", "kind": "coin", "asset": "", "amount": 3 * COIN},
             "trade_key": "02" + secrets.token_hex(32), "msg_key": secrets.token_hex(32),
             "node": NODE, "expires": int(time.time()) + 3600}
    state.trade_orders.file_batch("nListerAddressXXXXXXXXXXXXXXXXXXXXX", "regtest", "aa" * 32,
                                  O.batch_json([order]))
    monkeypatch.setattr(state, "mesh", _Mesh({NODE}), raising=False)
    page = app.get("/exchange?tab=tokens").text
    assert "<strong>40 Gold</strong>" in page, "a token side reads as its amount and name"


def test_the_operator_posts_a_token_from_its_home_address(client, monkeypatch):
    from test_account_tokens import _balance, _token
    app, state = client
    monkeypatch.setattr(state, "mesh", None, raising=False)
    home = "nHomeAddressXXXXXXXXXXXXXXXXXXXXXXX"
    monkeypatch.setattr(state, "home_address", lambda chain: home, raising=False)
    _token(state, property_id=150, name="Gold")
    _balance(state, home, 150, 50 * COIN)
    said = dict(give_network="token:regtest:150:1", give_amount="40", get_network="main", get_amount="2",
                csrf_token=state.csrf_token)
    assert app.post("/exchange/trade-orders/stand", data=said, follow_redirects=False).status_code == 303
    standing = state.node_trader.standing()
    assert standing and standing[0]["give"] == {"network": "regtest", "kind": "token", "asset": "150",
                                                "amount": 40 * COIN, "fee_bps": 0}
    monkeypatch.setattr(state, "mesh", _Mesh(set()), raising=False)
    assert "40 Gold" in app.get("/exchange?tab=tokens").text, "this node's swap is named by its token"
    # More than the home address holds is refused.
    app.post("/exchange/trade-orders/stand", data={**said, "give_amount": "60"}, follow_redirects=False)
    assert len(state.node_trader.standing()) == 1


def test_a_swap_on_one_chain_is_sent_to_its_instant_market(client, monkeypatch):
    """2026-10-13: same-chain swaps are one atomic transaction on the chain's own
    market; lock-script orders are for two chains only."""
    from test_account_tokens import _balance, _token
    app, state = client
    monkeypatch.setattr(state, "mesh", None, raising=False)
    home = "nHomeAddressXXXXXXXXXXXXXXXXXXXXXXX"
    monkeypatch.setattr(state, "home_address", lambda chain: home, raising=False)
    _token(state, property_id=150, name="Gold")
    _balance(state, home, 150, 50 * COIN)
    app.post("/exchange/trade-orders/stand", data=dict(
        give_network="token:regtest:150:1", give_amount="40", get_network="regtest", get_amount="2",
        csrf_token=state.csrf_token), follow_redirects=False)
    assert state.node_trader.standing() == []
    import pytest
    with pytest.raises(O.OrderError):
        O.check_order({"id": "order-same1", "give": {"network": "regtest", "kind": "token", "asset": "150",
                                                     "amount": 1},
                       "get": {"network": "regtest", "kind": "coin", "asset": "", "amount": 1},
                       "trade_key": "02" + "11" * 32, "msg_key": "22" * 32, "node": NODE,
                       "expires": int(time.time()) + 60})


def test_the_operator_posts_an_nft_from_its_home_address(client, monkeypatch):
    from test_escrow import _piece
    app, state = client
    monkeypatch.setattr(state, "mesh", None, raising=False)
    home = "nHomeAddressXXXXXXXXXXXXXXXXXXXXXXX"
    monkeypatch.setattr(state, "home_address", lambda chain: home, raising=False)
    sword = "b7" * 32
    _piece(state, sword, home)
    page = app.get("/exchange?tab=tokens").text
    # Coins and tokens only in the pair list (2026-10-17: "NFT's should not be in the coin and
    # token pair list"); a piece is still listed for a swap, from its own form below.
    assert f'value="regtest-nft-{sword}"' not in page, "no NFT in the coin and token pair list"
    app.post("/exchange/trade-orders/stand", data=dict(
        give_network=f"nft:regtest:{sword}", give_amount="1", get_network="main", get_amount="20",
        csrf_token=state.csrf_token), follow_redirects=False)
    standing = state.node_trader.standing()
    assert standing and standing[0]["give"] == {"network": "regtest", "kind": "nft", "asset": sword, "amount": 1,
                                                "fee_bps": 0}
    app.post("/exchange/trade-orders/stand", data=dict(
        give_network=f"nft:regtest:{'b8' * 32}", give_amount="1", get_network="main", get_amount="20",
        csrf_token=state.csrf_token), follow_redirects=False)
    assert len(state.node_trader.standing()) == 1, "an NFT the node does not hold is refused"


@pytest.mark.node_trades_default
def test_the_operator_sees_the_orders_its_own_accounts_listed(client, monkeypatch):
    """2026-10-19: "orders from the order book are not showing up in the token exchange pages" on the
    operator's side. The operator's book dropped every order filed on its own node -- which is every order its own
    accounts list -- so it only ever saw other arcades'. Only the node's own standing orders are left out (they are
    "This node's swaps"), and with node-facilitated trades off the page says how to take them."""
    app, state = client
    mesh = _Mesh({_Mesh.node_id})
    monkeypatch.setattr(state, "mesh", mesh, raising=False)
    _file(state, oid="order-local", node=_Mesh.node_id)
    page = app.get("/exchange?tab=tokens").text
    assert "No arcade is offering a swap right now" not in page
    assert 'action="/exchange/trade-orders/take"' in page, "the operator takes it whatever the switch says"


def test_an_accounts_delegated_order_shows_to_the_operator(client, monkeypatch):
    """2026-10-19, after the fix above: the token exchange still showed no orders to the operator. A delegated
    order (an account's, finished by the node while its page is shut) is kept among the node's standing orders, and
    those were all left out as "the node's own"."""
    import json
    app, state = client
    monkeypatch.setattr(state, "mesh", _Mesh({_Mesh.node_id}), raising=False)
    monkeypatch.setattr("arcade.trade_courier.mesh_inbox", lambda st: None)
    trader = state.node_trader
    give = {"network": "main", "kind": "coin", "asset": "", "amount": 2 * COIN}
    get = {"network": "regtest", "kind": "coin", "asset": "", "amount": 3 * COIN}
    trader.stand("order-held", give, get)
    trader.stand("order-node", give, get)
    with trader._db() as conn:
        conn.execute("INSERT INTO delegated(order_id, key, deposit, payout, owner, state, created) "
                     "VALUES('order-held', '', '{}', '{}', 'someone', 'open', 0)")
    assert [o["id"] for o in trader.standing(own=True)] == ["order-node"]
    _file(state, oid="order-held", node=_Mesh.node_id)
    page = app.get("/exchange?tab=tokens").text
    assert "No arcade is offering a swap right now" not in page


def test_the_book_names_the_orders_it_holds_as_cancelled(tmp_path):
    """2026-10-23: a tab kept re-posting two long-cancelled frgy orders; the book says which of a lister's are cancelled."""
    import sqlite3
    from arcade.trade_orders import OrderBook
    book = OrderBook(tmp_path / "trade-orders.sqlite")
    with sqlite3.connect(book.path) as conn:
        for oid, gone in (("ord-a", 1), ("ord-b", 0)):
            conn.execute("INSERT INTO trade_order(id, lister, network, inscription, body, node, expires, cancelled) "
                         "VALUES (?, 'L', 'test', 'x', '{}', 'n', 9999999999, ?)", (oid, gone))
    assert book.cancelled_of("L", ["ord-a", "ord-b", "ord-c"]) == ["ord-a"]
    assert book.cancelled_of("someone-else", ["ord-a"]) == [], "another lister's order is never named"
