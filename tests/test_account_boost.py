"""Paying the operator for extra room (2026-09-28: "users should be
able to pay the operator for extra usage ... in whatever the operator decides,
coins [or] tokens")."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import node, _seated, _settled, _signed   # noqa: F401,E402

from arcade import accounts as accountslib                           # noqa: E402


def test_an_account_buys_more_room_from_the_operator(node):
    app, state, rpc = node
    op_client, _op_secret, op_pubkey, op_address = _seated(app, state, rpc, 60)
    who, secret, pubkey, address = _seated(app, state, rpc, 61)

    assert who.get("/account").json().get("boost") is None, "nothing for sale by default"
    assert who.post("/account/boost", json={}).status_code == 400

    state.claim_operator(op_client.get("/account").json()["pubkey"])
    state.set_setting("boost:price", "1")
    state.set_setting("boost:asset", 0)
    state.set_setting("boost:bytes", 100000)
    state.set_setting("boost:days", 7)
    said = who.get("/account").json()
    assert said["boost"]["bytes"] == 100000 and said["boost"]["days"] == 7
    before = said["quota"]["bytes"]["limit"]

    offer = who.post("/account/boost", json={})
    assert offer.status_code == 200, offer.text
    terms = offer.json()
    assert any(o["address"] == address for o in terms["inputs"])
    done = _signed(who, secret, pubkey, terms)
    assert done.status_code == 200, done.text
    tx = rpc.call("getrawtransaction", done.json()["txid"], 1)
    paid = [o for o in tx["vout"]
            if op_address in (o.get("scriptPubKey", {}).get("addresses") or [])]
    assert paid and abs(float(paid[0]["value"]) - 1.0) < 1e-8, "one coin to the operator"

    room = who.get("/account").json()["quota"]["bytes"]
    assert room["limit"] == before + 100000 and room["boost"] == 100000
    _settled(state, rpc)


def test_boost_terms_are_off_without_a_price():
    assert accountslib.boost_terms({}) is None
    t = accountslib.boost_terms({"boost:price": "2", "boost:asset": "14"})
    assert t == {"bytes": accountslib.BYTES_PER_DAY, "days": 30, "price": "2", "asset": 14}


def test_a_boost_can_be_priced_in_a_token(node):
    """Whatever the operator decides: here, 5 of a token."""
    from test_account_order import _bookcoin, _held, COIN
    maker = _bookcoin(node, 62)
    app, state, rpc = node
    op_client, _s, _p, op_address = _seated(app, state, rpc, 63)
    state.claim_operator(op_client.get("/account").json()["pubkey"])
    state.set_setting("boost:price", "5")
    state.set_setting("boost:asset", maker["pid"])
    who = maker["client"]
    said = who.get("/account").json()["boost"]
    assert said["asset"] == maker["pid"] and said["asset_name"]
    offer = who.post("/account/boost", json={})
    assert offer.status_code == 200, offer.text
    done = _signed(who, maker["secret"], maker["pubkey"], offer.json())
    assert done.status_code == 200, done.text
    _settled(state, rpc)
    assert _held(state, op_address, maker["pid"])[0] == 5 * COIN, "five tokens reached the operator"
    assert who.get("/account").json()["quota"]["bytes"].get("boost"), "and the room was counted"
