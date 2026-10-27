"""The LLM service (arcade/llm.py, mesh/llmshare.py, web/llmweb.py; 2026-10-23): an operator sells their own
model per token in any coin or token, buyers prepay credit straight to them, and a buyer's own programs reach it through
an OpenAI-compatible /v1 with an API key."""

import asyncio
import json
import pathlib
import sys
import time

import pytest

from arcade import llm
from arcade.script import b58check_encode
from arcade.service import _G, _compressed, _mul, sign_message

sys.path.insert(0, str(pathlib.Path(__file__).parent))

SECRET = 0x1D2C3B4A59687766554433221100FFEEDDCCBBAA99887766554433221100AA55
PUB = _compressed(_mul(SECRET, _G))
ADDR = b58check_encode(0x37, llm.hash160(PUB))          # any version: the key hash is what is checked


# ------------------------------------------------------------------------------------------------ signatures

def test_a_signature_is_checked_from_the_bytes_against_the_address_key():
    words = llm.grant_words("seller", ADDR, 1_900_000_000)
    sig = sign_message(SECRET, words, llm.MAGIC)
    assert llm.recover_hash160(sig, words) == llm.hash160(PUB)
    assert llm.signed_by(sig, words, ADDR)
    assert not llm.signed_by(sig, llm.grant_words("other-seller", ADDR, 1_900_000_000), ADDR), "bound to one seller"
    other = b58check_encode(0x37, b"\x01" * 20)
    assert not llm.signed_by(sig, words, other)
    assert llm.recover_hash160("bm90IGEgc2ln", words) is None
    # The same key on another chain (another address version): the same hash, so it checks there too.
    assert llm.signed_by(sig, words, b58check_encode(0x1E, llm.hash160(PUB)))


def test_a_grant_is_refused_expired_too_long_or_for_another_arcade():
    from arcade.mesh.llmshare import grant_ok
    now = time.time()
    exp = int(now) + 86400
    good = {"address": ADDR, "expires": exp, "sig": sign_message(SECRET, llm.grant_words("me", ADDR, exp), llm.MAGIC)}
    assert grant_ok(good, "me") == ADDR
    with pytest.raises(llm.LlmError):
        grant_ok(good, "someone-else")
    old = int(now) - 10
    with pytest.raises(llm.LlmError, match="run out"):
        grant_ok({**good, "expires": old, "sig": sign_message(SECRET, llm.grant_words("me", ADDR, old), llm.MAGIC)}, "me")
    far = int(now) + 4000 * 86400                              # ten years at most since 2026-10-27
    with pytest.raises(llm.LlmError, match="10 years"):
        grant_ok({**good, "expires": far, "sig": sign_message(SECRET, llm.grant_words("me", ADDR, far), llm.MAGIC)}, "me")


# ------------------------------------------------------------------------------------------------ prices and books

def test_prices_cost_and_what_a_balance_still_pays_for():
    p = {"in": "0.001", "out": "0.01"}
    assert llm.cost_units(p, 1000, 100, llm.COIN) == 2 * llm.COIN, "1 + 1 coin"
    assert llm.cost_units({"in": "0.00000000001", "out": "0"}, 1, 0, llm.COIN) == 1, "a tiny cost rounds up to a unit"
    assert llm.cost_units({"in": 0, "out": 0}, 10**6, 10**6, llm.COIN) == 0
    assert llm.affordable_out(p, 2 * llm.COIN, 1000, llm.COIN) == 100
    assert llm.affordable_out(p, llm.COIN // 2, 1000, llm.COIN) == 0, "the prompt alone is more than the credit"
    assert llm.affordable_out({"in": "0.001", "out": 0}, llm.COIN, 10, llm.COIN) == -1, "free output: no cap"
    with pytest.raises(llm.LlmError):
        llm.price("-1")
    with pytest.raises(llm.LlmError):
        llm.price("lots")
    assert llm.fmt(150_000_000, llm.COIN) == "1.5" and llm.fmt(1234, 1) == "1,234"


def test_the_admin_form_is_checked_and_keeps_the_key():
    kept = {**llm.DEFAULTS, "key": "secret"}
    got = llm.clean({"url": "http://127.0.0.1:8000/v1/", "models": ["m", "m", ""], "at_once": 99, "max_out": 1,
                     "prices": [{"leg": "main-coin", "label": "PEP", "in": "0.1", "out": "1.50"}]}, kept)
    assert got["url"] == "http://127.0.0.1:8000" and got["models"] == ["m"] and got["key"] == "secret"
    assert got["at_once"] == llm.AT_ONCE_MOST and got["max_out"] == 16
    assert got["prices"] == [{"leg": "main-coin", "label": "PEP", "in": "0.1", "out": "1.5"}]
    with pytest.raises(llm.LlmError):
        llm.clean({"url": "ftp://x"}, kept)
    with pytest.raises(llm.LlmError):
        llm.clean({"prices": [{"leg": "not a leg"}]}, kept)
    with pytest.raises(llm.LlmError, match="each coin"):
        llm.clean({"prices": [{"leg": "main-coin"}, {"leg": "main-coin"}]}, kept)
    with pytest.raises(llm.LlmError, match="to offer"):
        llm.clean({"on": True}, {**llm.DEFAULTS})


def test_a_top_up_counts_once_and_charges_never_go_below_zero(tmp_path):
    books = llm.Books(tmp_path)
    assert books.credit("aa" * 32, "main-coin", "buyer", 500)
    assert not books.credit("aa" * 32, "main-coin", "someone", 500), "one payment, counted once, for one buyer"
    assert books.balance("buyer", "main-coin") == 500
    assert books.charge("buyer", "main-coin", "m", 10, 10, 800) == 0
    assert books.earned()[0]["units"] == 800


def test_a_top_up_is_proved_by_the_key_that_paid(tmp_path):
    books = llm.Books(tmp_path)
    seen = []

    def verify(leg, txid, to, signer):
        seen.append((leg, txid, to, signer))
        if signer != llm.hash160(PUB):
            raise llm.LlmError("that payment was not sent from the key that signed for it")
        return 25_000_000                                   # 0.25 PEP
    cfg = {**llm.DEFAULTS, "on": True, "url": "http://x", "models": ["m"],
           "prices": [{"leg": "main-coin", "label": "PEP", "in": "0", "out": "0.0001"}]}
    seller = llm.Seller(lambda: cfg, books, lambda leg: "PAYTO", lambda leg: llm.COIN, verify, lambda b: False)
    seller.node_id = lambda: "me"
    txid = "ab" * 32
    sig = sign_message(SECRET, llm.topup_words("main", txid, "me", "buyer-address"), llm.MAGIC)
    got = seller.topup("buyer-address", "main-coin", txid, sig)
    # Credits per model (2026-10-27): the per-token price before credits reads as 0.1 PEP a credit (1,000 written
    # tokens), so 0.25 PEP buys 2.5 credits, for the seller's first model when the buyer names none.
    assert got["units"] == 25_000_000 and got["model"] == "m" and got["models"]["m"]["mc"] == 2500
    assert got["credits"] == "2.5"
    assert seen == [("main-coin", txid, "PAYTO", llm.hash160(PUB))]
    assert seller.topup("buyer-address", "main-coin", txid, sig)["again"] is True
    with pytest.raises(llm.LlmError, match="somebody else"):
        sig2 = sign_message(SECRET, llm.topup_words("main", txid, "me", "thief"), llm.MAGIC)
        seller.topup("thief", "main-coin", txid, sig2)
    with pytest.raises(llm.LlmError, match="does not take"):
        seller.topup("buyer-address", "doge-coin", txid, sig)


def test_a_coin_payment_must_come_from_the_signer_and_reach_the_seller():
    pay_to = "PAYTO"
    tx = {"confirmations": 0,
          "vin": [{"scriptSig": {"hex": "47" + "30" * 71 + "21" + PUB.hex()}}],
          "vout": [{"value": 1.5, "scriptPubKey": {"address": pay_to}},
                   {"value": 9, "scriptPubKey": {"address": "change"}}]}

    class Rpc:
        def call(self, method, *args):
            assert method == "getrawtransaction"
            return tx
    txid = "cd" * 32
    assert llm.check_coin_payment(Rpc(), txid, pay_to, llm.hash160(PUB), real_coins=False) == 150_000_000
    assert llm.check_coin_payment(Rpc(), txid, pay_to, llm.hash160(PUB), real_coins=True) == 150_000_000, \
        "the operator: real coins count as soon as the payment is broadcast (in the seller's mempool), no block"
    with pytest.raises(llm.LlmError, match="not sent from"):
        llm.check_coin_payment(Rpc(), txid, pay_to, b"\x02" * 20, real_coins=False)
    with pytest.raises(llm.LlmError, match="sends nothing"):
        llm.check_coin_payment(Rpc(), txid, "elsewhere", llm.hash160(PUB), real_coins=False)


# ------------------------------------------------------------------------------------------------ the web side

def fake_model_app(seen: list):
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    app = FastAPI()

    @app.get("/v1/models")
    def models():
        return {"object": "list", "data": [{"id": "tiny", "max_model_len": 4096}]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        seen.append(body)

        def events():
            for word in ("Hello", " there"):
                yield "data: " + json.dumps({"id": "c1", "model": "tiny", "choices": [
                    {"index": 0, "delta": {"content": word}, "finish_reason": None}]}) + "\n\n"
            yield "data: " + json.dumps({"id": "c1", "model": "tiny", "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"}]}) + "\n\n"
            yield "data: " + json.dumps({"id": "c1", "choices": [], "usage": {
                "prompt_tokens": 100, "completion_tokens": 20}}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(events(), media_type="text/event-stream")
    return app


@pytest.fixture
def arcade(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from arcade.web.app import create_app
    from test_account_feed import _sign_in
    from test_realtime_web import Served, make_state

    seen: list = []
    model = Served(fake_model_app(seen))
    state = make_state(tmp_path, "node")
    monkeypatch.setattr(type(state), "payout_address", lambda self, chain: "PAYTO-" + chain.network)
    state.set_setting("llm", {**llm.DEFAULTS, "on": True, "url": str(model.client.base_url).rstrip("/"),
                              "models": ["tiny"], "max_out": 1000,
                              "prices": [{"leg": "main-coin", "label": "PEP · Pepecoin", "in": "0.001",
                                          "out": "0.01"}]})
    client = TestClient(create_app(state))
    pubkey = _sign_in(client)
    state.set_setting(f"address:{pubkey}", ADDR)
    try:
        yield client, state, pubkey, seen
    finally:
        model.close()


def _events(text: str) -> list:
    out = []
    for line in text.split("\n\n"):
        line = line.strip()
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            out.append(json.loads(line[5:]))
    return out


def test_the_tab_lists_this_arcades_offer_and_a_chat_is_metered(arcade):
    client, state, pubkey, seen = arcade
    offers = client.get("/account/llm/offers").json()
    assert offers["signed_in"] and offers["me"] == ADDR
    (mine,) = offers["offers"]
    # Credits per model (2026-10-27): 0.001 / 0.01 PEP a token read as 10 PEP a credit, "tiny" 0.1 / 1 credit per
    # 1,000 tokens read / written; a typical reply (300 in, 400 out) about 0.43 credits.
    assert mine["here"] and mine["models"] == [{"id": "tiny", "rate": {"in": "0.1", "out": "1"}, "typical": "0.43"}]
    assert mine["credit_prices"] == [{"leg": "main-coin", "label": mine["credit_prices"][0]["label"], "per": "10",
                                      "to": "PAYTO-main"}], "what a credit costs, and where to pay"

    said = client.post("/account/llm/chat", json={"seller": "here", "leg": "main-coin",
                                                  "messages": [{"role": "user", "content": "hi"}]})
    assert said.status_code == 400 and "credit" in said.json()["detail"], "no credit, no reply"

    state.llm_seller.credits.give(ADDR, "tiny", 200)                   # 0.2 credits for "tiny"
    said = client.post("/account/llm/chat", json={"seller": "here", "leg": "main-coin", "model": "tiny",
                                                  "messages": [{"role": "user", "content": "hi"}], "max_tokens": 99999})
    assert said.status_code == 200, said.text
    evs = _events(said.text)
    assert "".join(c["delta"].get("content", "") for e in evs for c in e.get("choices", [])) == "Hello there"
    cost = evs[-1]["arcade"]
    assert cost["tokens_in"] == 100 and cost["tokens_out"] == 20
    # the reply cost 0.1 * 100 / 1000 + 1 * 20 / 1000 = 0.03 credits (30 thousandths) of "tiny"'s own
    assert cost["cost"] == 30 and cost["model"] == "tiny" and cost["cost_text"] == "0.03"
    assert client.get("/account/llm/balance?seller=here").json()["models"]["tiny"]["mc"] == 200 - 30
    assert seen[-1]["max_tokens"] <= 1000, "never past the operator's longest reply"
    assert seen[-1]["max_tokens"] < 200, "and never past what the credit pays for"


def test_an_api_key_reaches_the_model_like_openai(arcade):
    client, state, pubkey, seen = arcade
    state.llm_seller.books.credit("12" * 32, "main-coin", ADDR, llm.COIN)
    key = client.post("/account/llm/keys/new", json={"seller": "here", "leg": "main-coin", "name": "laptop"}).json()["key"]
    assert key.startswith("sk-arcade-")
    listed = client.get("/account/llm/keys").json()["keys"]
    assert listed[0]["tail"] == key[-4:] and "key" not in listed[0], "only a hash and the last four are kept"

    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"][0]["id"] == "tiny"
    got = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                      json={"model": "tiny", "messages": [{"role": "user", "content": "hi"}]})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["object"] == "chat.completion" and body["choices"][0]["message"]["content"] == "Hello there"
    assert body["usage"]["completion_tokens"] == 20

    streamed = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                           json={"model": "tiny", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    evs = _events(streamed.text)
    assert evs and not any("arcade" in e for e in evs), "OpenAI's own stream, nothing of ours in it"

    h = listed[0]["hash"]
    assert client.post("/account/llm/keys/drop", json={"hash": h}).json()["ok"]
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).status_code == 401, "revoked at once"


def test_the_operator_uses_their_own_model_free(arcade, monkeypatch):
    client, state, pubkey, seen = arcade
    monkeypatch.setattr(type(state), "operator", pubkey, raising=False)
    said = client.post("/account/llm/chat", json={"seller": "here", "leg": "main-coin",
                                                  "messages": [{"role": "user", "content": "hi"}]})
    assert said.status_code == 200, said.text
    assert _events(said.text)[-1]["arcade"]["free"] is True


def test_admin_saves_and_tests_the_server(arcade):
    client, state, pubkey, seen = arcade
    url = llm.config(state)["url"]
    h = {"X-Arcade-Admin": "1"}
    got = client.post("/admin/api/llm/test", json={"url": url}, headers=h).json()
    assert got["models"] == [{"id": "tiny", "ctx": 4096}]
    saved = client.post("/admin/api/llm", headers=h, json={"on": False, "url": url, "models": ["tiny"],
                                                           "prices": [{"leg": "main-coin", "in": "1", "out": "2"}]})
    assert saved.status_code == 200 and saved.json()["selling"] is False
    st = client.get("/admin/api/llm").json()
    assert st["config"]["prices"][0]["out"] == "2" and "key" not in st["config"]
    assert client.get("/account/llm/offers").json()["offers"] == [], "not selling: not on the book"


def test_the_door_lets_the_llm_routes_through():
    from arcade.web import door
    for path in ("/account/llm/chat", "/account/llm/topup", "/account/llm/grant", "/account/llm/keys/new",
                 "/account/llm/keys/drop", "/v1/chat/completions"):
        assert door.public_path(path, "POST"), path
    for path in ("/account/llm/offers", "/account/llm/balance", "/account/llm/keys", "/v1/models"):
        assert door.public_path(path, "GET"), path


# ------------------------------------------------------------------------------------------------ between arcades

def test_a_buyer_arcade_asks_a_seller_arcade_over_a_link(tmp_path):
    """Two LlmShares joined by in-memory links: the offer is heard, a chat with a grant is served and charged on the
    seller's books, and one without the right grant is refused."""
    from arcade.mesh.llmshare import LlmShare

    books = llm.Books(tmp_path)
    cfg = {**llm.DEFAULTS, "on": True, "url": "http://x", "models": ["tiny"],
           "prices": [{"leg": "main-coin", "label": "PEP", "in": "0", "out": "0.01"}]}

    class FakeSeller(llm.Seller):
        def chat(self, buyer, leg, body, via=""):
            yield {"choices": [{"delta": {"content": "ok " + buyer[:4]}}]}
            yield {"arcade": {"left": self.books.charge(buyer, leg, "tiny", 1, 1, 5, via)}}
    seller = FakeSeller(lambda: cfg, books, lambda leg: "PAYTO", lambda leg: llm.COIN, None, lambda b: False)
    books.credit("99" * 32, "main-coin", ADDR, 100)

    async def run():
        loop = asyncio.get_running_loop()
        a, b = LlmShare(lambda: None), LlmShare(lambda: seller)          # a buys, b sells

        class Node:
            def __init__(self, node_id):
                self.node_id, self.links = node_id, {}

            def _spawn(self, coro):
                return asyncio.ensure_future(coro)
        a.node, b.node = Node("A"), Node("B")

        class Link:
            def __init__(self, me, to, peer_id):
                self.me, self.to, self.peer_id = me, to, peer_id

            async def send(self, header, blob=b""):
                await self.to.on_frame(self.to.node.links[self.peer_back], header, blob)
        ab, ba = Link(a, b, "B"), Link(b, a, "A")
        ab.peer_back, ba.peer_back = "A", "B"
        a.node.links, b.node.links = {"B": ab}, {"A": ba}
        await b.link_up(ba)
        assert a.offers["B"]["models"] == [{"id": "tiny"}]
        exp = int(time.time()) + 86400
        grant = {"address": ADDR, "expires": exp, "sig": sign_message(SECRET, llm.grant_words("B", ADDR, exp), llm.MAGIC)}
        evs = await loop.run_in_executor(None, lambda: list(a.ask("B", "chat", {"grant": grant, "leg": "main-coin",
                                                                                "body": {"messages": []}})))
        assert evs[0]["choices"][0]["delta"]["content"] == "ok " + ADDR[:4] and evs[-1]["arcade"]["left"] == 95
        wrong = {**grant, "sig": sign_message(SECRET, llm.grant_words("C", ADDR, exp), llm.MAGIC)}
        with pytest.raises(llm.LlmError, match="signature"):
            await loop.run_in_executor(None, lambda: list(a.ask("B", "chat", {"grant": wrong, "leg": "main-coin",
                                                                               "body": {}})))
        a.loop = b.loop = loop
    asyncio.run(_with_loop(run))


async def _with_loop(fn):
    from arcade.mesh import llmshare
    loop = asyncio.get_running_loop()
    orig = llmshare.LlmShare.ask

    def ask(self, *a, **k):
        self.loop = loop
        return orig(self, *a, **k)
    llmshare.LlmShare.ask = ask
    try:
        await fn()
    finally:
        llmshare.LlmShare.ask = orig


def test_a_model_can_be_offered_free_for_everyone(arcade):
    """2026-10-23: no asks = free for everyone, with everything a paid model has; adding an ask makes it paid."""
    client, state, pubkey, seen = arcade
    url = llm.config(state)["url"]
    h = {"X-Arcade-Admin": "1"}
    saved = client.post("/admin/api/llm", headers=h, json={"on": True, "url": url, "models": ["tiny"], "prices": []})
    assert saved.status_code == 200 and saved.json()["selling"] is True, saved.text
    (o,) = client.get("/account/llm/offers").json()["offers"]
    assert o["free"] is True and o["prices"] == []
    said = client.post("/account/llm/chat", json={"seller": "here", "leg": "free",
                                                  "messages": [{"role": "user", "content": "hi"}]})
    assert said.status_code == 200, said.text
    end = _events(said.text)[-1]["arcade"]
    assert end["free"] is True and end["cost"] == 0
    assert client.get("/account/llm/balance?seller=here").json()["free"] is True
    key = client.post("/account/llm/keys/new", json={"seller": "here", "leg": "free"}).json()["key"]
    got = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                      json={"messages": [{"role": "user", "content": "hi"}]})
    assert got.status_code == 200 and got.json()["choices"][0]["message"]["content"] == "Hello there"
    with pytest.raises(llm.LlmError, match="free"):
        state.llm_seller.topup(ADDR, "main-coin", "ab" * 32, "x")
    # An ask added: paid from then on, the same key now needs a coin's credit.
    client.post("/admin/api/llm", headers=h, json={"prices": [{"leg": "main-coin", "in": "0.001", "out": "0.01"}]})
    assert client.get("/account/llm/offers").json()["offers"][0]["free"] is False
    late = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                       json={"messages": [{"role": "user", "content": "hi"}]})
    assert late.status_code == 402 and "credit" in late.json()["error"]["message"], \
        "a key made while it was free now spends credit, of any coin; with none it says so"
    client.post("/admin/api/llm", headers=h, json={"prices": [{"leg": "main-coin", "in": "0", "out": "0"}]})
    assert client.get("/account/llm/offers").json()["offers"][0]["free"] is True, "an ask of 0 and 0 is free too"


def test_the_book_and_the_admin_see_how_busy_a_model_is_and_its_analytics(arcade):
    """2026-10-23: "Users also need a way to know if one of the models is congested or being frequently used" --
    "So should the admin" -- and "a link on their model ... and get all of the usage analytics"."""
    client, state, pubkey, seen = arcade
    state.llm_seller.books.credit("34" * 32, "main-coin", ADDR, llm.COIN)
    (o,) = client.get("/account/llm/offers").json()["offers"]
    assert o["load"]["word"] == "idle" and o["load"]["at_once"] == 2
    for _ in range(3):
        assert client.post("/account/llm/chat", json={"seller": "here", "leg": "main-coin",
                                                      "messages": [{"role": "user", "content": "hi"}]}).status_code == 200
    load = client.get("/admin/api/llm/load").json()
    assert load["last_15m"] == 3 and load["word"] == "steady" and load["tok_s"] > 0
    assert llm.Seller.word({"active": 2, "at_once": 2}) == "busy" and llm.Seller.word({"waiting": 1}) == "full"
    got = client.get("/admin/api/llm/usage?days=1").json()
    assert got["total"]["replies"] == 3 and got["total"]["tokens_out"] == 60 and got["bucket"] == 300
    assert got["by"]["model"][0]["key"] == "tiny" and got["by"]["via"][0]["label"].startswith("This arcade")
    assert got["by"]["leg"][0]["text"] and got["last"][0]["ms"] >= 0
    assert client.get("/admin/llm/usage").status_code == 200


def test_an_old_usage_table_gains_the_new_columns(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "llm.sqlite")
    db.execute("CREATE TABLE usage (at REAL NOT NULL, buyer TEXT NOT NULL, model TEXT NOT NULL, leg TEXT NOT NULL, "
               "tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL, units INTEGER NOT NULL, via TEXT NOT NULL DEFAULT '')")
    db.execute("INSERT INTO usage VALUES (1, 'b', 'm', 'main-coin', 1, 2, 3, 'web')")
    db.commit()
    db.close()
    books = llm.Books(tmp_path)
    books.charge("b", "main-coin", "m", 1, 1, 0, "web", ms=500, first_ms=100)
    assert books.analytics(0)["total"]["replies"] == 2


# ------------------------------------------------------------------------------------------------ pictures

def _png() -> bytes:
    import struct
    import zlib as _z

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", _z.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", _z.compress(b"\x00" + b"\xff\x00\x00" * 2 + b"\x00" + b"\x00\xff\x00" * 2))
            + chunk(b"IEND", b""))


PNG = _png()


def fake_comfy_app(jobs: list):
    from fastapi import FastAPI, Request, UploadFile
    from fastapi.responses import Response
    app = FastAPI()
    done: dict = {}

    @app.get("/system_stats")
    def stats():
        return {"system": {}}

    @app.get("/object_info/{node}")
    def info(node: str):
        names = {"UNETLoader": ("unet_name", ["qwen_image_2.1_int8_convrot.safetensors", "other.safetensors"]),
                 "CLIPLoader": ("clip_name", ["qwen3vl_8b_w4a8.safetensors"]),
                 "VAELoader": ("vae_name", ["qwen_image_2.1_vae_bf16.safetensors"])}[node]
        return {node: {"input": {"required": {names[0]: [names[1]]}}}}

    @app.post("/upload/image")
    async def upload(image: UploadFile):
        await image.read()
        await image.close()
        return {"name": image.filename}

    @app.post("/prompt")
    async def prompt(request: Request):
        body = await request.json()
        jobs.append(body["prompt"])
        pid = f"p{len(jobs)}"
        done[pid] = {"status": {"status_str": "success"},
                     "outputs": {"8": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]}}}
        return {"prompt_id": pid}

    @app.get("/history/{pid}")
    def history(pid: str):
        return {pid: done[pid]}

    @app.get("/view")
    def view():
        return Response(PNG, media_type="image/png")
    return app


@pytest.fixture
def pictures(arcade):
    from test_realtime_web import Served
    client, state, pubkey, seen = arcade
    jobs: list = []
    comfy = Served(fake_comfy_app(jobs))
    try:
        yield client, state, pubkey, jobs, str(comfy.client.base_url).rstrip("/")
    finally:
        comfy.close()


def _png_of(w: int, h: int) -> bytes:
    import struct
    import zlib as _z

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", _z.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + b"\x10\x20\x30" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", _z.compress(rows)) + chunk(b"IEND", b""))


def test_an_image_model_is_found_shared_and_sold_in_credits_by_size(pictures):
    """2026-10-23: share the image model; credits by size -- "250 pixels by 250 ... 251 ... two credits ... 500
    ... two credits ... 501 ... three credits", the longest side; an edit "per upload and download"; pictures kept 24 h."""
    import base64
    from arcade import imagegen
    client, state, pubkey, jobs, url = pictures
    assert [imagegen.credits_for(*s) for s in ((250, 250), (251, 251), (500, 500), (501, 501), (500, 375), (250, 188))] \
        == [1, 2, 2, 3, 2, 1]
    h = {"X-Arcade-Admin": "1"}
    found = client.post("/admin/api/llm/images/scan", headers=h, json={"url": url}).json()
    assert [m["id"] for m in found["models"]] == ["qwen_image_2.1_int8_convrot.safetensors"], "only a family it can draw"
    saved = client.post("/admin/api/llm/images", headers=h, json={
        "on": True, "url": url, "models": ["qwen_image_2.1_int8_convrot.safetensors"], "max_side": 1000,
        "prices": [{"leg": "main-coin", "label": "PEP", "per": "0.5"}]})
    assert saved.status_code == 200 and saved.json()["sharing"], saved.text
    (o,) = client.get("/account/llm/offers").json()["offers"]
    assert o["images"]["prices"][0]["per"] == "0.5" and o["images"]["prices"][0]["to"] == "PAYTO-main"

    said = client.post("/account/llm/image", json={"seller": "here", "leg": "main-coin", "prompt": "a dragon"})
    assert said.status_code == 400 and "credit" in said.json()["detail"]
    # Credits per model (2026-10-27): the picture model's own credits, 1 a 250 px of the longest side by default.
    MODEL = "qwen_image_2.1_int8_convrot.safetensors"
    assert o["images"]["credit_prices"][0]["per"] == "0.5" and o["images"]["models"][0]["per_250px"] == "1"
    state.llm_seller.credits.give(ADDR, MODEL, 2000)                          # 2 credits
    got = client.post("/account/llm/image", json={"seller": "here", "leg": "main-coin", "prompt": "a dragon",
                                                  "size": "500x375"}).json()
    assert base64.b64decode(got["png"]) == PNG and got["credits"] == 2 and got["arcade"]["cost"] == 2000
    assert jobs[-1]["5"]["inputs"]["width"] == 496 // 2 and jobs[-1]["5"]["inputs"]["height"] == 368 // 2, \
        "drawn at a multiple of 16 no bigger than what was paid for"
    big = client.post("/account/llm/image", json={"seller": "here", "leg": "main-coin", "prompt": "x", "size": "4000x100"})
    assert big.status_code == 400 and "4 credits" in big.json()["detail"], "cut to the 1000 cap: 4 credits"

    up = _png_of(400, 300)                                      # 2 credits up; comes back at most 400: 2 more down
    state.llm_seller.credits.give(ADDR, MODEL, 4000)
    edit = client.post("/account/llm/image", json={"seller": "here", "leg": "main-coin", "prompt": "add a hat",
                                                   "image": "data:image/png;base64," + base64.b64encode(up).decode()})
    assert edit.status_code == 200, edit.text
    assert "images.image_1" in jobs[-1]["4"]["inputs"] and jobs[-1]["4"]["inputs"]["resolution"] == 400
    assert edit.json()["credits"] <= 4, "upload credits plus what came back"

    pics = client.get("/account/llm/pictures").json()["pictures"]
    assert len(pics) == 2 and pics[0]["prompt"] == "add a hat" and pics[1]["credits"] == 2, "kept 24 hours"
    assert client.get(pics[1]["url"]).content == PNG

    key = client.post("/account/llm/keys/new", json={"seller": "here", "leg": "main-coin"}).json()["key"]
    state.llm_seller.credits.give(ADDR, MODEL, 1000)
    v1 = client.post("/v1/images/generations", headers={"Authorization": f"Bearer {key}"},
                     json={"prompt": "a fox", "size": "250x250"})
    assert v1.status_code == 200 and base64.b64decode(v1.json()["data"][0]["b64_json"]) == PNG
    state.llm_seller.credits.give(ADDR, MODEL, 5000)
    ed = client.post("/v1/images/edits", headers={"Authorization": f"Bearer {key}"},
                     files={"image": ("x.png", up, "image/png")}, data={"prompt": "add a hat"})
    assert ed.status_code == 200, ed.text
    usage = client.get("/admin/api/llm/usage?days=1").json()
    assert usage["total"]["images"] == 4


def test_the_wake_command_is_set_at_the_node_machine_only():
    from arcade import imagegen
    kept = dict(imagegen.DEFAULTS)
    assert imagegen.clean({"wake": "systemctl --user start comfyui"}, kept, local=True)["wake"].startswith("systemctl")
    with pytest.raises(llm.LlmError, match="node machine"):
        imagegen.clean({"wake": "rm -rf /"}, kept, local=False)
    assert imagegen.clean({"wake": "", "steps": 30}, kept, local=False)["steps"] == 30, "left as it was: no refusal"
    assert imagegen.size_of("4000x1000", 1000) == (1000, 250)
    assert imagegen.is_free({"prices": [{"leg": "main-coin", "per": "0"}]})


def test_a_big_answer_crosses_the_mesh_in_pieces():
    """A picture is more than a frame holds: llmshare sends it in `more` pieces and the buyer puts it back."""
    from arcade.mesh.llmshare import CHUNK, LlmShare
    import queue
    sent = []

    class Link:
        peer_id = "B"

        async def send(self, header, blob=b""):
            sent.append((header, blob))
    a, b = LlmShare(lambda: None), LlmShare(lambda: None)
    big = {"png": "x" * (CHUNK * 3 + 10)}
    asyncio.run(b._answer(Link(), 7, big, end=False))
    assert len(sent) == 4 and all(h.get("more") for h, _ in sent[:3])
    q = queue.Queue()
    a._waiting[7] = q
    for h, blob in sent:
        a._on_answer(h, blob)
    kind, ev = q.get_nowait()
    assert kind == "ev" and ev == big


def test_a_scan_wakes_comfyui_with_the_wake_typed_at_the_node_machine(arcade, monkeypatch):
    """2026-10-23: "when i scan it says comfy is not running" -- the wake in the box, not yet saved, is used."""
    from arcade import imagegen
    client, state, pubkey, seen = arcade
    woke = []
    monkeypatch.setattr(imagegen.ComfyServer, "up", lambda self: bool(woke))
    monkeypatch.setattr(imagegen.ComfyServer, "ensure_up", lambda self: woke.append(self.wake))
    monkeypatch.setattr(imagegen.ComfyServer, "models", lambda self: [{"id": "qwen_image_x", "label": "Qwen-Image"}])
    h = {"X-Arcade-Admin": "1"}
    asleep = client.post("/admin/api/llm/images/scan", headers=h, json={"url": "http://127.0.0.1:9"}).json()
    assert asleep["asleep"] and not woke
    got = client.post("/admin/api/llm/images/scan", headers=h,
                      json={"url": "http://127.0.0.1:9", "wake_now": True, "wake": "true"}).json()
    assert woke == ["true"] and got["models"][0]["id"] == "qwen_image_x"


def test_find_models_lists_every_model_server_on_this_computer(pictures, monkeypatch):
    """2026-10-23: "a button that they can click to scan for all models and then it will just give them a list
    of models that is on their computer" -- each local port asked whether it is an OpenAI server or ComfyUI."""
    from arcade.web import llmweb
    client, state, pubkey, jobs, comfy_url = pictures
    text_url = llm.config(state)["url"]
    ports = {int(text_url.rsplit(":", 1)[1]), int(comfy_url.rsplit(":", 1)[1]), 9}
    monkeypatch.setattr(llmweb, "listening_ports", lambda: ports)
    got = client.post("/admin/api/llm/discover", headers={"X-Arcade-Admin": "1"}, json={}).json()
    assert [(g["url"], [m["id"] for m in g["models"]]) for g in got["text"]] == [(text_url, ["tiny"])]
    assert got["images"][0]["url"] == comfy_url and got["images"][0]["models"][0]["label"] == "Qwen-Image 2.1"
    assert got["asleep"] == [], "a ComfyUI that answers is not offered a wake"


def test_the_chat_knows_the_docs_and_does_not_charge_for_them(arcade):
    """2026-10-23: the browser chat is "an expert on Dogecoin arcade" with its documentation -- the parts that
    bear on the question, not charged to the buyer."""
    from arcade import llmdocs
    client, state, pubkey, seen = arcade
    parts = llmdocs.pick("how do I make a prize pool for my game")
    assert parts and parts[0]["doc"] in ("prize-pools.md", "referee.md")
    assert not any(p["doc"] in ("DECISIONS.md", "hosting-plan.md", "voice-plan.md") for s in [llmdocs.sections()[0]]
                   for p in s), "public docs only"
    state.llm_seller.books.credit("61" * 32, "main-coin", ADDR, llm.COIN)
    said = client.post("/account/llm/chat", json={"seller": "here", "leg": "main-coin", "docs": True,
                                                  "messages": [{"role": "user", "content": "how do prize pools work?"}]})
    assert said.status_code == 200, said.text
    sent = seen[-1]["messages"]
    assert sent[0]["role"] == "system" and "DogecoinArcade" in sent[0]["content"] and "prize" in sent[0]["content"].lower()
    end = _events(said.text)[-1]["arcade"]
    assert end["docs_tokens"] > 0
    # the fake server says 100 prompt tokens; the docs' share of them is not charged
    from arcade import llm_credits
    assert end["cost"] == llm_credits.text_cost_mc({"in": "0.1", "out": "1"}, max(0, 100 - end["docs_tokens"]), 20)


def test_the_helper_can_reach_every_public_doc():
    """2026-10-25: "Make sure that the helper bot has access to all of these docs." Every public page is cut into
    sections the helper can pick, its prompt lists every page, the new guides answer their questions, and the plans
    stay out."""
    from arcade import llmdocs
    from arcade.web import guide
    public = [p["name"] for p in guide.pages(private=False) if p["name"] not in llmdocs.LEAVE_OUT]
    have = {s["doc"] for s in llmdocs.sections()[0]}
    assert set(public) <= have, sorted(set(public) - have)
    assert not have & set(guide.PRIVATE), "the plans are not the helper's"
    listed = llmdocs.contents()
    assert all(f"(/docs/{name})" in listed for name in public)
    assert not any(f"(/docs/{name})" in listed for name in guide.PRIVATE)
    assert llmdocs.pick("how do I make an app and put it on the Apps tab")[0]["doc"] == "games.md"
    assert llmdocs.pick("how do I inscribe a HashLips collection, step by step")[0]["doc"] == "collections.md"
    said = llmdocs.system_message([{"role": "user", "content": "zzzz qqqq"}])
    assert "/docs/collections.md" in said and "/docs/games.md" in said, "even with no match, the pages are named"


def test_an_operator_can_be_given_the_models_free(arcade, monkeypatch):
    """2026-10-23: "share my model with Bigchiefenergy if I choose for free while simultaneously having it
    listed" -- that operator's account only; names resolved on the chain, never taken as addresses from the form."""
    from arcade.web import llmweb
    client, state, pubkey, seen = arcade
    h = {"X-Arcade-Admin": "1"}
    monkeypatch.setattr(type(state), "token_index", lambda self, chain: type("I", (), {
        "address_of": lambda _s, tag: ADDR if tag == "bigchiefenergy" else None})())
    assert client.post("/admin/api/llm", headers=h, json={"free_for": ["@nobodyhere"]}).status_code == 400
    assert client.post("/admin/api/llm", headers=h, json={"free_for": ["@BigChiefEnergy"]}).status_code == 200
    assert llm.config(state)["free_for"] == [{"tag": "bigchiefenergy", "address": ADDR}]
    said = client.post("/account/llm/chat", json={"seller": "here", "leg": "main-coin",
                                                  "messages": [{"role": "user", "content": "hi"}]})
    assert said.status_code == 200 and _events(said.text)[-1]["arcade"]["free"] is True, "no credit, still served"
    assert client.get("/account/llm/offers").json()["offers"][0]["prices"], "still listed for everybody else"


def test_the_arcade_helper_answers_with_the_page_it_was_asked_on(arcade, monkeypatch):
    """2026-10-23: the robot in the corner of every page, "that same Dogecoin arcade chat harness ... but using
    any LLM that is shared with the operator", with "context on what tab the user is on"; never a paid source."""
    client, state, pubkey, seen = arcade
    node = b58check_encode(0x37, b"\x07" * 20)
    monkeypatch.setattr(type(state), "derived_address", node, raising=False)
    got = client.get("/account/llm/helper").json()
    assert got["available"] and got["model"] == "tiny", "this arcade's own model, which its operator uses free"
    said = client.post("/account/llm/helper", json={"messages": [{"role": "user", "content": "what is this tab?"}],
                                                    "page": "Page /exchange?tab=llm; open tab(s): LLMs"})
    assert said.status_code == 200, said.text
    end = _events(said.text)[-1]["arcade"]
    assert end["free"] is True and end["cost"] == 0
    system = seen[-1]["messages"][0]["content"]
    assert "open tab(s): LLMs" in system and "DogecoinArcade" in system
    assert seen[-1]["max_tokens"] <= 1200
    assert state.llm_seller.books.analytics(0)["by"]["via"][0]["key"] == "helper"
    # the page carries the robot; a person may turn it off on Your arcade (kept in their browser)
    page = client.get("/exchange?tab=llm").text
    assert 'id="arc-bot-face"' in page and "<textarea" not in page.split('id="arc-bot-face"')[1], \
        "the robot is on the page, its chat built by script (no box in the page's own HTML)"
    assert 'id="helper-on"' in client.get("/me").text


def test_the_helper_never_runs_up_a_bill_and_is_limited_per_person(arcade, monkeypatch):
    client, state, pubkey, seen = arcade
    monkeypatch.setattr(type(state), "derived_address", None, raising=False)
    assert client.get("/account/llm/helper").json()["available"] is False, "no operator identity: no helper"
    node = b58check_encode(0x37, b"\x08" * 20)
    monkeypatch.setattr(type(state), "derived_address", node, raising=False)
    import arcade.web.llmweb as lw
    ok = 0
    for _ in range(45):
        r = client.post("/account/llm/helper", json={"messages": [{"role": "user", "content": "hi"}]})
        ok += r.status_code == 200
    assert ok == 40, "40 an hour for a signed-in person"


def test_a_paid_picture_model_is_not_called_free_because_the_words_are(pictures):
    """Live 2026-10-23: @apple saw "Free, your own model" on Strawberry's PAID pictures because its LLM was free."""
    client, state, pubkey, jobs, url = pictures
    h = {"X-Arcade-Admin": "1"}
    client.post("/admin/api/llm", headers=h, json={"prices": []})                 # words free for everyone
    client.post("/admin/api/llm/images", headers=h, json={
        "on": True, "url": url, "models": ["qwen_image_2.1_int8_convrot.safetensors"],
        "prices": [{"leg": "main-coin", "label": "PEP", "per": "0.5"}]})
    b = client.get("/account/llm/balance?seller=here").json()
    assert b["free"] is True and b["own"] is False and b["images_free"] is False


def test_comfyui_is_woken_again_when_it_dies_starting(monkeypatch):
    """Live 2026-10-23: ComfyUI started as the voice model let go of the GPU died ("CUDA error: out of memory"); a
    second start, seconds later, came up. The wake is run again while it does not answer."""
    from arcade import imagegen
    runs, ups = [], iter([False, False, False, False, True])
    monkeypatch.setattr(imagegen.ComfyServer, "up", lambda self: next(ups))
    monkeypatch.setattr(imagegen.subprocess, "run", lambda *a, **k: runs.append(a))
    monkeypatch.setattr(imagegen, "WAKE_AGAIN", 0.0)
    monkeypatch.setattr(imagegen.time, "sleep", lambda s: None)
    imagegen.ComfyServer("http://127.0.0.1:9", "true").ensure_up()
    assert len(runs) >= 2, "woken again, not only once"


def test_a_reply_that_breaks_off_with_an_error_costs_nothing(tmp_path, monkeypatch):
    """2026-10-23: "If there is an error, they should be refunded their credit"."""
    books = llm.Books(tmp_path)
    books.credit("70" * 32, "main-coin", "b", llm.COIN)
    cfg = {**llm.DEFAULTS, "on": True, "url": "http://x", "models": ["m"],
           "prices": [{"leg": "main-coin", "label": "PEP", "in": "0.001", "out": "0.01"}]}

    def broken(self, body):
        yield {"choices": [{"delta": {"content": "half a rep"}}]}
        raise llm.LlmError("the model server said: out of memory")
    monkeypatch.setattr(llm.Server, "chat", broken)
    seller = llm.Seller(lambda: cfg, books, lambda leg: "P", lambda leg: llm.COIN, None, lambda b: False)
    with pytest.raises(llm.LlmError):
        list(seller.chat("b", "main-coin", {"messages": [{"role": "user", "content": "hi"}]}))
    # the old coin credit (1 PEP at 10 PEP a credit) became 0.1 credits for "m"; the broken reply took none of it
    assert seller.credits.balance("b", "m") == 100, "nothing taken"
    assert books.analytics(0)["total"]["failed"] == 1


def test_each_model_has_its_own_credits_and_pictures_can_be_deleted(pictures):
    """2026-10-27: "Credits should not be shared across models each model has their own credit, and those
    credits are stored by the operator of that model" (before, one coin credit paid for any model of a seller);
    "User should be able to delete generated images from the cache" (2026-10-23)."""
    client, state, pubkey, jobs, url = pictures
    h = {"X-Arcade-Admin": "1"}
    MODEL = "qwen_image_2.1_int8_convrot.safetensors"
    client.post("/admin/api/llm/images", headers=h, json={
        "on": True, "url": url, "models": [MODEL], "prices": [{"leg": "main-coin", "label": "PEP", "per": "0.5"}]})
    state.llm_seller.credits.give(ADDR, MODEL, 1000)                            # 1 picture credit
    got = client.post("/account/llm/image", json={"seller": "here", "prompt": "x", "size": "250x250"})
    assert got.status_code == 200 and got.json()["arcade"]["model"] == MODEL, got.text
    state.llm_seller.credits.give(ADDR, MODEL, 5000)
    said = client.post("/account/llm/chat", json={"seller": "here", "messages": [{"role": "user", "content": "hi"}]})
    assert said.status_code == 400 and "credits" in said.json()["detail"], "a picture model's credits pay no chat"
    bal = client.get("/account/llm/balance?seller=here").json()["models"]
    assert bal[MODEL]["mc"] == 5000 and all(v["mc"] == 0 for m, v in bal.items() if m != MODEL)
    pics = client.get("/account/llm/pictures").json()["pictures"]
    assert client.post("/account/llm/pictures/delete", json={"id": pics[0]["id"]}).json()["ok"]
    assert client.get("/account/llm/pictures").json()["pictures"] == []
    assert client.get(pics[0]["url"]).status_code == 404


def test_old_coin_credit_becomes_the_first_models_credits_once(tmp_path):
    """the operator's choice (2026-10-27): a buyer's coin credit from before credits becomes credits for the seller's first
    model, at the seller's credit price, once."""
    books = llm.Books(tmp_path)
    books.credit("71" * 32, "main-coin", "b", 3 * llm.COIN)
    cfg = {**llm.DEFAULTS, "on": True, "url": "http://x", "models": ["first", "second"],
           "credit_prices": [{"leg": "main-coin", "label": "PEP", "per": "0.5"}]}
    seller = llm.Seller(lambda: cfg, books, lambda leg: "P", lambda leg: llm.COIN, None, lambda b: False)
    got = seller.balance("b")["models"]
    assert got["first"]["mc"] == 6000 and got["second"]["mc"] == 0, "3 PEP at 0.5 a credit: 6 credits, first model"
    books.credit("72" * 32, "main-coin", "b", llm.COIN)
    assert seller.balance("b")["models"]["first"]["mc"] == 6000, "once"


def test_the_chat_can_leave_the_arcade_docs_out(arcade):
    """2026-10-23: "You should be able to disable the arcade context for the in page LLM chat"."""
    client, state, pubkey, seen = arcade
    state.llm_seller.books.credit("90" * 32, "main-coin", ADDR, llm.COIN)
    said = client.post("/account/llm/chat", json={"seller": "here", "leg": "auto", "docs": False,
                                                  "messages": [{"role": "user", "content": "how do prize pools work?"}]})
    assert said.status_code == 200 and _events(said.text)[-1]["arcade"]["docs_tokens"] == 0
    assert seen[-1]["messages"] == [{"role": "user", "content": "how do prize pools work?"}], "nothing of ours added"


def test_a_shared_model_is_used_by_the_helper_only_once_approved(arcade, monkeypatch):
    """2026-10-23: "I want to be able to choose, which shared models my arcade uses ... the other arcade will need
    to approve the use of that model" (a 9B model shared with him could not keep the context)."""
    client, state, pubkey, seen = arcade
    monkeypatch.setattr(type(state), "derived_address", b58check_encode(0x37, b"\x09" * 20), raising=False)
    monkeypatch.setattr(type(state), "operator_tag", lambda self: "boss", raising=False)

    class Share:
        offers = {"peer9": {"models": [{"id": "small-9b"}], "prices": [], "free": False, "free_tags": ["boss"],
                            "tag": "other", "host": "other.example", "load": {"word": "idle"}}}

        def say_again(self):
            pass
    state.mesh = type("M", (), {"node_id": "me", "llmshare": Share(), "operators": None})()
    h = {"X-Arcade-Admin": "1"}
    assert client.get("/account/llm/helper").json()["model"] == "tiny", "this arcade's own model first"
    client.post("/admin/api/llm", headers=h, json={"helper_off_own": True})
    assert client.get("/account/llm/helper").json()["available"] is False, "the share is not used before it is approved"
    rows = client.get("/admin/api/llm").json()["helper"]
    assert {r["key"]: r["approved"] for r in rows} == {"here|tiny": False, "peer9|small-9b": False}
    client.post("/admin/api/llm", headers=h, json={"helper_ok": ["peer9|small-9b"]})
    got = client.get("/account/llm/helper").json()
    assert got["available"] and got["model"] == "small-9b" and got["seller"] == "@other"
    client.post("/admin/api/llm", headers=h, json={"helper_off_own": False})
    assert client.get("/account/llm/helper").json()["model"] == "tiny", "own model back on: first again"


def test_a_grant_is_carried_by_the_first_arcade_that_brings_it(tmp_path):
    """2026-10-27, from the audit: a grant names seller, buyer and expiry, not who carries it. The seller binds each to
    the first arcade that brings it, so a copy leaked off the buyer's arcade is refused from any other."""
    from arcade.llm import Books
    books = Books(tmp_path)
    assert books.carried_by("sig-1", "arcadeA") and books.carried_by("sig-1", "arcadeA")
    assert not books.carried_by("sig-1", "arcadeB"), "the same grant from another arcade"
    assert books.carried_by("sig-2", "arcadeB"), "a grant the buyer signed anew for another arcade"


def test_a_coin_sold_for_before_still_counts_after_the_seller_stops_taking_it():
    """2026-10-27, from the audit: paid, then the seller made its model free or dropped that coin -> coins lost."""
    from arcade import llm
    seller = llm.Seller(lambda: {"on": True, "url": "x", "models": ["m"], "prices": []}, None, lambda leg: "addr",
                        lambda leg: llm.COIN, lambda *a: 0, lambda b: False, lambda: {})
    with pytest.raises(llm.LlmError, match="free"):
        seller.topup("buyer", "main-coin", "ab" * 32, "sig")
    seller.paid_before = lambda leg: leg == "main-coin"
    with pytest.raises(llm.LlmError) as exc:
        seller.topup("buyer", "main-coin", "ab" * 32, "sig")
    assert "free" not in str(exc.value) and "does not take" not in str(exc.value), "it goes on to check the payment"


def test_credit_set_aside_for_a_reply_keeps_two_at_once_from_overdrawing(tmp_path):
    """2026-10-27, from the audit: replies at once each checked the same balance before either was charged."""
    from arcade.llm import Books
    books = Books(tmp_path)
    books.credit("ab" * 32, "main-coin", "buyer", 100)
    assert books.reserve("buyer", "main-coin", 70)
    assert not books.reserve("buyer", "main-coin", 70), "the second reply finds only what is left"
    books.release("buyer", "main-coin", 70)
    assert books.balance("buyer", "main-coin") == 100


def test_the_first_credit_prices_keep_what_pictures_cost(pictures):
    """2026-10-27: an operator's first save of credit prices sets each picture model's rate from its old picture price,
    so 0.5 PEP a picture credit against a 10 PEP credit becomes 0.05 credits a 250 px -- the same 0.5 PEP."""
    from arcade import imagegen
    client, state, pubkey, jobs, url = pictures
    h = {"X-Arcade-Admin": "1"}
    MODEL = "qwen_image_2.1_int8_convrot.safetensors"
    client.post("/admin/api/llm/images", headers=h, json={
        "on": True, "url": url, "models": [MODEL], "prices": [{"leg": "main-coin", "label": "PEP", "per": "0.5"}]})
    saved = client.post("/admin/api/llm", headers=h, json={
        "credit_prices": [{"leg": "main-coin", "label": "PEP", "per": "10"}], "prices": []})
    assert saved.status_code == 200, saved.text
    assert imagegen.config(state)["rates"][MODEL]["px"] == "0.05"
    eff = client.get("/admin/api/llm").json()["effective"]
    assert eff["picture_rates"][MODEL] == "0.05" and eff["picture_prices"][0]["per"] == "10"
    client.post("/admin/api/llm/images", headers=h, json={"rates": {MODEL: {"px": "0.2"}}})
    client.post("/admin/api/llm", headers=h, json={"credit_prices": [{"leg": "main-coin", "label": "PEP", "per": "5"}]})
    assert imagegen.config(state)["rates"][MODEL]["px"] == "0.2", "a rate the operator set is never changed"


def test_a_model_sold_only_in_credits_is_on_the_book(arcade):
    """A seller with credit prices and no old per-token asks is still offered (found by the page rebuild, 2026-10-27)."""
    client, state, pubkey, seen = arcade
    url = llm.config(state)["url"]
    h = {"X-Arcade-Admin": "1"}
    client.post("/admin/api/llm", headers=h, json={"on": True, "url": url, "models": ["tiny"], "prices": [],
                                                   "credit_prices": [{"leg": "main-coin", "label": "PEP", "per": "1"}]})
    (o,) = client.get("/account/llm/offers").json()["offers"]
    assert [m["id"] for m in o["models"]] == ["tiny"] and not o["free"] and o["credit_prices"][0]["per"] == "1"


def test_pictures_picked_for_a_collection_are_kept_a_week(pictures):
    """2026-10-27, SadFrog: "Have a prefilled template of a metadata that they can optionally fill out incase they want
    to make a small collection": a picture picked for a collection outlives the 24-hour cache (a week), put back it
    leaves on the day it was made, and only so many are kept at once."""
    import os as _os
    import time as _time
    client, state, pubkey, jobs, url = pictures
    h = {"X-Arcade-Admin": "1"}
    client.post("/admin/api/llm/images", headers=h, json={
        "on": True, "url": url, "models": ["qwen_image_2.1_int8_convrot.safetensors"]})
    for _ in range(2):
        got = client.post("/account/llm/image", json={"seller": "here", "prompt": "a frog", "size": "250x250"})
        assert got.status_code == 200, got.text
    a, b = client.get("/account/llm/pictures").json()["pictures"]
    assert not a["kept"] and a["until"] - a["at"] == 24 * 3600
    got = client.post("/account/llm/pictures/keep", json={"ids": [a["id"]], "keep": True})
    assert got.status_code == 200 and got.json()["pictures"][0]["kept"], got.text
    # Two days on: the kept one is still there, the other is gone.
    from pathlib import Path
    folder = next((Path(state.home) / "llm-pictures").iterdir())
    old = _time.time() - 2 * 24 * 3600
    import json as _json
    for f in folder.iterdir():
        if f.suffix == ".json":
            f.write_text(_json.dumps({**_json.loads(f.read_text()), "at": old}))
        _os.utime(f, (old, old))
    left = client.get("/account/llm/pictures").json()["pictures"]
    assert [p["id"] for p in left] == [a["id"]] and left[0]["until"] - _time.time() > 4 * 24 * 3600
    assert client.get(left[0]["url"]).status_code == 200
    # Put back: it leaves a day after it was made, which was two days ago.
    assert client.post("/account/llm/pictures/keep", json={"ids": [a["id"]], "keep": False}).status_code == 200
    assert client.get("/account/llm/pictures").json()["pictures"] == []
    assert client.post("/account/llm/pictures/keep", json={"ids": ["../../etc"]}).status_code == 400
    assert client.post("/account/llm/pictures/keep", json={"ids": [b["id"]]}).status_code == 400, "gone"
