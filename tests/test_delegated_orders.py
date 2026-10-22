"""Delegated swap orders (2026-10-17: "I want pre-signed partially signed transactions for all orders. So that
orders can be filled without the counterparty being on Arcade"; he chose: the seller's own node finishes it, with a key
for that one order). The seller's side waits at the order key's address; the node answers, locks from there, and claims
the taker's coins to the seller's own address -- with the node's own trading switch off and no page of the seller's open."""

import pathlib
import secrets
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_htlc import doge                                            # noqa: E402,F401
from test_node_trader import _mine, _scan                             # noqa: E402
from test_trade_tab_browser import _catch_up, world                   # noqa: E402,F401

from arcade import trade as T                                         # noqa: E402
from arcade.node_trader import HeldKey                                # noqa: E402
from arcade.script import b58check_encode, hash160                    # noqa: E402

COIN = 100_000_000


def test_a_delegated_order_fills_with_the_seller_away(world):
    base, a, b, pep, dg = world
    seller_node = a.node_trader
    seller_node.enabled = lambda: False                               # the operator's own trading is off
    secret = f"{secrets.randbelow(2 ** 250) + 1:064x}"
    held = HeldKey(secret)
    deposit = b58check_encode(a.doge.params.pubkeyhash_version, hash160(held.pubkey))
    payout = pep.call("getnewaddress")                               # the seller's own PEP address
    dg.call("sendtoaddress", deposit, 2.2)                           # 2 DOGE for the order, 0.2 for its fees
    _mine(dg)
    _catch_up(a)
    key = seller_node.delegate("order-away", {"network": "doge", "kind": "coin", "amount": 2 * COIN},
                               {"network": "regtest", "kind": "coin", "amount": 3 * COIN},
                               secret, {"doge": deposit}, {"regtest": payout}, owner="seller")
    assert key == held.pubkey
    from arcade import trade_courier
    trade_courier.mesh_inbox(a)                                      # the seller's node is on the mesh

    give = b.leg("regtest", 3 * COIN, key)                           # the taker gives 3 PEP
    get = T.Leg("doge", "coin", "", 2 * COIN, key.hex(), b.key.pubkey.hex())
    trade = b.propose(a.identity.public_bytes, "order-away", give, get)
    log, seen = [], set()
    for _ in range(40):
        for t in (b, seller_node):
            _scan(t)
            _catch_up(t.state)
            log.extend(t.tick())
        seen |= {o["state"] for o in seller_node.delegated_of("seller")}
        _mine(pep, dg)
        if b.book.get(trade.id).state == "done" and (seller_node.book.get(trade.id) or trade).state == "done":
            break
    assert b.book.get(trade.id).state == "done", log[-6:]
    assert seller_node.book.get(trade.id).state == "done", log[-6:]
    assert "taking" in seen, f"its seller's page was told a swap was under way: {seen}"
    got = sum(u["amount"] for u in pep.call("listunspent", 0, 9999999, [payout]))
    assert got >= 2.98, f"the seller's 3 PEP (less the claim fee) at its own address: {got}"
    assert seller_node.delegated("order-away")["state"] in ("releasing", "returned"), "taken whole, off the book"


def test_a_delegated_token_order_fills_with_the_seller_away(world):
    """The seller's GOLD for the taker's DOGE: the token waits at the order key's address with a little coin for its
    lock; the node locks it there with one OP_RETURN, and the DOGE goes to the seller's own Dogecoin address."""
    from test_trade_tab_lists import _gold_everywhere
    base, a, b, pep, dg = world
    seller_node = a.node_trader
    seller_node.enabled = lambda: False
    secret = f"{secrets.randbelow(2 ** 250) + 1:064x}"
    held = HeldKey(secret)
    deposit = b58check_encode(a.messaging.params.pubkeyhash_version, hash160(held.pubkey))
    pep.call("sendtoaddress", deposit, 1)                             # the lock's reserve and fees
    _mine(pep)
    _gold_everywhere(a, b, deposit, 100 * COIN)                       # the seller's GOLD at the order key's address
    _catch_up(a)
    payout = dg.call("getnewaddress")
    key = seller_node.delegate("order-gold-away",
                               {"network": "regtest", "kind": "token", "asset": "150", "amount": 40 * COIN},
                               {"network": "doge", "kind": "coin", "amount": 2 * COIN},
                               secret, {"regtest": deposit}, {"doge": payout}, owner="seller")
    from arcade import trade_courier
    trade_courier.mesh_inbox(a)
    give = b.leg("doge", 2 * COIN, key)
    get = T.Leg("regtest", "token", "150", 40 * COIN, key.hex(), b.key.pubkey.hex())
    trade = b.propose(a.identity.public_bytes, "order-gold-away", give, get)
    log = []
    for _ in range(40):
        for t in (b, seller_node):
            _scan(t)
            _catch_up(t.state)
            log.extend(t.tick())
        _mine(pep, dg)
        if b.book.get(trade.id).state == "done" and (seller_node.book.get(trade.id) or trade).state == "done":
            break
    assert b.book.get(trade.id).state == "done", log[-6:]
    assert seller_node.book.get(trade.id).state == "done", log[-6:]
    got = sum(u["amount"] for u in dg.call("listunspent", 0, 9999999, [payout]))
    assert got >= 1.98, f"the seller's 2 DOGE (less the claim fee) at its own address: {got}"


def test_a_page_lists_a_delegated_order_and_closes_and_it_still_fills(world, monkeypatch):
    """The whole way: the seller's page derives the order key, tells its node, deposits 2 DOGE there and lists; then
    nobody runs that page any more. Another node takes the order and the 3 PEP land at the seller's own address."""
    from playwright.sync_api import sync_playwright
    from test_trade_tab_browser import _scan_a
    from test_trade_tab_lists import _Mesh
    from arcade import trade_orders as O
    base, a, b, pep, dg = world
    monkeypatch.setattr(a, "mesh", _Mesh(), raising=False)
    a.node_trader.enabled = lambda: False
    net = {"network": "regtest", "version": 111}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        tab = browser.new_page()
        tab.goto(f"{base}/join")
        tab.evaluate("async () => { window.w = await import('/wallet.js'); }")
        made = tab.evaluate("""async (net) => {
            const r = await w.signUp('away' + Math.floor(Math.random() * 1e6), 'a long enough password', net);
            return {regtest: r.wallet.on.regtest.address, doge: r.wallet.on.doge.address}; }""", net)
        pep.call("sendtoaddress", made["regtest"], 5)
        dg.call("sendtoaddress", made["doge"], 5)
        _mine(pep, dg)
        _scan_a(a)
        order = tab.evaluate("""async ([net, give, get]) => w.listOrder(await w.opened(net), give, get,
                                {approved: true, delegated: true})""",
                             [net, {"network": "doge", "amount": 2 * COIN}, {"network": "regtest", "amount": 3 * COIN}])
        browser.close()                                              # the seller's page is gone
    assert order["delegated"] and order["msg_key"] == a.identity.public_bytes.hex()
    _mine(pep, dg)
    _scan_a(a)
    O.import_announced(a.trade_orders, a.token_index(a.messaging), "regtest")
    shown = next(o for o in a.trade_orders.visible(a.mesh.online) if o["id"] == order["id"])
    assert not shown.get("tab") and shown["delegated"], "no page needed for it to stand"
    from arcade import trade_courier
    trade_courier.mesh_inbox(a)
    key = bytes.fromhex(shown["trade_key"])
    give = b.leg("regtest", 3 * COIN, key)
    get = T.Leg("doge", "coin", "", 2 * COIN, key.hex(), b.key.pubkey.hex())
    trade = b.propose(bytes.fromhex(shown["msg_key"]), order["id"], give, get)
    for _ in range(40):
        for t in (b, a.node_trader):
            _scan(t)
            _catch_up(t.state)
            t.tick()
        _mine(pep, dg)
        if b.book.get(trade.id).state == "done":
            break
    assert b.book.get(trade.id).state == "done"
    import contextlib
    from arcade import utxos
    _mine(pep)
    _catch_up(a)
    with contextlib.closing(a.token_index(a.messaging).open()) as db:
        got = utxos.balance(db, made["regtest"]) / COIN
    assert got >= 7.9, f"the seller's own address: its 5 PEP and the 3 PEP it sold for (less a fee): {got}"



def test_a_withdrawn_delegated_order_goes_back_to_the_seller(world):
    """Cancel: everything at the order key's address goes back to the seller's own address, signed by the node."""
    import contextlib
    from arcade import utxos
    base, a, b, pep, dg = world
    seller_node = a.node_trader
    secret = f"{secrets.randbelow(2 ** 250) + 1:064x}"
    held = HeldKey(secret)
    deposit = b58check_encode(a.doge.params.pubkeyhash_version, hash160(held.pubkey))
    home = dg.call("getnewaddress")
    dg.call("sendtoaddress", deposit, 2.2)
    _mine(dg)
    _catch_up(a)
    seller_node.delegate("order-back", {"network": "doge", "kind": "coin", "amount": 2 * COIN},
                         {"network": "regtest", "kind": "coin", "amount": 3 * COIN},
                         secret, {"doge": deposit}, {"regtest": pep.call("getnewaddress"), "doge": home})
    txid = seller_node.release("order-back")
    assert txid, "sent back at once: nobody was taking it"
    tx = dg.call("getrawtransaction", txid, 1)
    assert [o["scriptPubKey"]["addresses"] for o in tx["vout"]] == [[home]]
    assert float(tx["vout"][0]["value"]) > 2.19, "all of it, less one small fee"
    assert seller_node.delegated("order-back")["state"] == "returned"
    assert seller_node.delegated("order-back") and not seller_node.has_delegated()


def test_a_filled_delegated_order_leaves_its_sellers_page(world, monkeypatch):
    """2026-10-17: the buyer filled his orders, "but I'm still shown the order and the cancel button on my side".
    The page keeps its own list; it asks the node (GET /account/orders/delegated) and drops what is no longer open."""
    from playwright.sync_api import sync_playwright
    from test_trade_tab_browser import _scan_a
    from test_trade_tab_lists import _Mesh
    base, a, b, pep, dg = world
    monkeypatch.setattr(a, "mesh", _Mesh(), raising=False)
    net = {"network": "regtest", "version": 111}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        tab = browser.new_page()
        tab.goto(f"{base}/join")
        tab.evaluate("async () => { window.w = await import('/wallet.js'); }")
        made = tab.evaluate("""async (net) => {
            const r = await w.signUp('filled' + Math.floor(Math.random() * 1e6), 'a long enough password', net);
            return {regtest: r.wallet.on.regtest.address, doge: r.wallet.on.doge.address}; }""", net)
        pep.call("sendtoaddress", made["regtest"], 5)
        dg.call("sendtoaddress", made["doge"], 5)
        _mine(pep, dg)
        _scan_a(a)
        order = tab.evaluate("""async ([net, give, get]) => w.listOrder(await w.opened(net), give, get,
                                {approved: true, delegated: true})""",
                             [net, {"network": "doge", "amount": 2 * COIN}, {"network": "regtest", "amount": 3 * COIN}])
        assert tab.evaluate("async (net) => w.myOrders(await w.opened(net)).length", net) == 1
        with a.node_trader._db() as conn:
            conn.execute("UPDATE delegated SET state='filled' WHERE order_id=?", (order["id"],))
        assert tab.evaluate("async (net) => w.syncDelegated(await w.opened(net))", net) is True
        assert tab.evaluate("async (net) => w.myOrders(await w.opened(net)).length", net) == 0, "gone from the page"
        browser.close()


def test_a_taken_order_never_goes_back_on_the_book(world, monkeypatch):
    """2026-10-18, the operator: "the order remained in the orderbook, even after it was even half fulfilled" -- "It
    should've been removed as soon as it was taken". A PEP for LTC order's PEP was claimed by the taker, the seller's
    node could not claim the LTC (htlc's fee then), gave up when the taker's refund time came, and put the order back
    on the book with nothing behind it. Once the seller's side is locked the order is taken: it is withdrawn, and what
    comes back to the deposit goes back to the seller."""
    from arcade import htlc as H
    base, a, b, pep, dg = world
    seller_node = a.node_trader
    seller_node.enabled = lambda: False
    secret = f"{secrets.randbelow(2 ** 250) + 1:064x}"
    held = HeldKey(secret)
    deposit = b58check_encode(a.doge.params.pubkeyhash_version, hash160(held.pubkey))
    payout = pep.call("getnewaddress")
    dg.call("sendtoaddress", deposit, 2.2)
    _mine(dg)
    _catch_up(a)
    key = seller_node.delegate("order-taken", {"network": "doge", "kind": "coin", "amount": 2 * COIN},
                               {"network": "regtest", "kind": "coin", "amount": 3 * COIN},
                               secret, {"doge": deposit}, {"regtest": payout}, owner="seller")
    from arcade import trade_courier
    trade_courier.mesh_inbox(a)
    real_way_out = seller_node._way_out

    def cannot_claim(coin, leg, redeem, chain, to="", **more):
        if leg.network == "regtest":                                 # the seller's claim of the taker's PEP
            raise H.HtlcError("the lock holds too little to pay for its own spend")
        return real_way_out(coin, leg, redeem, chain, to, **more)
    monkeypatch.setattr(seller_node, "_way_out", cannot_claim)
    give = b.leg("regtest", 3 * COIN, key)
    get = T.Leg("doge", "coin", "", 2 * COIN, key.hex(), b.key.pubkey.hex())
    trade = b.propose(a.identity.public_bytes, "order-taken", give, get)
    for _ in range(40):
        for t in (b, seller_node):
            _scan(t)
            _catch_up(t.state)
            try:
                t.tick()
            except H.HtlcError:
                pass
        _mine(pep, dg)
        if b.book.get(trade.id).state == "done":
            break
    assert b.book.get(trade.id).state == "done", "the taker took the seller's DOGE"
    mine = seller_node.book.get(trade.id)
    for t in (mine.initiator_locktime + 60, mine.initiator_locktime + 120):
        _scan(seller_node)
        try:
            seller_node.tick(now=t)
        except H.HtlcError:
            pass
    assert seller_node.book.get(trade.id).state == "aborted"
    assert "order-taken" not in [o["id"] for o in seller_node.standing()], "taken: never back on the book"
    assert seller_node.delegated("order-taken")["state"] in ("releasing", "returned")
