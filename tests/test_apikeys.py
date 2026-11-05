"""API keys (arcade/apikeys.py, web/apikeys_web.py): an operator's key is the operator at the machine, from anywhere,
within the limits it was made with; a user's key is that account and nothing more; an unknown, switched-off, ended
or misplaced key is refused before the door."""
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state  # noqa: F401,E402
from test_auth_web import _sign_in  # noqa: E402

from arcade import apikeys as keylib  # noqa: E402
from arcade.web.app import create_app  # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}
ADMIN = {**LOCAL, "x-arcade-admin": "1"}
OUTSIDE = {"host": "arcade.example.com", "cf-connecting-ip": "198.51.100.9"}


@pytest.fixture
def app(app_state):
    app_state.set_setting("public_hosts", ["arcade.example.com"])
    yield TestClient(create_app(app_state))
    app_state.set_setting("public_hosts", [])
    app_state.set_setting(keylib.SETTING, None)


def _key(app, **body):
    said = app.post("/admin/api/keys", headers=ADMIN, json={"name": "site", **body})
    assert said.status_code == 200, said.text
    return said.json()["key"]


def _from(app, ip):
    """The same arcade, called from `ip` (the test client's own peer is not an address)."""
    inner = app.app

    async def asgi(scope, receive, send):
        await inner({**scope, "client": (ip, 40000)} if scope["type"] == "http" else scope, receive, send)
    return TestClient(asgi)


def _with(key, extra=OUTSIDE):
    return {**extra, "authorization": f"Bearer {key}"}


def test_an_operator_key_is_the_operator_from_anywhere(app):
    assert app.get("/admin/api/modules", headers=OUTSIDE).status_code in (401, 403, 404), "a stranger is not let in"
    key = _key(app)
    assert key.startswith("ak_") and len(key) > 40
    assert app.get("/admin/api/modules", headers=_with(key)).status_code == 200, "from outside, with the key"
    who = app.get("/api/v1/whoami", headers=_with(key)).json()
    assert who["as"] == "operator" and who["key"]["name"] == "site" and "key" not in who["key"]
    # an operator write, with neither the admin page's header nor a form token
    said = app.post("/admin/api/modules", headers=_with(key), json={"module": "feed", "access": "user"})
    assert said.status_code == 200, said.text
    run = app.post("/api/v1/run", headers=_with(key), json={"line": "getmeshinfo"}).json()
    assert run["ok"], run
    listed = app.get("/admin/api/keys", headers=ADMIN).json()["keys"]
    assert [k["name"] for k in listed] == ["site"] and listed[0]["used"], "it says when it was last used"


def test_a_key_that_is_not_ours_switched_off_or_ended_is_refused(app):
    assert app.get("/api/v1/whoami", headers=_with("ak_" + "0" * 48)).status_code == 401
    key = _key(app)
    kid = key[3:11]
    assert app.post(f"/admin/api/keys/{kid}", headers=ADMIN, json={"off": True}).status_code == 200
    assert app.get("/admin/api/modules", headers=_with(key)).status_code == 401
    assert app.post(f"/admin/api/keys/{kid}", headers=ADMIN, json={"off": False}).status_code == 200
    assert app.get("/admin/api/modules", headers=_with(key)).status_code == 200
    assert app.delete(f"/admin/api/keys/{kid}", headers=ADMIN).status_code == 200
    assert app.get("/admin/api/modules", headers=_with(key)).status_code == 401


def test_a_key_holds_to_what_it_was_made_for(app):
    reader = _key(app, write=False, modules=["ords"])
    assert app.get("/explore", headers=_with(reader)).status_code == 200
    assert app.get("/feed", headers=_with(reader)).status_code == 403, "not one of its modules"
    assert app.post("/admin/api/modules", headers=_with(reader), json={"module": "feed", "on": False}).status_code == 403
    home = _key(app, where="machine")
    assert app.get("/admin/api/modules", headers=_with(home)).status_code == 403, "through an edge is not the machine"
    here = _from(app, "127.0.0.1")
    assert here.get("/admin/api/modules", headers={**LOCAL, "authorization": f"Bearer {home}"}).status_code == 200
    lan = _key(app, where="lan")
    assert _from(app, "10.20.30.40").get("/api/v1/whoami", headers={**LOCAL, "authorization": f"Bearer {lan}"}) \
        .status_code == 200
    assert _from(app, "8.8.8.8").get("/api/v1/whoami", headers={**LOCAL, "authorization": f"Bearer {lan}"}) \
        .status_code == 403
    listed = _key(app, where="203.0.113.0/24")
    assert app.get("/api/v1/whoami", headers=_with(listed)).status_code == 403
    assert app.get("/api/v1/whoami", headers={**OUTSIDE, "cf-connecting-ip": "203.0.113.7",
                                              "authorization": f"Bearer {listed}"}).status_code == 200
    assert app.post("/admin/api/keys", headers=ADMIN, json={"name": "x", "where": "nowhere"}).status_code == 400
    assert app.post("/admin/api/keys", headers=ADMIN, json={"name": "x", "modules": ["nothing"]}).status_code == 400


def test_a_users_key_is_that_account_and_never_the_operator(app, app_state):
    signer = SigningKey.generate()
    assert app.get("/account/keys", headers=OUTSIDE).status_code == 401
    assert _sign_in(app, signer, headers=OUTSIDE).status_code == 200
    made = app.post("/account/keys", headers=OUTSIDE, json={"name": "my bot"})
    assert made.status_code == 200, made.text
    key = made.json()["key"]
    app.cookies.clear()                                   # the key alone says who calls
    who = app.get("/api/v1/whoami", headers=_with(key)).json()
    assert who["as"] == "account" and who["account"] == signer.verify_key.encode().hex()
    assert app.get("/admin/api/modules", headers=_with(key)).status_code in (401, 403, 404), "never the operator"
    assert app.post("/account/keys", headers=_with(key), json={"name": "more"}).status_code == 403, "keys make no keys"
    run = app.post("/api/v1/run", headers=_with(key), json={"line": "whoami"}).json()
    assert run["ok"] and run["result"]["audience"] == "user", run
    # The operator's say: how many each, and whether users may have keys at all
    app_state.set_setting(keylib.SETTING, {"users": True, "per_user": 1})
    assert _sign_in(app, signer, headers=OUTSIDE).status_code == 200
    assert app.post("/account/keys", headers=OUTSIDE, json={"name": "second"}).status_code == 403
    app_state.set_setting(keylib.SETTING, {"users": False})
    assert app.get("/api/v1/whoami", headers=_with(key)).status_code == 403
    keys = app.get("/admin/api/keys", headers=ADMIN).json()["keys"]
    assert keys[0]["owner"] == "account" and keys[0]["account"] == signer.verify_key.encode().hex()


def test_a_users_key_has_its_calls_a_minute(app, app_state):
    signer = SigningKey.generate()
    _sign_in(app, signer, headers=OUTSIDE)
    key = app.post("/account/keys", headers=OUTSIDE, json={"name": "fast"}).json()["key"]
    app_state.set_setting(keylib.SETTING, {"per_minute": 3})
    answers = [app.get("/api/v1/whoami", headers=_with(key)).status_code for _ in range(5)]
    assert answers[:3] == [200, 200, 200] and answers[3:] == [429, 429]
    operator = _key(app)
    assert {app.get("/api/v1/whoami", headers=_with(operator)).status_code for _ in range(10)} == {200}


def test_the_routes_are_listed_by_module_for_build(app):
    # A stranger is not told every route (2026-11-05 review); a key or a signed-in caller is.
    assert app.get("/api/v1/routes", headers=OUTSIDE).status_code == 401
    got = app.get("/api/v1/routes", headers=_with(_key(app))).json()["modules"]
    ids = [m["id"] for m in got]
    assert ids[0] == "node" and "feed" in ids and "wallet" in ids
    feed = next(m for m in got if m["id"] == "feed")
    assert any(r["path"] == "/feed" and "GET" in r["methods"] for r in feed["routes"])
    reader = _key(app, modules=["feed"])
    scoped = app.get("/api/v1/routes", headers=_with(reader)).json()["modules"]
    assert {m["id"]: m["opened"] for m in scoped}["feed"] and not {m["id"]: m["opened"] for m in scoped}["wallet"]


def test_keys_are_kept_as_hashes(tmp_path):
    store = keylib.KeyStore(tmp_path)
    key, row = store.make("node", "a")
    assert key not in (tmp_path / keylib.FILE).read_bytes().decode("latin-1")
    assert store.find(key).id == row.id and store.find(key[:-1] + "x") is None
    with pytest.raises(keylib.KeyRefused):
        store.make("node", "late", expires=time.time() - 1)


def test_a_key_runs_console_lines_only_as_itself_and_never_as_the_machine(app):
    """2026-11-05 review: /api/v1/run took no key at all, a key limited to some modules ran the whole console, and an
    operator key counted as the node's own machine, so `confirm` would send from the node wallet."""
    assert app.post("/api/v1/run", headers=OUTSIDE, json={"line": "help"}).status_code == 401
    games = _key(app, modules=["feed"])
    assert app.post("/api/v1/run", headers=_with(games), json={"line": "help"}).status_code == 403
    whole = _key(app)
    said = app.post("/api/v1/run", headers=_with(whole), json={"line": "help"})
    assert said.status_code == 200 and said.json()["ok"], said.text
    from arcade import apikeys as keylib
    assert keylib.caller_ip({"x-forwarded-for": "203.0.113.5"}, "198.51.100.9") == ("198.51.100.9", False), \
        "a stranger's X-Forwarded-For is not taken"
    assert keylib.caller_ip({"x-forwarded-for": "203.0.113.5"}, "127.0.0.1") == ("203.0.113.5", True)
