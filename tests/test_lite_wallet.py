"""The node's wallet in the console and arcade-cli: balances, a new address, transactions, coins, and a send in Admin's
own two steps (built and shown with its fee, then broadcast on confirm, at the node's machine only). Against a real
regtest node, then the rules on their own."""
import types

import pytest

from test_tokens_web import web, mine_and_index  # noqa: F401  (fixture re-export)
from test_end_to_end import chain  # noqa: F401
from test_web import app_state  # noqa: F401

from arcade.lite import commands as cmdlib

LINE = {"x-arcade-lite": "1", "x-arcade-admin": "1"}


def console(app, line):
    return app.post("/lite/api/console", headers=LINE, json={"line": line}).json()


def test_the_node_wallet_from_the_console(web):
    app, state, node, alice, bob = web
    wallets = console(app, "getbalances")
    assert wallets["ok"], wallets
    which = next(w["which"] for w in wallets["result"] if w["network"] == "regtest" and not w.get("keyed"))
    fresh = console(app, f"getnewaddress {which}")["result"]
    assert node.rpc.call("validateaddress", fresh)["isvalid"]
    assert console(app, f"listunspent {which}")["result"]

    offer = console(app, f"send {which} {bob} 1.5")["result"]
    assert offer["to"] == bob and offer["amount"] == "1.5" and float(offer["fee"]) > 0
    assert offer["next"] == f"confirm {offer['ticket']}"
    assert node.rpc.call("getrawmempool") == [], "send only prepares"
    sent = console(app, f"confirm {offer['ticket']}")
    assert sent["ok"] and sent["result"]["txid"] in node.rpc.call("getrawmempool")
    again = console(app, f"confirm {offer['ticket']}")
    assert not again["ok"] and "expired" in again["error"], "a ticket goes once"
    mine_and_index(node, state)
    rows = console(app, f"listtransactions {which} 5")["result"]
    assert any(r["txid"] == sent["result"]["txid"] for r in rows) and len(rows) <= 5


def test_a_wrong_wallet_or_amount_is_said_and_nothing_is_built(web):
    app, state, node, alice, bob = web
    assert "unknown wallet" in console(app, f"send nowhere {bob} 1")["error"]
    assert not console(app, "send messaging nobody 1")["ok"]
    assert not console(app, f"listtransactions messaging 0")["ok"]
    assert node.rpc.call("getrawmempool") == []


class _Node:
    """A node wallet that only says what it was asked."""
    def __init__(self):
        self.asked = []

    def prepare(self, *args):
        self.asked.append(args)
        return {"ticket": "t1", "to": args[1], "amount": args[2], "fee": "0.01", "network": "P main"}

    def broadcast(self, ticket):
        self.asked.append(("broadcast", ticket))
        return "ab" * 32


def test_an_amount_is_the_text_typed(app_state):
    node = _Node()
    op = cmdlib.Caller(app_state, "operator", node=node, local=True)
    cmdlib.run_line(op, "send ledger @alice 0.00000001")
    cmdlib.run_line(op, "send ledger @alice 100")
    assert node.asked == [("ledger", "@alice", "0.00000001"), ("ledger", "@alice", "100")]


def test_a_send_is_confirmed_only_at_the_node_machine(app_state):
    node = _Node()
    away = cmdlib.Caller(app_state, "operator", node=node, local=False)
    assert cmdlib.run_line(away, "send ledger @alice 1")["next"] == "confirm t1"
    with pytest.raises(cmdlib.CommandError, match="at the node's own machine"):
        cmdlib.run_line(away, "confirm t1")
    assert ("broadcast", "t1") not in node.asked
    here = cmdlib.Caller(app_state, "operator", node=node, local=True)
    assert cmdlib.run_line(here, "confirm t1") == {"txid": "ab" * 32}


def test_only_the_operator_has_the_node_wallet(app_state):
    for audience in ("anyone", "user"):
        who = cmdlib.Caller(app_state, audience, "02" + "ab" * 32, node=types.SimpleNamespace())
        for name in ("getbalances", "getnewaddress", "listtransactions", "listunspent", "send", "confirm"):
            with pytest.raises(cmdlib.CommandError, match=f"no command '{name}'"):
                cmdlib.run_line(who, f"{name} ledger")
        assert "send wallet to amount" not in cmdlib.run_line(who, "help"), "the wallet's send is the operator's"
    op = cmdlib.Caller(app_state, "operator")
    assert "== Wallet ==" in cmdlib.run_line(op, "help") and "send wallet to amount" in cmdlib.run_line(op, "help")
    with pytest.raises(cmdlib.CommandError, match="answers through the arcade's web app"):
        cmdlib.run_line(op, "getbalances")
