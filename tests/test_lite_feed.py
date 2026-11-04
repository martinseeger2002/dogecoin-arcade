"""The feed in the console and arcade-cli: read by anyone, as the Feed tab is; posted to, replied to and liked by the
node's operator, with the tab's own rule that a post needs a @name first."""
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state  # noqa: F401,E402
from test_feed_web import THEM, a_post, an_act  # noqa: E402

from arcade.lite import commands as cmdlib  # noqa: E402
from arcade.messaging import feed  # noqa: E402
from arcade.web.app import create_app  # noqa: E402

LINE = {"x-arcade-lite": "1", "x-arcade-admin": "1"}
OUTSIDE = {"host": "arcade.example.com", "cf-connecting-ip": "198.51.100.9", "x-arcade-lite": "1"}


@pytest.fixture
def app(app_state):
    app_state.set_setting("public_hosts", ["arcade.example.com"])
    yield TestClient(create_app(app_state)), app_state
    app_state.set_setting("public_hosts", [])


def console(client, line, headers=LINE):
    return client.post("/lite/api/console", headers=headers, json={"line": line}).json()


def test_anyone_reads_the_feed_as_its_tab_shows_it(app):
    client, state = app
    a_post(state, "d" * 64, text="regrettable", height=99)
    an_act(state, "e" * 64, feed.DELETE, "d" * 64)
    for n in range(3):
        a_post(state, f"{n:064x}", text=f"post {n}", height=100 + n)
    an_act(state, "f" * 64, feed.LIKE, f"{1:064x}", author="nSomebodyElseAAAAAAAAAAAAAAAAAAAAA")
    said = console(client, "readfeed new 10", headers=OUTSIDE)
    assert said["ok"], said
    texts = [p["text"] for p in said["result"]]
    assert texts == ["post 2", "post 1", "post 0"], "newest first, the deleted one gone"
    assert next(p for p in said["result"] if p["text"] == "post 1")["likes"] == 1
    assert all("mine" not in p for p in said["result"]), "a visitor is nobody's 'mine'"
    assert [p["text"] for p in console(client, f"readposts {THEM} 2", headers=OUTSIDE)["result"]] == ["post 2", "post 1"]
    assert not console(client, "readfeed oldest", headers=OUTSIDE)["ok"]
    assert "mine" in console(client, "readfeed new 1")["result"][0]


def test_a_post_needs_a_name_and_only_the_operator_posts(app):
    client, state = app
    refused = console(client, "post hello there")
    assert not refused["ok"] and ("claim a @tag first" in refused["error"]
                                  or "waiting for the Pepecoin test node" in refused["error"])
    with state.store() as store:
        assert store.feed_posts(state.messaging.network) == []
    for line in ("post hello", "like " + "a" * 64, "reply " + "a" * 64 + " hi"):
        assert "no command" in console(client, line, headers=OUTSIDE)["error"]


class _Node:
    def __init__(self):
        self.did = []

    def feed_post(self, text):
        self.did.append(("post", text))
        return "11" * 32

    def feed_act(self, doing, txid, text=""):
        self.did.append((doing, txid, text))
        return "22" * 32


def test_the_operators_feed_commands_are_the_tabs_buttons(app_state):
    node = _Node()
    op = cmdlib.Caller(app_state, "operator", node=node, local=True)
    target = "a" * 64
    assert cmdlib.run_line(op, "post the arcade's open: true") == {"txid": "11" * 32}
    cmdlib.run_line(op, f"reply {target} see you there")
    for verb, doing in (("like", "like"), ("unlike", "unlike"), ("share", "share"), ("deletepost", "delete")):
        cmdlib.run_line(op, f"{verb} {target}")
    assert node.did == [("post", "the arcade's open: true"), ("reply", target, "see you there"),
                        ("like", target, ""), ("unlike", target, ""), ("share", target, ""), ("delete", target, "")]
    with pytest.raises(cmdlib.CommandError, match="usage: reply"):
        cmdlib.run_line(op, f"reply {target}")


def test_the_feed_commands_go_with_the_feed_module(app_state):
    op = cmdlib.Caller(app_state, "operator", node=_Node(), local=True)
    assert "== Feed ==" in cmdlib.run_line(op, "help")
    app_state.set_setting("edition", "lite")
    with pytest.raises(cmdlib.CommandError, match="Feed module is off on this arcade; `install feed`"):
        cmdlib.run_line(op, "readfeed")
    assert "readfeed" not in cmdlib.run_line(cmdlib.Caller(app_state, "anyone"), "help")
