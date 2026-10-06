"""The cross-chain book's pages (2026-10-06): the market page says it is not
trustless, the operator opens the book, the mainnet picker opens a market once it
is open, and a market with an order is listed with the rest, badged MAINNET."""

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                                 # noqa: F401,E402

COIN = 100_000_000


def _csrf(page: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)


def test_the_market_page_says_it_is_not_trustless_and_starts_closed(client, monkeypatch):
    app, state = client
    import arcade.ledger as L
    monkeypatch.setattr(L.LedgerIndex, "property", lambda self, pid: {"property_id": 59, "name": "LOGS",
                                                                      "divisible": True, "managed": 0})
    page = app.get("/exchange/x/token/59").text
    assert "Not trustless" in page and "trust the operator" in page
    assert "not open on this node" in page and "MAINNET" in page and "TESTNET" in page
    assert app.get("/exchange/pairs?base=59&quote=main", follow_redirects=False).headers["location"] \
        .startswith("/exchange?tab=tokens"), "the mainnet choice waits for the book to open"


def test_the_operator_opens_the_book_and_its_markets_join_the_list(client, monkeypatch):
    app, state = client
    import arcade.ledger as L
    monkeypatch.setattr(L.LedgerIndex, "property", lambda self, pid: {"property_id": 59, "name": "LOGS",
                                                                      "divisible": True, "managed": 0})
    monkeypatch.setattr(type(state.xchain), "address", lambda self, which: f"n{which}Exchange")
    page = app.get("/exchange/x/token/59").text
    app.post("/exchange/x/enable", data={"on": "1", "csrf_token": _csrf(page)})
    assert state.setting("xchain:enabled")
    assert app.get("/exchange/pairs?base=59&quote=main", follow_redirects=False).headers["location"] == "/exchange/x/token/59"
    assert app.get("/exchange/pairs?base=coin&quote=main", follow_redirects=False).headers["location"] == "/exchange/x/coin/coin"
    book = state.xchain.book
    o = book.place(owner="x", side="sell", kind="token", asset="59", amount=10 * COIN, pepe=5 * COIN,
                   pay_test="nT", pay_main="PM")
    book.deposited(o["id"], "dep1")
    book.count_deposit(o["id"])
    listing = app.get("/exchange?tab=tokens").text
    assert "/exchange/x/token/59" in listing, "an open market is a row of the one table"
    row = listing[listing.index("/exchange/x/token/59"):][:2500]
    assert "MAINNET" in row and "PEPE" in row
    market = app.get("/exchange/x/token/59").text
    assert "0.5" in market and "Exchange addresses" in market, "the operator sees where deposits go"
