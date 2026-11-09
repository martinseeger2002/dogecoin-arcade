"""Node 2's review of the cross-arcade sign-in (2026-10-20), with node 3's review: what another arcade or a stranger
can make this one do.

- An announced arcade is asked only at a public address and never through a redirect (`_ask_arcade`).
- A name the chain says another address holds is not filed by /signup.
- The mesh port's pass-through says who really asked (`X-Forwarded-For`), so the web app never takes it for a
  local request, and never a visitor address the caller named itself (2026-11-09).
"""
import asyncio
import socket
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

import signup_proof as SP                                         # noqa: E402
from arcade.web import app as appmod                              # noqa: E402


def _resolves_to(monkeypatch, *addresses):
    def fake(host, port, *a, **k):
        if not addresses:
            raise socket.gaierror("no such name")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
                for a in addresses]
    monkeypatch.setattr(appmod.socket, "getaddrinfo", fake)


@pytest.mark.parametrize("addresses, public", [
    (("93.184.216.34",), True),
    (("2606:2800:220:1:248:1893:25c8:1946",), True),
    (("127.0.0.1",), False),
    (("10.0.0.5",), False),
    (("192.168.0.10",), False),
    (("169.254.169.254",), False),               # a cloud's metadata address
    (("::1",), False),
    (("fd00::1",), False),
    (("93.184.216.34", "127.0.0.1"), False),     # one private answer is enough to refuse
    ((), False),                                  # a name that does not resolve
])
def test_only_a_public_address_is_asked(monkeypatch, addresses, public):
    _resolves_to(monkeypatch, *addresses)
    assert appmod._host_is_public("arcade.example.com") is public


def test_asking_another_arcade_never_follows_a_redirect_nor_reaches_a_private_one(monkeypatch):
    asked = []
    monkeypatch.setattr(appmod.requests, "get", lambda url, **k: asked.append((url, k)) or "answer")
    _resolves_to(monkeypatch, "127.0.0.1")
    with pytest.raises(requests.ConnectionError):
        appmod._ask_arcade("https://sneaky.example.com/signin/x?here=1", timeout=8)
    assert asked == [], "a private address is not asked at all"
    _resolves_to(monkeypatch, "93.184.216.34")
    assert appmod._ask_arcade("https://arcade.example.com/signin/x?here=1", timeout=8) == "answer"
    assert asked[0][1]["allow_redirects"] is False, "a 302 to http://127.0.0.1:8420 would undo the check"


def test_signup_refuses_a_name_the_chain_gives_to_another_address(client, monkeypatch):
    page, state = client
    owner = SP.coin_key()
    theirs = SP.address_of(state, owner)

    class Index:
        def address_of(self, wanted):
            return theirs if wanted == "taken" else None
    monkeypatch.setattr(type(state), "token_index", lambda self, chain: Index())
    blob = {"sealed": "c2VhbGVk", "salt": "00", "nonce": "00", "iterations": 600000}

    refused = SP.signup(page, state, "taken", blob=blob)
    assert refused.status_code == 400 and "claimed on the chain" in refused.json()["detail"]
    assert state.vault().get("taken") is None, "nothing filed under somebody else's name"
    # The owner's address is public: posting it is not enough (2026-11-08, security review). Before, this filed the
    # name under the poster's own key and sealed wallet, and the owner's password no longer opened it here.
    page.cookies.clear()
    posed = SP.signup(page, state, "taken", address=theirs, blob=blob)
    assert posed.status_code == 400, posed.text
    assert state.vault().get("taken") is None and "arcade_session" not in page.cookies
    owned = SP.signup(page, state, "taken", coin=owner, blob=blob)
    assert owned.status_code == 200, owned.text
    assert SP.signup(page, state, "unclaimed", blob=blob).status_code == 200, "a name nobody holds yet is free"


def test_the_mesh_pass_through_says_who_really_asked():
    from arcade.mesh import wsock

    async def go():
        heads = []

        async def web(reader, writer):                     # the node's web app, standing in
            heads.append((await reader.readuntil(b"\r\n\r\n")).decode("latin-1").lower())
            body = b'{"ok":true}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            await writer.drain()
            writer.close()
        web_srv = await asyncio.start_server(web, "127.0.0.1", 0)

        async def mesh_handler(r, w):
            pass
        ws_srv = await wsock.serve(mesh_handler, "127.0.0.1", 0, http_port=web_srv.sockets[0].getsockname()[1])
        port = ws_srv.sockets[0].getsockname()[1]
        try:
            async def ask(extra=b""):
                r, w = await asyncio.open_connection("127.0.0.1", port)
                w.write(b"GET /signin/wanderer?here=1 HTTP/1.1\r\nHost: 127.0.0.1:8420\r\n" + extra + b"\r\n")
                await w.drain()
                out = await asyncio.wait_for(r.read(), 5)
                w.close()
                await w.wait_closed()
                return out
            assert b"200 OK" in await ask()
            assert "x-forwarded-for: 127.0.0.1" in heads[-1], "a bare caller is marked as forwarded"
            assert b"200 OK" in await ask(b"X-Forwarded-For: 198.51.100.7\r\n")
            assert heads[-1].count("x-forwarded-for:") == 1, "an edge's own header is passed on, not doubled"
        finally:
            ws_srv.close()
            web_srv.close()
            await ws_srv.wait_closed()
            await web_srv.wait_closed()
            await asyncio.sleep(0.1)
    asyncio.run(asyncio.wait_for(go(), 20))


def test_behind_the_tunnel_each_visitor_has_their_own_sign_in_allowance(client):
    """2026-11-08, security review: every visitor through the arcade's own tunnel came from 127.0.0.1, so one person
    asking 20 times in five minutes stopped sign-in for everybody. Each is counted by the address the tunnel names."""
    from fastapi.testclient import TestClient
    page, state = client
    inner = page.app

    async def tunnel(scope, receive, send):
        await inner({**scope, "client": ("127.0.0.1", 40000)} if scope["type"] == "http" else scope, receive, send)
    via = TestClient(tunnel)
    one = {"host": "127.0.0.1:8420", "cf-connecting-ip": "203.0.113.1"}
    other = {"host": "127.0.0.1:8420", "cf-connecting-ip": "203.0.113.2"}
    codes = [via.get("/signin/nobody?here=1", headers=one).status_code for _ in range(22)]
    assert codes[-1] == 429, codes
    assert via.get("/signin/nobody?here=1", headers=other).status_code == 404, "the next visitor is not held up"


class _Caller:
    """The far end of one connection to the mesh port, as wsock._proxy sees it: its socket's own address."""

    def __init__(self, peer: str):
        self.peer, self.out = (peer, 40000), bytearray()

    def get_extra_info(self, name, default=None):
        return self.peer if name == "peername" else default

    def write(self, data):
        self.out += data

    async def drain(self):
        pass

    def close(self):
        pass


def _through_the_mesh_port(client, asks):
    """Each (socket address, header lines) in `asks` through wsock._proxy to the real web app, which sees the
    pass-through arrive from loopback as it does on a node: (the status codes, the heads the web app was handed)."""
    from fastapi.testclient import TestClient
    from arcade.mesh import wsock
    page, _ = client
    inner = page.app

    async def loopback(scope, receive, send):
        await inner({**scope, "client": ("127.0.0.1", 40000)} if scope["type"] == "http" else scope, receive, send)
    via = TestClient(loopback)
    heads = []

    async def go():
        async def web(reader, writer):                     # hands what the pass-through sent to the web app
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            heads.append(head.lower())
            line, *rest = [l for l in head.split("\r\n") if l]
            said = [(k.strip(), v.strip()) for k, _, v in (l.partition(":") for l in rest)]
            got = await asyncio.to_thread(via.get, line.split(" ")[1], headers=said)
            writer.write(b"HTTP/1.1 %d X\r\nContent-Length: 0\r\n\r\n" % got.status_code)
            await writer.drain()
            writer.close()
        web_srv = await asyncio.start_server(web, "127.0.0.1", 0)
        codes = []
        try:
            for peer, extra in asks:
                caller = _Caller(peer)
                head = ("GET /signin/nobody?here=1 HTTP/1.1\r\nHost: 127.0.0.1:8420\r\n"
                        + "".join(l + "\r\n" for l in extra) + "\r\n").encode("latin-1")
                await wsock._proxy(head, asyncio.StreamReader(), caller, web_srv.sockets[0].getsockname()[1])
                codes.append(int(bytes(caller.out).split(b" ")[1]))
        finally:
            web_srv.close()
            await web_srv.wait_closed()
        return codes
    return asyncio.run(asyncio.wait_for(go(), 60)), heads


def test_a_visitor_address_named_at_the_mesh_port_is_not_believed(client):
    """2026-11-09, security review: the pass-through reaches the web app from loopback, where an edge's header is
    believed, and it passed the caller's own Cf-Connecting-Ip on. On a node whose mesh port is open with no edge in
    front, one caller naming a new address each time had a new sign-in allowance each time. Now the address is the
    socket's own, whatever the caller says."""
    forged = [("198.51.100.20", [f"Cf-Connecting-Ip: 203.0.113.{n}", f"X-Forwarded-For: 203.0.113.{n}",
                                 f"X-Real-Ip: 203.0.113.{n}", f"Forwarded: for=203.0.113.{n}",
                                 f"True-Client-Ip: 203.0.113.{n}", "X-Forwarded-Proto: https",
                                 "X-Forwarded-Host: elsewhere.example", f" Cf-Connecting-Ip: 203.0.113.{n}"])
              for n in range(22)]
    codes, heads = _through_the_mesh_port(client, forged + [("198.51.100.21", ["Cf-Connecting-Ip: 203.0.113.1"])])
    assert codes[21] == 429, codes
    assert codes[22] == 404, "another caller is not held up"
    head = heads[0]
    assert head.count("x-forwarded-for:") == 1 and "x-forwarded-for: 198.51.100.20\r\n" in head, head
    for name in ("cf-connecting-ip", "x-real-ip", "forwarded:", "true-client-ip", "x-forwarded-proto",
                 "x-forwarded-host", "203.0.113."):
        assert name not in head, (name, head)


def test_through_the_mesh_port_the_tunnel_still_names_each_visitor(client):
    """The arcade's own tunnel connects to the mesh port from loopback: its Cf-Connecting-Ip names the visitor, and
    each visitor keeps their own allowance (2026-11-09, security review)."""
    one = ["Cf-Connecting-Ip: 203.0.113.1", "X-Forwarded-For: 203.0.113.1", "X-Forwarded-Proto: https"]
    other = ["Cf-Connecting-Ip: 203.0.113.2", "X-Forwarded-Proto: https"]
    codes, heads = _through_the_mesh_port(client, [("127.0.0.1", one)] * 22 + [("127.0.0.1", other)])
    assert codes[21] == 429, codes
    assert codes[22] == 404, "the next visitor is not held up"
    assert heads[0].count("x-forwarded-for:") == 1 and "x-forwarded-for: 203.0.113.1\r\n" in heads[0]
    assert "x-forwarded-proto: https" in heads[0], "how the visitor came is the tunnel's word"
def _through_the_tunnel(page):
    """A client whose every request arrives from loopback, as the arcade's own tunnel's do."""
    from fastapi.testclient import TestClient
    inner = page.app

    async def tunnel(scope, receive, send):
        await inner({**scope, "client": ("127.0.0.1", 40000)} if scope["type"] == "http" else scope, receive, send)
    return TestClient(tunnel)


def _from(ip):
    return {"host": "127.0.0.1:8420", "cf-connecting-ip": ip}


def test_a_visitor_over_the_sign_in_limit_is_told_how_long_to_wait(client):
    """2026-11-09, security review: the 429 said "a few minutes"; it says how many, and Retry-After does too."""
    page, state = client
    via = _through_the_tunnel(page)
    answers = [via.get("/signin/nobody?here=1", headers=_from("203.0.113.9")) for _ in range(21)]
    held = answers[-1]
    assert [a.status_code for a in answers[:20]] == [404] * 20
    assert held.status_code == 429, held.text
    wait = held.json()["wait"]
    assert 0 < wait <= 300 and held.headers["retry-after"] == str(wait)
    assert "Wait 5 minutes" in held.json()["detail"], held.json()


def test_one_names_sealed_wallet_goes_to_a_bounded_number_of_places_an_hour(client):
    """2026-11-09, security review: the per-visitor limit held one address, and a name asked for from many addresses
    was handed out to every one. Now a name goes to VAULT_VISITORS different visitors an hour; a visitor already
    counted (a second try, the owner's own phone) is let in again, and other names are not held up."""
    from arcade import accounts as accountslib
    page, state = client
    via = _through_the_tunnel(page)
    many = accountslib.VAULT_VISITORS
    codes = [via.get("/signin/wanted?here=1", headers=_from(f"198.51.100.{i}")).status_code for i in range(many)]
    assert codes == [404] * many, codes
    held = via.get("/signin/wanted?here=1", headers=_from("192.0.2.200"))
    assert held.status_code == 429, held.text
    assert 3500 < held.json()["wait"] <= 3600 and held.headers["retry-after"] == str(held.json()["wait"])
    assert "Wait 60 minutes" in held.json()["detail"]
    # Its sealed record of imported wallets counts the same places (same password, same guessing).
    assert via.get("/signin/wanted/imported", headers=_from("192.0.2.200")).status_code == 429
    assert via.get("/signin/WANTED?here=1", headers=_from("192.0.2.201")).status_code == 429, "any spelling"
    assert via.get("/signin/wanted?here=1", headers=_from("198.51.100.3")).status_code == 404, \
        "a visitor already counted signs in again"
    assert via.get("/signin/someone-else?here=1", headers=_from("192.0.2.200")).status_code == 404


def test_a_names_lookup_places_free_up_as_the_hour_passes(client):
    from arcade import accounts as accountslib
    _, state = client
    register = state.accounts()
    now = 1_800_000_000
    for i in range(accountslib.VAULT_VISITORS):
        assert register.vault_lookup_wait("wanted", f"v{i}", now + i) == 0
    wait = register.vault_lookup_wait("wanted", "late", now + 100)
    assert wait == accountslib.VAULT_WINDOW - 100, "until the first visitor's lookup is an hour old"
    assert register.vault_lookup_wait("wanted", "late", now + accountslib.VAULT_WINDOW + 1) == 0
    register.sweep(now + 1000)
    assert register.vault_lookup_wait("wanted", "later", now + 1000) > 0, "the sweep keeps an hour of lookups"
