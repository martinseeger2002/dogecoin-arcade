"""The shop through the wallet: the buyer's door, the approval, the hand-off
to the shop's node -- and the owner's door, which sends without asking."""

import json
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
