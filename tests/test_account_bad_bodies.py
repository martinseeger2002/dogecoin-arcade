"""A badly shaped body is refused, never a crash (2026-11-09, security review).

Seven /account routes answered 500 to a body of the wrong shape: /account/trade/facts, /account/mailbox,
/account/fill, /account/fill/sign, /account/list/sign and /account/bundle/fill asked the node with no signed bytes or
no txid at all, or read a list where a number goes; /account/dust/sweep reads nothing but its chain and asked the node
because it should, so its 500 was the tests' missing node (now a 503 here, `no_node_is_503`). app.py reads such fields
through `_said_int`, `_said_list` and `_said_hex`, which refuse with a plain reason -- 400, before the node -- and read
a good body exactly as before.

The sweep below tries every write route under /account and every POST a stranger may make (door.public_path) with
bodies that are not objects, and with each field the route reads set to the wrong kind of thing. None may answer 500.
Its first run found more of the same, fixed with the same readers: a number where a list goes (orders/post,
orders/cancel, mintpad/announced, standing/sign), a body that is not an object (ord/forget, keys/<id>), a list where a
number goes (claimlots, standing, tokenpad/inscribe, token/manage, pools/open, split, /r/referee/sign), Infinity or
NaN in an amount (ledger.parse_amount, wallet.parse_amount, llm/grant, react), and a number past 64 bits as a token or
inscription number (LedgerIndex.property and .inscription, /r/state/update).
"""
from __future__ import annotations

import inspect
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_cross_writes import FIELD, LOCAL, WRITES, _sign_in, no_node_is_503  # noqa: E402
from test_funding import _pubkey                                 # noqa: E402
from test_web import app_state                                   # noqa: F401,E402

from arcade.config import NETWORKS                               # noqa: E402
from arcade.script import b58check_encode, hash160               # noqa: E402

#: Not objects at all.
NOT_OBJECTS = ["[]", '"a string"', "5", "null", "true"]
#: The wrong kind of thing in a field: numbers where words go and words where numbers go, lists, objects, hex that is
#: not hex, a long word, and the numbers Python's JSON reads and nothing else does (Infinity, NaN, past 64 bits).
WRONG = [1, -1, 1.5, True, [], ["x"], {"x": 1}, "zz", "x" * 300, "@Infinity", "@NaN", 10 ** 30,
         {"raw": 5, "signatures": 5, "pubkey": 5, "a_sigs": 5, "amount": [1], "kind": "token", "asset": "1",
          "take": {"kind": "coins", "sats": [1]}, "give": {"token": [1]}, "get": {"token": "x"}}]


def _text(body) -> str:
    """JSON as a browser could send it, with Infinity and NaN, which only Python's reader takes."""
    return json.dumps(body).replace('"@Infinity"', "Infinity").replace('"@NaN"', "NaN")


def _app(app_state):
    from fastapi.testclient import TestClient

    from arcade.web.app import create_app
    app = create_app(app_state)
    no_node_is_503(app)
    client = TestClient(app, raise_server_exceptions=False)
    _sign_in(client, LOCAL)
    address = b58check_encode(NETWORKS["test"].pubkeyhash_version, hash160(_pubkey(int("a1" * 32, 16))))
    assert client.post("/account/address", headers=LOCAL, json={"address": address}).status_code == 200
    return app, client


def _ask(client, path: str, body, method: str = "POST"):
    return client.request(method, path, headers={**LOCAL, "content-type": "application/json"},
                          content=body if isinstance(body, str) else _text(body))


def _writes(app) -> list[tuple[str, str, list[str]]]:
    """(method, path, the fields its code reads) for every write under /account and every public POST."""
    from fastapi.routing import APIRoute

    from arcade.web import door
    found = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = re.sub(r"\{[^}]+\}", "0badc0de", route.path)
        methods = route.methods & WRITES if route.path.startswith("/account") else (
            {"POST"} & route.methods if door.public_path(path, "POST") else set())
        if not methods:
            continue
        try:
            source = inspect.getsource(route.endpoint)
        except (OSError, TypeError):
            source = ""
        for method in methods:
            found[(method, path)] = sorted({"".join(m) for m in FIELD.findall(source)})
    return [(m, p, f) for (m, p), f in sorted(found.items())]


def test_no_write_route_answers_500_to_a_badly_shaped_body(app_state):
    app_state.set_setting("node_trades", True)        # the routes behind it are swept too (list/sign)
    app, client = _app(app_state)
    routes = _writes(app)
    assert len(routes) > 150 and ("POST", "/account/trade/facts") in {(m, p) for m, p, _ in routes}
    assert any(not p.startswith("/account") for _, p, _ in routes), "the public POSTs are swept too"
    crashed = set()
    for method, path, fields in routes:
        bodies = NOT_OBJECTS + ["{}"] + [{f: v} for f in fields for v in WRONG]
        bodies += [{"leg": WRONG[-1], "handoff": WRONG[-1], "expect": WRONG[-1]}]
        for body in bodies:
            if _ask(client, path, body, method).status_code == 500:
                crashed.add(f"{method} {path} {body if isinstance(body, str) else _text(body)}"[:160])
    where = sorted({" ".join(c.split(" ")[:2]) for c in crashed})
    assert not crashed, f"{len(crashed)} bodies crashed {where}, for one {sorted(crashed)[:3]}"


def test_the_seven_routes_say_what_is_wrong_with_a_body(app_state):
    """Each of the routes the review named answers 400 and a plain reason, before the node is asked."""
    app_state.set_setting("node_trades", True)
    _, client = _app(app_state)
    leg = "00" * 60
    refused = [
        ("/account/trade/facts", {}, "name the lock's txid"),
        ("/account/trade/facts", {"txid": "ab"}, "the lock's txid has to be 32 bytes"),
        ("/account/trade/facts", {"txid": "ab" * 32, "vout": [1]}, "the lock's output has to be a number"),
        ("/account/trade/facts", {"txid": "ab" * 32, "leg": {"kind": "token", "amount": "@Infinity"}},
         "a leg's amount has to be a whole number"),
        ("/account/mailbox", {"blob": "x", "base": [1]}, "the base has to be a number"),
        ("/account/fill", {}, "name the signed leg"),
        ("/account/fill", {"leg": {"raw": 5}}, "the signed leg has to be hex"),
        ("/account/fill", {"leg": {"raw": "00" * 32}}, "the signed leg is too short"),
        ("/account/fill", {"leg": {"raw": leg, "signatures": 5}}, "the leg's signatures has to be a list"),
        ("/account/fill", {"leg": {"raw": leg, "pubkey": "02" * 20}}, "the seller's public key has to be 33 or 65"),
        ("/account/fill", {"leg": {"raw": leg, "take": {"kind": "coins", "sats": [1]}}},
         "the coins a leg takes has to be a number"),
        ("/account/fill/sign", {"leg": {"raw": "zz"}}, "the signed leg has to be hex"),
        ("/account/list/sign", {}, "name the signed leg"),
        ("/account/list/sign", {"raw": leg, "signatures": "ab"}, "the leg's signatures has to be a list"),
        ("/account/bundle/fill", {"handoff": {"raw": "zz"}}, "the bundle's bytes has to be hex"),
        ("/account/bundle/fill", {"handoff": {}}, "name the bundle's bytes"),
        ("/account/orders/post", {"orders": 1}, "orders has to be a list"),
        ("/account/orders/cancel", {"ids": 1}, "ids has to be a list"),
    ]
    for path, body, said in refused:
        r = _ask(client, path, body)
        assert r.status_code == 400 and said in r.json()["detail"], (path, body, r.status_code, r.text)


def test_a_well_shaped_body_still_goes_to_the_node(app_state):
    """Nothing a good body says is refused by the new reading: each of these gets as far as the node (here none, so
    503 from `no_node_is_503`), as it always did."""
    app_state.set_setting("node_trades", True)
    _, client = _app(app_state)
    leg = "00" * 60
    good = [
        ("/account/trade/facts", {"txid": "AB" * 32, "vout": "1"}),
        ("/account/trade/facts", {"txid": "ab" * 32, "vout": 0, "leg": {"kind": "coin", "amount": 2.0}}),
        ("/account/fill", {"leg": {"raw": leg, "signatures": ["aa"], "pubkey": "02" + "11" * 32}}),
        ("/account/fill", {"leg": {"raw": leg, "take": {"kind": "coins", "sats": "5000"}}}),
        ("/account/list/sign", {"raw": leg}),
        ("/account/bundle/fill", {"handoff": {"raw": leg}}),
        ("/account/dust/sweep", {}),
    ]
    for path, body in good:
        r = _ask(client, path, body)
        assert r.status_code == 503 and r.json()["detail"] == "no node available (test)", (path, body, r.text)
