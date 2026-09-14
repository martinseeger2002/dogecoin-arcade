"""Sends asked for by a page or a bot, and the user's yes or no.

Neither caller may spend. What each gets is a request in a queue; what the
person gets is the transaction it would be, decoded, and two buttons.
"""

import pathlib
import sys
import time

import pytest

from arcade import approvals as A
from arcade.config import NETWORKS
from arcade.script import b58check_encode

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

TESTNET = b58check_encode(NETWORKS["test"].pubkeyhash_version, bytes([7]) * 20)
TESTNET2 = b58check_encode(NETWORKS["test"].pubkeyhash_version, bytes([9]) * 20)
MAINNET = b58check_encode(NETWORKS["main"].pubkeyhash_version, bytes([7]) * 20)


class FakeIndex:
    def __init__(self):
        self.props = {7: {"property_id": 7, "name": "Arcade", "divisible": True}}
        self.rows = {"a" * 64: {"txid": "a" * 64, "number": 3, "owner": TESTNET2}}

    def property(self, pid):
        return self.props.get(pid)

    def address_of(self, tag):
        return TESTNET if tag == "@bob" else None

    def inscription(self, key):
        if key == 3:
            return self.rows["a" * 64]
        return self.rows.get(key)


# --- the queue ---------------------------------------------------------------

def test_a_request_waits_then_is_decided_once(tmp_path):
    queue = A.Requests(tmp_path / "q.sqlite")
    rid = queue.file("test", "coins", "page", TESTNET, units=150_000_000,
                     amount="1.5", label="Shop <b>x</b>", note="for the hat")
    row = queue.get(rid)
    assert row["status"] == "pending" and row["amount"] == "1.5"
    assert row["label"] == "Shop <b>x</b>", "shown in quotes, never as markup"
    assert queue.waiting() == 1 and [r["id"] for r in queue.pending()] == [rid]
    assert A.summary(row) == f"1.5 coins to {TESTNET}"

    assert queue.decide(rid, "sent", txid="f" * 64) is True
    assert queue.decide(rid, "denied") is False, "decided once"
    assert queue.get(rid)["status"] == "sent" and queue.get(rid)["txid"] == "f" * 64
    assert queue.waiting() == 0 and queue.recent()[0]["id"] == rid
    with pytest.raises(ValueError):
        queue.decide(rid, "pending")


def test_a_request_nobody_answers_expires(tmp_path, monkeypatch):
    queue = A.Requests(tmp_path / "q.sqlite")
    rid = queue.file("test", "coins", "rpc", TESTNET, units=1, amount="0.00000001")
    later = time.time() + A.TTL + 1
    monkeypatch.setattr(A.time, "time", lambda: later)
    assert queue.get(rid)["status"] == "expired"
    assert queue.pending() == []
    assert queue.decide(rid, "sent", txid="f" * 64) is False, "too late to approve"


def test_the_queue_has_a_ceiling(tmp_path):
    queue = A.Requests(tmp_path / "q.sqlite")
    for _ in range(A.MAX_PENDING):
        queue.file("test", "coins", "page", TESTNET, units=1, amount="0.00000001")
    with pytest.raises(A.RequestError, match="already waiting"):
        queue.file("test", "coins", "page", TESTNET, units=1, amount="0.00000001")


def test_a_note_is_cut_to_length_and_kept_printable(tmp_path):
    queue = A.Requests(tmp_path / "q.sqlite")
    rid = queue.file("test", "coins", "page", TESTNET, units=1, amount="1",
                     note="x" * 900 + "\x00\x07", label="\tnew\nline")
    row = queue.get(rid)
    assert len(row["note"]) == A.MAX_TEXT and "\x00" not in row["note"]
    assert row["label"] == "newline"


# --- what is refused before anybody is asked ---------------------------------

def test_validation_refuses_what_can_be_refused_at_once():
    index = FakeIndex()
    own = [TESTNET2]
    ok = A.validate("coins", index, own, mainnet=False, to=TESTNET, amount="2.5")
    assert ok == {"toaddress": TESTNET, "units": 250_000_000, "amount": "2.5"}

    with pytest.raises(A.RequestError, match="main"):
        A.validate("coins", index, own, mainnet=False, to=MAINNET, amount="1")
    with pytest.raises(A.RequestError, match="than zero"):
        A.validate("coins", index, own, mainnet=False, to=TESTNET, amount="0")
    with pytest.raises(A.RequestError, match="amount"):
        A.validate("coins", index, own, mainnet=False, to=TESTNET, amount="lots")
    with pytest.raises(A.RequestError, match="can sign for"):
        A.validate("coins", index, own, mainnet=False, to=TESTNET, amount="1",
                   fromaddress=TESTNET)
    with pytest.raises(A.RequestError, match="kind must be"):
        A.validate("nft", index, own, mainnet=False, to=TESTNET)

    tagged = A.validate("token", index, own, mainnet=False, to="@bob",
                        propertyid="7", amount="1.25")
    assert tagged["toaddress"] == TESTNET and tagged["totag"] == "bob"
    assert tagged["units"] == 125_000_000 and tagged["propertyname"] == "Arcade"
    with pytest.raises(A.RequestError, match="nobody holds"):
        A.validate("token", index, own, mainnet=False, to="@nope", propertyid=7, amount="1")
    with pytest.raises(A.RequestError, match="no token 8"):
        A.validate("token", index, own, mainnet=False, to=TESTNET, propertyid=8, amount="1")
    with pytest.raises(A.RequestError, match="propertyid"):
        A.validate("token", index, own, mainnet=False, to=TESTNET, propertyid="x", amount="1")

    held = A.validate("inscription", index, own, mainnet=False, to=TESTNET, inscription="3")
    assert held == {"toaddress": TESTNET, "inscription": "a" * 64, "number": 3,
                    "fromaddress": TESTNET2}
    with pytest.raises(A.RequestError, match="no such inscription"):
        A.validate("inscription", index, own, mainnet=False, to=TESTNET, inscription="9")
    with pytest.raises(A.RequestError, match="not this wallet's"):
        A.validate("inscription", index, [TESTNET], mainnet=False, to=TESTNET2, inscription="3")
    with pytest.raises(A.RequestError, match="already holds"):
        A.validate("inscription", index, own, mainnet=False, to=TESTNET2, inscription="3")


# --- the page API and the pages, without a node ------------------------------

def test_a_page_files_a_request_and_the_wallet_shows_it(client):
    app, state = client
    # Addresses for the chain the pages are on, and one from the other chain.
    here, elsewhere = ((MAINNET, TESTNET) if state.token_chain.is_mainnet
                       else (TESTNET, MAINNET))
    assert app.options("/r/send").status_code == 204
    assert app.options("/r/send").headers["access-control-allow-origin"] == "*"

    bad = app.post("/r/send", json={"kind": "coins", "to": elsewhere, "amount": "1"})
    assert bad.status_code == 400 and "address, not a" in bad.json()["error"]
    assert bad.headers["access-control-allow-origin"] == "*"
    assert app.post("/r/send", json={"kind": "coins", "to": here, "amount": "x"}).status_code == 400
    assert app.get("/r/send/nope").status_code == 404
    assert "waiting for your approval" not in app.get("/").text

    filed = app.post("/r/send", json={"kind": "coins", "to": here, "amount": "1.5",
                                      "label": "Hat Shop", "note": "one red hat"})
    assert filed.status_code == 202, filed.text
    body = filed.json()
    assert body["status"] == "pending" and body["origin"] == "page"
    assert body["amount"] == "1.5" and body["to"] == here
    rid = body["id"]
    assert app.get(f"/r/send/{rid}").json()["status"] == "pending"

    assert "1 send waiting for your approval" in app.get("/").text
    assert app.get("/approvals/waiting").json()["waiting"] == 1
    page = app.get("/approvals").text
    assert "1.5 coins to" in page and "Hat Shop" in page and "one red hat" in page
    assert "an inscribed page" in page

    # Looking at it without a node: no transaction to show, no traceback,
    # and it can still be refused.
    look = app.get(f"/approvals/{rid}")
    assert look.status_code == 200 and "Traceback" not in look.text
    assert "could not be built" in look.text
    assert "Approve and send" not in look.text, "nothing to approve without a transaction"

    # Approving what was never shown is refused.
    yes = app.post(f"/approvals/{rid}", data={"csrf_token": state.csrf_token,
                                              "confirmed": "f" * 64})
    assert "no longer held" in yes.text
    assert app.get(f"/r/send/{rid}").json()["status"] == "pending"

    no = app.post(f"/approvals/{rid}", data={"csrf_token": state.csrf_token,
                                             "decision": "deny"})
    assert no.status_code == 200 and str(no.url).endswith("/approvals")
    assert "Refused: 1.5 coins" in no.text
    assert app.get(f"/r/send/{rid}").json()["status"] == "denied"
    assert app.get("/approvals/waiting").json()["waiting"] == 0
    assert "denied" in app.get("/approvals").text
    assert app.post(f"/approvals/{rid}", data={"decision": "deny"}).status_code == 400, "csrf"


def test_the_viewer_watches_for_requests():
    source = pathlib.Path("arcade/web/templates/inscription_view.html").read_text()
    assert "/approvals/waiting" in source and "setInterval" in source
    assert "Look before anything goes out" in source


def test_a_payment_to_ones_own_address_is_told_apart_from_its_change():
    """The recipient can be one of the wallet's own addresses, and then the
    change comes back to the same place. On the live wallet both outputs read
    "the recipient" and From was blank; the amount is what tells them apart."""
    decoded = {"vout": [
        {"value": 1.0, "scriptPubKey": {"addresses": [TESTNET]}},
        {"value": 0.32434, "scriptPubKey": {"addresses": [TESTNET]}},
    ]}
    rows = A._plain_outputs(decoded, TESTNET, None, 100_000_000)
    assert [r["is_recipient"] for r in rows] == [True, False]
    assert [r["is_change"] for r in rows] == [False, True]
    # And with the change first, since the node orders outputs as it likes.
    rows = A._plain_outputs({"vout": decoded["vout"][::-1]}, TESTNET, None, 100_000_000)
    assert [r["is_recipient"] for r in rows] == [False, True]
    assert [r["is_change"] for r in rows] == [True, False]
