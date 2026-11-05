"""Who runs this node (2026-11-04, Plan B of the proposal SadFrog chose): the arcade and its owner are two names.

The node's own key is the place: it signs the arcade's broadcasts, takes its fees and keeps its own @name. The account
that owns it is the person, with its own @name and its coins in its own browser. Before this an account made the
operator took over the arcade's name; now the arcade answers to its node's own name when its key has one, and to its
owner's when it has none, until the owner chooses otherwise in Admin -> This node.
"""

import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_public import _seat                                   # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}


@pytest.fixture
def names(client, monkeypatch):
    """A node named @pumpkin, and the names the chain knows, which a test adds to."""
    app, state = client
    monkeypatch.setattr(type(state), "derived_address", "nTheNodesOwn")
    known = {"nTheNodesOwn": "pumpkin"}
    monkeypatch.setattr(type(state), "token_index",
                        lambda self, chain: types.SimpleNamespace(tag_of=known.get, indexed_height=lambda: 0))
    yield app, state, known
    for key in ("operator", "arcade_name"):
        state.set_setting(key, None)


def _owner(state, known, pubkey="ab" * 32, name="sadfrog"):
    """An owner account with a wallet and a name of its own, as one made operator before Plan B."""
    state.claim_operator(pubkey)
    state.set_setting(f"address:{pubkey}", "nTheOwners")
    known["nTheOwners"] = name
    return pubkey


def test_a_node_with_nobody_answers_to_its_own_name(names):
    app, state, known = names
    assert (state.operator_tag(), state.node_tag(), state.owner_tag()) == ("pumpkin", "pumpkin", "")
    assert state.owner_kind() == ""
    assert state.arcade_name() == "node"


def test_an_owned_arcade_answers_to_its_own_name_when_its_key_has_one(names):
    """Unset, an owner account's arcade answers to the node's own name when its key has one (SadFrog: a node whose own
    key holds @orange "should be presenting as @orange" with its owner as owner), and to the owner's when it has none.
    The owner chooses either in Admin -> This node."""
    app, state, known = names
    _owner(state, known)
    assert state.owner_kind() == "account" and state.arcade_name() == "node"
    assert (state.operator_tag(), state.node_tag(), state.owner_tag()) == ("pumpkin", "pumpkin", "sadfrog")
    assert app.post("/admin/api/settings", headers={**LOCAL, "X-Arcade-Admin": "1"},
                    json={"arcade_name": "owner"}).status_code == 200
    assert state.operator_tag() == "sadfrog" and state.owner_tag() == "sadfrog"
    setup = app.get("/admin/api/setup", headers=LOCAL).json()
    assert setup["operator_is_node"] is False, "the owner's own key claims the owner's name"
    refused = app.post("/admin/api/settings", headers={**LOCAL, "X-Arcade-Admin": "1"}, json={"arcade_name": "both"})
    assert refused.status_code == 400 and state.arcade_name() == "owner"
    state.set_setting("arcade_name", None)
    del known["nTheNodesOwn"]
    assert state.arcade_name() == "owner" and state.operator_tag() == "sadfrog", "a node with no name of its own"


def test_a_new_owner_leaves_the_arcade_its_name(names):
    """Made the owner at the machine, an account no longer takes the arcade's name: it changes hands, not names."""
    app, state, known = names
    mine = _seat(app)
    assert app.post("/auth/operator", headers=LOCAL).status_code == 200
    state.set_setting(f"address:{mine}", "nTheOwners")
    known["nTheOwners"] = "sadfrog"
    assert state.operator == mine and state.owner_kind() == "account"
    assert state.operator_tag() == "pumpkin" and state.owner_tag() == "sadfrog"


def test_a_password_runs_the_node_and_has_no_name_of_its_own(names):
    app, state, known = names
    assert app.post("/auth/set-password", headers=LOCAL,
                    json={"username": "robin", "password": "correct horse battery"}).status_code == 200
    assert state.owner_kind() == "password" and state.owner_tag() == "" and state.operator_tag() == "pumpkin"


def test_admin_shows_both_names_and_the_till(names):
    app, state, known = names
    page = app.get("/admin", headers=LOCAL).text
    assert "This arcade is\n    <strong>@pumpkin</strong>." in page and 'name="arcade_name"' not in page
    assert "@pumpkin's till" in page
    _owner(state, known)
    page = app.get("/admin", headers=LOCAL).text
    assert "owned by <strong>@sadfrog</strong>" in page
    assert 'value="node" checked' in page and 'value="@sadfrog"' in page, "the till sends to the owner by default"
    assert '>Status</button>' in page, "not a second Overview inside Lite's"
    info = app.post("/lite/api/console", headers={**LOCAL, "X-Arcade-Lite": "1"}, json={"line": "getnodeinfo"}).json()
    assert info["ok"] and {k: info["result"][k] for k in ("operator", "node_name", "owner", "owned_by")} == {
        "operator": "pumpkin", "node_name": "pumpkin", "owner": "sadfrog", "owned_by": "account"}
