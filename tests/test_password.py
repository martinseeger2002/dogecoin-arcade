"""The operator's own door: a username and a password.

The one place in this application where a password is checked, and the
reasoning for that exception is in D-154: the rule it appears to break is
about other people's keys, and a node holding a hash of the password that
guards its own pages creates nothing worth stealing that the machine does
not already hold.
"""

import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import accounts                                      # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}
EDGE = {"host": "node.dogecoinarcade.com", "cf-ray": "abc123-LHR"}


@pytest.fixture
def named(client):
    app, state = client
    state.set_setting("public_hosts", ["node.dogecoinarcade.com"])
    yield app, state
    state.set_setting("public_hosts", [])
    state.set_setting("operator", "")


def _claim(app, username="robin", password="correct horse battery"):
    return app.post("/auth/set-password", headers=LOCAL,
                    json={"username": username, "password": password})


# --- claiming the node ---------------------------------------------------------

def test_a_node_is_claimed_from_the_machine_it_runs_on(named):
    app, state = named
    assert app.get("/auth/door", headers=LOCAL).json()["settable"] is True
    answer = _claim(app)
    assert answer.status_code == 200
    assert answer.json()["username"] == "robin"
    assert state.operator == answer.json()["operator"]
    assert app.get("/auth/door", headers=LOCAL).json() == {
        "password": True, "settable": False,
        "min_password": accounts.MIN_PASSWORD}


def test_it_cannot_be_claimed_from_outside(named):
    """A page that offers this to the internet offers the node away."""
    app, state = named
    refused = app.post("/auth/set-password", headers=EDGE,
                       json={"username": "thief", "password": "0123456789"})
    assert refused.status_code in (403, 404)
    assert state.operator == ""
    assert app.get("/auth/door", headers=EDGE).json()["settable"] is False


def test_a_short_password_is_allowed(named):
    """No floor (D-157). A minimum stops the person who would have chosen
    something short and stops nobody else -- an attacker does not type
    passwords into a form. What the page owes somebody is the truth about
    what they picked, not a refusal, and this is their wallet."""
    app, state = named
    assert _claim(app, password="1234").status_code == 200
    assert state.operator != ""
    app.cookies.clear()
    opened = app.post("/auth/password", headers=EDGE,
                      json={"username": "robin", "password": "1234"})
    assert opened.status_code == 200, "and it opens what it was set on"


def test_no_password_at_all_is_still_refused(named):
    """A password of nothing is not a short password, it is an account
    anybody can open by knowing the name."""
    app, state = named
    refused = _claim(app, password="")
    assert refused.status_code == 400
    assert "even a short one" in refused.json()["detail"]
    assert state.operator == ""


def test_a_username_is_a_name(named):
    app, _ = named
    for bad in ("", "  ", "two words", "no/slashes"):
        assert _claim(app, username=bad).status_code == 400


# --- signing in ----------------------------------------------------------------

def test_the_operator_signs_in_from_anywhere_and_gets_their_wallet(named):
    """The whole point: the arcade, from somewhere other than the room the
    machine is in, exactly as it is on the machine."""
    app, _ = named
    _claim(app)
    app.cookies.clear()

    assert app.get("/wallet", headers=EDGE).status_code == 404, "not yet"
    answer = app.post("/auth/password", headers=EDGE,
                      json={"username": "robin",
                            "password": "correct horse battery"})
    assert answer.status_code == 200
    assert answer.json()["operator"] is True

    for path in ("/wallet", "/messages", "/contacts", "/backup",
                 "/inscriptions/collection", "/approvals"):
        assert app.get(path, headers=EDGE).status_code == 200, path


def test_the_front_page_is_their_own_again(named):
    app, _ = named
    _claim(app)
    app.cookies.clear()
    app.post("/auth/password", headers=EDGE,
             json={"username": "robin", "password": "correct horse battery"})
    body = app.get("/", headers=EDGE).text
    assert "Known contacts" in body, "their overview, not a splash"
    assert 'href="/wallet"' in body, "and their own navigation with it"


def test_the_name_is_one_name_however_it_is_typed(named):
    app, _ = named
    _claim(app, username="the operator")
    for typed in ("robin", "ROBIN", " the operator ", "@robin"):
        app.cookies.clear()
        answer = app.post("/auth/password", headers=EDGE,
                          json={"username": typed,
                                "password": "correct horse battery"})
        assert answer.status_code == 200, typed


def test_a_wrong_password_says_the_same_as_a_wrong_name(named):
    """A login that answers differently tells somebody which names exist."""
    app, _ = named
    _claim(app)
    wrong_password = app.post("/auth/password", headers=EDGE,
                              json={"username": "robin", "password": "nope"})
    wrong_name = app.post("/auth/password", headers=EDGE,
                          json={"username": "nobody", "password": "nope"})
    assert wrong_password.status_code == wrong_name.status_code == 403
    assert wrong_password.json() == wrong_name.json()


def test_a_wrong_name_costs_the_same_work_as_a_wrong_password(named):
    """And takes about as long, or the timing says it instead."""
    app, _ = named
    _claim(app)

    def took(username):
        start = time.monotonic()
        app.post("/auth/password", headers=LOCAL,
                 json={"username": username, "password": "not the password"})
        return time.monotonic() - start

    known, unknown = took("robin"), took("nobody-at-all")
    assert min(known, unknown) > 0.02, "the KDF is actually being run"
    assert abs(known - unknown) < max(known, unknown), \
        "one of them skipped the work"


def test_attempts_are_rate_limited(named):
    app, state = named
    _claim(app)
    for _ in range(accounts.ATTEMPTS):
        app.post("/auth/password", headers=LOCAL,
                 json={"username": "robin", "password": "wrong"})
    stopped = app.post("/auth/password", headers=LOCAL,
                       json={"username": "robin",
                             "password": "correct horse battery"})
    assert stopped.status_code == 403
    assert "too many" in stopped.json()["detail"]


def test_the_password_is_never_stored(named, app_state):
    """scrypt with a per-account salt. The file holds a hash and the
    parameters it was made with, so they can be raised later without
    locking anybody out of a wallet they still know the password to."""
    app, state = named
    _claim(app, password="correct horse battery")
    row = state.credentials().named("robin")
    assert "correct horse battery" not in str(row)
    assert row["n"] == accounts.SCRYPT_N and row["r"] == accounts.SCRYPT_R
    assert len(row["hash"]) == 64 and len(row["salt"]) == 32
    raw = (app_state.home / "accounts.sqlite").read_bytes()
    assert b"correct horse battery" not in raw


def test_two_nodes_with_one_password_do_not_share_a_hash(client):
    """Per-account salt, so a table of precomputed answers is worthless."""
    app, state = client
    creds = state.credentials()
    creds.set("one", "correct horse battery", "aa" * 32)
    first = creds.named("one")
    creds.set("two", "correct horse battery", "bb" * 32)
    second = creds.named("two")
    assert first["salt"] != second["salt"]
    assert first["hash"] != second["hash"]


def test_a_stranger_who_signs_in_with_words_is_still_a_stranger(named):
    """The seat login and the operator login are different doors into
    different places, and holding one does not open the other."""
    from nacl.signing import SigningKey

    app, _ = named
    _claim(app)
    app.cookies.clear()

    key = SigningKey.generate()
    challenge = app.get("/auth/challenge", headers=EDGE).json()
    signature = key.sign(accounts.login_message(
        challenge["origin"], challenge["nonce"])).signature
    seated = app.post("/auth/login", headers=EDGE, json={
        "pubkey": key.verify_key.encode().hex(), "nonce": challenge["nonce"],
        "signature": signature.hex(), "join": True})
    assert seated.status_code == 200, "they get a seat"
    assert app.get("/wallet", headers=EDGE).status_code == 404, "and no wallet"
