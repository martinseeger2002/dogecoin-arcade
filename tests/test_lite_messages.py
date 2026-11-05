"""The node's Messenger in the console and arcade-cli: its conversations, one read (and marked read), and a text
message sent at once on testnet with the Messages tab's guards. Against a real regtest node, then the rules alone."""
import shlex

import pytest

from test_tokens_web import web  # noqa: F401  (fixture re-export)
from test_end_to_end import chain  # noqa: F401
from test_web import app_state  # noqa: F401

from arcade.lite import commands as cmdlib
from arcade.messaging import contact
from arcade.messaging.keys import Identity

LINE = {"x-arcade-lite": "1", "x-arcade-admin": "1"}


def console(app, line):
    return app.post("/lite/api/console", headers=LINE, json={"line": line}).json()


def test_a_message_from_the_console_is_sent_kept_and_read_back(web):
    app, state, node, alice, bob = web
    state.ensure_identity()                            # mined coins cannot pay for a message: an ordinary send can
    node.rpc.call("sendtoaddress", state.derived_address, 50)
    node.generate(1)
    peer = Identity.generate()
    code = contact.encode(state.messaging.network, peer.public_bytes)
    sent = console(app, f"sendmessage {code} don't forget: true 100")
    assert sent["ok"], sent
    assert sent["result"]["txid"] in node.rpc.call("getrawmempool")
    with state.store() as store:
        row = store.conn.execute("SELECT body FROM sent").fetchone()
    assert bytes(row["body"]) == b"don't forget: true 100", "the text as typed, kept as our own copy"
    again = console(app, f"sendmessage {code} don't forget: true 100")
    assert not again["ok"] and "just sent" in again["error"]

    threads = console(app, "listconversations")["result"]
    mine = next(t for t in threads if t["key"] == peer.public_bytes.hex())
    assert mine["outgoing"] and mine["preview"] == "don't forget: true 100"
    read = console(app, f"readmessages {peer.public_bytes.hex()} 5")["result"]
    assert read[-1]["mine"] and read[-1]["text"] == "don't forget: true 100"


def test_a_long_message_or_an_unknown_name_sends_nothing(web):
    app, state, node, alice, bob = web
    before = set(node.rpc.call("getrawmempool"))
    code = contact.encode(state.messaging.network, Identity.generate().public_bytes)
    long = console(app, f"sendmessage {code} " + "word " * 4000)
    assert not long["ok"] and "send it from Messages" in long["error"]
    assert not console(app, "sendmessage @nobodyhere hello")["ok"]
    assert not console(app, f"sendmessage {code}")["ok"]
    assert set(node.rpc.call("getrawmempool")) == before


class _Node:
    def __init__(self):
        self.sent = []

    def message(self, who, text):
        self.sent.append((who, text))
        return {"txid": "ab" * 32, "to": who}


@pytest.mark.parametrize("line,who,text", [
    ("sendmessage @bob don't go", "@bob", "don't go"),
    ("sendmessage @bob   see you at 10.5, true", "@bob", "see you at 10.5, true"),
    (shlex.join(["sendmessage", "@bob", "don't go", "now"]), "@bob", "don't go now"),   # as arcade-cli sends it
])
def test_the_rest_of_the_line_is_the_message_as_typed(app_state, line, who, text):
    node = _Node()
    cmdlib.run_line(cmdlib.Caller(app_state, "operator", node=node, local=True), line)
    assert node.sent == [(who, text)]


def test_messages_are_the_operators_and_an_accounts_own_in_every_edition(app_state):
    op = cmdlib.Caller(app_state, "operator", node=_Node(), local=True)
    assert cmdlib.run_line(op, "sendmessage -help").startswith("sendmessage who text...")
    assert "== Messages ==" in cmdlib.run_line(op, "help")
    user = cmdlib.Caller(app_state, "user", "02" + "ab" * 32)
    assert "sendmessage" in cmdlib.run_line(user, "help"), "a user's console lists them too"
    for line in ("sendmessage @bob hi", "listconversations", "readmessages @bob"):
        with pytest.raises(cmdlib.CommandError, match="open only in your own browser"):
            cmdlib.run_line(user, line)                  # the node cannot read an account's letters: its window does
    with pytest.raises(cmdlib.CommandError, match="no command 'sendmessage'"):
        cmdlib.run_line(cmdlib.Caller(app_state, "anyone"), "sendmessage @bob hi")
    app_state.set_setting("edition", "lite")             # Messages is part of the core (2026-11-04)
    assert "sendmessage" in cmdlib.run_line(op, "help")
    with pytest.raises(cmdlib.CommandError, match="part of the core"):
        cmdlib.run_line(op, "remove messages")
