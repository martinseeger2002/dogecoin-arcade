"""The owner's signed word that it owns a node (arcade/owner.py, 2026-11-04, "Who runs this node", Plan B).

Signed in the owner's browser by its wallet (coins.js ownerWords, wallet.js ownerSign) in Admin -> This node, checked
from its bytes by this node before it is kept, and said in the mesh's "me" frame only when the owner ticks to.
"""

import asyncio
import pathlib
import sys
import time
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_blind_signing import HARNESS                    # noqa: E402
from test_htlc import Key                                        # noqa: E402
from test_trade_tab import run_wallet_js                         # noqa: E402
from test_web import app_state, client                          # noqa: F401,E402

from arcade import llm, owner                                    # noqa: E402
from arcade.config import NETWORKS                               # noqa: E402
from arcade.mesh import operators                                # noqa: E402
from arcade.mesh.service import operator_about                   # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}
ADMIN = {**LOCAL, "X-Arcade-Admin": "1"}
NODE, OTHER = "a1" * 32, "b2" * 32


def _signed(tmp_path, node=NODE, yes=True, network="test"):
    """A statement the real wallet.js signs for `node` on `network`, and the key that signed it."""
    key = Key(NETWORKS[network].pubkeyhash_version)
    data = {"priv": key.secret.to_bytes(32, "big").hex(), "address": key.address, "yes": yes, "node": node,
            "network": network}
    got = run_wallet_js(tmp_path, HARNESS + """
      store.set("arcade.node.self", data.node);
      const signed = await tryIt(() => w.ownerSign(mine, data.network, data.node));
      return {signed, asked, words: c.ownerWords(data.network, data.node, data.address, now + 3600)};
    """, data)
    return key, got


def test_the_wallet_signs_the_words_the_node_checks(tmp_path):
    key, got = _signed(tmp_path)
    said = got["signed"]["ok"]
    assert said["a"] == key.address and owner.shaped(said) == said
    assert owner.check(said, "test", NODE) == key.address
    assert owner.check(said, "test", OTHER) == "", "good for the node it names only"
    assert owner.check(said, "main", NODE) == "", "and on its own chain"
    assert owner.check({**said, "x": said["x"] + 1}, "test", NODE) == ""
    assert got["words"].startswith("arcade-owner\ntest\n" + NODE + "\n" + key.address + "\n")
    assert len(got["asked"]) == 1 and got["asked"][0]["words"].startswith("arcade-owner\n"), "shown word for word"
    assert llm.signed_by(said["s"], owner.words("test", NODE, key.address, said["x"]), key.address)


def test_nothing_is_signed_when_the_owner_says_no(tmp_path):
    key, got = _signed(tmp_path, yes=False)
    assert got["signed"]["error"] == "Nothing was signed: you said no."


def test_a_statement_runs_out_and_is_due_again_before_it_does():
    now = time.time()
    said = {"a": "n" + "A" * 33, "x": int(now + 10 * 86400), "s": "B" * 88}
    assert owner.due(said, now) and owner.due(None, now)
    assert not owner.due({**said, "x": int(now + 300 * 86400)}, now)
    assert owner.check(said, "test", NODE, lambda *a: True, now=now + 11 * 86400) == "", "run out"
    assert owner.check({**said, "x": int(now + 500 * 86400)}, "test", NODE, lambda *a: True, now=now) == "", "too long"
    for bad in ({**said, "x": True}, {**said, "a": "n\nx"}, {**said, "s": "x" * 500}, "words", None):
        assert owner.shaped(bad) is None


@pytest.fixture
def owned(client, monkeypatch, tmp_path):
    """A node with mesh id NODE whose owner account is the key a real wallet signed with."""
    app, state = client
    monkeypatch.setattr(state, "mesh", types.SimpleNamespace(node_id=NODE), raising=False)
    key, got = _signed(tmp_path, network=state.messaging.network)
    state.claim_operator("cd" * 32)
    state.set_setting(f"address:{'cd' * 32}", key.address)
    yield app, state, got["signed"]["ok"]
    for k in ("operator", "owner_statement", "owner_say"):
        state.set_setting(k, None)


def test_admin_keeps_only_this_nodes_owner_saying_it(owned):
    app, state, said = owned
    assert app.get("/admin", headers=LOCAL).text.count('id="owner-sign"') == 1, "asked to sign"
    other = {**said, "a": Key(NETWORKS[state.messaging.network].pubkeyhash_version).address}
    assert app.post("/admin/api/owner", headers=ADMIN, json={"statement": other}).status_code == 400
    assert state.setting("owner_statement") is None
    kept = app.post("/admin/api/owner", headers=ADMIN, json={"statement": said, "say": True})
    assert kept.status_code == 200 and kept.json()["signed"] and kept.json()["say"] and not kept.json()["due"]
    assert state.setting("owner_statement") == said
    assert 'id="owner-sign"' not in app.get("/admin", headers=LOCAL).text, "not asked again until it is due"
    state.claim_operator("ef" * 32)
    assert state.setting("owner_statement") is None, "a new owner does not inherit the last one's word"


def test_linked_arcades_hear_it_only_when_the_owner_ticks_to(owned):
    app, state, said = owned
    state.set_setting("owner_statement", said)
    assert "owner" not in operator_about(state)
    state.set_setting("owner_say", True)
    assert operator_about(state)["owner"] == said
    heard = operators.OperatorsShare(lambda: {})
    link = types.SimpleNamespace(peer_id=NODE)
    asyncio.run(heard.on_frame(link, {"op": "me", "owner": said}, b""))
    assert heard.peers[NODE]["owner"] == said and "owner" not in heard.status()[0]
    asyncio.run(heard.on_frame(link, {"op": "me", "owner": {"a": "x"}}, b""))
    assert heard.peers[NODE]["owner"] is None


def test_your_nodes_are_the_ones_whose_statements_name_your_key(owned, tmp_path):
    """Heard from the node itself or passed on by a linked one; a statement for another node or another key is not."""
    app, state, said = owned
    me, fig, quince, stranger, hub = NODE, "c3" * 32, "d4" * 32, "e5" * 32, "f6" * 32
    good = lambda node: _signed(tmp_path / node[:4], node=node, network=state.messaging.network)  # noqa: E731
    key, got = good(fig)
    state.set_setting(f"address:{state.operator}", key.address)    # the owner's key is this one now
    fig_said = got["signed"]["ok"]
    quince_said = _sign_as(tmp_path / "q", key, quince, state.messaging.network)
    now = time.time()
    peers = {
        fig: {"tag": "fig", "hosts": ["fig.example.net"], "owner": fig_said, "at": now, "links": []},
        hub: {"tag": "bigchiefenergy", "at": now, "links": [
            {"id": quince, "tag": "quince", "host": "quince.example.org", "owner": quince_said},
            {"id": stranger, "tag": "stranger", "owner": fig_said}]},       # fig's statement says nothing of stranger
    }
    rows = operators.owned_by(peers, {fig, hub}, key.address, state.messaging.network, owner.check, me)
    # quince's host and @name were the hub's word, unsigned: not shown (2026-11-05 review), only who passed it on.
    assert [(r["id"], r["tag"], r["host"], r["linked"], r["by_tag"]) for r in rows] == [
        (fig, "fig", "fig.example.net", True, ""), (quince, "", "", False, "bigchiefenergy")]
    assert operators.owned_by(peers, {fig, hub}, "n" + "Z" * 33, state.messaging.network, owner.check, me) == []
    clean = operators.clean_links(peers[hub]["links"])
    assert clean[0]["owner"] == quince_said, "passed on unchanged"


def _sign_as(path, key, node, network):
    """`key`'s statement for `node`, signed by the real wallet.js."""
    data = {"priv": key.secret.to_bytes(32, "big").hex(), "address": key.address, "yes": True, "node": node,
            "network": network}
    return run_wallet_js(path, HARNESS + """
      store.set("arcade.node.self", data.node);
      return await w.ownerSign(mine, data.network, data.node);
    """, data)


def test_admin_asks_once_and_lists_your_nodes(owned):
    app, state, said = owned
    page = app.get("/admin", headers=LOCAL).text
    assert 'id="yournodes"' in page and "Sign that you own this node, so your other nodes list it" in page
    got = app.get("/admin/api/your-nodes", headers=LOCAL).json()
    assert got["rows"] == [], "no mesh here: nothing heard"
    state.set_setting("owner_statement", said)
    assert "Sign that you own this node, so your other nodes" not in app.get("/admin", headers=LOCAL).text
    state.set_setting(f"address:{state.operator}", None)
    state.claim_operator("")
    assert app.post("/auth/set-password", headers=LOCAL,
                    json={"username": "robin", "password": "correct horse battery"}).status_code == 200
    page = app.get("/admin", headers=LOCAL).text
    assert 'id="yournodes"' not in page and "This node runs with a machine password: make your account its owner" in page
