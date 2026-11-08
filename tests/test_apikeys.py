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
    said = app.post("/admin/api/keys", headers=ADMIN, json={"name": "site", "write": True, **body})   # full unless said
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
    named = {**OUTSIDE, "cf-connecting-ip": "203.0.113.7", "authorization": f"Bearer {listed}"}
    assert _from(app, "127.0.0.1").get("/api/v1/whoami", headers=named).status_code == 200, "through the tunnel"
    # Named by anybody else, the address is their own word (2026-11-08): a listed key is not used from anywhere by it.
    assert _from(app, "198.51.100.20").get("/api/v1/whoami", headers=named).status_code == 403
    assert app.post("/admin/api/keys", headers=ADMIN, json={"name": "x", "where": "nowhere"}).status_code == 400
    assert app.post("/admin/api/keys", headers=ADMIN, json={"name": "x", "modules": ["nothing"]}).status_code == 400


def test_a_users_key_is_that_account_and_never_the_operator(app, app_state):
    signer = SigningKey.generate()
    assert app.get("/account/keys", headers=OUTSIDE).status_code == 401
    assert _sign_in(app, signer, headers=OUTSIDE).status_code == 200
    made = app.post("/account/keys", headers=OUTSIDE, json={"name": "my bot", "write": True})
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
    assert app.post("/api/v1/run", headers=_with(games), json={"line": "getbalances"}).status_code == 403
    whole = _key(app)
    said = app.post("/api/v1/run", headers=_with(whole), json={"line": "help"})
    assert said.status_code == 200 and said.json()["ok"], said.text
    from arcade import apikeys as keylib
    assert keylib.caller_ip({"x-forwarded-for": "203.0.113.5"}, "198.51.100.9") == ("198.51.100.9", False), \
        "a stranger's X-Forwarded-For is not taken"
    assert keylib.caller_ip({"x-forwarded-for": "203.0.113.5"}, "127.0.0.1") == ("203.0.113.5", True)


def _run(app, key, line):
    return app.post("/api/v1/run", headers=_with(key), json={"line": line})


def test_a_key_limited_to_some_modules_runs_their_console_lines_and_the_cores_public_reads_only(app):
    """2026-11-06: a key limited to some modules was refused every console line; now the line is held to the key's
    modules, and the core's public reads (help, getnodeinfo, listchains) stay open to every key."""
    feed = _key(app, modules=["feed"])
    said = _run(app, feed, "readfeed")
    assert said.status_code == 200, said.text
    for line in ("help", "getnodeinfo", "listchains"):
        assert _run(app, feed, line).status_code == 200, line
    assert "readfeed" in _run(app, feed, "help").json()["result"]
    assert "getbalances" not in _run(app, feed, "help").json()["result"], "help lists only what the key may run"
    refused = _run(app, feed, "getbalances")
    assert refused.status_code == 403 and "Wallet module" in refused.json()["detail"], refused.text
    assert _run(app, feed, "install mining").status_code == 403, "the core's changes are not the feed's"
    assert _run(app, feed, "getmeshinfo").status_code == 403, "the core's operator reads are not every key's"
    assert _run(app, feed, "post hello from a feed key").status_code == 200, "a feed key with write posts"


def test_a_key_that_reads_only_runs_the_console_lines_that_read_and_no_others(app):
    """2026-11-06: a key made to read only could not post to /api/v1/run at all; now it runs the lines that only read
    and is refused, by name, every line that changes something. Its other posts are refused as before."""
    reader = _key(app, write=False)
    for line in ("listchains", "getnodeinfo", "help", "getmeshinfo"):
        said = _run(app, reader, line)
        assert said.status_code == 200 and said.json()["ok"], (line, said.text)
    for line in ("post hello", "install mining", "edition lite", "look web", "confirm 1234"):
        said = _run(app, reader, line)
        assert said.status_code == 403 and "reads only" in said.json()["detail"], (line, said.text)
    assert "install" not in _run(app, reader, "help").json()["result"].split()
    assert app.post("/admin/api/modules", headers=_with(reader), json={"module": "feed", "on": False}).status_code == 403
    both = _key(app, write=False, modules=["feed"])
    assert _run(app, both, "readfeed").status_code == 200
    assert _run(app, both, "post hello").status_code == 403
    assert _run(app, both, "getbalances").status_code == 403


#: The console commands that only read (2026-11-08, on PR #113): a key made to read only runs these and no others. A
#: command is a change unless it says `writes=False` by being here too, so one added without a thought fails below.
READS_ONLY = {
    "checkmodule", "checktheme", "getbalances", "getbook", "getmeshinfo", "getnodeinfo", "getpair", "help", "listapps",
    "listarcades", "listchains", "listconversations", "listdoginals", "listmining", "listmodules", "listthemes",
    "listtokens", "listtransactions", "listunspent", "mining", "readfeed", "readposts", "sharedthemes", "whoami",
}


def test_every_console_command_says_whether_a_read_only_key_may_run_it():
    from arcade.lite import commands as cmdlib
    reads = {name for name, cmd in cmdlib.COMMANDS.items() if not cmd.writes}
    assert reads == READS_ONLY, (
        f"read-only now but not listed: {sorted(reads - READS_ONLY)}; listed but now a change: {sorted(READS_ONLY - reads)}"
        " -- a new command that changes anything takes writes=True; one that only reads is added to READS_ONLY")


def test_sign_out_everywhere_switches_the_accounts_keys_off(app, app_state):
    """2026-11-08, security review: a key outlived "sign out everywhere", so a key made by whoever held a session went
    on acting as the account. Now they go off with the sessions; another account's keys stay on."""
    signer, other = SigningKey.generate(), SigningKey.generate()
    assert _sign_in(app, other, headers=OUTSIDE).status_code == 200
    theirs = app.post("/account/keys", headers=OUTSIDE, json={"name": "other bot"}).json()["key"]
    app.cookies.clear()
    assert _sign_in(app, signer, headers=OUTSIDE).status_code == 200
    key = app.post("/account/keys", headers=OUTSIDE, json={"name": "my bot"}).json()["key"]
    assert app.get("/api/v1/whoami", headers=_with(key)).status_code == 200
    ended = app.post("/auth/logout-everywhere", headers=OUTSIDE)
    assert ended.status_code == 200 and ended.json()["keys_off"] == 1, ended.text
    app.cookies.clear()
    assert app.get("/api/v1/whoami", headers=_with(key)).status_code in (401, 403), "off with the sessions"
    assert app.get("/api/v1/whoami", headers=_with(theirs)).status_code == 200, "another account's key is untouched"
    assert _sign_in(app, signer, headers=OUTSIDE).status_code == 200
    listed = app.get("/account/keys", headers=OUTSIDE).json()["keys"]
    assert len(listed) == 1 and listed[0]["off"], "still there, switched off, for the owner to switch back on"


def test_a_key_is_read_only_and_ends_in_90_days_unless_asked(app, app_state):
    """2026-11-08, security review: every new key was full write, for ever."""
    said = app.post("/admin/api/keys", headers=ADMIN, json={"name": "plain"}).json()
    assert said["write"] is False
    assert 89 * 86400 < said["expires"] - time.time() <= 90 * 86400
    asked = app.post("/admin/api/keys", headers=ADMIN, json={"name": "full", "write": True, "days": None}).json()
    assert asked["write"] is True and asked["expires"] is None, "asked for: write, and never ending"


def test_an_edge_header_counts_only_from_this_machine():
    """Cf-Connecting-Ip from anybody but the arcade's own tunnel (loopback) is the caller's own word: a per-address limit
    or an address-limited key could be got round by naming another address each time."""
    said = {"cf-connecting-ip": "203.0.113.7"}
    assert keylib.caller_ip(said, "127.0.0.1") == ("203.0.113.7", True)
    assert keylib.caller_ip(said, "198.51.100.20") == ("198.51.100.20", False)
    assert keylib.caller_ip({"x-forwarded-for": "203.0.113.8, 10.0.0.1"}, "::1") == ("203.0.113.8", True)
    assert keylib.caller_ip({}, "198.51.100.20") == ("198.51.100.20", False)
