"""The operator's admin panel.

2026-09-25: at the node machine it needs no password and is where the
remote password is set; from anywhere else it needs the operator account AND
that password, and sending the node's coins asks for the password again. The
operator is otherwise an account like any other.
"""

import pathlib
import sys

import pytest
from nacl.signing import SigningKey

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_auth_web import _sign_in                               # noqa: E402

from arcade import adminwindow                                   # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}
EDGE = {"host": "node.dogecoinarcade.com", "cf-ray": "abc-LHR"}
WRITE = {"x-arcade-admin": "1"}
PASSWORD = "correct horse battery"


@pytest.fixture
def public(client):
    app, state = client
    state.set_setting("public_hosts", ["node.dogecoinarcade.com"])
    yield app, state
    state.set_setting("public_hosts", [])


def _operator(app, state):
    """An account, made the operator at the node machine, with a password."""
    key = SigningKey.generate()
    assert _sign_in(app, key, headers=EDGE).status_code == 200
    pubkey = key.verify_key.encode().hex()
    assert app.post("/admin/api/operator", headers={**LOCAL, **WRITE},
                    json={"pubkey": pubkey}).status_code == 200
    assert app.post("/admin/api/password", headers={**LOCAL, **WRITE},
                    json={"password": PASSWORD}).status_code == 200
    return pubkey


def test_at_the_node_machine_it_opens_without_a_password(public):
    app, state = public
    page = app.get("/admin", headers=LOCAL)
    assert page.status_code == 200 and "The node's coins" in page.text
    got = app.get("/admin/api/state", headers=LOCAL).json()
    assert got["local"] is True
    assert {"seats", "accounts", "quotas", "wallets", "faucet"} <= set(got)
    assert 'href="/admin"' in app.get("/", headers=LOCAL).text, "an Admin tab"


def test_writes_need_the_admin_header_and_this_origin(public):
    """So a page on another site cannot drive the no-password panel."""
    app, state = public
    assert app.post("/admin/api/seats", headers=LOCAL, json={"seats": 7}).status_code == 403
    assert app.post("/admin/api/seats", json={"seats": 7}, headers={
        **LOCAL, **WRITE, "origin": "https://evil.example"}).status_code == 403
    assert app.post("/admin/api/seats", headers={**LOCAL, **WRITE},
                    json={"seats": 7}).status_code == 200
    assert state.setting("seats") == 7 and state.accounts().seats == 7


def test_limits_faucet_and_switches_are_saved(public):
    app, state = public
    ok = app.post("/admin/api/quotas", headers={**LOCAL, **WRITE},
                  json={"hour": {"post": 5, "message": 0}, "bytes": 1234})
    assert ok.status_code == 200
    assert state.setting("quota:post") == 5 and state.setting("quota:message") == 0
    assert state.setting("quota:bytes") == 1234
    assert app.post("/admin/api/settings", headers={**LOCAL, **WRITE}, json={
        "faucet_gift": "50", "auto_update": False,
        "moderation": {"url": "http://m:1/v1", "model": "q"}}).status_code == 200
    assert state.setting("faucet") == 50 * 10**8
    assert state.setting("auto_update") is False
    assert state.setting("moderation") == {"url": "http://m:1/v1", "model": "q"}
    app.post("/admin/api/settings", headers={**LOCAL, **WRITE}, json={"moderation": {}})
    assert state.setting("moderation") is None


def test_a_stranger_gets_nothing_from_outside(public):
    app, state = public
    assert app.get("/admin/api/state", headers=EDGE).status_code in (403, 404)
    page = app.get("/admin/login", headers=EDGE)
    assert page.status_code == 200 and "operator" in page.text


def test_from_outside_it_takes_the_operator_account_and_the_password(public):
    app, state = public
    _operator(app, state)
    # signed in as the operator, but no admin password yet this session
    assert app.get("/admin/api/state", headers=EDGE).status_code == 401
    assert app.post("/admin/login", headers=EDGE,
                    json={"password": "wrong wrong wrong"}).status_code == 403
    assert app.post("/admin/login", headers=EDGE,
                    json={"password": PASSWORD}).status_code == 200
    got = app.get("/admin/api/state", headers=EDGE).json()
    assert got["local"] is False and got["operator"]["password_set"] is True
    # ...and still cannot rename the operator or change the password from out here
    assert app.post("/admin/api/password", headers={**EDGE, **WRITE},
                    json={"password": "x" * 20}).status_code == 403
    # sending the node's coins asks for the password again
    refused = app.post("/admin/api/send/confirm", headers={**EDGE, **WRITE},
                       json={"ticket": "whatever"})
    assert refused.status_code == 403 and "password" in refused.json()["detail"]


def test_outside_the_operator_is_an_account_plus_admin_not_the_node(public):
    """The operator's session no longer opens the node's own pages from outside;
    everything the node does from there is in /admin, behind the password."""
    app, state = public
    _operator(app, state)
    app.post("/admin/login", headers=EDGE, json={"password": PASSWORD})
    assert app.get("/wallet", headers=EDGE).status_code == 404
    page = app.get("/me", headers=EDGE).text
    assert 'href="/me/messages"' in page and 'href="/admin"' in page


def test_a_new_password_signs_every_remote_session_out(public):
    app, state = public
    _operator(app, state)
    app.post("/admin/login", headers=EDGE, json={"password": PASSWORD})
    assert app.get("/admin/api/state", headers=EDGE).status_code == 200
    app.post("/admin/api/password", headers={**LOCAL, **WRITE},
             json={"password": "another long password"})
    assert app.get("/admin/api/state", headers=EDGE).status_code == 401


def test_the_startup_window_points_at_the_panel_and_installs(tmp_path, monkeypatch):
    monkeypatch.setattr(adminwindow.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    url = adminwindow.admin_url(8420)
    assert url == "http://127.0.0.1:8420/admin"
    assert url in adminwindow.message(url, True)
    path = adminwindow.install(8420)
    assert path == tmp_path / "autostart" / "dogecoinarcade-admin.desktop"
    assert "--port 8420" in path.read_text()
    assert adminwindow.uninstall() and not path.exists()
