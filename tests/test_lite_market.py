"""Market queries in the console and arcade-cli, for anyone, as the Exchange's public pages show them: the tokens, a
token's sell book, a token pair, and listed Doginals. Read only; nothing here trades. Against a real regtest chain,
then the rules alone."""
import unittest.mock as mock

import pytest

from test_tokens_web import web, mine_and_index, shown  # noqa: F401  (fixture re-export)
from test_end_to_end import chain  # noqa: F401
from test_web import app_state  # noqa: F401

from arcade.lite import commands as cmdlib

LINE = {"x-arcade-lite": "1"}                      # no admin header: as anybody's console line


def console(app, line, headers=LINE):
    return app.post("/lite/api/console", headers=headers, json={"line": line}).json()


def test_a_token_and_its_order_read_from_the_console(web):
    app, state, node, alice, bob = web
    csrf = state.csrf_token
    form = dict(csrf_token=csrf, sender=alice, name="Lite Token", supply="1000", kind="fixed", units="divisible")
    txid = shown(app.post("/tokens/create", data=form).text, "txid")
    app.post("/tokens/create", data={**form, "confirmed": txid}, follow_redirects=False)
    mine_and_index(node, state)
    index = state.token_index(state.ledger)
    token = next(p for p in index.properties() if p["name"] == "Lite Token")
    home = index.balances([alice])[0]["address"]
    with mock.patch.object(type(state), "home_address", lambda self, chain: home):
        app.post("/exchange/order", data=dict(csrf_token=csrf, property_id=str(token["property_id"]), side="ask",
                                              amount="100", price="0.5"), follow_redirects=False)
        mine_and_index(node, state)

    listed = console(app, "listtokens")
    assert listed["ok"], listed
    mine = next(t for t in listed["result"] if t["id"] == token["property_id"])
    assert mine["name"] == "Lite Token" and mine["divisible"] and mine["holders"] >= 1
    book = console(app, f"getbook {token['property_id']}")["result"]
    assert book["token"] == "Lite Token" and len(book["asks"]) == 1
    assert book["asks"][0]["tokens"] == "100" and book["asks"][0]["coins"] == "50"
    assert "no such token" in console(app, "getbook 999999")["error"]
    pair = console(app, f"getpair {token['property_id']} {token['property_id']}")
    assert not pair["ok"] and "two different tokens" in pair["error"]


class _Reads:
    def __init__(self):
        self.asked = []

    def pair(self, base, quote, count):
        self.asked.append((base, quote, count))
        return {"pair": "A/B", "asks": [], "bids": [], "trades": []}

    def doginals(self, chain):
        self.asked.append(chain)
        return []


def test_market_queries_are_anyones_and_only_read(app_state):
    reads = _Reads()
    anyone = cmdlib.Caller(app_state, "anyone", reads=reads)
    assert cmdlib.run_line(anyone, "getpair 3 4 500")["pair"] == "A/B"
    assert cmdlib.run_line(anyone, "listdoginals main") == []
    assert reads.asked == [(3, 4, cmdlib.MOST_POSTS), "main"]
    with pytest.raises(cmdlib.CommandError, match="a token's number"):
        cmdlib.run_line(anyone, "getbook abc")
    assert "== Market ==" in cmdlib.run_line(anyone, "help")
    assert all(cmdlib.COMMANDS[n].audience == "anyone" for n in ("listtokens", "getbook", "getpair", "listdoginals"))


def test_market_queries_go_with_their_modules(app_state):
    anyone = cmdlib.Caller(app_state, "anyone", reads=_Reads())
    app_state.set_setting("edition", "lite")
    with pytest.raises(cmdlib.CommandError, match="Exchange module is off on this arcade$"):
        cmdlib.run_line(anyone, "getpair 3 4")
    with pytest.raises(cmdlib.CommandError, match="Tokens module is off"):
        cmdlib.run_line(anyone, "listtokens")
    app_state.set_setting("modules", {"exchange": True})
    assert cmdlib.run_line(anyone, "getpair 3 4")["pair"] == "A/B"
