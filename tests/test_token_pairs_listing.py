"""Token/token pairs are listed with the coin pairs, most popular first
(2026-10-06: "Token token pairs should be listed in the exact same place
as token test net pairs. pairings should be sorted by popularity")."""

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                                 # noqa: F401,E402
from fractions import Fraction                                         # noqa: E402

COIN = 100_000_000


def test_a_token_pair_is_a_row_of_the_markets_table_and_popularity_orders_them(client, monkeypatch):
    app, state = client
    props = {7: {"property_id": 7, "name": "LOGS", "divisible": True},
             8: {"property_id": 8, "name": "GOLD", "divisible": True},
             9: {"property_id": 9, "name": "ORE", "divisible": True}}
    now = time.time()
    trades = {(7, 8): [{"when": now - 60 * i, "height": 100 + i, "txid": f"{i:064x}", "base": 10 * COIN,
                        "quote": 15 * COIN, "side": "buy", "taker": "a", "maker": "b"} for i in range(3)],
              (7, 9): [{"when": now - 30, "height": 99, "txid": "f" * 64, "base": COIN, "quote": 2 * COIN,
                        "side": "sell", "taker": "a", "maker": "b"}]}
    rest = {"txid": "e" * 64, "block_height": 1, "position": 0, "address": "x",
            "base": 5 * COIN, "quote": 9 * COIN, "price": Fraction(9, 5)}

    import arcade.ledger as L
    monkeypatch.setattr(L.LedgerIndex, "token_pairs", lambda self: [(7, 8), (7, 9)], raising=False)
    monkeypatch.setattr(L.LedgerIndex, "pair_trades", lambda self, a, b, limit=500: trades.get((a, b), []), raising=False)
    monkeypatch.setattr(L.LedgerIndex, "pair_book", lambda self, a, b, limit=50: {"asks": [dict(rest)], "bids": []}, raising=False)
    real_property = L.LedgerIndex.property
    monkeypatch.setattr(L.LedgerIndex, "property", lambda self, pid: props.get(pid) or real_property(self, pid))

    page = app.get("/exchange?tab=tokens")
    assert page.status_code == 200, page.text[:400]
    text = page.text
    assert "/exchange/pairs/7/8" in text and "/exchange/pairs/7/9" in text, "each pair is a row of the one table"
    assert text.index("/exchange/pairs/7/8") < text.index("/exchange/pairs/7/9"), \
        "three trades today come before one"
    assert "/GOLD" in text and "/ORE" in text, "a pair's row names its quote token, not the coin"


def test_opening_a_pair_starts_from_a_token_you_hold(client, monkeypatch):
    """2026-10-06: "When creating a pair the drop-down should only list
    assets that you have in your wallet". "You have" lists what this wallet holds;
    "Trade it for" lists every token."""
    app, state = client
    import arcade.ledger as L
    import arcade.web.app as W
    held = [{"address": "nWalletAddress", "property_id": 8, "balance": 5, "name": "GOLD",
             "property_type": 2, "ecosystem": 2, "issuer": "x"}]
    monkeypatch.setattr(L.LedgerIndex, "balances", lambda self, addresses: held if addresses else [])
    # this node's wallet, as far as the page asks it: one address of its own
    import contextlib
    @contextlib.contextmanager
    def fake_rpc():
        yield object()
    monkeypatch.setattr(W, "_ledger_addresses", lambda rpc: ["nWalletAddress"])
    for ch in {id(state.messaging): state.messaging, **{id(c): c for c in getattr(state, "chains", {}).values()}}.values():
        monkeypatch.setattr(ch, "rpc", fake_rpc)
    monkeypatch.setattr(L.LedgerIndex, "properties", lambda self: [
        {"property_id": 7, "name": "LOGS", "divisible": True}, {"property_id": 8, "name": "GOLD", "divisible": True}, {"property_id": 9, "name": "ORE", "divisible": True}])
    page = app.get("/exchange?tab=tokens").text
    assert "You have" in page, "the wallet's own tokens are offered"
    if True:
        have = page[page.index("You have"):page.index("Trade it for")]
        assert "GOLD (#8)" in have and "LOGS (#7)" not in have and "ORE (#9)" not in have
        rest = page[page.index("Trade it for"):]
        assert "LOGS (#7)" in rest and "ORE (#9)" in rest


def test_a_pair_of_one_token_is_sent_back_to_pick_again(client):
    app, state = client
    went = app.get("/exchange/pairs?base=8&quote=8", follow_redirects=False)
    assert went.status_code == 303 and went.headers["location"].startswith("/exchange?tab=tokens")


def test_a_pair_page_draws_the_candles_asked_for(client, monkeypatch):
    """?tf= as on the coin pair page; without it, a short history is drawn in the
    finest timeframe (a promo's run of trades over an hour, 2026-10-06)."""
    app, state = client
    import arcade.ledger as L
    props = {7: {"property_id": 7, "name": "LOGS", "divisible": True},
             8: {"property_id": 8, "name": "GOLD", "divisible": True}}
    now = time.time()
    done = [{"when": now - 300 * i, "height": 100 + i, "txid": f"{i:064x}", "base": COIN,
             "quote": (2 + i % 3) * COIN, "side": "buy", "taker": "a", "maker": "b"} for i in range(12)]
    monkeypatch.setattr(L.LedgerIndex, "pair_trades", lambda self, a, b, limit=500: done, raising=False)
    monkeypatch.setattr(L.LedgerIndex, "pair_book", lambda self, a, b, limit=50, pool=True: {"asks": [], "bids": []}, raising=False)
    real_property = L.LedgerIndex.property
    monkeypatch.setattr(L.LedgerIndex, "property", lambda self, pid: props.get(pid) or real_property(self, pid))
    page = app.get("/exchange/pairs/7/8").text
    assert 'href="?tf=15m"' in page and 'aria-current="true">15m<' in page, "an hour of trades: 15-minute candles"
    page = app.get("/exchange/pairs/7/8?tf=4h").text
    assert 'aria-current="true">4h<' in page
