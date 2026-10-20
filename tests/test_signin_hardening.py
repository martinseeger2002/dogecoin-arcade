"""Node 2's review of the cross-arcade sign-in (2026-10-20), with node 3's review: what another arcade or a stranger
can make this one do.

- An announced arcade is asked only at a public address and never through a redirect (`_ask_arcade`).
- A name the chain says another address holds is not filed by /signup.
- The mesh port's pass-through says who really asked (`X-Forwarded-For`), so the web app never takes it for a
  local request.
"""
import asyncio
import socket
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import script                                         # noqa: E402
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
    mine = script.b58check_encode(113, bytes(range(20)))
    theirs = script.b58check_encode(113, bytes(range(20, 40)))

    class Index:
        def address_of(self, wanted):
            return theirs if wanted == "taken" else None
    monkeypatch.setattr(type(state), "token_index", lambda self, chain: Index())
    blob = {"sealed": "c2VhbGVk", "salt": "00", "nonce": "00", "iterations": 600000}

    def signup(tag, pubkey, address):
        return page.post("/signup", json={"tag": tag, "pubkey": pubkey, "address": address, "blob": blob})

    refused = signup("taken", "11" * 32, mine)
    assert refused.status_code == 400 and "claimed on the chain" in refused.json()["detail"]
    assert state.vault().get("taken") is None, "nothing filed under somebody else's name"
    owned = signup("taken", "22" * 32, theirs)
    assert owned.status_code == 200, owned.text
    assert signup("unclaimed", "33" * 32, mine).status_code == 200, "a name nobody holds yet is free"


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
