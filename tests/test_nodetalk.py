"""An inscribed page talking to another node, and hearing back.

It sends as this node, at once and without a queue -- a message moves no
value and lives on testnet -- and reads only the answers to what it sent.
"""

import pathlib
import sys
import time

import pytest

from arcade import nodetalk as N
from arcade.messaging.keys import Identity
from arcade.messaging.store import MessageStore

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

PAGE, OTHER = "ab" * 32, "cd" * 32
SHOP, BANK = bytes([1]) * 32, bytes([2]) * 32
ME = "fp-me"


@pytest.fixture(autouse=True)
def on_mainnet(app_state):
    """This file's fixtures write the MAINNET ledger, so the pages are asked
    about that chain explicitly. A wallet with no choice recorded opens on
    the chain its identity is on now (D-134); before that these inherited a
    default."""
    (app_state.home / "tokens-chain").write_text("main\n")
    app_state._token_chain = None
    yield

def _heard(store, who, body, n):
    return store.add_api_message("regtest", f"tx{n}", 100 + n, 1000 + n, "nThem",
                                 who, ME, body, protocol=1, fingerprint=b"\0" * 4)


def test_a_page_reads_only_what_the_nodes_it_wrote_to_said_afterwards(tmp_path):
    talk = N.Talk(tmp_path / "talk.sqlite")
    with MessageStore(tmp_path / "m.sqlite") as store:
        _heard(store, SHOP, b"before anybody wrote", 1)
        assert N.replies(store, talk, PAGE, "regtest", ME) == [], "wrote to nobody yet"

        newest = store.conn.execute("SELECT MAX(id) FROM api_message").fetchone()[0]
        talk.record(PAGE, "regtest", SHOP.hex(), "sent1", newest)
        _heard(store, SHOP, b'{"hat":"yours"}', 2)
        _heard(store, BANK, b"not for this page", 3)
        _heard(store, SHOP, b"and a receipt", 4)

        got = N.replies(store, talk, PAGE, "regtest", ME)
        assert [r["body"] for r in got] == ['{"hat":"yours"}', "and a receipt"]
        assert got[0]["json"] == {"hat": "yours"} and got[0]["frompubkey"] == SHOP.hex()
        assert got[0]["compatible"] is False, "reported, not enforced"
        # The page's cursor, and the limit, both work the way a bot's do.
        assert [r["body"] for r in N.replies(store, talk, PAGE, "regtest", ME,
                                              after=got[0]["id"])] == ["and a receipt"]
        assert len(N.replies(store, talk, PAGE, "regtest", ME, limit=1)) == 1
        assert N.replies(store, talk, OTHER, "regtest", ME) == [], "another page, nothing"
        # Writing to the bank now opens the bank's replies -- from now on.
        talk.record(PAGE, "regtest", BANK.hex(), "sent2",
                    store.conn.execute("SELECT MAX(id) FROM api_message").fetchone()[0])
        _heard(store, BANK, b"balance: 3", 5)
        assert [r["body"] for r in N.replies(store, talk, PAGE, "regtest", ME)] == [
            '{"hat":"yours"}', "and a receipt", "balance: 3"]
    assert [l["txid"] for l in talk.letters(PAGE, "regtest")] == ["sent2", "sent1"]
    assert talk.sent_lately(PAGE, "regtest") == 2 and talk.sent_lately(OTHER, "regtest") == 0


def test_a_recipient_is_a_key_or_a_contact_code():
    from arcade.messaging import contact
    assert N.parse_pubkey(SHOP.hex()) == SHOP
    assert N.parse_pubkey(contact.encode("test", SHOP)) == SHOP
    for bad, why in (("", "name who"), ("zz", "64 hex"), ("ab" * 31, "32 bytes")):
        with pytest.raises(N.TalkError, match=why):
            N.parse_pubkey(bad)
    assert N.body_bytes({"a": 1}) == b'{"a":1}' and N.body_bytes("hi") == b"hi"
    with pytest.raises(N.TalkError):
        N.body_bytes(7)


# --- through the wallet -------------------------------------------------------

def _page(state):
    from arcade.db import Database
    from arcade.state import install_schema
    db = Database(state.home / "main-ledger.sqlite")
    install_schema(db)
    db.conn.execute(
        "INSERT OR IGNORE INTO inscription(txid,number,creator,owner,block_height,position,"
        "content_type,content_len,sha256,json,chunks,content) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (PAGE, 1, "nMe", "nMe", 100, 0, "text/html", 5, "ef" * 32, "", 1, b"<p>hi"))
    db.conn.commit()
    db.close()


def _fake_node(monkeypatch, state, sent):
    class FakeSender:
        def __init__(self, *a, **k):
            pass

        def prepare(self, address, payload):
            sent["payload"] = payload
            return type("P", (), {"fee_sats": 1000, "total_sats": 1000, "size": 300,
                                  "txid": "tx"})()

        def broadcast(self, prepared):
            sent["n"] = sent.get("n", 0) + 1
            return f"tx-{sent['n']}"

    class FakeRpc:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr("arcade.web.app.MessageSender", FakeSender)
    monkeypatch.setattr("arcade.web.app.funded_address", lambda *a, **k: "nAddr")
    monkeypatch.setattr(state.messaging, "rpc", lambda: FakeRpc())
    state.identity = Identity.generate()
    state.ensure_identity = lambda: state.identity


def test_a_page_sends_as_this_node_and_hears_the_answer(monkeypatch, client):
    app, state = client
    _page(state)
    sent = {}
    _fake_node(monkeypatch, state, sent)
    shop = Identity.generate()
    door, csrf = f"/node/{PAGE}", state.csrf_token

    assert app.post(door, json={"op": "send", "to": shop.public_bytes.hex(),
                                "body": "x"}).status_code == 400, "the viewer's token or nothing"
    assert app.post(f"/node/{OTHER}", json={"csrf_token": csrf, "op": "identity"}).status_code == 404
    assert "access-control-allow-origin" not in app.post(
        door, json={"csrf_token": csrf, "op": "identity"}).headers, "same origin only"

    me = app.post(door, json={"csrf_token": csrf, "op": "identity"}).json()
    assert me["ok"] and me["pubkey"] == state.identity.public_bytes.hex()
    assert me["network"] == "regtest" and me["maxbytes"] > 7000

    r = app.post(door, json={"csrf_token": csrf, "op": "send", "to": shop.public_bytes.hex(),
                             "body": {"order": "hat"}}).json()
    assert r["ok"] and r["txid"] == "tx-1" and r["fromaddress"] == "nAddr"
    assert r["fee"] == "0.00001000" and r["size"] == 300
    # Sealed to the shop, from this node, and it says what the page said.
    from arcade.messaging import api
    sender, body = api.open_payload(shop, sent["payload"])
    assert sender == state.identity.public_bytes and body == b'{"order":"hat"}'
    with pytest.raises(Exception):
        api.open_payload(Identity.generate(), sent["payload"])

    # The shop answers this node; the page reads it. Somebody else's answer
    # to somebody else stays out of sight.
    from arcade.messaging.keys import fingerprint_of
    with state.store() as store:
        store.add_api_message("regtest", "t1", 5, 5, "nShop", shop.public_bytes,
                              fingerprint_of(state.identity.public_bytes), b'{"hat":"sent"}')
        store.add_api_message("regtest", "t2", 5, 5, "nOther", Identity.generate().public_bytes,
                              fingerprint_of(state.identity.public_bytes), b"secret bot traffic")
    got = app.post(door, json={"csrf_token": csrf, "op": "replies"}).json()
    assert got["ok"] and [x["json"] for x in got["replies"]] == [{"hat": "sent"}]
    assert app.post(door, json={"csrf_token": csrf, "op": "replies",
                                "after": got["replies"][0]["id"]}).json()["replies"] == []
    assert app.post(f"/node/{OTHER}", json={"csrf_token": csrf, "op": "replies"}).status_code == 404
    letters = app.post(door, json={"csrf_token": csrf, "op": "sent"}).json()["sent"]
    assert [l["txid"] for l in letters] == ["tx-1"] and letters[0]["to"] == shop.public_bytes.hex()

    # What is refused, and why, in words.
    for body, why in (({"op": "send", "to": "nope", "body": "x"}, "64 hex"),
                      ({"op": "send", "to": shop.public_bytes.hex(), "body": ""}, "nothing to send"),
                      ({"op": "send", "to": shop.public_bytes.hex(), "body": "x" * 9000}, "one transaction"),
                      ({"op": "send", "to": shop.public_bytes.hex(), "body": 7}, "string or a JSON"),
                      ({"op": "dance"}, "op must be")):
        r = app.post(door, json={"csrf_token": csrf, **body})
        assert r.status_code == 400 and why in r.json()["error"], body
    assert sent["n"] == 1, "nothing refused was sent"


def test_a_page_may_send_thirty_an_hour_and_never_on_mainnet(monkeypatch, client):
    app, state = client
    _page(state)
    sent = {}
    _fake_node(monkeypatch, state, sent)
    csrf, shop = state.csrf_token, Identity.generate().public_bytes.hex()
    for _ in range(N.MAX_PER_HOUR):
        assert app.post(f"/node/{PAGE}", json={"csrf_token": csrf, "op": "send",
                                              "to": shop, "body": "ping"}).json()["ok"]
    r = app.post(f"/node/{PAGE}", json={"csrf_token": csrf, "op": "send", "to": shop, "body": "ping"})
    assert r.status_code == 400 and "in the last hour" in r.json()["error"]
    assert sent["n"] == N.MAX_PER_HOUR
    # An hour later it may again; another page was never counted.
    later = time.time() + N.WINDOW + 1
    monkeypatch.setattr(N.time, "time", lambda: later)
    assert app.post(f"/node/{PAGE}", json={"csrf_token": csrf, "op": "send",
                                          "to": shop, "body": "ping"}).json()["ok"]

    monkeypatch.setattr(state.messaging, "network", "main")
    r = app.post(f"/node/{PAGE}", json={"csrf_token": csrf, "op": "send", "to": shop, "body": "x"})
    assert r.status_code == 400 and "testnet only" in r.json()["error"]
    assert sent["n"] == N.MAX_PER_HOUR + 1


def test_the_shim_and_the_bridge_are_there(client):
    app, state = client
    shim = app.get("/r/node.js")
    assert shim.status_code == 200 and "javascript" in shim.headers["content-type"]
    assert shim.headers["access-control-allow-origin"] == "*"
    for word in ("arcade.node", "send:", "replies:", "listen:", "identity:"):
        assert word in shim.text
    _page(state)
    view = app.get(f"/inscriptions/{PAGE}/view").text
    assert "node: '/node/'" in view and "storage: '/storage/'" in view
