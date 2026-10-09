"""Realtime rooms through the web app: the routes, the door, the stream, who is who.

Two real MeshServices on localhost stand in for two arcade nodes; each test app
gets one. Nothing here reaches a chain node: verification is a stand-in that
accepts a signature of the form "good:<message>".
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import nacl.signing
import pytest
import uvicorn
from fastapi.testclient import TestClient

from arcade.mesh import announce as meshannounce
from arcade.mesh import node as meshlib
from arcade.mesh.service import MeshService, cert_message, load_or_make_key
from arcade.messaging.store import MessageStore
from arcade.web import door as doorlib
from arcade.web.app import create_app
from arcade.web.state import AppState, ChainContext


def fake_verify(address, signature, message):
    return signature == "good:" + message


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(meshlib, "SUB_EVERY", 0.15)
    monkeypatch.setattr(meshlib, "DIAL_EVERY", 0.05)


def make_state(tmp_path, name):
    (tmp_path / name).mkdir(exist_ok=True)
    return AppState(
        home=tmp_path / name,
        messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                               datadir=Path("/nonexistent")),
        ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                            datadir=Path("/nonexistent")),
    )


class Served:
    """A real server for one app, on a free port: the TestClient buffers a whole
    response before handing it over, so an event stream that never ends never
    arrives through it. httpx against uvicorn streams the way a browser does."""

    def __init__(self, app):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                                    log_level="warning", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        end = time.monotonic() + 10
        while not self.server.started and time.monotonic() < end:
            time.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10)

    def close(self):
        self.client.close()
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def two_nodes(tmp_path, no_nodes):
    a = MeshService("arcade-test", nacl.signing.SigningKey.generate(),
                    listen_host="127.0.0.1", listen_port=0, verify=fake_verify).start()
    b = MeshService("arcade-test", nacl.signing.SigningKey.generate(),
                    listen_host="127.0.0.1", listen_port=0, verify=fake_verify,
                    peers=[("127.0.0.1", a.node.listen_port)]).start()
    sa, sb = make_state(tmp_path, "a"), make_state(tmp_path, "b")
    sa.mesh, sb.mesh = a, b
    end = time.monotonic() + 5
    while not (a.node.links and b.node.links) and time.monotonic() < end:
        time.sleep(0.02)
    assert a.node.links and b.node.links
    servers = [Served(create_app(sa)), Served(create_app(sb))]
    try:
        yield (servers[0].client, sa), (servers[1].client, sb)
    finally:
        for s in servers:
            s.close()
        b.stop()
        a.stop()


def join(client, state, room="town", game="g1"):
    r = client.post("/realtime/join", json={"csrf_token": state.csrf_token,
                                           "game": game, "room": room})
    assert r.status_code == 200, r.text
    return r.json()


class Stream:
    """Read a /realtime/stream in a thread, collecting its events. Its own
    client, so closing it never pulls a socket out from under another thread."""

    def __init__(self, client, token):
        self.events = []
        self._client = httpx.Client(base_url=client.base_url, timeout=None)
        self._t = threading.Thread(target=self._run, args=(token,), daemon=True)
        self._t.start()

    def _run(self, token):
        try:
            with self._client.stream("GET", f"/realtime/stream?token={token}") as r:
                for line in r.iter_lines():
                    if line.startswith("data: "):
                        self.events.append(json.loads(line[6:]))
        except (httpx.HTTPError, RuntimeError, OSError):
            pass                                  # closed by the test: that is the end

    def wait(self, check, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for ev in list(self.events):
                if check(ev):
                    return ev
            time.sleep(0.02)
        raise AssertionError(f"no such event in {self.events}")

    def close(self):
        self._client.close()
        self._t.join(timeout=5)


# ------------------------------------------------------------------ door and files

def test_the_door_lets_realtime_through_on_a_public_instance():
    for path in ("/realtime/hello", "/realtime/stream"):
        assert doorlib.public_path(path, "GET")
    for path in ("/realtime/join", "/realtime/send", "/realtime/leave"):
        assert doorlib.public_path(path, "POST")
    assert doorlib.pages_path("/r/realtime.js")


def test_the_page_half_is_served_like_storage(tmp_path, no_nodes):
    client = TestClient(create_app(make_state(tmp_path, "x")))
    r = client.get("/r/realtime.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert r.headers["access-control-allow-origin"] == "*"
    assert "arcade.realtime" in r.text


def test_a_node_without_the_mesh_says_so_and_games_play_solo(tmp_path, no_nodes):
    state = make_state(tmp_path, "x")
    client = TestClient(create_app(state))
    hello = client.get("/realtime/hello").json()
    assert hello["online"] is False and "mesh" in hello["why"]
    r = client.post("/realtime/join", json={"csrf_token": state.csrf_token,
                                           "game": "g", "room": "r"})
    assert r.status_code == 400 and r.json()["ok"] is False


def test_join_needs_the_form_token(two_nodes):
    (ca, sa), _ = two_nodes
    r = ca.post("/realtime/join", json={"csrf_token": "wrong", "game": "g", "room": "r"})
    assert r.status_code == 400


def test_rooms_and_games_are_checked(two_nodes):
    (ca, sa), _ = two_nodes
    for game, room in (("", "r"), ("has space", "r"), ("g", ""), ("g", "x" * 61)):
        r = ca.post("/realtime/join", json={"csrf_token": sa.csrf_token,
                                           "game": game, "room": room})
        assert r.status_code == 400, (game, room)


# ------------------------------------------------------------------ across two nodes

def test_two_nodes_meet_in_a_room_and_hear_each_other(two_nodes):
    (ca, sa), (cb, sb) = two_nodes
    ja = join(ca, sa)                 # no Core to sign with: the operator plays as a guest
    assert ja["online"] and ja["me"]["guest"] and ja["me"]["id"].startswith("guest-")
    sa_stream = Stream(ca, ja["token"])
    jb = join(cb, sb)
    sb_stream = Stream(cb, jb["token"])
    try:
        sa_stream.wait(lambda e: e["type"] == "join" and e["member"]["id"] == jb["me"]["id"])
        r = cb.post("/realtime/send", json={"csrf_token": sb.csrf_token, "token": jb["token"],
                                           "data": json.dumps({"x": 3, "y": 4})})
        assert r.json() == {"ok": True}
        got = sa_stream.wait(lambda e: e["type"] == "message")
        assert got["room"] == "town" and json.loads(got["data"]) == {"x": 3, "y": 4}
        assert got["member"]["id"] == jb["me"]["id"]
        # Different game, same room name: a different room.
        jc = join(cb, sb, game="g2")
        cb.post("/realtime/send", json={"csrf_token": sb.csrf_token, "token": jc["token"],
                                       "data": "elsewhere"})
        time.sleep(0.5)
        assert not any(e.get("data") == "elsewhere" for e in sa_stream.events)
        # Leaving says so.
        cb.post("/realtime/leave", json={"csrf_token": sb.csrf_token, "token": jb["token"]})
        sa_stream.wait(lambda e: e["type"] == "leave" and e["member"]["id"] == jb["me"]["id"])
    finally:
        sa_stream.close()
        sb_stream.close()


def test_send_refuses_too_big_too_fast_and_strangers(two_nodes):
    (ca, sa), _ = two_nodes
    j = join(ca, sa)
    post = lambda body: ca.post("/realtime/send", json={"csrf_token": sa.csrf_token, **body})
    assert post({"token": j["token"], "data": "x" * 513}).status_code == 400
    assert post({"token": "not-a-token", "data": "x"}).status_code == 400
    codes = [post({"token": j["token"], "data": "x"}).status_code for _ in range(meshlib.BURST + 3)]
    assert codes[:meshlib.BURST] == [200] * meshlib.BURST and 429 in codes


# ------------------------------------------------------------------ certificates

def test_a_certificate_proves_one_address_on_one_node_until_it_expires(two_nodes):
    (_, sa), (_, sb) = two_nodes
    a, b = sa.mesh, sb.mesh
    expires = a.expiry()
    good = {"a": "nAddress1", "x": expires,
            "s": "good:" + cert_message(a.network, a.node_id, "nAddress1", expires)}
    assert a.check_cert("nAddress1", good)
    assert not a.check_cert("nSomebodyElse", good)                 # names another address
    assert not b.check_cert("nAddress1", good)                     # signed for node a, not b
    assert not a.check_cert("nAddress1", dict(good, s="bad"))
    old = int(time.time()) - 10
    assert not a.check_cert("nAddress1", {"a": "nAddress1", "x": old,
                                          "s": "good:" + cert_message(a.network, a.node_id,
                                                                      "nAddress1", old)})
    far = int(time.time()) + 30 * 86400
    assert not a.check_cert("nAddress1", {"a": "nAddress1", "x": far,
                                          "s": "good:" + cert_message(a.network, a.node_id,
                                                                      "nAddress1", far)})


def test_the_mesh_key_is_made_once_and_kept_private(tmp_path):
    path = tmp_path / "home" / "mesh.key"
    first = load_or_make_key(path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert bytes(load_or_make_key(path)) == bytes(first)


# ------------------------------------------------------------------ the chain

def test_mesh_announcements_are_kept_latest_per_key(tmp_path):
    store = MessageStore(tmp_path / "m.sqlite")
    k1, k2 = "a" * 64, "b" * 64
    store.add_mesh_announcement("t1", "regtest", "nA", "8.8.4.4", 8421, k1, 0, 0)
    time.sleep(1.1)
    store.add_mesh_announcement("t2", "regtest", "nA", "8.8.8.8", 9000, k1, 0, 0)
    store.add_mesh_announcement("t3", "regtest", "nB", "1.1.1.1", 8421, k2, 5, 100)
    rows = {r["mesh_key"]: (r["host"], r["port"]) for r in store.mesh_peers("regtest")}
    assert rows == {k1: ("8.8.8.8", 9000), k2: ("1.1.1.1", 8421)}
    assert store.mesh_peers("main") == []


def test_an_announcement_says_an_ip_and_never_a_name():
    payload = meshannounce.build("8.8.4.4", 8421, "c" * 64)
    assert meshannounce.parse(payload) == {"host": "8.8.4.4", "port": 8421, "key": "c" * 64}
    for host in ("example.com", "https://example.com", "192.168.1.5", "127.0.0.1", "10.0.0.1"):
        with pytest.raises(ValueError):
            meshannounce.build(host, 8421, "c" * 64)
    assert meshannounce.parse(payload.replace(b"8421", b"0")) is None


# ------------------------------------------------------------------ announcing itself

def test_the_public_address_is_what_most_peers_see():
    from arcade.mesh.service import public_address
    assert public_address(["129.222.44.26", "129.222.44.26:44874", "8.8.8.8"]) == "129.222.44.26"
    assert public_address(["129.222.44.26"]) is None, "one peer is not a majority of anything"
    assert public_address(["192.168.1.20", "192.168.1.20", "10.0.0.1"]) is None
    v6 = "[2605:59ca:13cf:2e10:4b3e:ae74:97dd:c48e]:33874"
    assert public_address([v6, v6]) == "2605:59ca:13cf:2e10:4b3e:ae74:97dd:c48e"
    assert public_address([v6, v6, "1.1.1.1", "1.1.1.1"]) == "1.1.1.1", "IPv4 first"
    assert public_address(["", "-", "nonsense"]) is None


def test_a_node_announces_itself_when_its_address_is_new_and_not_otherwise(tmp_path):
    import asyncio as aio
    seen = {"now": ["1.1.1.1", "1.1.1.1"]}
    told = []
    svc = MeshService("arcade-test", nacl.signing.SigningKey.generate(),
                      listen_host="127.0.0.1", listen_port=0,
                      observe=lambda: seen["now"], announce=lambda h, p: told.append((h, p)),
                      remember=tmp_path / "mesh-announced.json").start()
    reach = {"ok": True}

    async def reaches(host, port):
        return reach["ok"]
    svc.node.reaches_itself = reaches
    due = lambda: svc._call(svc.announce_if_due(), timeout=10)
    try:
        port = svc.node.listen_port
        assert due() and told == [("1.1.1.1", port)]
        assert not due(), "same address, announced today: nothing to say"
        seen["now"] = ["2.2.2.2", "2.2.2.2"]
        assert due() and told[-1] == ("2.2.2.2", port), "the address changed: say so"
        reach["ok"] = False
        seen["now"] = ["3.3.3.3", "3.3.3.3"]
        assert not due() and len(told) == 2, "an address that does not lead here is not announced"
        assert svc.status()["reachable"] is False
        reach["ok"] = True
        seen["now"] = ["192.168.1.9", "192.168.1.9"]
        assert not due(), "a private address is nobody's way in"
    finally:
        svc.stop()
    # A restart remembers what it last said.
    again = MeshService("arcade-test", nacl.signing.SigningKey.generate(),
                        listen_host="127.0.0.1", listen_port=0,
                        remember=tmp_path / "mesh-announced.json")
    assert again.last_announced["host"] == "2.2.2.2"


def test_a_fixed_address_is_announced_without_looking(tmp_path):
    told = []
    svc = MeshService("arcade-test", nacl.signing.SigningKey.generate(),
                      listen_host="127.0.0.1", listen_port=0, announce_at=("4.4.4.4", 9000),
                      announce=lambda h, p: told.append((h, p))).start()
    try:
        assert svc._call(svc.announce_if_due(), timeout=10) and told == [("4.4.4.4", 9000)]
    finally:
        svc.stop()


def test_a_guest_coming_back_keeps_its_name_unless_somebody_holds_it(two_nodes):
    """After a node restarts, the viewer joins again for its page (2026-10-02),
    asking for the guest name it had, so the other players see the same player
    come back. A name somebody here holds now is never handed over: joining
    under a held name would be handed that seat."""
    (ca, sa), _ = two_nodes
    first = join(ca, sa)
    held = first["me"]["id"]
    again = lambda name: ca.post("/realtime/join", json={
        "csrf_token": sa.csrf_token, "game": "g1", "room": "town", "guest": name}).json()
    taken = again(held)
    assert taken["me"]["id"] != held and taken["token"] != first["token"], \
        "a seat somebody is in is not given to whoever names it"
    ca.post("/realtime/leave", json={"csrf_token": sa.csrf_token, "token": first["token"]})
    back = again(held)
    assert back["me"]["id"] == held, "the name is free again, so it comes back"
    assert again("not-a-guest-name")["me"]["id"].startswith("guest-")


def test_one_player_in_two_tabs_gets_two_sessions_that_both_hear_and_outlive_each_other(two_nodes):
    """ASHVALE and Ziibiing open in two tabs as the same account (2026-10-09): the
    tabs shared one session, so each heard half of the room and either one leaving
    took the other out with it -- they kicked each other off over and over and their
    saves failed meanwhile. Each tab now has its own session and its own copy of every
    event, and the player leaves the room only with its last tab."""
    (ca, sa), (cb, sb) = two_nodes
    svc = sa.mesh
    one = svc.join("g1/town", "nTabPlayer", None)
    two = svc.join("g1/town", "nTabPlayer", None)
    assert one.token != two.token and one.queue is not two.queue
    s1, s2 = Stream(ca, one.token), Stream(ca, two.token)
    jb = join(cb, sb, room="town")
    other = Stream(cb, jb["token"])
    try:
        cb.post("/realtime/send", json={"csrf_token": sb.csrf_token, "token": jb["token"], "data": "hello"})
        for s in (s1, s2):
            s.wait(lambda e: e["type"] == "message" and e.get("data") == "hello")
        # The first tab closes: the second is still in the room and still hears.
        svc.leave(one.token)
        cb.post("/realtime/send", json={"csrf_token": sb.csrf_token, "token": jb["token"], "data": "still there"})
        s2.wait(lambda e: e["type"] == "message" and e.get("data") == "still there")
        assert svc.present("g1/town", "nTabPlayer")
        # The last tab closes: now the player leaves the room.
        svc.leave(two.token)
        assert not svc.present("g1/town", "nTabPlayer")
    finally:
        s1.close(); s2.close(); other.close()
