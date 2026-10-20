"""The operator's Wallet is the accounts' Wallet page, answered by the node's own wallets (2026-10-19: "the
wallet should look the same as the wall looks on the client side. Everything should look the same as it looks on the
client side on the operator side"). Against a real regtest node: its balance, its history, its tokens, and a send that
is built, shown and only then broadcast."""

from test_tokens_web import web, mine_and_index  # noqa: F401  (fixture re-export)
from test_end_to_end import chain  # noqa: F401

LOCAL = {"X-Arcade-Admin": "1"}


def test_the_operators_wallet_is_the_accounts_page(web):
    app, state, node, alice, bob = web
    page = app.get("/wallet").text
    assert 'class="wl-hero"' in page and 'id="tab-activity"' in page, "the accounts' Wallet, not the old panels"
    assert '"/node-wallet.js"' in page and '"/node/wallet"' in page
    assert "drawEscrows" not in page and "drawDust" not in page, "an account's own panels stay off the node's"
    assert app.get("/node-wallet.js").status_code == 200
    assert "Mine one block" in app.get("/wallet/tools").text, "the node's tools are kept"


def test_the_node_wallet_says_its_coins_history_and_tokens(web):
    app, state, node, alice, bob = web
    said = app.get("/node/wallet/state").json()
    row = next(c for c in said["chains"] if c["network"] == "regtest")
    assert row["address"] and row["spendable"] > 0 and "error" not in row
    act = app.get("/node/wallet/activity", params={"chain": "regtest"}).json()
    assert act["rows"] and all({"txid", "height", "net", "confirmations"} <= set(r) for r in act["rows"])
    assert app.get("/node/wallet/tokens").status_code == 200
    assert app.get("/node/wallet/nfts/collections", params={"chain": "regtest"}).status_code == 200


def test_a_send_is_shown_first_and_goes_on_confirm(web):
    app, state, node, alice, bob = web
    offer = app.post("/node/wallet/send", json={"to": bob, "amount": "1.5", "chain": "regtest"}, headers=LOCAL).json()
    assert offer["amount"] == 150_000_000 and offer["to"] == bob and offer["fee"] > 0 and offer["ticket"]
    assert node.rpc.call("getrawmempool") == [], "an offer sends nothing"
    sent = app.post("/admin/api/send/confirm", json={"ticket": offer["ticket"]}, headers=LOCAL).json()
    assert sent["txid"] in node.rpc.call("getrawmempool")
    mine_and_index(node, state)
    act = app.get("/node/wallet/activity", params={"chain": "regtest"}).json()
    assert any(r["txid"] == sent["txid"] for r in act["rows"])


def test_an_account_view_never_reads_the_node_wallet(web, monkeypatch):
    app, state, node, alice, bob = web
    from arcade.web import app as appmod
    monkeypatch.setattr(state, "public", True, raising=False)
    assert app.get("/node/wallet/state").status_code in (403, 404)


def test_a_token_goes_from_the_node_wallet_page(web):
    app, state, node, alice, bob = web
    form = dict(csrf_token=state.csrf_token, sender=alice, name="Node Token", supply="1000",
                kind="fixed", units="divisible")
    import re
    page = app.post("/tokens/create", data=form).text
    txid = re.search(r'name="confirmed" value="([0-9a-f]{64})"', page).group(1)
    app.post("/tokens/create", data={**form, "confirmed": txid}, follow_redirects=False)
    mine_and_index(node, state)
    (prop,) = state.token_index(state.ledger).properties()
    held = app.get("/node/wallet/tokens").json()["chains"]
    assert any(t["name"] == "Node Token" for c in held for t in c["tokens"]), "the page's Tokens tab lists it"
    offer = app.post("/node/wallet/token/send", headers=LOCAL, json={
        "property_id": prop["property_id"], "amount": "250", "to": bob, "chain": "regtest"}).json()
    assert offer["amount"] == 250 * 10**8 and offer["property_id"] == prop["property_id"] and offer["to"] == bob
    assert node.rpc.call("getrawmempool") == []
    sent = app.post("/admin/api/send/confirm", json={"ticket": offer["ticket"]}, headers=LOCAL).json()
    mine_and_index(node, state)
    assert state.token_index(state.ledger).balance(bob, prop["property_id"]) == 250 * 10**8, sent
