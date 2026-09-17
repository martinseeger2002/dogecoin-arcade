"""The shop through the wallet: the buyer's door, the approval, the hand-off
to the shop's node -- and the owner's door, which sends without asking."""

import json
import time
import pathlib
import sys

import pytest

from arcade import approvals as approvalslib
from arcade import swap as S
from arcade.messaging import api
from arcade.messaging.keys import Identity, fingerprint_of

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_swap import (FakeIndex, FakeNode, SELLER, BUYER, OTHER, SHOP, PIECE,  # noqa: E402
                       PIECE2, GOOF, decode, _SIGNED)
from arcade.ledger import COIN                                   # noqa: E402
from arcade.messaging.sender import SendError                    # noqa: E402

UNSPENT = [("1" * 64, 0, SELLER, 3 * COIN), ("1" * 64, 1, SELLER, int(0.5 * COIN)),
           ("2" * 64, 0, BUYER, 5 * COIN), ("2" * 64, 1, BUYER, 1 * COIN)]


@pytest.fixture
def shopfront(monkeypatch, client):
    """This wallet is the BUYER's; the shop is SELLER's, its node `shopkey`."""
    _SIGNED.clear()
    app, state = client
    index = FakeIndex()
    shopkey = Identity.generate()
    shop = json.loads(index.rows[SHOP]["json"])
    shop["shop"]["node"] = shopkey.public_bytes.hex()
    index.rows[SHOP]["json"] = json.dumps(shop)
    monkeypatch.setattr(type(state), "token_index", lambda self, chain: index)
    node = FakeNode({BUYER}, UNSPENT)
    monkeypatch.setattr(type(state.messaging), "rpc", lambda self: node)
    sent = {}

    class FakeSender:
        def __init__(self, *a, **k):
            pass

        def prepare(self, address, payload):
            sent.setdefault("payloads", []).append(payload)
            return type("P", (), {"fee_sats": 1000, "total_sats": 1000, "size": 300})()

        def broadcast(self, prepared):
            sent["n"] = sent.get("n", 0) + 1
            return f"msg-{sent['n']}"

    monkeypatch.setattr("arcade.web.app.MessageSender", FakeSender)
    monkeypatch.setattr("arcade.web.app.funded_address", lambda *a, **k: BUYER)
    state.identity = Identity.generate()
    state.ensure_identity = lambda: state.identity
    return app, state, index, node, shopkey, sent


def _said(sent, shopkey, n):
    """What the n-th message this wallet sent says, opened as the shop."""
    _, body = api.open_payload(shopkey, sent["payloads"][n])
    return json.loads(body)


def _heard(state, shopkey, body, n):
    with state.store() as store:
        store.add_api_message("regtest", f"t{n}", 5, 5, "nShop", shopkey.public_bytes,
                              fingerprint_of(state.identity.public_bytes),
                              json.dumps(body).encode(), protocol=1, fingerprint=b"\0" * 4)


def test_a_page_buys_from_the_shop_it_is(shopfront):
    app, state, index, node, shopkey, sent = shopfront
    door, csrf = f"/swap/{SHOP}", state.csrf_token
    assert app.post(door, json={"op": "shop"}).status_code == 400, "the viewer's token or nothing"
    assert app.post(f"/swap/{'9' * 64}", json={"csrf_token": csrf, "op": "shop"}).status_code == 404

    front = app.post(door, json={"csrf_token": csrf, "op": "shop"}).json()
    assert front["ok"] and front["node"] == shopkey.public_bytes.hex()
    assert front["seller"] == SELLER and front["mine"] is False and front["open"] is True
    assert front["ready"] is True and front["from"] == 0
    assert [l["text"] for l in front["listings"]][:2] == [
        "100 Arcade Test for 2 coins", "a random Goofball (3 left) for 10 Arcade Test"]

    # 1. The page asks; the wallet chooses the address that will pay, and
    #    the question goes to the node the SHOP's JSON names.
    asked = app.post(door, json={"csrf_token": csrf, "op": "offer", "listing": 0}).json()
    assert asked["ok"] and asked["txid"] == "msg-1" and asked["buyer"] == BUYER
    assert _said(sent, shopkey, 0) == {"swap": "offer", "swapv": S.PROTOCOL, "shop": SHOP,
                                      "listing": 0, "buyer": BUYER}

    # 2. The shop's node answers with an offer (made here with the shop's
    #    own book, as the shopkeeper would), and the page hears it.
    seller_node = FakeNode({SELLER}, UNSPENT)
    offers = S.Offers(state.home / "seller-swaps.sqlite")
    offer = S.make_offer(seller_node, index, offers, "regtest", index.rows[SHOP], 0, BUYER,
                         state.identity.public_bytes.hex(), own=[SELLER])
    _heard(state, shopkey, {"swap": "offer", "swapv": S.PROTOCOL, "ok": True, "re": "msg-1",
                            "offer": offer}, 1)
    heard = app.post(door, json={"csrf_token": csrf, "op": "replies"}).json()["replies"]
    assert [h["json"]["re"] for h in heard] == ["msg-1"]

    # 3. Accepting files an approval -- after the offer is checked against
    #    THIS node's reading of the shop, not the page's.
    for change, why in ((dict(shop="9" * 64), "different shop"),
                        (dict(seller=OTHER), "not from the wallet that holds this shop"),
                        (dict(buyer=OTHER), "cannot sign for")):
        r = app.post(door, json={"csrf_token": csrf, "op": "accept", "offer": dict(offer, **change)})
        assert r.status_code == 400 and why in r.json()["error"], change
    filed = app.post(door, json={"csrf_token": csrf, "op": "accept", "offer": offer}).json()
    assert filed["ok"] and filed["offer"] == offer["id"]
    row = state.approvals.get(filed["request"])
    assert row["kind"] == "swap" and row["page"] == SHOP and row["peer"] == shopkey.public_bytes.hex()
    assert row["toaddress"] == SELLER and row["fromaddress"] == BUYER
    waiting = app.get("/approvals/waiting").json()
    assert waiting["requests"][0]["summary"] == "swap 2 coins for 100 Arcade Test"

    # 4. The approval page shows the swap, built; approving signs the
    #    buyer's half and hands it to the shop's node -- nothing is broadcast.
    page = app.get(f"/approvals/{filed['request']}?embed=1").text
    assert "You give" in page and "2 coins" in page and "100 Arcade Test" in page
    assert "Approve and sign" in page and "the seller: its own input back" in page
    held = next(p for (net, txid), p in state.prepared_tokens.items() if net == "regtest")
    assert _SIGNED[held.hex] == {BUYER}
    done = app.post(f"/approvals/{filed['request']}?embed=1",
                    data={"csrf_token": csrf, "confirmed": held.txid}).text
    assert "handed to the shop" in done, done
    assert node.sent == [], "the buyer's wallet broadcasts nothing"
    row = state.approvals.get(filed["request"])
    assert row["status"] == "sent" and row["txid"] == "msg-2"
    handed = _said(sent, shopkey, 1)
    assert handed == {"swap": "sign", "swapv": S.PROTOCOL, "offer": offer["id"], "hex": held.hex}
    status = app.post(door, json={"csrf_token": csrf, "op": "status",
                                  "request": filed["request"]}).json()["request"]
    assert status["status"] == "sent" and status["message"] == "msg-2"
    assert "confirmations" not in status, "a message, not the swap; nothing to count yet"

    # 5. The shop's node signs exactly what it offered and sends.
    txid = S.countersign(seller_node, index, offers, offers.get(offer["id"]), handed["hex"])
    assert seller_node.sent == [held.hex]
    _heard(state, shopkey, {"swap": "sign", "swapv": S.PROTOCOL, "ok": True, "re": "msg-2",
                            "offer": offer["id"], "txid": txid}, 2)
    heard = app.post(door, json={"csrf_token": csrf, "op": "replies"}).json()["replies"]
    assert heard[-1]["json"]["txid"] == txid
    assert decode(held.hex)["vin"][0] == {"txid": offer["outpoint"]["txid"],
                                          "vout": offer["outpoint"]["vout"]}


def test_the_shop_door_refuses_what_it_should(shopfront, monkeypatch):
    app, state, index, node, shopkey, sent = shopfront
    door, csrf = f"/swap/{SHOP}", state.csrf_token
    r = app.post(door, json={"csrf_token": csrf, "op": "offer", "listing": 9})
    assert r.status_code == 400 and "no listing 9" in r.json()["error"]
    r = app.post(door, json={"csrf_token": csrf, "op": "dance"})
    assert r.status_code == 400 and "op must be" in r.json()["error"]
    # A shop whose take this wallet cannot pay is refused before anything is sent.
    index.balances[(BUYER, 3)] = 0
    r = app.post(door, json={"csrf_token": csrf, "op": "offer", "listing": 1})
    assert r.status_code == 400 and "no address in this wallet holds 10 Arcade Test" in r.json()["error"]
    assert sent.get("n", 0) == 0
    # The seller's own wallet does not buy from itself.
    node.mine = {SELLER}
    r = app.post(door, json={"csrf_token": csrf, "op": "offer", "listing": 0})
    assert r.status_code == 400 and "your own shop" in r.json()["error"]
    assert app.post(door, json={"csrf_token": csrf, "op": "shop"}).json()["mine"] is True
    # Not a shop at all.
    r = app.post(f"/swap/{PIECE}", json={"csrf_token": csrf, "op": "shop"})
    assert r.status_code == 400 and "shop" in r.json()["error"]
    # Never on mainnet.
    monkeypatch.setattr(state.messaging, "network", "main")
    r = app.post(door, json={"csrf_token": csrf, "op": "shop"})
    assert r.status_code == 400 and "testnet only" in r.json()["error"]


def test_the_owners_page_sends_without_asking(shopfront, monkeypatch):
    app, state, index, node, shopkey, sent = shopfront
    from arcade import approvals as A
    csrf = state.csrf_token
    node.mine = {SELLER}                         # this wallet made and holds the shop
    monkeypatch.setattr(A, "prepare", lambda row, rpc, params, index, own: A.Prepared(
        hex="00", txid="built", what="1 Arcade Test", sender=SELLER, fee_sats=1000,
        dust_sats=1_000_000, size=250))
    monkeypatch.setattr(A, "broadcast", lambda rpc, prepared: "sent-" + prepared.txid)

    # The Tokens page is on mainnet by default: a page is told it has to ask there.
    door = f"/owner/{SHOP}"
    r = app.post(door, json={"csrf_token": csrf, "op": "send", "kind": "token",
                             "propertyid": 3, "amount": "1", "to": OTHER})
    assert r.status_code == 400 and "testnet only" in r.json()["error"]

    state.token_chain_path.write_text("regtest")
    state._token_chain = None
    me = app.post(door, json={"csrf_token": csrf, "op": "identity"}).json()
    assert me == {"ok": True, "owner": True, "creator": SELLER, "holder": SELLER,
                  "network": "regtest"}
    r = app.post(door, json={"csrf_token": csrf, "op": "send", "kind": "token",
                             "propertyid": 3, "amount": "1", "to": OTHER, "label": "Shop"}).json()
    assert r["ok"] and r["txid"] == "sent-built" and r["what"] == "1 Arcade Test"
    row = state.approvals.get(r["request"])
    assert row["status"] == "sent" and row["txid"] == "sent-built" and row["origin"] == "own page"
    assert "a page of your own" in app.get("/approvals").text

    # Somebody else's page, or one this wallet sold on, has to ask.
    index.rows[PIECE]["creator"] = SELLER            # made here, but held by...
    index.rows[PIECE]["owner"] = OTHER               # ...somebody else now
    r = app.post(f"/owner/{PIECE}", json={"csrf_token": csrf, "op": "send", "kind": "token",
                                          "propertyid": 3, "amount": "1", "to": OTHER})
    assert r.status_code == 400 and "has to ask" in r.json()["error"]
    assert app.post(f"/owner/{PIECE}", json={"csrf_token": csrf, "op": "identity"}).json()["owner"] is False
    # And what it asks for is still checked.
    r = app.post(door, json={"csrf_token": csrf, "op": "send", "kind": "token",
                             "propertyid": 9, "amount": "1", "to": OTHER})
    assert r.status_code == 400 and "no token 9" in r.json()["error"]
    assert app.post(door, json={"op": "identity"}).status_code == 400, "the viewer's token or nothing"


def test_the_shims_and_the_doors_are_there(client):
    app, state = client
    for name, words in (("swap", ("arcade.swap", "buy:", "awaitOffer:", "accept:", "status:")),
                        ("owner", ("arcade.owner", "identity:", "send:"))):
        shim = app.get(f"/r/{name}.js")
        assert shim.status_code == 200 and "javascript" in shim.headers["content-type"]
        assert shim.headers["access-control-allow-origin"] == "*"
        for word in words:
            assert word in shim.text, (name, word)
    from test_nodetalk import _page, PAGE
    _page(state)
    view = app.get(f"/inscriptions/{PAGE}/view").text
    assert "swap: '/swap/'" in view and "owner: '/owner/'" in view


# --- the shopkeeper: the seller's side, nobody pressing anything ----------

@pytest.fixture
def shop(monkeypatch, client):
    """This wallet is the SELLER's and made the shop; `buyer` is another node."""
    _SIGNED.clear()
    app, state = client
    index = FakeIndex()
    state.identity = Identity.generate()
    state.ensure_identity = lambda: state.identity
    shop = json.loads(index.rows[SHOP]["json"])
    shop["shop"]["node"] = state.identity.public_bytes.hex()
    index.rows[SHOP]["json"] = json.dumps(shop)
    monkeypatch.setattr(type(state), "token_index", lambda self, chain: index)
    node = FakeNode({SELLER}, UNSPENT)
    monkeypatch.setattr(type(state.messaging), "rpc", lambda self: node)
    answers = []

    class FakeSender:
        def __init__(self, *a, **k):
            pass

        def prepare(self, address, payload):
            answers.append((address, payload))
            return object()

        def broadcast(self, prepared):
            return f"ans-{len(answers)}"

    monkeypatch.setattr("arcade.shopkeeper.MessageSender", FakeSender)
    monkeypatch.setattr("arcade.shopkeeper.funded_address", lambda rpc, prefer=None: SELLER)
    from arcade.shopkeeper import Shopkeeper
    return state, index, node, Identity.generate(), answers, Shopkeeper(state)


def _ask(state, buyer, body, n):
    with state.store() as store:
        return store.add_api_message("regtest", f"q{n}", 5, 5, "nBuyer", buyer.public_bytes,
                                     fingerprint_of(state.identity.public_bytes),
                                     json.dumps(body).encode(), protocol=1, fingerprint=b"\0" * 4)


def _answers(answers, buyer):
    out = []
    for address, payload in answers:
        _, body = api.open_payload(buyer, payload)
        out.append((address, json.loads(body)))
    return out


def test_the_shopkeeper_sells_what_the_shop_says(shop):
    state, index, node, buyer, answers, keeper = shop
    # Before there was a shopkeeper: whatever is in the inbox is not its business.
    old = _ask(state, buyer, {"swap": "offer", "swapv": S.PROTOCOL, "shop": SHOP,
                              "listing": 0, "buyer": BUYER}, 0)
    assert keeper.tick() == 0 and answers == []
    assert state.offers.cursor("regtest") == old
    assert keeper.tick() == 0, "nothing new"

    # An order, and a message that is somebody else's, and a bad one.
    _ask(state, buyer, {"kind": "hello", "text": "not a swap"}, 1)
    _ask(state, buyer, {"swap": "offer", "swapv": S.PROTOCOL, "shop": SHOP,
                        "listing": 0, "buyer": BUYER}, 2)
    _ask(state, buyer, {"swap": "offer", "swapv": 99, "shop": SHOP, "listing": 0,
                        "buyer": BUYER}, 3)
    _ask(state, buyer, {"swap": "offer", "swapv": S.PROTOCOL, "shop": PIECE,
                        "listing": 0, "buyer": BUYER}, 4)
    assert keeper.tick() == 3
    said = _answers(answers, buyer)
    assert [a for a, _ in said] == [SELLER] * 3
    offered, wrong, notashop = [body for _, body in said]
    assert offered["ok"] and offered["re"] == "q2" and offered["swapv"] == S.PROTOCOL
    assert offered["version"] == state.running_version
    offer = offered["offer"]
    assert offer["seller"] == SELLER and offer["buyer"] == BUYER and offer["shop"] == SHOP
    assert offer["give"]["kind"] == "token" and offer["give"]["amount"] == "100"
    assert "buyer_pubkey" not in offer, "the bookkeeping stays home"
    assert state.offers.get(offer["id"])["buyer_pubkey"] == buyer.public_bytes.hex()
    assert wrong["ok"] is False and "speaks swap protocol 1, not 99" in wrong["error"]
    assert notashop["ok"] is False and "shop" in notashop["error"]
    with state.store() as store:
        unread = [r["txid"] for r in store.api_messages(
            fingerprint_of(state.identity.public_bytes), "regtest", unread_only=True)]
    assert unread == ["q0", "q1"], "the swap questions are read; the rest is left alone"
    assert offer["outpoint"] == {"txid": "1" * 64, "vout": 1, "value": int(0.5 * COIN)}, \
        "the smallest of the seller's outputs"
    assert node.locked == {(offer["outpoint"]["txid"], offer["outpoint"]["vout"])}

    # The buyer's wallet builds its half, as the buyer door does, and sends it.
    buyer_node = FakeNode({BUYER}, UNSPENT)
    half = S.build(buyer_node, index, offer, own=[BUYER]).hex
    assert _SIGNED[half] == {BUYER}
    stranger = Identity.generate()
    _ask(state, stranger, {"swap": "sign", "swapv": S.PROTOCOL, "offer": offer["id"],
                           "hex": half}, 5)
    _ask(state, buyer, {"swap": "sign", "swapv": S.PROTOCOL, "offer": "nope", "hex": half}, 6)
    _ask(state, buyer, {"swap": "sign", "swapv": S.PROTOCOL, "offer": offer["id"],
                        "hex": half}, 7)
    assert keeper.tick() == 3
    _, stolen = _answers(answers[3:4], stranger)[0]
    assert stolen["ok"] is False and "somebody else" in stolen["error"]
    _, unknown = _answers(answers[4:5], buyer)[0]
    assert unknown["ok"] is False and "no such offer" in unknown["error"]
    _, signed = _answers(answers[5:6], buyer)[0]
    assert signed["ok"] and signed["offer"] == offer["id"] and signed["re"] == "q7"
    assert node.sent == [half]
    assert signed["txid"] == decode(node.sent[0])["txid"]
    assert _SIGNED[node.sent[0]] == {BUYER, SELLER}
    assert state.offers.get(offer["id"])["status"] == "sent"

    # The sale is in the approvals book, already done: nobody was asked, but
    # Approvals is where a person looks to see what this wallet signed (D-029).
    written = state.approvals.recent()
    assert len(written) == 1
    sale = written[0]
    assert sale["origin"] == "shop" and sale["status"] == "sent"
    assert sale["kind"] == "swap" and sale["txid"] == signed["txid"]
    assert sale["toaddress"] == BUYER and sale["fromaddress"] == SELLER
    assert sale["page"] == SHOP and sale["decided"]
    assert approvalslib.summary(sale) == "sold 100 Arcade Test for 2 coins"
    assert state.approvals.pending() == [], "a sale is not waiting for anybody"

    # Asking again gets a refusal, not a second sale.
    _ask(state, buyer, {"swap": "sign", "swapv": S.PROTOCOL, "offer": offer["id"],
                        "hex": half}, 8)
    assert keeper.tick() == 1 and len(node.sent) == 1
    _, again = _answers(answers[6:7], buyer)[0]
    assert again["ok"] is False


def test_the_shopkeeper_keeps_out_of_mainnet_and_survives_a_dead_node(shop, monkeypatch):
    state, index, node, buyer, answers, keeper = shop
    monkeypatch.setattr(state.messaging, "network", "main")
    assert keeper.tick() == 0
    monkeypatch.setattr(state.messaging, "network", "regtest")
    state.ensure_identity = lambda: (_ for _ in ()).throw(RuntimeError("node down"))
    assert keeper.tick() == 0
    state.ensure_identity = lambda: state.identity
    keeper.tick()                                  # sets the cursor
    _ask(state, buyer, {"swap": "offer", "swapv": S.PROTOCOL, "shop": SHOP,
                        "listing": 0, "buyer": BUYER}, 1)

    def dead(self):
        raise ConnectionError("gone")
    monkeypatch.setattr(type(state.messaging), "rpc", dead)
    assert keeper.tick() == 0, "not raised"
    monkeypatch.setattr(type(state.messaging), "rpc", lambda self: node)
    assert keeper.tick() == 1, "the order is still there for the next tick"


def test_a_shopkeeper_does_not_answer_an_answer(shop):
    """Two shops that talk to each other must not talk for ever.

    An answer has the same "swap" value as the question it answers, so a
    shopkeeper that reads every `{"swap": ...}` as an order answers the other
    shop's refusals, which are refused in turn: one message a block out of
    each wallet until a node is stopped. It happened on testnet (D-027).
    """
    state, index, node, buyer, answers, keeper = shop
    keeper.tick()                                  # sets the cursor
    _ask(state, buyer, {"swap": "offer", "swapv": S.PROTOCOL, "re": "q0", "ok": False,
                        "error": "no such inscription on this node"}, 1)
    _ask(state, buyer, {"swap": "sign", "swapv": S.PROTOCOL, "re": "q1", "ok": True,
                        "offer": "cd88cd26ee2eccab", "txid": "d" * 64}, 2)
    assert keeper.tick() == 0 and answers == [], "an answer is not an order"

    # A question with neither `re` nor `ok` is still answered.
    _ask(state, buyer, {"swap": "offer", "swapv": S.PROTOCOL, "shop": SHOP,
                        "listing": 0, "buyer": BUYER}, 3)
    assert keeper.tick() == 1
    _, offered = _answers(answers, buyer)[0]
    assert offered["ok"] and offered["re"] == "q3"
    # And the answer it just made would not move it, were it sent back.
    _ask(state, buyer, offered, 4)
    assert keeper.tick() == 0 and len(answers) == 1


def test_an_offer_asked_for_by_message_is_not_answered(shop):
    """Offers are said on the chain, where they reach anybody (D-042).

    A bid arriving as a message is an older peer talking: there is nothing
    to answer and nothing to write down, because the Exchange shows what the
    chain holds rather than what arrived here.
    """
    state, index, node, buyer, answers, keeper = shop
    keeper.tick()                                   # sets the cursor
    _ask(state, buyer, {"swap": "bid", "swapv": S.PROTOCOL, "id": "b1",
                        "inscription": PIECE, "buyer": BUYER,
                        "take": {"kind": "coins", "amount": "2", "sats": 2 * COIN}}, 1)
    assert keeper.tick() == 0 and answers == []
    assert state.offers.bids("regtest", "in") == []


def test_the_answer_to_our_own_offer_is_signed_against_the_terms_we_gave(shop):
    state, index, node, seller_identity, answers, keeper = shop
    keeper.tick()
    now = __import__("time").time()
    take = {"kind": "coins", "amount": "2.00000000", "sats": 2 * COIN}
    state.offers.add_bid({"id": "o1", "network": "regtest", "direction": "out",
                          "inscription": PIECE2, "number": 3, "owner": BUYER,
                          "buyer": SELLER, "peer_pubkey": "",
                          "take": take, "created": now, "expires": now + 900})

    # The holder's answer, as offer_for_bid would build it.
    other = FakeNode({BUYER}, UNSPENT)
    offer = S.offer_for_bid(other, index, S.Offers(state.home / "theirs.sqlite"),
                            "regtest",
                            {"inscription": PIECE2, "take": take, "buyer": SELLER,
                             "peer_pubkey": ""}, own=[BUYER])
    _ask(state, seller_identity, {"swap": "bid", "swapv": S.PROTOCOL, "id": "o1",
                                  "ok": True, "offer": offer}, 1)
    assert keeper.tick() == 1
    _, signed = _answers(answers, seller_identity)[0]
    assert signed["swap"] == "sign" and signed["offer"] == offer["id"]
    assert signed["hex"], "our half, signed"
    assert state.offers.get_bid("o1")["status"] == "signed"

    # A different price for the same offer id is refused, not signed.
    state.offers.add_bid({"id": "o2", "network": "regtest", "direction": "out",
                          "inscription": PIECE2, "number": 3, "owner": BUYER,
                          "buyer": SELLER, "peer_pubkey": "",
                          "take": {"kind": "coins", "amount": "1.00000000",
                                   "sats": COIN},
                          "created": now, "expires": now + 900})
    _ask(state, seller_identity, {"swap": "bid", "swapv": S.PROTOCOL, "id": "o2",
                                  "ok": True, "offer": offer}, 2)
    assert keeper.tick() == 0, "a different price is not what was agreed"
    bid = state.offers.get_bid("o2")
    assert bid["status"] == "failed" and "price" in bid["error"]


def test_a_bid_is_marked_signed_only_when_its_answer_goes(shop, monkeypatch):
    """A bid closed as "signed" whose reply never went out is a lie about
    what this wallet did, and the reply is what the other side acts on."""
    state, index, node, seller_identity, answers, keeper = shop
    keeper.tick()
    now = __import__("time").time()
    take = {"kind": "coins", "amount": "2", "sats": 2 * COIN}
    state.offers.add_bid({"id": "o1", "network": "regtest", "direction": "out",
                          "inscription": PIECE2, "number": 3, "owner": BUYER,
                          "buyer": SELLER, "peer_pubkey": "",
                          "take": take, "created": now, "expires": now + 900})
    other = FakeNode({BUYER}, UNSPENT)
    offer = S.offer_for_bid(other, index, S.Offers(state.home / "theirs.sqlite"),
                            "regtest",
                            {"inscription": PIECE2, "take": take, "buyer": SELLER,
                             "peer_pubkey": ""}, own=[BUYER])

    def wont_send(*a, **k):
        raise SendError("the node would not take it")
    monkeypatch.setattr(keeper, "_reply", wont_send)
    _ask(state, seller_identity, {"swap": "bid", "swapv": S.PROTOCOL, "id": "o1",
                                  "ok": True, "offer": offer}, 1)
    keeper.tick()
    assert state.offers.get_bid("o1")["status"] == "open", \
        "nothing went out, so nothing is signed"


def test_an_answer_is_checked_against_the_chain_when_the_note_is_missing(shop,
                                                                         monkeypatch):
    """An offer is on the chain; a wallet's own note of it is a convenience.

    For a day the note was never written at all, so every answer to every
    offer was silently ignored and every offer timed out. The chain is what
    an answer is checked against (D-049).
    """
    state, index, node, seller_identity, answers, keeper = shop
    keeper.tick()
    take = {"kind": "coins", "amount": "2", "sats": 2 * COIN}
    offer_txid = "9" * 64

    # The chain says: this wallet offered 2 coins for PIECE2.
    index.offers = {offer_txid: {"txid": offer_txid, "inscription": PIECE2,
                                 "buyer": SELLER, "owner": BUYER,
                                 "take_kind": 3, "take_property": 0,
                                 "take_amount": 2 * COIN}}
    index.offer = lambda txid: index.offers.get(txid)
    assert state.offers.get_bid(offer_txid) is None, "no local note of it"

    other = FakeNode({BUYER}, UNSPENT)
    offer = S.offer_for_bid(other, index, S.Offers(state.home / "theirs.sqlite"),
                            "regtest",
                            {"inscription": PIECE2, "take": take, "buyer": SELLER,
                             "peer_pubkey": ""}, own=[BUYER])
    _ask(state, seller_identity, {"swap": "bid", "swapv": S.PROTOCOL,
                                  "id": offer_txid, "ok": True, "offer": offer}, 1)
    assert keeper.tick() == 1, "the chain told it what it had offered"
    _, signed = _answers(answers, seller_identity)[0]
    assert signed["swap"] == "sign" and signed["hex"]

    # An offer somebody ELSE made is not this wallet's to sign.
    index.offers[offer_txid] = {**index.offers[offer_txid], "buyer": OTHER}
    _ask(state, seller_identity, {"swap": "bid", "swapv": S.PROTOCOL,
                                  "id": offer_txid, "ok": True, "offer": offer}, 2)
    assert keeper.tick() == 0, "not ours, not signed"


# --- filling a standing order ------------------------------------------------

def _book_order(index, txid="or" + "d" * 62, address=SELLER, tokens=1000 * 10 ** 8,
                coins=8 * 10 ** 8):
    index.orders = getattr(index, "orders", {})
    index.orders[txid] = {"txid": txid, "block_height": 500, "position": 0,
                          "address": address, "sale_property": 3,
                          "sale_amount": tokens, "want_property": 0,
                          "want_amount": coins, "reserved": tokens}
    index.order = lambda key: index.orders.get(str(key))
    return txid


def test_the_shopkeeper_answers_a_fill_with_its_own_price(shop):
    """Somebody taking a price off this wallet's book. What they cannot work
    out alone is which output carries the swap; the price comes from the
    order, never from the question (D-063)."""
    state, index, node, buyer, answers, keeper = shop
    keeper.tick()                  # the first pass only sets the cursor
    order = _book_order(index)
    _ask(state, buyer, {"swap": "fill", "swapv": S.PROTOCOL, "order": order,
                        "tokens": 250 * 10 ** 8, "buyer": BUYER}, 20)
    assert keeper.tick() == 1
    _, body = _answers(answers, buyer)[-1]
    assert body["ok"] is True
    assert body["offer"]["seller"] == SELLER and body["offer"]["buyer"] == BUYER
    assert body["offer"]["give"]["units"] == 250 * 10 ** 8
    assert body["offer"]["take"]["sats"] == 2 * 10 ** 8, "a quarter of the order"
    assert body["offer"]["outpoint"]["txid"], "and which output carries it"


def test_a_fill_of_an_order_that_is_not_ours_is_refused(shop):
    state, index, node, buyer, answers, keeper = shop
    keeper.tick()                  # the first pass only sets the cursor
    order = _book_order(index, address=OTHER)
    _ask(state, buyer, {"swap": "fill", "swapv": S.PROTOCOL, "order": order,
                        "tokens": 10 * 10 ** 8, "buyer": BUYER}, 21)
    assert keeper.tick() == 1
    _, body = _answers(answers, buyer)[-1]
    assert body["ok"] is False and "not this wallet's to fill" in body["error"]


def test_an_answer_nobody_asked_for_is_ignored(shop):
    """The taker's side. An answer makes this wallet sign a transaction that
    pays coins, so without a note of having asked, any node could send one."""
    state, index, node, buyer, answers, keeper = shop
    order = _book_order(index, address=OTHER)
    offer = {"id": "deadbeef", "network": "regtest", "shop": "", "listing": -1,
             "seller": OTHER, "buyer": SELLER,
             "give": {"kind": "token", "propertyid": 3, "amount": "100",
                      "units": 100 * 10 ** 8},
             "take": {"kind": "coins", "amount": "1.00000000", "sats": 10 ** 8},
             "outpoint": {"txid": "1" * 64, "vout": 0, "value": 10 ** 7},
             "created": time.time(), "expires": time.time() + 600}
    _ask(state, buyer, {"swap": "fill", "swapv": S.PROTOCOL, "re": "n" * 64,
                        "ok": True, "offer": offer}, 22)
    assert keeper.tick() == 0, "read, and dropped: nothing to answer"
    assert _answers(answers, buyer) == [], "nothing signed, nothing sent"


def test_an_answer_that_asks_for_more_than_the_book_says_is_refused(shop):
    """The note says what this wallet worked out from the chain. An answer
    that wants more than that is not the price that was taken."""
    state, index, node, buyer, answers, keeper = shop
    keeper.tick()                  # the first pass only sets the cursor
    order = _book_order(index, address=OTHER)
    now = time.time()
    state.offers.add_fill({"id": "f" * 64, "network": "regtest", "order": order,
                           "maker": OTHER, "buyer": SELLER, "tokens": 100 * 10 ** 8,
                           "coins": 10 ** 8, "created": now, "expires": now + 600})
    offer = {"id": "deadbeef", "network": "regtest", "shop": "", "listing": -1,
             "seller": OTHER, "buyer": SELLER,
             "give": {"kind": "token", "propertyid": 3, "amount": "100",
                      "units": 100 * 10 ** 8},
             "take": {"kind": "coins", "amount": "9.00000000", "sats": 9 * 10 ** 8},
             "outpoint": {"txid": "1" * 64, "vout": 0, "value": 10 ** 7},
             "created": now, "expires": now + 600}
    _ask(state, buyer, {"swap": "fill", "swapv": S.PROTOCOL, "re": "f" * 64,
                        "ok": True, "offer": offer}, 23)
    assert keeper.tick() == 0, "a refusal to sign is not an answer"
    assert _answers(answers, buyer) == [], "nothing signed"
    assert state.offers.get_fill("f" * 64)["status"] == "failed"
    assert "priced at" in state.offers.get_fill("f" * 64)["error"]


# --- one piece asking another -------------------------------------------------

def _fighter(index, txid=PIECE, owner=SELLER, power=55, ready=None):
    import json as _json
    index.rows[txid] = {
        "txid": txid, "number": 2, "creator": OTHER, "owner": owner,
        "collection": None, "edition": None,
        "json": _json.dumps({"name": "Sparky", "stats": {"power": power, "hp": 120},
                             "api": {"power": {"json": "stats.power"},
                                     "ready": {"store": "ready"}}})}
    return txid


def test_a_node_answers_for_a_piece_it_holds(shop):
    """With nobody at the screen. That is the point: two pieces can interact
    while one of their owners is asleep (D-091)."""
    state, index, node, buyer, answers, keeper = shop
    keeper.tick()
    item = _fighter(index)
    state.pagestore.set(item, "ready", "yes")

    _ask(state, buyer, {"ask": item, "route": "power"}, 30)
    _ask(state, buyer, {"ask": item, "route": "ready"}, 31)
    assert keeper.tick() == 2
    said = [body for _, body in _answers(answers, buyer)][-2:]
    assert said[0]["ok"] is True and said[0]["answer"] == 55
    assert said[1]["ok"] is True and said[1]["answer"] == "yes"


def test_it_will_not_answer_for_a_piece_it_does_not_hold(shop):
    state, index, node, buyer, answers, keeper = shop
    keeper.tick()
    item = _fighter(index, owner=OTHER)
    _ask(state, buyer, {"ask": item, "route": "power"}, 32)
    assert keeper.tick() == 1
    _, body = _answers(answers, buyer)[-1]
    assert body["ok"] is False and "not held by this wallet" in body["error"]


def test_an_undeclared_route_is_refused_by_name(shop):
    state, index, node, buyer, answers, keeper = shop
    keeper.tick()
    item = _fighter(index)
    _ask(state, buyer, {"ask": item, "route": "secret"}, 33)
    assert keeper.tick() == 1
    _, body = _answers(answers, buyer)[-1]
    assert body["ok"] is False and "answers no route" in body["error"]


def test_an_answer_is_not_mistaken_for_a_question(shop):
    """Two nodes answering each other's answers for ever is how the shopkeeper
    loop happened (D-027). An answer carries `re`; a question does not."""
    state, index, node, buyer, answers, keeper = shop
    keeper.tick()
    item = _fighter(index)
    _ask(state, buyer, {"ask": item, "route": "power", "re": "x" * 64,
                        "ok": True, "answer": 55}, 34)
    assert keeper.tick() == 0, "an answer is not answered"
    assert _answers(answers, buyer) == []


# --- selling at a price already published -----------------------------------

def _standing(index, piece, sats, buyer=BUYER, height=300, position=0, txid="off1"):
    """An ask of the seller's, and an offer somebody made on it."""
    index.asks_standing = [{"inscription": piece, "seller": SELLER,
                            "take_kind": 3, "take_property": None,
                            "take_amount": sats}]
    index.offers_standing = [{"txid": txid, "inscription": piece, "buyer": buyer,
                              "take_kind": 3, "take_property": None,
                              "take_amount": sats, "number": 2, "owner": SELLER,
                              "block_height": height, "position": position}]


def _published(state, buyer):
    """The buyer has a key on this node, so there is somebody to answer."""
    with state.store() as store:
        store.add_key_announcement(
            txid="k" * 64, address=BUYER, pubkey=buyer.public_bytes,
            fingerprint=fingerprint_of(buyer.public_bytes), height=200,
            block_time=0, stated=True)


def test_an_offer_that_meets_the_asking_price_is_accepted_without_asking(
        shop, monkeypatch):
    """The owner already said yes, in writing, on the chain: an ask names the
    piece and the price, and this accepts exactly it (D-101)."""
    state, index, node, buyer, answers, keeper = shop
    _published(state, buyer)
    _standing(index, PIECE, 3 * COIN)

    assert keeper.sell_at_asking_price() == 1
    (address, body), = _answers(answers, buyer)
    assert body["swap"] == "bid" and body["ok"] is True and body["id"] == "off1"
    assert body["offer"]["give"]["txid"] == PIECE
    assert body["offer"]["take"]["sats"] == 3 * COIN

    # And never twice: the piece is reserved for that buyer until it expires.
    answers.clear()
    assert keeper.sell_at_asking_price() == 0 and answers == []


def test_less_than_the_asking_price_is_not_a_sale(shop):
    state, index, node, buyer, answers, keeper = shop
    _published(state, buyer)
    _standing(index, PIECE, 3 * COIN)
    index.offers_standing[0]["take_amount"] = 3 * COIN - 1

    assert keeper.sell_at_asking_price() == 0 and answers == []


def test_a_price_asked_in_a_token_is_not_met_by_coins(shop):
    """Coins are not a bid for a token price, and one token is not another."""
    state, index, node, buyer, answers, keeper = shop
    _published(state, buyer)
    _standing(index, PIECE, 3 * COIN)
    index.asks_standing[0].update(take_kind=2, take_property=3, take_amount=10)

    assert keeper.sell_at_asking_price() == 0 and answers == []
    index.offers_standing[0].update(take_kind=2, take_property=4, take_amount=99)
    assert keeper.sell_at_asking_price() == 0, "another token is not that token"
    index.offers_standing[0].update(take_property=3, take_amount=10)
    assert keeper.sell_at_asking_price() == 1, "the token asked for, in full"


def test_more_than_the_asking_price_is_still_a_yes(shop):
    state, index, node, buyer, answers, keeper = shop
    _published(state, buyer)
    _standing(index, PIECE, 3 * COIN)
    index.offers_standing[0]["take_amount"] = 5 * COIN

    assert keeper.sell_at_asking_price() == 1
    (_, body), = _answers(answers, buyer)
    assert body["offer"]["take"]["sats"] == 5 * COIN, "at what they offered, not the ask"


def test_the_best_offer_wins_and_at_a_tie_the_earliest(shop):
    state, index, node, buyer, answers, keeper = shop
    _published(state, buyer)
    _standing(index, PIECE, 3 * COIN)
    index.offers_standing.append(
        {"txid": "off2", "inscription": PIECE, "buyer": BUYER, "take_kind": 3,
         "take_property": None, "take_amount": 4 * COIN, "number": 2,
         "owner": SELLER, "block_height": 301, "position": 0})

    assert keeper.sell_at_asking_price() == 1
    (_, body), = _answers(answers, buyer)
    assert body["id"] == "off2", "four coins beats three"


def test_nothing_is_sold_when_the_switch_is_off(shop):
    state, index, node, buyer, answers, keeper = shop
    _published(state, buyer)
    _standing(index, PIECE, 3 * COIN)
    state.set_setting("auto_sell", False)

    assert keeper.sell_at_asking_price() == 0 and answers == []
    state.set_setting("auto_sell", True)
    assert keeper.sell_at_asking_price() == 1


def test_a_buyer_with_no_published_key_waits_for_a_person(shop):
    """There is nobody to send the seller's half to. Their offer stands, and
    the Exchange still shows it for a person to accept by hand (D-042)."""
    state, index, node, buyer, answers, keeper = shop
    _standing(index, PIECE, 3 * COIN)

    assert keeper.sell_at_asking_price() == 0 and answers == []


def test_a_piece_with_no_price_on_it_is_not_sold_by_itself(shop):
    state, index, node, buyer, answers, keeper = shop
    _published(state, buyer)
    _standing(index, PIECE, 3 * COIN)
    index.asks_standing = []

    assert keeper.sell_at_asking_price() == 0 and answers == []


# --- taking a price this wallet's own order crosses -------------------------

BID = "bid" + "1" * 61


def _crossing(index, bid_tokens=100 * 10 ** 8, bid_coins=10 ** 8,
              ask_tokens=100 * 10 ** 8, ask_coins=10 ** 8, maker=OTHER):
    """One bid of this wallet's, and one ask on the same pair."""
    index.my_orders = [{"txid": BID, "address": SELLER, "sale_property": 0,
                        "sale_amount": bid_coins, "want_property": 3,
                        "want_amount": bid_tokens, "block_height": 500,
                        "position": 0, "reserved": 0}]
    index.book_rows = {3: {"asks": [{"txid": "ask" + "2" * 61, "address": maker,
                                     "sale_property": 3, "sale_amount": ask_tokens,
                                     "want_property": 0, "want_amount": ask_coins,
                                     "reserved": ask_tokens, "block_height": 499,
                                     "position": 0}], "bids": []}}


@pytest.fixture
def trading(shop, monkeypatch):
    """The shopkeeper, with the maker reachable and token sends captured."""
    state, index, node, buyer, answers, keeper = shop
    maker = Identity.generate()
    with state.store() as store:
        store.add_key_announcement(txid="m" * 64, address=OTHER,
                                   pubkey=maker.public_bytes,
                                   fingerprint=fingerprint_of(maker.public_bytes),
                                   height=200, block_time=0, stated=True)
    built = []

    class FakeTokenSender:
        def __init__(self, *a, **k):
            pass

        def prepare(self, address, payload):
            built.append((address, payload))
            return f"tx-{len(built)}"

        def broadcast(self, prepared):
            return str(prepared)

    monkeypatch.setattr("arcade.tokens.TokenSender", FakeTokenSender)
    monkeypatch.setattr(node, "call",
                        lambda method, *a: ([{"txid": "u", "vout": 0, "spendable": True},
                                             {"txid": "u", "vout": 1, "spendable": True}]
                                            if method == "listunspent"
                                            else FakeNode.call(node, method, *a)))
    return state, index, keeper, answers, maker, built


def _orders_built(built):
    from arcade import payload as P

    return [P.decode(payload) for _, payload in built]


def test_a_bid_takes_the_ask_it_crosses(trading):
    state, index, keeper, answers, maker, built = trading
    _crossing(index)

    assert keeper.fill_what_crosses() == 1
    (_, body), = _answers(answers, maker)
    assert body["swap"] == "fill" and body["tokens"] == 100 * 10 ** 8
    assert body["buyer"] == SELLER
    note, = state.offers.fills("regtest")
    assert note["tokens"] == 100 * 10 ** 8 and note["coins"] == 10 ** 8
    # The bid is withdrawn at its own price first: a book must not advertise
    # what has already been committed.
    (cancel,) = _orders_built(built)
    from arcade import payload as P
    assert isinstance(cancel, P.MetaDExCancelPrice)
    assert cancel.property_id_desired == 3


def test_a_partial_fill_puts_the_remainder_back(trading):
    """What is left of THEIR order shrinks and stays on the book by itself;
    what is left of this wallet's bid is posted again, because no engine
    reduces a bid -- there is no reserve behind one."""
    state, index, keeper, answers, maker, built = trading
    _crossing(index, bid_tokens=100 * 10 ** 8, ask_tokens=40 * 10 ** 8,
              ask_coins=4 * 10 ** 7)

    assert keeper.fill_what_crosses() == 1
    (_, body), = _answers(answers, maker)
    assert body["tokens"] == 40 * 10 ** 8, "as much as was there"
    cancel, again = _orders_built(built)
    from arcade import payload as P
    assert isinstance(cancel, P.MetaDExCancelPrice)
    assert isinstance(again, P.MetaDExTrade)
    assert again.amount_desired == 60 * 10 ** 8, "the rest, at the same price"
    assert again.amount_for_sale == 6 * 10 ** 7


def test_a_bid_below_the_ask_is_not_a_trade(trading):
    state, index, keeper, answers, maker, built = trading
    _crossing(index, bid_coins=10 ** 8 - 1)      # a satoshi short of the ask

    assert keeper.fill_what_crosses() == 0
    assert answers == [] and built == []


def test_this_wallet_does_not_take_its_own_ask(trading):
    state, index, keeper, answers, maker, built = trading
    _crossing(index, maker=SELLER)

    assert keeper.fill_what_crosses() == 0 and built == []


def test_a_maker_nobody_can_reach_is_left_alone(trading):
    """Their order stands, but it cannot be negotiated with, and stopping on
    it would let one unreachable wallet block a price for everybody (D-042)."""
    state, index, keeper, answers, maker, built = trading
    _crossing(index, maker=BUYER)               # no key announced for BUYER

    assert keeper.fill_what_crosses() == 0 and built == []


def test_one_fill_at_a_time_per_order(trading):
    state, index, keeper, answers, maker, built = trading
    _crossing(index)

    assert keeper.fill_what_crosses() == 1
    answers.clear(); built.clear()
    assert keeper.fill_what_crosses() == 0, "the first has not landed yet"
    assert answers == [] and built == []


def test_two_bids_at_one_price_are_left_to_a_person(trading):
    """Cancelling by price would take both off and only one would come back."""
    state, index, keeper, answers, maker, built = trading
    _crossing(index)
    twin = dict(index.my_orders[0], txid="bid" + "9" * 61)
    index.my_orders.append(twin)

    assert keeper.fill_what_crosses() == 0 and built == []


def test_nothing_is_taken_when_the_switch_is_off(trading):
    state, index, keeper, answers, maker, built = trading
    _crossing(index)
    state.set_setting("auto_fill", False)

    assert keeper.fill_what_crosses() == 0 and built == []


def test_a_rebuilt_message_store_does_not_leave_the_shopkeeper_deaf(shop):
    """A chain reset moves the message store aside (D-106). Its ids then
    start again at 1 while the swap cursor remembers thousands -- and the
    shopkeeper would answer nothing until the new store grew past the old
    one's last id, without saying so."""
    state, index, node, buyer, answers, keeper = shop
    state.offers.set_cursor("regtest", 4000)

    _ask(state, buyer, {"swap": "offer", "swapv": S.PROTOCOL, "shop": SHOP,
                        "listing": 0, "buyer": BUYER}, 1)
    assert keeper.tick() == 0, "the first pass after a rebuild only re-marks"
    assert state.offers.cursor("regtest") < 4000, "the cursor comes back to earth"

    _ask(state, buyer, {"swap": "offer", "swapv": S.PROTOCOL, "shop": SHOP,
                        "listing": 0, "buyer": BUYER}, 2)
    assert keeper.tick() == 1, "and what arrives after it is answered"
