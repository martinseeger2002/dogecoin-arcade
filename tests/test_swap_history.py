"""Finished lock-script swaps fill their market's Recent trades, chart and day line (2026-10-17:
"The swap worked but the recent trades didn't update and there is no chart started")."""
import pathlib
import sys

from nacl.signing import SigningKey

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_web import app_state, client                                  # noqa: F401,E402
from test_admin import EDGE, public                                     # noqa: F401,E402
from test_auth_web import _sign_in                                      # noqa: E402
from test_asset_pair import COIN, _gold                                 # noqa: E402

from arcade.swap_history import SwapHistory                              # noqa: E402

TID = "tr-" + "1a" * 8


def test_the_first_report_files_it_and_the_second_fills_in_the_other_side(tmp_path):
    h = SwapHistory(tmp_path / "s.sqlite")
    h.record(TID, "test-token-59", "main-coin", 10 * COIN, 20 * COIN, seller="nS", lock_base="a" * 64)
    h.record(TID, "test-token-59", "main-coin", 10 * COIN, 20 * COIN, buyer="nB", lock_quote="b" * 64)
    row = h.for_pair("test-token-59", "main-coin")[0]
    assert (row["seller"], row["buyer"], row["lock_base"], row["lock_quote"]) == ("nS", "nB", "a" * 64, "b" * 64)
    assert h.of({"nB"})[0]["id"] == TID and h.for_pair("main-coin", "test-token-59") == []


def test_a_page_reports_its_finished_swap_and_the_market_shows_it(public, monkeypatch):
    app, state = public
    _gold(monkeypatch)
    net, main = state.messaging.network, state.ledger.network

    class Rpc:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def call(self, method, *args):
            assert method == "getrawtransaction"
            return "00"
    for chain in state.token_chains:
        monkeypatch.setattr(type(chain), "rpc", lambda self: Rpc())
    assert _sign_in(app, SigningKey.generate(), headers=EDGE).status_code == 200
    said = app.post("/account/trade/done", headers=EDGE, json={
        "id": TID, "role": "responder",
        "give": {"network": net, "kind": "token", "asset": "59", "amount": 10 * COIN},
        "get": {"network": main, "kind": "coin", "asset": "", "amount": 20 * COIN},
        "my_lock": "a" * 64, "their_lock": "b" * 64})
    assert said.status_code == 200, said.text
    page = app.get(f"/exchange/swap/{net}-token-59/{main}-coin", headers=EDGE).text
    assert f"/exchange/swap-trade/{TID}" in page, "a row of Recent trades, opening the swap"
    assert "2.00000000" in page and "No trades yet on this chain" not in page, "priced 2 PEP each, and charted"
    swap = app.get(f"/exchange/swap-trade/{TID}", headers=EDGE).text
    assert "Seller" in swap and "Buyer" in swap and "a" * 64 in swap and "b" * 64 in swap
    bad = app.post("/account/trade/done", headers=EDGE, json={"id": "nope"})
    assert bad.status_code == 400
