"""How long a session lasts, and what ends it (issue #13).

2026-10-25, SadFrog: "14 days, renewed by use, plus 'sign out everywhere'"; and a
new password ends the account's other sessions. A session not used for 14 days
ends, one in use is renewed (in the register at most once a day, and the cookie
with it), "Sign out everywhere" ends every one of an account's sessions, and a
re-sealed wallet (/account/vault) ends every one but the browser that sent it.
"""

import json
import pathlib
import sys
import types

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import accounts                                      # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}
DAY = 86400
START = 1_800_000_000


@pytest.fixture
def clock(monkeypatch):
    """The register's own clock, moved by hand; nothing else's time is touched."""
    now = [START]
    monkeypatch.setattr(accounts, "time", types.SimpleNamespace(time=lambda: now[0]))
    return now


def _sign_in(app, key, headers=LOCAL):
    challenge = app.get("/auth/challenge", headers=headers).json()
    signature = key.sign(accounts.login_message(
        challenge["origin"], challenge["nonce"])).signature
    answer = app.post("/auth/login", headers=headers, json={
        "pubkey": key.verify_key.encode().hex(), "nonce": challenge["nonce"],
        "signature": signature.hex(), "join": True})
    assert answer.status_code == 200, answer.text
    return answer


def _who(app, headers=LOCAL):
    return app.get("/auth/who", headers=headers).json()["pubkey"]


def _device(client):
    """A second browser on the same arcade: its own cookie jar."""
    return TestClient(client[0].app)


def _login(register, key, now):
    nonce = register.challenge(now=now)["nonce"]
    return register.login(key.verify_key.encode().hex(), nonce,
                          key.sign(accounts.login_message("", nonce)).signature.hex(),
                          join=True, now=now)


# --- the register --------------------------------------------------------------

def test_a_session_lasts_14_days_and_ends_after_14_idle_days(tmp_path):
    assert accounts.SESSION_DAYS == 14
    register = accounts.Accounts(tmp_path / "a.sqlite")
    token = _login(register, SigningKey.generate(), START)
    assert register.session(token, now=START + 14 * DAY - 60) is not None
    later = _login(register, SigningKey.generate(), START)
    assert register.session(later, now=START + 14 * DAY + 60) is None


def test_using_a_session_renews_it(tmp_path):
    register = accounts.Accounts(tmp_path / "a.sqlite")
    token = _login(register, SigningKey.generate(), START)
    for day in range(10, 100, 10):                 # used every ten days: never ends
        assert register.session(token, now=START + day * DAY) is not None
    assert register.session(token, now=START + 90 * DAY + 15 * DAY) is None


def test_renewal_is_written_at_most_once_a_day(tmp_path):
    register = accounts.Accounts(tmp_path / "a.sqlite")
    token = _login(register, SigningKey.generate(), START)

    def expires():
        return register.conn.execute("SELECT expires FROM session").fetchone()[0]

    assert expires() == START + 14 * DAY
    register.session(token, now=START + 3600)      # an hour later: nothing written
    assert expires() == START + 14 * DAY
    register.session(token, now=START + DAY + 60)  # a day later: moved on
    assert expires() == START + DAY + 60 + 14 * DAY


def test_sessions_written_when_they_lasted_30_days_end_14_days_after_their_last_use(tmp_path):
    """A row from before the upgrade said 'last renewed + 30 days'. Once, and only once, the file is moved to
    the new rule: a long-lived old cookie stops 14 days after it was last used."""
    path = tmp_path / "a.sqlite"
    register = accounts.Accounts(path)
    register.conn.execute("PRAGMA user_version = 0")            # as a file the old code wrote
    register.conn.execute("DELETE FROM session")
    for name, used in (("recent", START - 2 * DAY), ("idle", START - 20 * DAY)):
        register.conn.execute("INSERT INTO session (token_hash, pubkey, made, expires) VALUES (?,?,?,?)",
                              (accounts._hash(name), "ab" * 32, used, used + 30 * DAY))
    register.close()

    upgraded = accounts.Accounts(path)
    rows = dict(upgraded.conn.execute("SELECT token_hash, expires FROM session").fetchall())
    assert rows[accounts._hash("recent")] == START - 2 * DAY + 14 * DAY
    assert rows[accounts._hash("idle")] == START - 20 * DAY + 14 * DAY   # already past: ended
    upgraded.close()

    again = accounts.Accounts(path)                               # a restart takes nothing more off
    assert dict(again.conn.execute("SELECT token_hash, expires FROM session").fetchall()) == rows


def test_end_sessions_keeps_only_the_one_asked_for(tmp_path):
    register = accounts.Accounts(tmp_path / "a.sqlite")
    key, other = SigningKey.generate(), SigningKey.generate()
    here, there, theirs = (_login(register, key, START), _login(register, key, START),
                           _login(register, other, START))
    assert register.end_sessions(key.verify_key.encode().hex(), keep=here) == 1
    assert register.session(here, now=START) is not None
    assert register.session(there, now=START) is None
    assert register.end_sessions(key.verify_key.encode().hex()) == 1
    assert register.session(here, now=START) is None
    assert register.session(theirs, now=START) is not None   # another account's are its own


# --- the routes ----------------------------------------------------------------

def test_the_cookie_is_sent_again_when_the_session_is_renewed(client, clock):
    app, _ = client
    signed = _sign_in(app, SigningKey.generate())
    assert f"Max-Age={14 * DAY}" in signed.headers["set-cookie"]
    quiet = app.get("/auth/who", headers=LOCAL)
    assert quiet.json()["pubkey"] and "arcade_session=" not in quiet.headers.get("set-cookie", "")
    clock[0] += 2 * DAY
    renewed = app.get("/auth/who", headers=LOCAL)
    assert renewed.json()["pubkey"]
    cookie = renewed.headers.get("set-cookie", "")
    assert "arcade_session=" in cookie and f"Max-Age={14 * DAY}" in cookie and "HttpOnly" in cookie


def test_a_session_unused_for_14_days_is_signed_out(client, clock):
    app, _ = client
    _sign_in(app, SigningKey.generate())
    clock[0] += 13 * DAY
    assert _who(app) is not None                   # used: renewed for 14 more days
    clock[0] += 14 * DAY - 60
    assert _who(app) is not None
    clock[0] += 14 * DAY + 60
    assert _who(app) is None


def test_sign_out_everywhere_ends_every_session_of_the_account(client, clock):
    app, state = client
    key = SigningKey.generate()
    phone, stranger = _device(client), _device(client)
    _sign_in(app, key)
    _sign_in(phone, key)
    _sign_in(stranger, SigningKey.generate())
    answer = phone.post("/auth/logout-everywhere", headers=LOCAL)
    assert answer.status_code == 200 and answer.json()["ended"] == 2
    assert _who(phone) is None and _who(app) is None
    assert _who(stranger) is not None              # somebody else's account is untouched


def test_sign_out_everywhere_needs_a_session(client):
    app, _ = client
    assert app.post("/auth/logout-everywhere", headers=LOCAL).status_code == 403


def test_sign_out_everywhere_from_another_site_is_refused(client):
    app, _ = client
    key = SigningKey.generate()
    _sign_in(app, key)
    refused = app.post("/auth/logout-everywhere",
                       headers={**LOCAL, "origin": "https://elsewhere.example"})
    assert refused.status_code == 403
    assert _who(app) == key.verify_key.encode().hex()


def test_sign_out_everywhere_is_served_on_a_public_arcade(client):
    app, state = client
    state.public = True
    try:
        key = SigningKey.generate()
        _sign_in(app, key)
        assert app.post("/auth/logout-everywhere", headers=LOCAL).status_code == 200
        assert _who(app) is None
    finally:
        state.public = False


def test_a_new_password_ends_the_other_sessions_but_not_this_one(client):
    app, state = client
    key = SigningKey.generate()
    pubkey = key.verify_key.encode().hex()
    state.vault().put("apple", pubkey, "nAddress", json.dumps({"v": 1, "old": True}))
    laptop, stranger = _device(client), _device(client)
    _sign_in(laptop, key)
    _sign_in(stranger, SigningKey.generate())
    _sign_in(app, key)                             # the Restore page signs in with the words first
    answer = app.post("/account/vault", headers=LOCAL, json={"blob": {"v": 1, "new": True}})
    assert answer.status_code == 200 and answer.json()["ended"] == 1
    assert _who(app) == pubkey                     # the browser that changed it stays signed in
    assert _who(laptop) is None                    # the one signed in with the old password does not
    assert _who(stranger) is not None
    assert json.loads(state.vault().by_pubkey(pubkey)["blob"]) == {"v": 1, "new": True}


def test_a_new_operator_password_ends_the_operators_other_sessions(client):
    """The operator's username and password (/auth/set-password) are a password too: changing them on the
    node's machine signs out every device signed in with the old one."""
    app, state = client
    edge = {"host": "node.dogecoinarcade.com", "cf-ray": "abc123-LHR"}
    state.set_setting("public_hosts", ["node.dogecoinarcade.com"])
    try:
        claimed = app.post("/auth/set-password", headers=LOCAL,
                           json={"username": "robin", "password": "correct horse battery"})
        assert claimed.status_code == 200
        phone = _device(client)
        opened = phone.post("/auth/password", headers=edge,
                            json={"username": "robin", "password": "correct horse battery"})
        assert opened.status_code == 200
        assert _who(phone, edge) == claimed.json()["operator"]
        changed = app.post("/auth/set-password", headers=LOCAL,
                           json={"username": "robin", "password": "a different horse battery"})
        assert changed.status_code == 200
        assert _who(phone, edge) is None
    finally:
        state.set_setting("public_hosts", [])
        state.set_setting("operator", "")
