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
    page = app.get("/exchange/x/token/59?held=1").text
    assert "Not trustless" in page and "trust the operator" in page
    assert "not open on this node" in page and "MAIN</span>" in page and "TEST</span>" in page
    # The market itself is lock scripts (2026-10-15): open, and never "held by this node".
    swaps = app.get("/exchange/x/token/59").text
    assert "Lock scripts" in swaps and "Not trustless" not in swaps and "MAIN</span>" in swaps
    assert app.get("/exchange/pairs?base=59&quote=main", follow_redirects=False).headers["location"] \
        == "/exchange/swap/regtest-token-59/main-coin", "the mainnet choice needs no switch"


def test_the_operator_opens_the_book_and_its_markets_join_the_list(client, monkeypatch):
    app, state = client
    import arcade.ledger as L
    monkeypatch.setattr(L.LedgerIndex, "property", lambda self, pid: {"property_id": 59, "name": "LOGS",
                                                                      "divisible": True, "managed": 0})
    monkeypatch.setattr(type(state.xchain), "address", lambda self, which: f"n{which}Exchange")
    from test_admin import LOCAL, WRITE
    page = app.get("/exchange/x/token/59?held=1").text
    assert "admin panel" in page, "the held page sends the operator to the admin panel"
    said = app.post("/admin/api/settings", headers={**LOCAL, **WRITE}, json={"xchain_enabled": True})
    assert said.status_code == 200, said.text
    assert state.setting("xchain:enabled")
    status = app.get("/admin/api/state", headers=LOCAL).json()["xchain"]
    assert status["enabled"] and status["addresses"]["mainnet"] == "nmainExchange"
    assert "Not trustless" in status["trust"]
    assert app.get("/exchange/pairs?base=59&quote=main", follow_redirects=False).headers["location"] \
        == "/exchange/swap/regtest-token-59/main-coin"
    assert app.get("/exchange/pairs?base=coin&quote=main", follow_redirects=False).headers["location"] \
        == "/exchange/swap/regtest-coin/main-coin"
    book = state.xchain.book
    o = book.place(owner="x", side="sell", kind="token", asset="59", amount=10 * COIN, pepe=5 * COIN,
                   pay_test="nT", pay_main="PM")
    book.deposited(o["id"], "dep1")
    book.count_deposit(o["id"])
    listing = app.get("/exchange?tab=tokens").text
    assert "/exchange/swap/regtest-token-59/main-coin" in listing, "an open market is a row of the one table"
    row = listing[listing.index("/exchange/swap/regtest-token-59/main-coin"):][:2500]
    assert "MAIN</span>" in row and "PEP" in row
    market = app.get("/exchange/x/token/59?held=1").text
    assert "0.5" in market and "Not trustless" in market
    swaps = app.get("/exchange/x/token/59").text
    assert "Lock scripts" in swaps and "Manage it there" not in swaps, "the operator's node placed nothing"


def test_a_piece_sold_for_mainnet_pep_is_a_sale_not_a_pair(client, monkeypatch):
    """2026-10-11: "These NFTs are in the token exchange list, they should be in
    the recently sold NFTs list." A piece is not a pair: its cross-chain sale is under
    Recently sold on the Market tab, badged MAINNET, and never a row of the pairs table."""
    app, state = client
    import arcade.ledger as L
    piece = "ab" * 32
    row = {"txid": piece, "number": 2066, "json": '{"name": "Doge Punk #7"}',
           "collection": "Doge Punks", "edition": 7, "creator": "nCreator", "owner": "nBuyer",
           "held": 0, "content_type": "image/png"}
    monkeypatch.setattr(L.LedgerIndex, "inscription", lambda self, key: row if key == piece else None)
    book = state.xchain.book
    sell = book.place(owner="aa" * 33, side="sell", kind="nft", asset=piece, amount=1,
                      pepe=100 * COIN, pay_test="nSellerTest", pay_main="PSellerMain")
    book.deposited(sell["id"], "dep-sell")
    book.count_deposit(sell["id"])
    buy = book.place(owner="bb" * 33, side="buy", kind="nft", asset=piece, amount=1,
                     pepe=100 * COIN, pay_test="nBuyerTest", pay_main="PBuyerMain")
    book.deposited(buy["id"], "dep-buy")
    book.count_deposit(buy["id"])
    assert book.fills("nft", piece), "the two orders matched"

    pairs = app.get("/exchange?tab=tokens").text
    assert f"/exchange/x/nft/{piece}" not in pairs and "inscription #2066" not in pairs, \
        "a piece is not a row of the pairs table"
    market = app.get("/exchange?tab=market").text
    assert "Recently sold" in market, "the sale is on the Market tab"
    sold = market[market.index("Recently sold"):][:4000]
    assert "Doge Punk #7" in sold and "100 PEP" in sold and "MAIN</span>" in sold, sold
    assert "PSellerMain" in sold, "the seller is the order's mainnet address"
