"""The /tokens pages against a real regtest node.

test_web.py proves every token page renders with the node unreachable. This
proves the other half: that the forms build, show and broadcast a transaction
the index then reads back -- and that what the confirm screen says (fee, txid,
recipient) is what the node saw, not merely that a table was drawn.
"""

import dataclasses
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from arcade.config import NETWORKS
from arcade.rpc import RpcClient
from arcade.script import b58check_encode
from arcade.web.app import create_app
from arcade.web.state import AppState, ChainContext
from arcade.web.watcher import BlockWatcher
from test_end_to_end import chain  # noqa: F401  (fixture re-export)


@dataclasses.dataclass
class RegtestContext(ChainContext):
    """A ChainContext wired to the test node rather than a datadir on disk."""

    node: Any = None
    activation: int | None = None

    @property
    def params(self):
        return dataclasses.replace(super().params, activation_height=self.activation)

    def rpc(self) -> RpcClient:
        return RpcClient(self.node.rpc._creds)


@pytest.fixture
def web(tmp_path, chain):
    node, alice, bob = chain
    ledger = RegtestContext(network="regtest", role="ledger", label="Testnet",
                            node=node, activation=node.rpc.get_block_count() + 1,
                            marker=node.rpc.call("getnewaddress"))
    state = AppState(home=tmp_path, ledger=ledger,
                     messaging=RegtestContext(network="regtest", role="messaging",
                                              label="Testnet", node=node))
    return TestClient(create_app(state)), state, node, alice, bob


def mine_and_index(node, state):
    node.generate(1)
    BlockWatcher(state)._sync_ledgers()          # what the daemon thread would do
    assert state.token_index(state.ledger).stopped is None, state.token_index(state.ledger).stopped


def shown(page: str, label: str) -> str:
    """The value the confirm table shows beside `label`."""
    match = re.search(rf'<td class="muted"[^>]*>{label}</td>\s*<td[^>]*>(?:<strong>)?([^<\n]+)', page)
    assert match, f"{label!r} is not on the confirm page"
    return match.group(1).strip()


def test_create_confirm_broadcast_and_read_back(web):
    app, state, node, alice, bob = web
    csrf = state.csrf_token

    page = app.get("/tokens").text
    assert "No tokens exist" in page
    assert f'value="{alice}"' in page, "the funded address must be offered as issuer"

    assert app.post("/tokens/create", data={"name": "Web Token"}).status_code == 400

    # Prepare: nothing broadcast, the transaction shown.
    form = dict(csrf_token=csrf, sender=alice, name="Web Token", supply="1000",
                kind="fixed", units="divisible")
    page = app.post("/tokens/create", data=form).text
    assert "Broadcast" in page and 'name="confirmed"' in page
    txid = shown(page, "txid")
    fee = shown(page, "Fee")
    assert shown(page, "From") == alice
    assert shown(page, "Total") == fee, "a creation has no recipient, so the fee is the whole cost"
    assert node.rpc.call("getrawmempool") == [], "prepare must not broadcast"

    # A confirmation for a transaction this server no longer holds (say it
    # restarted in between) is shown again, not sent.
    page = app.post("/tokens/create", data={**form, "confirmed": "0" * 64}).text
    assert "Confirm before sending" in page
    assert node.rpc.call("getrawmempool") == []

    # Confirm: it reaches the mempool with exactly the fee the page showed.
    response = app.post("/tokens/create", data={**form, "confirmed": txid},
                        follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/tokens"
    entry = node.rpc.call("getmempoolentry", txid)
    assert f"{float(entry['fee']):.8f}" == fee
    page = app.get("/tokens").text
    assert txid in page and "waiting for its block" in page

    mine_and_index(node, state)
    page = app.get("/tokens").text
    assert "waiting for its block" not in page
    assert "Web Token" in page and "1,000" in page
    (prop,) = state.token_index(state.ledger).properties()
    detail = app.get(f"/tokens/{prop['property_id']}").text
    assert 'class="pill ok">you</span>' in detail, "the issuer is in this wallet"
    assert "create (fixed supply)" in detail
    assert "Issuer controls" in detail
    assert app.get("/tokens/999").status_code == 404

    # Send: refused before it costs anything, then shown, then read back.
    send = dict(csrf_token=csrf, sender=alice, property_id=str(prop["property_id"]))
    page = app.post("/tokens/send", data={**send, "amount": "5000", "recipient": bob}).text
    assert "holds 1,000 Web Token, not 5,000" in page
    mainnet_address = b58check_encode(NETWORKS["main"].pubkeyhash_version, bytes(20))
    page = app.post("/tokens/send", data={
        **send, "amount": "250", "recipient": mainnet_address}).text
    assert "that is a main address, not a testnet one" in page
    assert node.rpc.call("getrawmempool") == []

    page = app.post("/tokens/send", data={**send, "amount": "250", "recipient": bob}).text
    assert shown(page, "To") == bob
    txid = shown(page, "txid")
    app.post("/tokens/send", data={**send, "amount": "250", "recipient": bob,
                                   "confirmed": txid}, follow_redirects=False)
    assert txid in node.rpc.call("getrawmempool")
    mine_and_index(node, state)
    index = state.token_index(state.ledger)
    assert index.balance(alice, prop["property_id"]) == 750 * 10**8
    assert index.balance(bob, prop["property_id"]) == 250 * 10**8
    page = app.get("/tokens").text
    assert ">750<" in page and ">250<" in page, "both wallet addresses are 'yours'"
