"""Node-facilitated trades: OFF by default, an operator switch (2026-10-13: "I
want an option in my operator node admin panel. That will ... enable node facilitated
trades. It should be by default. That trades are only available if they leave their
tab open so that transaction building is complete on the client side solely. ... But
node facilitated trading should still be an option.")."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_me_page import _seat, LOCAL                            # noqa: E402

pytestmark = pytest.mark.node_trades_default

REFUSED = ("/account/list", "/account/list/sign", "/account/buy", "/account/offer/bid-build",
           "/account/offer/bid", "/account/accept/complete")


def test_it_is_off_until_the_operator_turns_it_on(client):
    from test_admin import WRITE
    app, state = client
    said = app.get("/admin/api/state", headers=LOCAL).json()["selling"]
    assert said["node_trades"] is False, "off by default"
    page = app.get("/admin", headers=LOCAL).text
    assert 'id="node_trades"' in page and "Node-facilitated trades" in page
    assert app.post("/admin/api/settings", headers={**LOCAL, **WRITE},
                    json={"node_trades": True}).status_code == 200
    assert app.get("/admin/api/state", headers=LOCAL).json()["selling"]["node_trades"] is True
    assert state.setting("node_trades", False) is True


def test_nothing_the_node_would_hold_is_taken_while_it_is_off(client):
    app, state = client
    _seat(app)
    for route in REFUSED:
        said = app.post(route, headers=LOCAL, json={})
        assert said.status_code == 403, (route, said.status_code, said.text[:200])
        assert "both people are here" in said.json()["detail"], route
    away = app.post("/account/standing", headers=LOCAL, json={"away": True, "property_id": 1,
                                                              "amount": "1", "price": "1"})
    assert away.status_code in (400, 403)
    if away.status_code == 403:
        assert "both people are here" in away.json()["detail"]


def test_the_custodial_swap_needs_the_switch_too(client, monkeypatch):
    """Custody of both sides' coins is the most node-facilitated trade there is: the
    cross-chain book stays closed while the switch is off, whatever its own switch says."""
    from test_admin import WRITE
    import arcade.ledger as L
    app, state = client
    monkeypatch.setattr(L.LedgerIndex, "property", lambda self, pid: {
        "property_id": 59, "name": "LOGS", "divisible": True, "managed": 0})
    monkeypatch.setattr(type(state.xchain), "address", lambda self, which: f"n{which}Exchange")
    state.set_setting("xchain:enabled", True)
    went = app.get("/exchange/pairs?base=59&quote=main", follow_redirects=False)
    assert went.headers["location"].startswith("/exchange?tab=tokens"), "closed while off"
    app.post("/admin/api/settings", headers={**LOCAL, **WRITE}, json={"node_trades": True})
    went = app.get("/exchange/pairs?base=59&quote=main", follow_redirects=False)
    assert went.headers["location"] == "/exchange/x/token/59", "open once the operator says so"


def test_the_pages_say_so_instead_of_offering_it(client):
    app, state = client
    _seat(app)
    nfts = app.get("/me/nfts", headers=LOCAL)
    if nfts.status_code == 200 and "Sell it" in nfts.text:
        assert "This arcade trades only while both people are here" in nfts.text
        assert 'id="list-it" disabled' in nfts.text
    pad = app.get("/mintpad/new", headers=LOCAL).text
    assert "Mintpads need the node to hold your listings" in pad
