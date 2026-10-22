"""A page names a send with `ref`, so asking again never sends twice (2026-10-21).

A game's POST /r/send reached the node, Safari lost the reply ("Load failed"), and the game
asked again with a fresh POST: 1,000 GOLD to a bank that credits only one transaction could
have been paid twice. Asked again with the same ref, by the same wallet on the same chain,
the arcade answers with the first request as it stands now (200, "repeat": true).
"""

import contextlib
import pathlib
import re
import sys
import threading
import types

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import approvals as A                                # noqa: E402
from arcade import send_refs as R                                # noqa: E402
from arcade.config import NETWORKS                               # noqa: E402
from arcade.script import b58check_encode                        # noqa: E402
from arcade.web import app as appmod                             # noqa: E402
from arcade.web.app import create_app                            # noqa: E402
from arcade.web.state import ChainContext                        # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}
PAGE = "d4" * 32


def _addr(n: int, chain) -> str:
    return b58check_encode(chain.params.pubkeyhash_version, bytes([n]) * 20)


def _waiting(state) -> int:
    return len(state.approvals.pending())


# --- the node's own wallet (the approvals queue) ----------------------------------------

def test_a_repeat_returns_the_first_request_and_files_nothing(client):
    app, state = client
    to = _addr(7, state.token_chain)
    body = {"kind": "coins", "to": to, "amount": "1.5", "ref": "bank-deposit-42"}
    first = app.post("/r/send", json=body)
    assert first.status_code == 202, first.text
    assert "repeat" not in first.json()
    again = app.post("/r/send", json=body)
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"] and again.json()["repeat"] is True
    assert again.json()["status"] == "pending"
    assert _waiting(state) == 1, "the repeat filed nothing"
    # Without a ref, today's behaviour: every POST is a request of its own.
    plain = {k: v for k, v in body.items() if k != "ref"}
    assert app.post("/r/send", json=plain).json()["id"] != first.json()["id"]
    assert _waiting(state) == 2
    # Decided since: the repeat says so (the queue's live row, not a stale copy).
    state.approvals.decide(first.json()["id"], "denied")
    late = app.post("/r/send", json=body).json()
    assert late["status"] == "denied" and late["repeat"] is True
    assert app.get(f"/r/send/{first.json()['id']}").json()["status"] == "denied"


@pytest.mark.parametrize("ref", ["", "x" * 65, 42, ["a"], {"a": 1}])
def test_a_ref_outside_the_limit_is_refused(client, ref):
    app, state = client
    said = app.post("/r/send", json={"kind": "coins", "to": _addr(7, state.token_chain),
                                     "amount": "1", "ref": ref})
    assert said.status_code == 400 and "1 to 64 characters" in said.json()["error"]
    assert _waiting(state) == 0


def test_a_ref_of_64_characters_and_a_null_ref_are_taken(client):
    app, state = client
    to = _addr(7, state.token_chain)
    assert app.post("/r/send", json={"kind": "coins", "to": to, "amount": "1",
                                     "ref": "x" * 64}).status_code == 202
    assert app.post("/r/send", json={"kind": "coins", "to": to, "amount": "1",
                                     "ref": None}).status_code == 202


def test_a_refusal_is_not_remembered_so_the_corrected_retry_is_filed(client):
    app, state = client
    bad = app.post("/r/send", json={"kind": "coins", "to": _addr(7, state.token_chain),
                                    "amount": "x", "ref": "r1"})
    assert bad.status_code == 400
    good = app.post("/r/send", json={"kind": "coins", "to": _addr(7, state.token_chain),
                                     "amount": "1", "ref": "r1"})
    assert good.status_code == 202 and "repeat" not in good.json()
    assert _waiting(state) == 1


def test_two_posts_at_once_file_one_request(client, monkeypatch):
    """The second waits for the first (its ref's lock) and gets its answer."""
    app, state = client
    body = {"kind": "coins", "to": _addr(7, state.token_chain), "amount": "1", "ref": "race"}
    real = A.Requests.file
    started, go = threading.Event(), threading.Event()

    def slow_file(self, *a, **k):
        started.set()
        go.wait(5)
        return real(self, *a, **k)

    monkeypatch.setattr(A.Requests, "file", slow_file)
    answers = []
    first = threading.Thread(target=lambda: answers.append(app.post("/r/send", json=body)))
    first.start()
    assert started.wait(5)
    second = threading.Thread(target=lambda: answers.append(app.post("/r/send", json=body)))
    second.start()
    second.join(0.5)
    assert second.is_alive(), "the second POST waits for the first"
    go.set()
    first.join(5)
    second.join(5)
    assert sorted(r.status_code for r in answers) == [200, 202]
    assert len({r.json()["id"] for r in answers}) == 1
    assert _waiting(state) == 1


def test_the_lock_is_let_go_and_other_refs_do_not_wait(tmp_path):
    refs = R.SendRefs(tmp_path / "s.sqlite")
    done = threading.Event()

    def other():
        with refs.held(("node", "test", "b")):
            done.set()

    with refs.held(("node", "test", "a")):
        threading.Thread(target=other).start()
        assert done.wait(2), "another ref is not held up"
    assert refs._locks == {} or refs._locks.keys() <= {("node", "test", "b")}
    with refs.held(("node", "test", "a")):
        pass                                             # let go: taken again at once


def test_refs_are_kept_seven_days_and_on_disk(tmp_path, monkeypatch):
    refs = R.SendRefs(tmp_path / "s.sqlite")
    refs.remember("node", "test", "a", request_id="r1", source="node", told={"id": "r1"})
    again = R.SendRefs(tmp_path / "s.sqlite")            # a restart
    assert again.find("node", "test", "a")["request_id"] == "r1"
    assert again.find("node", "main", "a") is None, "another chain is another ref"
    assert again.find("nAddr", "test", "a") is None, "another wallet is another ref"
    now = R.time.time()
    monkeypatch.setattr(R.time, "time", lambda: now + R.KEEP_FOR + 1)
    assert again.find("node", "test", "a") is None and again.by_id("r1") is None


# --- the silent path: sent at once, or failed at once -----------------------------------

class _Index:
    def __init__(self, creator):
        self.creator = creator

    def inscription(self, key):
        return {"txid": PAGE, "number": 9, "creator": self.creator, "owner": self.creator} \
            if key == PAGE else None

    def property(self, pid):
        return {"property_id": pid, "name": "GOLD", "divisible": False,
                "issuer": self.creator} if pid == 5 else None

    def address_of(self, tag):
        return None


def test_a_silent_repeat_does_not_broadcast_twice(client, monkeypatch):
    app, state = client
    chain = state.token_chain
    creator = _addr(3, chain)
    monkeypatch.setattr(state, "token_index", lambda c: _Index(creator))
    monkeypatch.setattr(ChainContext, "rpc", lambda self: contextlib.nullcontext(object()))
    monkeypatch.setattr(appmod, "_ledger_addresses", lambda rpc, fresh=False: [])
    monkeypatch.setattr(A, "prepare", lambda *a, **k: types.SimpleNamespace(
        fee_coins=0.001, what="1000 GOLD"))
    sent = []
    monkeypatch.setattr(A, "broadcast", lambda rpc, prepared: sent.append(1) or "ab" * 32)
    body = {"kind": "token", "to": creator, "propertyid": 5, "amount": "1000",
            "silent": True, "ref": "gold-1000"}
    head = {"referer": f"http://testserver/content/{PAGE}"}
    first = app.post("/r/send", json=body, headers=head)
    assert first.status_code == 202 and first.json()["status"] == "sent", first.text
    again = app.post("/r/send", json=body, headers=head)
    assert again.status_code == 200 and again.json()["repeat"] is True
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["status"] == "sent" and again.json()["txid"] == "ab" * 32
    assert sent == [1], "broadcast once"


def test_a_silent_send_that_failed_is_remembered_as_answered(client):
    app, state = client
    body = {"kind": "coins", "to": _addr(7, state.token_chain), "amount": "1",
            "silent": True, "ref": "nope"}
    first = app.post("/r/send", json=body)
    assert first.status_code == 202 and "say-so" in first.json()["error"]
    again = app.post("/r/send", json=body)
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert again.json()["status"] == "failed" and "say-so" in again.json()["error"]
    assert len(state.approvals.recent()) == 1, "filed once"


# --- a signed-in reader on a public arcade ----------------------------------------------

def _reader(app, state, address: str) -> dict:
    """Sign a fresh account in, give it `address`, open the page: its frame's headers."""
    from test_me_page import _seat
    _seat(app)
    app.post("/account/address", json={"address": address}, headers=LOCAL)
    page = app.get(f"/inscriptions/{PAGE}/full").text
    ticket = re.search(rf"/content/{PAGE}\?v=([A-Za-z0-9_-]+)", page).group(1)
    return {"host": "pages.example", "cf-ray": "abc",
            "referer": f"https://pages.example/content/{PAGE}?v={ticket}"}


@pytest.fixture
def public(client):
    app, state = client
    index = state.token_index(state.token_chain)
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,block_height,"
            "position,content_type,content_len,sha256,json,chunks,content) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (PAGE, 931, "nMe", "nSomebodyElse", 100, 0, "text/html", 2, "ab" * 32,
             None, 1, b"hi"))
        db.conn.commit()
    state.set_setting("pages_host", "pages.example")
    was, state.public = state.public, True
    yield app, state
    state.public = was


ASK = {"kind": "coins", "to": "@vex", "amount": "1000", "label": "ASHVALE", "ref": "bank-7"}


def test_a_readers_repeat_is_the_same_question_and_refs_are_per_reader(public):
    app, state = public
    first_reader = _reader(app, state, "mqxyzWHvgSMmDYPg9aWpcmXWnkouLUDbWg")
    one = app.post("/r/send", json=ASK, headers=first_reader)
    assert one.status_code == 202, one.text
    again = app.post("/r/send", json=ASK, headers=first_reader)
    assert again.status_code == 200 and again.json()["id"] == one.json()["id"]
    assert again.json()["repeat"] is True
    assert len(app.get("/account/pagesends").json()["requests"]) == 1, "asked once"

    second_reader = _reader(app, state, _addr(11, state.token_chain))
    two = app.post("/r/send", json=ASK, headers=second_reader)
    assert two.status_code == 202 and two.json()["id"] != one.json()["id"], \
        "the same ref from another wallet is another request"

    # Nobody signed in: told so, as before, and nothing is remembered.
    stranger = {"host": "pages.example", "cf-ray": "abc"}
    assert app.post("/r/send", json=ASK, headers=stranger).status_code == 403


def test_a_repeat_after_the_reader_answered_shows_the_answer(public, monkeypatch):
    app, state = public
    head = _reader(app, state, "mqxyzWHvgSMmDYPg9aWpcmXWnkouLUDbWg")
    rid = app.post("/r/send", json=ASK, headers=head).json()["id"]

    class Rpc:
        def call(self, method, *args):
            if method == "gettransaction":
                return {"confirmations": 2, "blockhash": "bb" * 32}
            return {"height": 9}

    monkeypatch.setattr(ChainContext, "rpc", lambda self: contextlib.nullcontext(Rpc()))
    assert app.post("/account/pagesends/answer",
                    json={"id": rid, "status": "sent", "txid": "cd" * 32}).status_code == 200
    again = app.post("/r/send", json=ASK, headers=head).json()
    assert again["repeat"] is True and again["id"] == rid
    assert again["status"] == "sent" and again["txid"] == "cd" * 32
    assert again["confirmations"] == 2


def test_a_poll_after_a_restart_still_gets_the_last_answer(public):
    app, state = public
    head = _reader(app, state, "mqxyzWHvgSMmDYPg9aWpcmXWnkouLUDbWg")
    rid = app.post("/r/send", json=ASK, headers=head).json()["id"]
    assert app.post("/account/pagesends/answer",
                    json={"id": rid, "status": "denied"}).status_code == 200
    # A restart: a new app on the same home; the questions in memory are gone.
    # (The frame's ticket is in memory too, so the page is opened again first.)
    cookies = app.cookies
    app = TestClient(create_app(state), cookies=cookies)
    head = {**head, "referer": re.sub(r"\?v=.*", "", head["referer"])}
    page = app.get(f"/inscriptions/{PAGE}/full").text
    ticket = re.search(rf"/content/{PAGE}\?v=([A-Za-z0-9_-]+)", page).group(1)
    head["referer"] += f"?v={ticket}"
    polled = app.get(f"/r/send/{rid}", headers=head)
    assert polled.status_code == 200, polled.text
    assert polled.json()["id"] == rid and polled.json()["status"] == "denied"
    again = app.post("/r/send", json=ASK, headers=head)
    assert again.status_code == 200 and again.json()["id"] == rid
    assert again.json()["status"] == "denied" and again.json()["repeat"] is True
    assert app.get("/r/send/nope", headers=head).status_code == 404
