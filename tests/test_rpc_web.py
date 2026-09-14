"""The bot RPC (/rpc/<chain>) against a real regtest node.

What a bot author is promised: Omni Core's method names answer, the cookie is
the only key, nothing is broadcast until omni_broadcast names what was
prepared, and what the index then reads back is what the RPC said it sent.
"""

import base64
import json
import stat

import pytest

from arcade import rpccli
from arcade.config import NETWORKS
from arcade.script import b58check_encode
from arcade.web import rpc as botrpc
from test_end_to_end import chain  # noqa: F401  (fixture re-export)
from test_tokens_web import mine_and_index, web  # noqa: F401


class Bot:
    """A client the way a bot would be written: cookie in, JSON out."""

    def __init__(self, app, state, which="test"):
        self.app, self.state, self.url = app, state, f"/rpc/{which}"
        user, secret = botrpc.read_cookie(state.write_rpc_cookie())
        token = base64.b64encode(f"{user}:{secret}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}"}
        self.calls = 0

    def raw(self, payload, headers=None):
        return self.app.post(self.url, content=json.dumps(payload),
                             headers=self.headers if headers is None else headers)

    def __call__(self, method, *params):
        self.calls += 1
        response = self.raw({"jsonrpc": "1.0", "id": self.calls,
                             "method": method, "params": list(params)})
        assert response.status_code == 200, response.text
        answer = response.json()
        assert answer["id"] == self.calls
        if answer["error"]:
            raise botrpc.RpcError(answer["error"]["code"], answer["error"]["message"])
        return answer["result"]


def refused(bot, code, method, *params):
    with pytest.raises(botrpc.RpcError) as caught:
        bot(method, *params)
    assert caught.value.code == code, caught.value.message
    return caught.value.message


def test_the_cookie_is_the_only_key(web):
    app, state, node, alice, bob = web
    bot = Bot(app, state)
    path = state.rpc_cookie_path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text().startswith("__cookie__:")

    body = {"method": "omni_getinfo", "params": [], "id": 1}
    assert bot.raw(body, headers={}).status_code == 401
    wrong = base64.b64encode(b"__cookie__:nope").decode()
    assert bot.raw(body, headers={"Authorization": f"Basic {wrong}"}).status_code == 401
    # The forms' CSRF token is not a key here, and vice versa.
    assert bot.raw(body, headers={"Authorization": f"Basic {state.csrf_token}"}).status_code == 401
    assert bot.raw(body).status_code == 200

    # A chain that is not indexed is a 404, not a silent answer from another.
    assert app.post("/rpc/main", content="{}", headers=bot.headers).status_code == 404
    assert app.post("/rpc", content="{}", headers=bot.headers).status_code == 404
    assert bot.raw(None).status_code == 200            # "null" is JSON, just not a request
    assert app.post(bot.url, content="not json", headers=bot.headers).status_code == 400


def test_help_and_bad_calls_answer_in_bitcoind_codes(web):
    app, state, node, alice, bob = web
    bot = Bot(app, state)
    listing = bot("help")
    for name in ("omni_getbalance", "omni_getallbalancesforid", "omni_send", "omni_broadcast",
                 "omni_sendissuancefixed", "omni_sendgrant", "omni_listtransactions"):
        assert name in listing
    assert "two calls" in listing
    assert 'omni_send "fromaddress"' in bot("help", "omni_send")
    assert refused(bot, -32601, "omni_sendsto", alice, 3, "1")
    assert "Usage: omni_getbalance" in refused(bot, -32602, "omni_getbalance", alice)
    assert refused(bot, -3, "omni_getbalance", alice, "three")
    assert refused(bot, -8, "omni_getproperty", 999) == "there is no token 999"
    assert refused(bot, -32601, "help", "nothing")
    # Named parameters work as well as positional ones.
    answer = bot.raw({"id": 9, "method": "omni_getproperty", "params": {"propertyid": 999}}).json()
    assert answer["error"]["code"] == -8


def test_a_bot_creates_sends_and_reads_back(web):
    app, state, node, alice, bob = web
    bot = Bot(app, state)

    info = bot("omni_getinfo")
    assert info["network"] == "regtest" and info["mainnet"] is False
    assert info["activationblock"] == state.ledger.activation
    assert bot("omni_listproperties") == []

    # Create: prepared, shown, not sent -- then sent byte for byte.
    prepared = bot("omni_sendissuancefixed", alice, 1, 2, 0, "Games", "Arcade",
                   "Bot Token", "https://example.test", "made by a bot", "1000")
    assert prepared["broadcast"] is False
    assert prepared["sendingaddress"] == alice and prepared["referenceaddress"] is None
    assert node.rpc.call("getrawmempool") == [], "prepare must not broadcast"
    assert refused(bot, -8, "omni_broadcast", "0" * 64).startswith("no prepared transaction")
    txid = bot("omni_broadcast", prepared["txid"])
    assert txid == prepared["txid"]
    entry = node.rpc.call("getmempoolentry", txid)
    assert f"{float(entry['fee']):.8f}" == prepared["fee"]
    assert refused(bot, -8, "omni_broadcast", txid), "a broadcast is forgotten once sent"
    assert "waiting for its block" in app.get("/tokens").text, "the page knows what the bot sent"

    mine_and_index(node, state)
    (prop,) = bot("omni_listproperties")
    assert prop["name"] == "Bot Token" and prop["divisible"] is True
    pid = prop["propertyid"]
    full = bot("omni_getproperty", pid)
    assert full["issuer"] == alice and full["fixedissuance"] is True
    assert full["totaltokens"] == "1000.00000000" and full["creationtxid"] == txid
    assert bot("omni_getbalance", alice, pid) == {
        "balance": "1000.00000000", "reserved": "0.00000000", "frozen": "0.00000000"}
    assert bot("omni_getallbalancesforid", pid) == [
        {"address": alice, "balance": "1000.00000000",
         "reserved": "0.00000000", "frozen": "0.00000000"}]
    created = bot("omni_gettransaction", txid)
    assert created["valid"] is True and created["type_int"] == 50
    assert created["propertyname"] == "Bot Token" and created["amount"] == "1000.00000000"
    assert created["blockhash"] == node.rpc.call("getblockhash", created["block"])
    assert created["confirmations"] == 1

    # Send: refused before it costs anything, in the code a client expects.
    assert "holds 1000.00000000" in refused(bot, -8, "omni_send", alice, bob, pid, "5000")
    mainnet_address = b58check_encode(NETWORKS["main"].pubkeyhash_version, bytes(20))
    assert "main address" in refused(bot, -5, "omni_send", alice, mainnet_address, pid, "1")
    stranger = b58check_encode(NETWORKS["regtest"].pubkeyhash_version, bytes(20))
    assert "not in this node's wallet" in refused(bot, -4, "omni_send", stranger, alice, pid, "1")
    assert refused(bot, -8, "omni_send", alice, bob, pid, "1.123456789")
    assert refused(bot, -3, "omni_send", alice, bob, pid, True)
    assert node.rpc.call("getrawmempool") == []

    # The airdrop shape: holders in, one prepare+broadcast per holder.
    holders = [h["address"] for h in bot("omni_getallbalancesforid", pid) if h["address"] != alice]
    assert holders == []
    prepared = bot("omni_send", alice, bob, pid, "250")
    assert prepared["referenceaddress"] == bob and prepared["class"] == "C"
    assert any(o["recipient"] and o["to"] == bob for o in prepared["outputs"])
    sent = bot("omni_broadcast", prepared["txid"])
    mine_and_index(node, state)
    assert bot("omni_getbalance", bob, pid)["balance"] == "250.00000000"
    assert bot("omni_getbalance", alice, pid)["balance"] == "750.00000000"
    assert bot("omni_getallbalancesforaddress", bob) == [
        {"propertyid": pid, "name": "Bot Token", "balance": "250.00000000",
         "reserved": "0.00000000", "frozen": "0.00000000"}]
    assert bot("omni_getwalletbalances") == [
        {"propertyid": pid, "name": "Bot Token", "balance": "1000.00000000",
         "reserved": "0.00000000", "frozen": "0.00000000"}]
    by_address = {row["address"]: row["balances"] for row in bot("omni_getwalletaddressbalances")}
    assert by_address[alice][0]["balance"] == "750.00000000"
    assert by_address[bob][0]["balance"] == "250.00000000"

    listed = bot("omni_listtransactions")
    assert [t["txid"] for t in listed] == [txid, sent], "oldest first"
    assert bot("omni_listtransactions", bob)[0]["txid"] == sent
    assert bot("omni_listtransactions", "*", 1) == [listed[-1]]
    assert bot("omni_listtransactions", "*", 1, 1) == [listed[0]]
    assert bot("omni_listtransactions", "*", 10, 0, created["block"], created["block"]) == [listed[0]]
    assert bot("omni_listblocktransactions", created["block"]) == [txid]
    assert bot("omni_listblocktransactions", created["block"] + 1) == [sent]

    # A batch, as python-bitcoinrpc's batch_() sends one.
    answers = bot.raw([{"id": "a", "method": "omni_getbalance", "params": [bob, pid]},
                       {"id": "b", "method": "nothing", "params": []},
                       "junk"]).json()
    assert answers[0]["result"]["balance"] == "250.00000000" and answers[0]["id"] == "a"
    assert answers[1]["error"]["code"] == -32601 and answers[1]["id"] == "b"
    assert answers[2]["error"]["code"] == -32600


def test_issuer_methods_are_the_issuers_alone(web):
    app, state, node, alice, bob = web
    bot = Bot(app, state)
    prepared = bot("omni_sendissuancemanaged", alice, 1, 1, 0, "", "", "Points", "", "")
    bot("omni_broadcast", prepared["txid"])
    mine_and_index(node, state)
    (prop,) = bot("omni_listproperties")
    pid = prop["propertyid"]
    assert bot("omni_getproperty", pid)["managedissuance"] is True
    assert bot("omni_getproperty", pid)["totaltokens"] == "0"

    assert "only the issuer" in refused(bot, -8, "omni_sendgrant", bob, bob, pid, "10")
    assert refused(bot, -8, "omni_sendgrant", alice, bob, pid, "1.5"), "whole units only"
    grant = bot("omni_sendgrant", alice, bob, pid, "10", "welcome")
    bot("omni_broadcast", grant["txid"])
    mine_and_index(node, state)
    assert bot("omni_getbalance", bob, pid)["balance"] == "10"
    assert bot("omni_gettransaction", grant["txid"])["type"] == "grant"

    to_self = bot("omni_sendgrant", alice, "", pid, "5")
    assert to_self["referenceaddress"] is None
    bot("omni_broadcast", to_self["txid"])
    mine_and_index(node, state)
    assert bot("omni_getbalance", alice, pid)["balance"] == "5"
    assert "all that can be revoked" in refused(bot, -8, "omni_sendrevoke", alice, pid, "6")
    bot("omni_broadcast", bot("omni_sendrevoke", alice, pid, "5")["txid"])
    mine_and_index(node, state)
    assert bot("omni_getproperty", pid)["totaltokens"] == "10"

    assert "already the issuer" in refused(bot, -8, "omni_sendchangeissuer", alice, alice, pid)
    bot("omni_broadcast", bot("omni_sendchangeissuer", alice, bob, pid)["txid"])
    mine_and_index(node, state)
    assert bot("omni_getproperty", pid)["issuer"] == bob
    assert "only the issuer" in refused(bot, -8, "omni_sendgrant", alice, bob, pid, "1")


def test_arcade_rpc_speaks_the_cookie(tmp_path, monkeypatch, capsys):
    """The command line reads the cookie and formats a request a bot would."""
    botrpc.write_cookie(tmp_path / "rpc.cookie", "s3cret")
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        @staticmethod
        def read():
            return json.dumps({"result": {"balance": "1"}, "error": None, "id": "arcade-rpc"}).encode()

    def fake_urlopen(request):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["body"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr(rpccli.urllib.request, "urlopen", fake_urlopen)
    assert rpccli.main(["--home", str(tmp_path), "omni_getbalance", "nAddress", "3"]) == 0
    assert seen["url"] == "http://127.0.0.1:8420/rpc/test", "testnet unless -main is said"
    assert seen["auth"] == "Basic " + base64.b64encode(b"__cookie__:s3cret").decode()
    assert seen["body"]["params"] == ["nAddress", 3], "numbers are numbers, addresses strings"
    assert json.loads(capsys.readouterr().out) == {"balance": "1"}

    assert rpccli.main(["--home", str(tmp_path), "-main", "omni_getinfo"]) == 0
    assert seen["url"].endswith("/rpc/main")
    assert rpccli.main(["--home", str(tmp_path / "nowhere"), "omni_getinfo"]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_a_bot_asks_and_the_owner_decides(web):
    """A program that is not trusted to spend files a request; the person
    sees the transaction it would be and says yes or no. Nothing reaches the
    mempool on the bot's word."""
    import re

    app, state, node, alice, bob = web
    bot = Bot(app, state)
    csrf = state.csrf_token
    prepared = bot("omni_sendissuancefixed", alice, 1, 1, 0, "Games", "Arcade",
                   "Ask Token", "", "", "100")
    bot("omni_broadcast", prepared["txid"])
    mine_and_index(node, state)
    (prop,) = bot("omni_listproperties")
    pid = prop["propertyid"]

    # Tokens: asked for, with the wallet choosing which address pays.
    asked = bot("da_requesttoken", "", bob, pid, "5", "for the tournament")
    assert asked["status"] == "pending" and asked["origin"] == "rpc"
    assert asked["propertyname"] == "Ask Token" and asked["amount"] == "5"
    assert asked["note"] == "for the tournament"
    assert node.rpc.call("getrawmempool") == [], "asking builds nothing"
    assert bot("da_request", asked["id"])["status"] == "pending"
    assert "5 Ask Token to" in app.get("/approvals").text
    assert "a program on the bot RPC" in app.get("/approvals").text
    assert refused(bot, -8, "da_requesttoken", alice, bob, 999, "1") == "there is no token 999"
    assert refused(bot, -8, "da_requestinscription", bob, "0") == "no such inscription"
    assert refused(bot, -8, "da_request", "nope") == "no such request"

    page = app.get(f"/approvals/{asked['id']}").text
    assert "Approve and send" in page and "for the tournament" in page
    txid = re.search(r'name="confirmed" value="([0-9a-f]{64})"', page).group(1)
    assert node.rpc.call("getrawmempool") == [], "looking builds; it does not send"
    done = app.post(f"/approvals/{asked['id']}", data={"csrf_token": csrf, "confirmed": txid})
    assert "Approved and sent" in done.text
    assert node.rpc.call("getrawmempool") == [txid], "exactly what was shown"
    answer = bot("da_request", asked["id"])
    assert answer["status"] == "sent" and answer["txid"] == txid
    mine_and_index(node, state)
    assert bot("omni_getbalance", bob, pid)["balance"] == "5"

    # Coins from a named address, looked at and refused: nothing moves.
    asked = bot("da_requestsend", alice, bob, "2.5")
    page = app.get(f"/approvals/{asked['id']}").text
    assert "2.50000000" in page and alice in page and "change, back to you" in page
    app.post(f"/approvals/{asked['id']}", data={"csrf_token": csrf, "decision": "deny"})
    assert bot("da_request", asked["id"])["status"] == "denied"
    assert node.rpc.call("getrawmempool") == []

    # Coins with the wallet choosing, approved: they arrive.
    had = float(node.rpc.call("getreceivedbyaddress", bob, 0))
    asked = bot("da_requestsend", "", bob, "2.5")
    page = app.get(f"/approvals/{asked['id']}").text
    txid = re.search(r'name="confirmed" value="([0-9a-f]{64})"', page).group(1)
    app.post(f"/approvals/{asked['id']}", data={"csrf_token": csrf, "confirmed": txid})
    mine_and_index(node, state)
    assert float(node.rpc.call("getreceivedbyaddress", bob, 0)) == had + 2.5
    assert bot("da_request", asked["id"])["txid"] == txid

    listing = bot("da_requests")
    assert sorted(r["status"] for r in listing) == ["denied", "sent", "sent"]
    assert "da_requestsend" in bot("help") and "Asking is one call" in bot("help")


def test_a_page_asks_for_an_inscription_and_it_changes_hands(web):
    """The same queue from the other door: an inscribed page (any origin,
    no cookie) asks, and the owner hands the inscription over."""
    import re

    from arcade import inscribe
    from arcade.messaging.sender import MessageSender

    app, state, node, alice, bob = web
    plan = inscribe.plan(b"a small thing worth asking for", "text/plain",
                         '{"name": "Askers #1"}')
    MessageSender(node.rpc, state.ledger.params, public_only=True).send_all(
        alice, plan.payloads)
    mine_and_index(node, state)
    index = state.token_index(state.ledger)
    (row,) = index.inscriptions()
    assert row["owner"] == alice

    # Somebody else's inscription cannot even be asked for. (Alice's coins
    # went into inscribing; the hand-over needs one small confirmed output.)
    node.rpc.call("sendtoaddress", bob, 5)
    node.rpc.call("sendtoaddress", alice, 5)
    mine_and_index(node, state)
    refused_ = app.post("/r/send", json={"kind": "inscription", "to": alice,
                                         "inscription": row["number"]})
    assert refused_.status_code == 400 and "already holds" in refused_.json()["error"]

    filed = app.post("/r/send", json={"kind": "inscription", "to": bob,
                                      "inscription": row["number"],
                                      "label": "Askers", "note": "you won it"})
    assert filed.status_code == 202, filed.text
    rid = filed.json()["id"]
    assert filed.json()["number"] == row["number"] and filed.json()["from"] == alice

    view = app.get(f"/inscriptions/{row['txid']}/view").text
    assert "1 send waiting for your approval" in view
    page = app.get(f"/approvals/{rid}").text
    assert f"inscription #{row['number']}" in page and "you won it" in page
    assert "the recipient" in page, page[page.find("<main>"):page.find("</main>")]
    txid = re.search(r'name="confirmed" value="([0-9a-f]{64})"', page).group(1)
    app.post(f"/approvals/{rid}", data={"csrf_token": state.csrf_token, "confirmed": txid})
    assert app.get(f"/r/send/{rid}").json()["status"] == "sent"
    mine_and_index(node, state)
    assert index.inscription(row["txid"])["owner"] == bob
    assert app.get(f"/r/inscription/{row['number']}").json()["owner"] == bob
