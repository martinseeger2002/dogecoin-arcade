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
