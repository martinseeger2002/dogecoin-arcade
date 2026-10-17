"""Trade history (2026-10-17: "a trade history button on the main token exchange page")."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_web import app_state, client                       # noqa: F401,E402
from test_trade_page import BUYER, SELLER, TAKE, _index, _write  # noqa: E402

from arcade.web import door                                   # noqa: E402


def test_the_operator_sees_its_wallet_trades_each_opening_its_page(client, monkeypatch):
    app, state = client
    chain, index = _index(state)
    _write(index)
    import arcade.web.app as appmod
    monkeypatch.setattr(appmod, "_ledger_addresses", lambda rpc, fresh=False: [SELLER])

    class Rpc:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(type(chain), "rpc", lambda self: Rpc())
    page = app.get("/exchange/history").text
    assert f"/exchange/trade/{TAKE}?chain={chain.network}" in page
    assert "<b>sold</b> 70 token #14" in page and BUYER in page, "what it sold, and to whom"


def test_the_exchange_and_every_market_point_at_it(client):
    app, _ = client
    assert 'href="/exchange/history">Trade history</a>' in app.get("/exchange?tab=tokens").text
    assert door.public_path("/exchange/history")


def test_from_a_market_it_shows_that_market_only(client, monkeypatch):
    app, state = client
    chain, index = _index(state)
    _write(index)
    import arcade.web.app as appmod
    monkeypatch.setattr(appmod, "_ledger_addresses", lambda rpc, fresh=False: [SELLER])

    class Rpc:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(type(chain), "rpc", lambda self: Rpc())
    net = chain.network
    here = app.get(f"/exchange/history?base={net}-token-14&quote={net}-coin").text
    assert f"/exchange/trade/{TAKE}" in here and "All your trades" in here
    other = app.get(f"/exchange/history?base={net}-token-99&quote={net}-coin").text
    assert f"/exchange/trade/{TAKE}" not in other
