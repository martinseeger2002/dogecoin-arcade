"""Who is the operator: a request is local only from this machine, to this machine's own name, through nothing.

door.from_outside decides it from the TCP peer, the Host header and the headers a proxy adds -- all three. And a
change made on local trust (which needs no cookie) must come from this origin, never from a page on another site.
"""

import asyncio
import pathlib
import sys

import httpx
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade.web import door                                       # noqa: E402

PUBLIC = ("node.dogecoinarcade.com",)


# --- the rule itself -----------------------------------------------------------

@pytest.mark.parametrize("peer,host,headers,outside", [
    ("127.0.0.1", "127.0.0.1:8420", {}, False),                 # the operator's own browser
    ("127.0.0.1", "localhost:8420", {}, False),
    ("::1", "[::1]:8420", {}, False),
    ("::ffff:127.0.0.1", "localhost", {}, False),
    ("127.0.0.1", "arcade.localhost:8420", {}, False),
    ("127.0.0.1", "rebind.example.com:8420", {}, True),        # a name somebody's DNS points here
    ("127.0.0.1", "", {}, True),                                # no Host at all
    ("127.0.0.1", "arcade.example.org", {"x-forwarded-for": "203.0.113.9"}, True),   # Caddy in front
    ("127.0.0.1", "arcade.example.org", {}, True),              # a proxy that adds nothing
    ("127.0.0.1", "127.0.0.1:8420", {"x-forwarded-for": "203.0.113.9"}, True),       # the mesh pass-through
    ("127.0.0.1", "127.0.0.1:8420", {"cf-ray": "abc-LHR"}, True),
    ("127.0.0.1", "node.dogecoinarcade.com", {}, True),         # a published name
    ("192.168.1.50", "127.0.0.1:8420", {}, True),               # another machine, whatever it says
    (None, "127.0.0.1:8420", {}, True),                         # no peer known
])
def test_local_is_the_narrow_case(peer, host, headers, outside):
    assert door.from_outside(headers, host, PUBLIC, peer=peer) is outside


def test_a_configured_local_name_is_local_only_over_loopback():
    assert door.from_outside({}, "arcade.lan:8420", PUBLIC, peer="127.0.0.1", local_hosts=("arcade.lan",)) is False
    assert door.from_outside({}, "arcade.lan:8420", PUBLIC, peer="10.0.0.2", local_hosts=("arcade.lan",)) is True
    assert door.from_outside({}, "arcade.lan:8420", PUBLIC, peer="127.0.0.1") is True


@pytest.mark.parametrize("headers,crossed", [
    ({}, False),                                                # a program: curl, a script, another node
    ({"sec-fetch-site": "same-origin"}, False),
    ({"sec-fetch-site": "none"}, False),
    ({"origin": "http://127.0.0.1:8420"}, False),
    ({"sec-fetch-site": "cross-site"}, True),
    ({"sec-fetch-site": "same-site"}, True),                    # another port, or a sibling name
    ({"origin": "https://elsewhere.example"}, True),
    ({"origin": "null"}, True),                                 # a sandboxed frame, a file
    ({"origin": "http://localhost:8420"}, True),                # another origin, even if it is this machine
])
def test_cross_site(headers, crossed):
    assert door.cross_site(headers, "127.0.0.1:8420") is crossed


# --- through the app, with real peers -----------------------------------------

@pytest.fixture
def real(client, monkeypatch):
    """The app, reached from a chosen TCP peer -- the suite's stand-ins for a loopback browser taken away."""
    app, state = client
    monkeypatch.setattr(door, "LOOPBACK_PEERS", ())
    monkeypatch.setattr(door, "LOOPBACK_HOSTS", ("localhost",))
    state.set_setting("public_hosts", list(PUBLIC))

    def call(method, path, peer="127.0.0.1", host="127.0.0.1:8420", **kw):
        async def go():
            transport = httpx.ASGITransport(app=app.app, client=(peer, 50000))
            async with httpx.AsyncClient(transport=transport, base_url=f"http://{host}") as http:
                return await http.request(method, path, **kw)
        return asyncio.run(go())

    yield call, state
    state.set_setting("public_hosts", [])
    state.set_setting("local_hosts", [])
    state.set_setting("operator", "")


def _settable(call, **kw) -> bool:
    return call("GET", "/auth/door", **kw).json()["settable"]


def test_the_operator_browsing_loopback_is_local(real):
    call, _ = real
    assert _settable(call) is True
    assert _settable(call, host="localhost:8420") is True


def test_a_rebound_name_is_outside(real):
    call, _ = real
    assert _settable(call, host="rebind.example.com:8420") is False


def test_a_proxy_on_this_machine_is_outside(real):
    call, _ = real
    assert _settable(call, host="arcade.example.org", headers={"x-forwarded-for": "203.0.113.9"}) is False
    assert _settable(call, host="arcade.example.org") is False


def test_another_machine_is_outside(real):
    call, _ = real
    assert _settable(call, peer="192.168.1.50") is False


def test_a_configured_local_name(real):
    call, state = real
    assert _settable(call, host="arcade.lan:8420") is False
    state.set_setting("local_hosts", ["arcade.lan"])
    assert _settable(call, host="arcade.lan:8420") is True


# --- setting the operator's password ------------------------------------------

BODY = b'{"username": "robin", "password": "correct horse battery"}'


@pytest.mark.parametrize("headers", [
    {},                                                         # no Content-Type: still parsed as JSON before
    {"content-type": "text/plain"},
    {"content-type": "application/x-www-form-urlencoded"},
    {"content-type": "application/json", "origin": "https://elsewhere.example"},
    {"content-type": "application/json", "sec-fetch-site": "cross-site"},
    {"content-type": "application/json", "origin": "null"},
])
def test_set_password_is_refused_unless_the_sign_in_page_sent_it(real, headers):
    call, state = real
    answer = call("POST", "/auth/set-password", content=BODY, headers=headers)
    assert answer.status_code == 403
    assert state.operator == ""
    assert _settable(call) is True


def test_set_password_from_a_rebound_name_is_refused(real):
    call, state = real
    answer = call("POST", "/auth/set-password", host="rebind.example.com:8420", content=BODY,
                  headers={"content-type": "application/json", "origin": "http://rebind.example.com:8420"})
    assert answer.status_code in (403, 404)
    assert state.operator == ""


def test_the_operator_sets_it_from_the_sign_in_page(real):
    call, state = real
    answer = call("POST", "/auth/set-password", content=BODY,
                  headers={"content-type": "application/json", "origin": "http://127.0.0.1:8420",
                           "sec-fetch-site": "same-origin"})
    assert answer.status_code == 200, answer.text
    assert answer.json()["username"] == "robin"
    assert state.operator == answer.json()["operator"]


def test_a_program_on_this_machine_may_still_set_it(real):
    call, state = real
    answer = call("POST", "/auth/set-password", content=BODY, headers={"content-type": "application/json"})
    assert answer.status_code == 200, answer.text


# --- every other change made on local trust -----------------------------------

def test_another_site_cannot_post_to_the_operators_machine(real):
    call, _ = real
    refused = call("POST", "/auth/logout", headers={"origin": "https://elsewhere.example"})
    assert refused.status_code == 403
    assert refused.json()["detail"] == "that request came from another site"
    refused = call("POST", "/account/favorites", json={}, headers={"sec-fetch-site": "cross-site"})
    assert refused.status_code == 403


def test_this_origin_and_programs_still_post(real):
    call, _ = real
    for headers in ({"origin": "http://127.0.0.1:8420"}, {"sec-fetch-site": "same-origin"}, {}):
        answer = call("POST", "/auth/logout", headers=headers)
        assert answer.status_code != 403 or answer.json().get("detail") != "that request came from another site"


def test_the_page_api_and_realtime_keep_taking_other_origins(real):
    call, _ = real
    for path in ("/r/state/update", "/realtime/send"):
        answer = call("POST", path, json={}, headers={"origin": "null", "sec-fetch-site": "cross-site"})
        assert not (answer.status_code == 403
                    and answer.headers.get("content-type", "").startswith("application/json")
                    and answer.json().get("detail") == "that request came from another site")
