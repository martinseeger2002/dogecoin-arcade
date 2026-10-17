"""A browser tab LISTS a swap, and another arcade's node takes it -- end to end.

2026-10-13: orders are "visible to other node operators as long as that node is
online. They can be inscribed like any other order". A person on node A lists 3 PEP for
2 DOGE from their tab (wallet.listOrder): the tab inscribes the order batch from its own
account, naming a key per chain (`take_key`), its messaging key and node A. Every node
reads it off the chain; node B (a NodeTrader) takes it, the tab answers the proposal
(wallet.answerProposals) and both sides settle through lock scripts.
"""

import os
import pathlib
import sys

import pytest

pytest.importorskip("playwright.sync_api", reason="needs playwright")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_htlc import doge                                            # noqa: E402,F401
from test_node_trader import _scan                                    # noqa: E402
from test_trade_tab_browser import _mine, _run, _scan_a, world        # noqa: E402,F401

from arcade import trade as T, trade_orders as O                      # noqa: E402

COIN = 100_000_000
NODE_A = "ab" * 32


class _Mesh:
    node_id = NODE_A

    def online(self, node_id):
        return node_id == NODE_A


def test_a_tab_lists_a_swap_and_another_node_takes_it(world, monkeypatch):
    from playwright.sync_api import sync_playwright
    base, a, b, pep, dg = world
    monkeypatch.setattr(a, "mesh", _Mesh(), raising=False)
    net = {"network": "regtest", "version": 111}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        tab = browser.new_page()
        tab.goto(f"{base}/join")
        tab.evaluate("async () => { window.w = await import('/wallet.js'); }")
        made = tab.evaluate("""async (net) => {
            const r = await w.signUp('lister' + Math.floor(Math.random() * 1e6), 'a long enough password', net);
            return {regtest: r.wallet.on.regtest.address, doge: r.wallet.on.doge && r.wallet.on.doge.address}; }""", net)
        assert made["doge"]
        pep.call("sendtoaddress", made["regtest"], 10)
        _mine(pep)
        _scan_a(a)
        listed = tab.evaluate("""async ([net, give, get]) => {
            const wal = await w.opened(net);
            const o = await w.listOrder(wal, give, get);
            return [o, w.myOrders(wal).length]; }""",
            [net, {"network": "regtest", "amount": 3 * COIN}, {"network": "doge", "amount": 2 * COIN}])
        mine, count = listed
        assert count == 1 and mine["trade_key"] != mine["take_key"], "a key per chain"
        _mine(pep)
        _scan_a(a)
        # The first inscription on this chain is number 0: the importer must not skip it.
        assert O.import_announced(a.trade_orders, a.token_index(a.messaging), "regtest") >= 1
        shown = [o for o in a.trade_orders.visible(a.mesh.online) if o["id"] == mine["id"]]
        assert len(shown) == 1, "the tab's order is on the chain and its node is online"
        found = shown[0]
        assert found["node"] == NODE_A and found["take_key"] == mine["take_key"]
        assert found["lister"] == made["regtest"], "listed by the account that inscribed it"
        assert not [o for o in a.trade_orders.visible(lambda n: False) if o["id"] == mine["id"]]

        # The account's Exchange shows its listed swap, with Withdraw, and the one way to list
        # another (Start a market, 2026-10-15).
        # (Its swaps show on Trade history since 2026-10-17; the Exchange keeps Start a market.)
        tab.goto(f"{base}/exchange/history")
        tab.wait_for_selector("#tab-orders:not([hidden])", timeout=15000)
        assert "for 2 DOGE" in tab.inner_text("#tab-orders-list")
        tab.goto(f"{base}/exchange?tab=tokens")
        assert tab.is_visible("#start-market")
        if os.environ.get("ARCADE_SHOT"):
            # A still picture of the card (the live page never settles for a screenshot).
            pathlib.Path(os.environ["ARCADE_SHOT"]).write_text(tab.evaluate("""() => {
                let css = "";
                for (const sheet of document.styleSheets) { try { for (const r of sheet.cssRules) css += r.cssText + "\\n"; } catch (e) {} }
                const theme = document.documentElement.getAttribute("data-theme") || "";
                return `<!doctype html><html data-theme="${theme}"><head><meta charset="utf-8"><style>${css}</style></head>`
                     + `<body style="padding:16px;max-width:900px">${document.getElementById("trade-orders").outerHTML}</body></html>`; }"""))
        # Off the page again: its own tick would answer alongside this test's calls.
        tab.goto(f"{base}/join")
        tab.evaluate("async () => { window.w = await import('/wallet.js'); }")

        # Node B takes it exactly as its Take button does.
        give = b.leg(found["get"]["network"], found["get"]["amount"], bytes.fromhex(found["take_key"]))
        get = T.Leg(found["give"]["network"], "coin", "", found["give"]["amount"],
                    found["trade_key"], b.key.pubkey.hex())
        trade = b.propose(bytes.fromhex(found["msg_key"]), found["id"], give, get).id

        seen = _run(tab, net, trade, a, b, pep, dg)
        browser.close()
    assert b.book.get(trade).state == "done", seen[-6:]
    assert seen[-1][0] == "done" and seen[-1][1] == "I claimed their lock", seen[-6:]
    assert any(locked for _, _, locked in seen), "the tab locked its own coins"


def test_a_tab_turns_down_terms_that_are_not_its_order(world, monkeypatch):
    from playwright.sync_api import sync_playwright
    base, a, b, pep, dg = world
    monkeypatch.setattr(a, "mesh", _Mesh(), raising=False)
    net = {"network": "regtest", "version": 111}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        tab = browser.new_page()
        tab.goto(f"{base}/join")
        tab.evaluate("async () => { window.w = await import('/wallet.js'); }")
        made = tab.evaluate("""async (net) => {
            const r = await w.signUp('picky' + Math.floor(Math.random() * 1e6), 'a long enough password', net);
            return r.wallet.on.regtest.address; }""", net)
        pep.call("sendtoaddress", made, 10)
        _mine(pep)
        _scan_a(a)
        mine = tab.evaluate("""async ([net, give, get]) => w.listOrder(await w.opened(net), give, get)""",
                            [net, {"network": "regtest", "amount": 3 * COIN}, {"network": "doge", "amount": 2 * COIN}])
        _mine(pep)
        _scan_a(a)
        O.import_announced(a.trade_orders, a.token_index(a.messaging), "regtest")
        found = next(o for o in a.trade_orders.visible(a.mesh.online) if o["id"] == mine["id"])
        # One DOGE short of what the tab asked for.
        give = b.leg("doge", 1 * COIN, bytes.fromhex(found["take_key"]))
        get = T.Leg("regtest", "coin", "", 3 * COIN, found["trade_key"], b.key.pubkey.hex())
        trade = b.propose(bytes.fromhex(found["msg_key"]), found["id"], give, get).id
        for _ in range(6):
            _mine(pep)
            _scan_a(a)
            n = tab.evaluate("""async (net) => { const wal = await w.opened(net);
                await w.answerProposals(wal); return w.myTrades(wal).length; }""", net)
            assert n == 0, "nothing accepted"
            _scan(b)
            b.tick()
            with b._db() as conn:
                agreed = conn.execute("SELECT agreed FROM link WHERE id=?", (trade,)).fetchone()["agreed"]
            if agreed == -1:
                break
        browser.close()
    assert agreed == -1, "the tab said no"
    assert b.book.get(trade).state != "done"


def _gold_everywhere(a, b, holder, units):
    from test_account_tokens import _balance, _token
    for st in (a, b.state):
        _token(st, property_id=150, name="Gold")
        _balance(st, holder, 150, units)
    return 150


def _settle(tab, a, b, pep, dg, trade, net):
    return _run(tab, net, trade, a, b, pep, dg, b_ledger=True)

def test_a_tab_lists_a_token_and_another_node_takes_it(world, monkeypatch):
    """The tab's Gold goes into a lock built by /account/trade/lock (the coin that pays its
    way out riding along); node B claims it with the secret, the tab claims B's DOGE."""
    from playwright.sync_api import sync_playwright
    base, a, b, pep, dg = world
    monkeypatch.setattr(a, "mesh", _Mesh(), raising=False)
    net = {"network": "regtest", "version": 111}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        tab = browser.new_page()
        tab.goto(f"{base}/join")
        tab.evaluate("async () => { window.w = await import('/wallet.js'); }")
        made = tab.evaluate("""async (net) => {
            const r = await w.signUp('goldie' + Math.floor(Math.random() * 1e6), 'a long enough password', net);
            return r.wallet.on.regtest.address; }""", net)
        pep.call("sendtoaddress", made, 10)
        _mine(pep)
        _scan_a(a)
        pid = _gold_everywhere(a, b, made, 100 * COIN)
        mine = tab.evaluate("""async ([net, give, get]) => w.listOrder(await w.opened(net), give, get)""",
                            [net, {"network": "regtest", "kind": "token", "asset": "150", "amount": 40 * COIN},
                             {"network": "doge", "amount": 2 * COIN}])
        _mine(pep)
        _scan_a(a)
        O.import_announced(a.trade_orders, a.token_index(a.messaging), "regtest")
        found = next(o for o in a.trade_orders.visible(a.mesh.online) if o["id"] == mine["id"])
        assert found["give"] == {"network": "regtest", "kind": "token", "asset": "150", "amount": 40 * COIN}
        give = b.leg("doge", 2 * COIN, bytes.fromhex(found["take_key"]))
        get = T.Leg("regtest", "token", "150", 40 * COIN, found["trade_key"], b.key.pubkey.hex())
        trade = b.propose(bytes.fromhex(found["msg_key"]), found["id"], give, get).id
        seen = _settle(tab, a, b, pep, dg, trade, net)
        browser.close()
    assert b.book.get(trade).state == "done", seen[-6:]
    assert seen[-1][0] == "done", seen[-6:]
    index = b.state.token_index(b.state.messaging)
    index.sync()
    assert int(index.balance(b._address("regtest"), pid)) == 40 * COIN, "B holds the Gold"
    assert int(index.balance(made, pid)) == 60 * COIN


def test_a_tab_takes_a_nodes_token_for_its_coins(world, monkeypatch):
    """Node B stands 40 Gold for 3 PEP; the tab takes it, locks its PEP, and claims the Gold
    out of B's lock -- an OP_RETURN, the lock's leftover and the tab last, checked in the tab."""
    from playwright.sync_api import sync_playwright
    base, a, b, pep, dg = world
    home = pep.call("getnewaddress")
    pep.call("sendtoaddress", home, 5)
    monkeypatch.setattr(b.state, "home_address",
                        lambda chain: home if chain.network == "regtest" else "", raising=False)
    pid = _gold_everywhere(a, b, home, 100 * COIN)
    b.stand("order-gold", {"network": "regtest", "kind": "token", "asset": "150", "amount": 40 * COIN},
            {"network": "doge", "kind": "coin", "amount": 2 * COIN})
    order = {"id": "order-gold",
             "give": {"network": "regtest", "kind": "token", "asset": "150", "amount": 40 * COIN},
             "get": {"network": "doge", "kind": "coin", "asset": "", "amount": 2 * COIN},
             "trade_key": b.key.pubkey.hex(), "msg_key": b.state.identity.public_bytes.hex(),
             "node": "ab" * 32, "expires": 0}
    net = {"network": "regtest", "version": 111}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        tab = browser.new_page()
        tab.goto(f"{base}/join")
        tab.evaluate("async () => { window.w = await import('/wallet.js'); }")
        made = tab.evaluate("""async (net) => {
            const r = await w.signUp('buyer' + Math.floor(Math.random() * 1e6), 'a long enough password', net);
            return {regtest: r.wallet.on.regtest.address, doge: r.wallet.on.doge.address}; }""", net)
        pep.call("sendtoaddress", made["regtest"], 10)
        dg.call("sendtoaddress", made["doge"], 10)
        _mine(pep, dg)
        _scan_a(a)
        # Started, not awaited: a proposal too big for one message transaction splits a coin
        # first and waits a block for it, and only this test mines blocks.
        tab.evaluate("""([net, order]) => { window.taking = w.opened(net).then((wal) => w.takeOrder(wal, order))
            .then((t) => { window.took = t.id; }, (e) => { window.took = 'ERR ' + e.message; }); }""", [net, order])
        for _ in range(20):
            took = tab.evaluate("() => window.took || ''")
            if took:
                break
            _mine(pep)
            _scan_a(a)
            tab.wait_for_timeout(500)
        assert took and not took.startswith("ERR"), took
        trade = took
        # Dogecoin is real money: locking it asks the person first (mainnetOk). Here, yes.
        tab.evaluate("() => { globalThis.arcadeMainnetReview = async () => true; }")
        seen = _settle(tab, a, b, pep, dg, trade, net)
        browser.close()
    assert b.book.get(trade).state == "done", seen[-6:]
    assert seen[-1][0] == "done", seen[-6:]
    index = a.token_index(a.messaging)
    index.sync()
    assert int(index.balance(made["regtest"], pid)) == 40 * COIN, "the tab holds the Gold"


def test_the_list_form_offers_a_token_the_account_holds(world, monkeypatch):
    """On the Exchange, "List swap" offers this account's own tokens beside the coins, and
    a token listed there shows under "Your listed swaps" by name."""
    from playwright.sync_api import sync_playwright
    base, a, b, pep, dg = world
    monkeypatch.setattr(a, "mesh", _Mesh(), raising=False)
    net = {"network": "regtest", "version": 111}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        tab = browser.new_page()
        tab.goto(f"{base}/join")
        tab.evaluate("async () => { window.w = await import('/wallet.js'); }")
        made = tab.evaluate("""async (net) => {
            const r = await w.signUp('former' + Math.floor(Math.random() * 1e6), 'a long enough password', net);
            return r.wallet.on.regtest.address; }""", net)
        pep.call("sendtoaddress", made, 10)
        _mine(pep)
        _scan_a(a)
        _gold_everywhere(a, b, made, 100 * COIN)
        from test_escrow import _piece
        _piece(a, "c9" * 32, made)
        # One way for every pair (2026-10-15): Start a market offers what the account holds,
        # and the swap is listed on the market it opens.
        tab.goto(f"{base}/exchange?tab=tokens")
        tab.wait_for_selector("#start-market")
        nft = tab.locator(f'#pair-base option[value="regtest-nft-{"c9" * 32}"]')
        assert nft.count() == 0, "no NFT in the coin and token pair list (2026-10-17)"
        option = tab.locator('#pair-base option[value="regtest-token-150"]')
        assert option.count() == 1 and "Gold" in option.inner_text()
        tab.select_option("#pair-base", "regtest-token-150")
        tab.select_option("#pair-quote", "doge-coin")
        # "I want to sell 40 Gold at 0.05 DOGE per Gold" (2026-10-15), straight from the box.
        tab.click('#start-market label:has(input[value=sell])')
        tab.fill("#start-market input[name=amount]", "40")
        tab.fill("#start-market input[name=price]", "0.05")
        assert "You get 2 DOGE for 40 Gold" in tab.inner_text("#pair-sum")
        tab.evaluate("() => { globalThis.arcadeAsk = async () => true; }")
        said = []
        tab.on("dialog", lambda d: (said.append(d.message), d.dismiss()))
        tab.evaluate("() => document.querySelector('#start-market').requestSubmit()")
        tab.wait_for_url("**/exchange/swap/regtest-token-150/doge-coin", timeout=30000)
        # The listing is a row of this market's own book at once, yours, with Cancel (2026-10-17:
        # "the users order should go into the order book").
        mine = tab.locator("#swap-pair .obrow.ask.mine")
        for _ in range(40):
            if said or mine.count():
                break
            tab.wait_for_timeout(500)
        assert not said, said
        row = mine.first.inner_text()
        assert "40" in row and "0.05" in row and "yours" in row and "Cancel" in row, row
        if os.environ.get("ARCADE_SHOT"):
            pathlib.Path(os.environ["ARCADE_SHOT"]).write_text(tab.evaluate("""() => {
                let css = "";
                for (const sheet of document.styleSheets) { try { for (const r of sheet.cssRules) css += r.cssText + "\\n"; } catch (e) {} }
                return `<!doctype html><html><head><meta charset="utf-8"><style>${css}</style></head>`
                     + `<body style="padding:16px;max-width:900px">${document.getElementById("trade-orders").outerHTML}</body></html>`; }"""))
        browser.close()
        mine = [o for o in a.trade_orders.visible(lambda n: True)] or []
    _mine(pep)
    _scan_a(a)
    O.import_announced(a.trade_orders, a.token_index(a.messaging), "regtest")
    listed = [o for o in a.trade_orders.visible(a.mesh.online) if o["lister"] == made]
    assert listed and listed[0]["give"] == {"network": "regtest", "kind": "token", "asset": "150",
                                            "amount": 40 * COIN}


def test_the_swap_finishes_with_the_listers_browser_closed(world, monkeypatch):
    """2026-10-13: nobody should have to stay online. The tab lists 3 PEP for 2 DOGE,
    node B takes it, and the tab is CLOSED right after it locks its PEP -- having handed its
    node a pre-signed claim of B's DOGE and a pre-signed refund (trade_watch.py). Node B
    claims the PEP, which puts the secret on the chain; node A reads it there and sends the
    tab's claim. The DOGE lands at the tab's own address."""
    from playwright.sync_api import sync_playwright
    base, a, b, pep, dg = world
    monkeypatch.setattr(a, "mesh", _Mesh(), raising=False)
    net = {"network": "regtest", "version": 111}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        tab = browser.new_page()
        tab.goto(f"{base}/join")
        tab.evaluate("async () => { window.w = await import('/wallet.js'); }")
        made = tab.evaluate("""async (net) => {
            const r = await w.signUp('closer' + Math.floor(Math.random() * 1e6), 'a long enough password', net);
            return {regtest: r.wallet.on.regtest.address, doge: r.wallet.on.doge.address}; }""", net)
        pep.call("sendtoaddress", made["regtest"], 10)
        _mine(pep)
        _scan_a(a)
        mine = tab.evaluate("""async ([net, give, get]) => w.listOrder(await w.opened(net), give, get)""",
                            [net, {"network": "regtest", "amount": 3 * COIN}, {"network": "doge", "amount": 2 * COIN}])
        _mine(pep)
        _scan_a(a)
        O.import_announced(a.trade_orders, a.token_index(a.messaging), "regtest")
        found = next(o for o in a.trade_orders.visible(a.mesh.online) if o["id"] == mine["id"])
        give = b.leg("doge", 2 * COIN, bytes.fromhex(found["take_key"]))
        get = T.Leg("regtest", "coin", "", 3 * COIN, found["trade_key"], b.key.pubkey.hex())
        trade = b.propose(bytes.fromhex(found["msg_key"]), found["id"], give, get).id
        tab.evaluate("async (net) => { const w = await import('/wallet.js'); w.startTradeRunner(net, 400); }", net)
        held = None
        for _ in range(40):
            _mine(pep, dg)
            _scan_a(a)
            _scan(b)
            b.tick()
            _scan_a(a)
            tab.evaluate("async () => (await import('/wallet.js')).kickTrades()")
            tab.wait_for_timeout(600)
            held = tab.evaluate("""async ([net, id]) => { const w = await import('/wallet.js');
                const t = w.myTrades(await w.opened(net)).find((t) => t.id === id);
                return t && [!!t.myLock, !!t.heldRefund, !!t.heldClaim, t.state, t.holdNote || '']; }""", [net, trade])
            if held and all(held[:3]):
                break
        assert held and all(held[:3]), held
        browser.close()                               # the lister walks away
    jobs = {j["kind"]: j for j in a.trade_watch.jobs()}
    assert set(jobs) == {"claim", "refund"}, jobs
    sent = None
    for _ in range(30):
        _mine(pep, dg)
        _scan(b)
        b.tick()
        done = dict(a.trade_watch.tick(a.chain_rpc_for))
        if done.get(jobs["claim"]["id"]) == "sent claim":
            sent = [j for j in a.trade_watch.jobs() if j["kind"] == "claim"][0]["sent_txid"]
        if sent and b.book.get(trade).state == "done":
            break
    assert sent, a.trade_watch.jobs()
    assert b.book.get(trade).state == "done"
    tx = dg.call("getrawtransaction", sent, 1)
    assert [o["scriptPubKey"]["addresses"] for o in tx["vout"]] == [[made["doge"]]], "the DOGE is the lister's"
