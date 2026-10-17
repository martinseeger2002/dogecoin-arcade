"""The PEP/DOGE market (2026-10-13, trustless-trading-plan.md phase 6; the operator: "Will a
PEP/DOGE pair be available?"): coin-for-coin swaps every arcade lists, both ways, priced in
DOGE per PEP, each taken whole and settled through lock scripts."""

import os
import pathlib
import secrets
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                               # noqa: F401,E402
from test_trade_orders_web import NODE, _Mesh                         # noqa: E402
from test_htlc import doge                                            # noqa: E402,F401
from test_trade_tab_browser import world                              # noqa: E402,F401

from arcade import trade_orders as O                                  # noqa: E402

COIN = 100_000_000


def _order(oid, give, get):
    return {"id": oid, "give": {"network": give[0], "kind": "coin", "asset": "", "amount": give[1]},
            "get": {"network": get[0], "kind": "coin", "asset": "", "amount": get[1]},
            "trade_key": "02" + secrets.token_hex(32), "msg_key": secrets.token_hex(32),
            "node": NODE, "expires": int(time.time()) + 3600}


def test_the_book_prices_both_ways_in_doge_per_pep():
    orders = [
        {**_order("ask-cheap", ("main", 100 * COIN), ("doge", 30 * COIN)), "lister": "x"},   # 0.30
        {**_order("ask-dear", ("main", 10 * COIN), ("doge", 5 * COIN)), "lister": "x"},      # 0.50
        {**_order("bid-low", ("doge", 2 * COIN), ("main", 10 * COIN)), "lister": "x"},       # 0.20
        {**_order("bid-high", ("doge", 5 * COIN), ("main", 20 * COIN)), "lister": "x"},      # 0.25
        {**_order("other", ("main", 1 * COIN), ("regtest", 1 * COIN)), "lister": "x"},
    ]
    book = O.coin_book(orders, "main", "doge")
    assert [o["id"] for o in book["asks"]] == ["ask-cheap", "ask-dear"], "cheapest ask first"
    assert [o["id"] for o in book["bids"]] == ["bid-high", "bid-low"], "dearest bid first"
    assert book["asks"][0]["price"] == 0.3 and book["bids"][0]["price"] == 0.25
    assert abs(book["spread"] - 0.05) < 1e-12
    assert book["bids"][0]["base_amount"] == 20 * COIN, "a bid's size is the PEP it buys"


def test_the_page_shows_the_book_and_takes_both_ways(client, monkeypatch):
    app, state = client
    batch = O.batch_json([_order("ask-0001", ("regtest", 10 * COIN), ("doge", 3 * COIN)),
                          _order("bid-0001", ("doge", 2 * COIN), ("regtest", 8 * COIN))])
    state.trade_orders.file_batch("nListerAddressXXXXXXXXXXXXXXXXXXXXX", "regtest", "aa" * 32, batch)
    monkeypatch.setattr(state, "mesh", _Mesh({NODE}), raising=False)
    assert app.get("/exchange/coins/regtest/doge", follow_redirects=False).headers["location"] \
        == "/exchange/swap/regtest-coin/doge-coin", "a coin pair is a swap market like every other"
    page = app.get("/exchange/coins/regtest/doge").text
    assert "Selling PEP TEST" in page and "Buying PEP TEST" in page
    assert "0.3" in page and "0.25" in page, "DOGE per PEP: 3/10 and 2/8"
    assert page.count('action="/exchange/trade-orders/take"') == 2, "the operator can take either side"
    assert 'name="back" value="/exchange/swap/regtest-coin/doge-coin"' in page
    assert app.get("/exchange/coins/regtest/regtest").status_code == 404
    assert app.get("/exchange/coins/nope/doge").status_code == 404


def test_the_operator_posts_a_bid_and_comes_back_to_the_market(client, monkeypatch):
    app, state = client
    monkeypatch.setattr(state, "mesh", None, raising=False)
    went = app.post("/exchange/trade-orders/stand", data={
        "give_network": "regtest", "give_amount": "3", "get_network": "main", "get_amount": "10",
        "back": "/exchange/coins/main/regtest", "csrf_token": state.csrf_token}, follow_redirects=False)
    assert went.headers["location"] == "/exchange/coins/main/regtest"
    went = app.post("/exchange/trade-orders/withdraw", data={
        "order": "x", "back": "https://elsewhere.example/", "csrf_token": state.csrf_token},
        follow_redirects=False)
    assert went.headers["location"] == "/exchange?tab=tokens#trade-orders", "never off this site"


def test_a_person_buys_on_the_market_page_and_the_page_settles_it(world, monkeypatch):
    """Node B posts an ask (10 PEP for 3 DOGE) the way its Post button does; a person on node A
    opens the market, presses Buy, and from then on only the page's own timer acts in the
    tab while blocks are mined and node B ticks."""
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright
    from test_trade_tab_browser import _mine, _scan_a
    from test_node_trader import _scan
    base, a, b, pep, dg = world
    if True:
        class Both:
            node_id = "ab" * 32

            def online(self, node_id):
                # Only node B: the regtest chain is shared, and swaps other tests listed
                # (from node "ab") are still on it.
                return node_id == "cd" * 32
        monkeypatch.setattr(a, "mesh", Both(), raising=False)
        b.stand("ask-pep-001", {"network": "regtest", "kind": "coin", "amount": 10 * COIN},
                {"network": "doge", "kind": "coin", "amount": 3 * COIN})
        b.stand("bid-pep-001", {"network": "doge", "kind": "coin", "amount": 2 * COIN},
                {"network": "regtest", "kind": "coin", "amount": 8 * COIN})
        b.post("cd" * 32)
        _mine(pep)
        _scan_a(a)
        O.import_announced(a.trade_orders, a.token_index(a.messaging), "regtest")
        net = {"network": "regtest", "version": 111}
        with sync_playwright() as p:
            browser = p.chromium.launch()
            tab = browser.new_page()
            tab.goto(f"{base}/join")
            tab.evaluate("async () => { window.w = await import('/wallet.js'); }")
            made = tab.evaluate("""async (net) => {
                const r = await w.signUp('market' + Math.floor(Math.random() * 1e6), 'a long enough password', net);
                return {regtest: r.wallet.on.regtest.address, doge: r.wallet.on.doge.address}; }""", net)
            pep.call("sendtoaddress", made["regtest"], 5)
            dg.call("sendtoaddress", made["doge"], 10)
            _mine(pep, dg)
            _scan_a(a)
            tab.goto(f"{base}/exchange/coins/regtest/doge")
            buy = tab.locator("[data-take-order]")
            assert buy.count() == 2 and buy.first.inner_text() == "Buy" and buy.last.inner_text() == "Sell"
            if os.environ.get("ARCADE_SHOT"):
                pathlib.Path(os.environ["ARCADE_SHOT"]).write_text(tab.evaluate("""() => {
                    let css = "";
                    for (const sheet of document.styleSheets) { try { for (const r of sheet.cssRules) css += r.cssText + "\\n"; } catch (e) {} }
                    return `<!doctype html><html><head><meta charset="utf-8"><style>${css}</style></head>`
                         + `<body style="padding:16px;max-width:1000px">${document.querySelector("main") ? document.querySelector("main").innerHTML : document.body.innerHTML}</body></html>`; }"""))
            assert "0.3" in tab.inner_text("#swap-pair"), "3 DOGE for 10 PEP is 0.3 DOGE each"
            said = []
            tab.on("dialog", lambda d: (said.append(d.message), d.dismiss()))
            tab.on("console", lambda m: said.append("console: " + m.text) if m.type == "error" else None)
            # The Take dialog is the approval (amounts and fees shown there): the DOGE lock must
            # not ask again, so the separate real-coin review here says NO to anything.
            tab.evaluate("""() => { globalThis.arcadeAsk = async (title, body) => { window.asked = body; return true; };
                                    globalThis.arcadeMainnetReview = async () => false;
                                    document.querySelectorAll('[data-take-order]')[0].click(); }""")
            done = None
            for _ in range(90):
                _mine(pep, dg)
                _scan_a(a)
                _scan(b)
                b.tick()
                _scan_a(a)
                tab.wait_for_timeout(1500)
                trades = tab.evaluate("""async () => { const w = await import('/wallet.js');
                    const wal = await w.opened({network: 'regtest', version: 111});
                    return w.myTrades(wal).map((t) => [t.state, t.note]); }""")
                asked = tab.evaluate("() => window.asked || ''")
                if trades and trades[0][0] == "done":
                    done = trades[0]
                    break
            browser.close()
        assert done, (trades, said)
        assert "Network fees: about 0.01 DOGE to lock yours" in asked and "0.01 PEP TEST comes off" in asked, asked
        assert not b.book.open_trades(), "node B finished its side too"
