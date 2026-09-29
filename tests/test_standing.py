"""Buy orders that fill without their buyer standing over them (the operator,
2026-09-28): the server fills them from pre-signed lots, or -- for a buyer who
trusts nobody -- the app fills them when the buyer is back."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_order import node, _bookcoin, _placed, _held, _signed, COIN, HELD  # noqa: F401,E402
from test_account_offer import _seated, _settled                                     # noqa: E402
from test_funding import _sign                                                       # noqa: E402

from arcade import funding                                                           # noqa: E402


def test_a_buy_order_fills_while_its_buyer_is_away(node):
    maker = _bookcoin(node, 70)
    app, state, rpc = node
    buyer, bsecret, bpubkey, baddr = _seated(app, state, rpc, 71, coins=(8.0, 1.0))
    pid = maker["pid"]

    placed = buyer.post("/account/standing", json={
        "property_id": pid, "amount": "20", "price": "0.1", "away": True, "lots": 2})
    assert placed.status_code == 200, placed.text
    sid = placed.json()["standing"]["id"]
    _signed(buyer, bsecret, bpubkey, placed)                 # the lots are made
    lots = buyer.post("/account/standing/lots", json={"standing": sid}).json()["lots"]
    assert len(lots) == 2 and all(l["units"] == 10 * COIN for l in lots)
    sigs = [_sign(bsecret, bytes.fromhex(l["digest"]), funding.SINGLE_ANYONECANPAY).hex()
            for l in lots]
    assert buyer.post("/account/standing/sign",
                      json={"standing": sid, "signatures": sigs}).status_code == 200
    _settled(state, rpc)

    page = app.get(f"/exchange/pair/{pid}").text
    assert "fills while they are away" in page, "it stands in the book"

    # The buyer goes away. A seller arrives under their price.
    _placed(maker, "ask", "30", "0.08")
    for _ in range(2):                                       # two passes, one lot each
        app.get(f"/exchange/pair/{pid}")
        _settled(state, rpc)
    assert _held(state, baddr, pid) == (20 * COIN, 0), "both lots bought while away"
    assert _held(state, maker["address"], pid)[1] == 10 * COIN, "10 left on the sell order"
    assert "fills while they are away" not in app.get(f"/exchange/pair/{pid}").text, \
        "and a filled buy order leaves the book"


def test_a_buy_order_fills_when_its_buyer_is_back(node):
    maker = _bookcoin(node, 72)
    app, state, rpc = node
    buyer, bsecret, bpubkey, baddr = _seated(app, state, rpc, 73, coins=(8.0, 1.0))
    pid = maker["pid"]
    placed = buyer.post("/account/standing", json={
        "property_id": pid, "amount": "5", "price": "0.2", "away": False})
    assert placed.status_code == 200 and "offer" not in placed.json(), placed.text
    sid = placed.json()["standing"]["id"]

    assert buyer.get("/account/standing/fillable").json()["fills"] == [], "nothing crosses yet"
    _placed(maker, "ask", "10", "0.1")
    app.get(f"/exchange/pair/{pid}")                         # the filler only tells
    assert _held(state, baddr, pid) == (0, 0), "nothing moves without the buyer"
    (fill,) = buyer.get("/account/standing/fillable").json()["fills"]
    assert fill["standing"] == sid and fill["units"] == 5 * COIN

    took = buyer.post("/account/order/take", json={
        "order": fill["order"], "units": str(fill["units"]), "standing": sid})
    assert took.status_code == 200, took.text
    _signed(buyer, bsecret, bpubkey, took)
    _settled(state, rpc)
    assert _held(state, baddr, pid) == (5 * COIN, 0)
    assert buyer.get("/account/standing/fillable").json()["fills"] == [], "filled, so nothing left"


def test_a_cancelled_buy_order_forgets_its_signatures(node):
    maker = _bookcoin(node, 74)
    app, state, rpc = node
    buyer, bsecret, bpubkey, baddr = _seated(app, state, rpc, 75, coins=(8.0, 1.0))
    placed = buyer.post("/account/standing", json={
        "property_id": maker["pid"], "amount": "10", "price": "0.1", "away": True, "lots": 1})
    sid = placed.json()["standing"]["id"]
    _signed(buyer, bsecret, bpubkey, placed)
    lots = buyer.post("/account/standing/lots", json={"standing": sid}).json()["lots"]
    buyer.post("/account/standing/sign", json={"standing": sid, "signatures": [
        _sign(bsecret, bytes.fromhex(lots[0]["digest"]), funding.SINGLE_ANYONECANPAY).hex()]})
    assert buyer.post("/account/standing/cancel", json={"standing": sid}).status_code == 200
    _settled(state, rpc)
    _placed(maker, "ask", "10", "0.05")
    app.get(f"/exchange/pair/{maker['pid']}")
    _settled(state, rpc)
    assert _held(state, baddr, maker["pid"]) == (0, 0), "a cancelled order buys nothing"


def test_a_token_creator_sees_buy_orders_on_offers(node):
    """2026-09-28: the creator of a token sees who wants to buy it on
    Exchange > Offers, with a way to sell to them."""
    maker = _bookcoin(node, 77)
    app, state, rpc = node
    buyer, _s, _p, baddr = _seated(app, state, rpc, 78, coins=(8.0, 1.0))
    pid = maker["pid"]
    index = state.token_index(state.messaging)
    with index.open() as db:
        db.conn.execute("UPDATE property SET issuer=? WHERE property_id=?", (maker["address"], pid))
        db.conn.commit()
    assert buyer.post("/account/standing", json={
        "property_id": pid, "amount": "7", "price": "0.25", "away": False}).status_code == 200
    state.public = True
    try:
        page = maker["client"].get("/exchange?tab=offers").text
        other = buyer.get("/exchange?tab=offers").text
    finally:
        state.public = False
    assert "Buy orders on your tokens" in page
    assert f"/exchange/pair/{pid}?sell=0.25&amp;amount=7" in page, "and a way to sell to them"
    assert "Buy orders on your tokens" not in other, "only the token's creator sees it"

    # Declined: gone from the creator's list, still on the book (2026-09-28).
    import re as _re
    key = _re.search(r'data-bid-decline="([^"]+)"', page).group(1)
    assert maker["client"].post("/account/token-bid/decline", json={"key": key}).status_code == 200
    state.public = True
    try:
        page = maker["client"].get("/exchange?tab=offers").text
        book = maker["client"].get(f"/exchange/pair/{pid}").text
    finally:
        state.public = False
    assert f'data-bid-decline="{key}"' not in page
    assert "fills when they are back" in book, "the order itself stays on the book"
