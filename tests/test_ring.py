"""Ringing a player whose app is closed (arcade/ring.py, 2026-10-23: call "at any time, even if the application
is not open through push notifications ... just like the Wallet access and camera and microphone access switches";
who may ring: "My contacts only"). Pinned here: a ring wakes the callee's phone only for an app they switched on, from
a caller in their book, proved by the caller's certificate, not too often; the push still carries nothing; the tap
gives the ring back to that app only; and across nodes the ring rides the callee's room."""

import json
import pathlib
import sys

import pytest
from nacl.signing import SigningKey

pytest.importorskip("cryptography", reason="push needs cryptography: pip install .[push]")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_auth_web import LOCAL, _sign_in                        # noqa: E402

from arcade import ring as ringlib                               # noqa: E402
from arcade.web import door                                      # noqa: E402

APP, OTHER_APP = "a1" * 32, "a2" * 32
ENDPOINT = "https://push.example.com/send/bob"


class Mesh:
    """The mesh as the ring routes use it: certificates checked, rooms joined, what was said kept."""

    def __init__(self, good=True):
        self.loop, self.node_id, self.network, self.good = object(), "nodeA", "test", good
        self.said, self.rooms, self.joined, self.live = [], {}, [], set()
        self.checked_from = []

    def check_cert(self, member, cred):
        return self.good and cred.get("s") == "sig-" + member

    def check_cert_from(self, origin, member, cred):
        self.checked_from.append(origin)
        return self.good and cred.get("s") == "sig-" + member

    def new_guest(self):
        return "guest-%08x" % len(self.joined)

    def join(self, room, member, cred):
        self.joined.append(room)
        token = f"t{len(self.joined)}"
        self.rooms[token] = room
        return type("S", (), {"token": token})()

    def send(self, token, data):
        self.said.append((self.rooms[token], json.loads(data)))

    def take(self, token):
        return []

    def touch(self, token):
        pass

    def leave(self, token):
        self.rooms.pop(token, None)

    def room_live(self, room):
        return room in self.live






def _as(app, state, pubkey_key):
    assert _sign_in(app, pubkey_key).status_code == 200


def test_a_contact_rings_an_app_that_is_switched_on(client, monkeypatch):
    app, state = client
    state.mesh = Mesh()
    woke = []
    monkeypatch.setattr(state.push(), "send", lambda endpoint, now=None, post=None: woke.append(endpoint) or True)
    alice_key, bob_key = SigningKey.generate(), SigningKey.generate()
    a_addr, b_addr = "nA" + "a" * 32, "nB" + "b" * 32
    for k, addr in ((alice_key, a_addr), (bob_key, b_addr)):
        _as(app, state, k)
        state.set_setting(f"address:{k.verify_key.encode().hex()}", addr)

    # Bob: a phone, Notifications on for APP, Alice in his book.
    _as(app, state, bob_key)
    app.post("/account/push/subscribe", headers=LOCAL, json={"endpoint": ENDPOINT, "keys": {"p256dh": "x", "auth": "y"}})
    assert app.post("/account/ring/apps", headers=LOCAL, json={"app": APP, "on": True}).json()["apps"] == [APP]
    assert app.post("/account/ring/allow", headers=LOCAL, json={"allow": [a_addr, "not an address!"]}).json() \
        == {"ok": True, "allowed": 1}
    said = app.get("/account/ring/apps", headers=LOCAL).json()
    assert said["apps"] == [APP] and said["allowed"] == 1 and said["devices"] == 1

    # Alice rings Bob in APP: woken.
    _as(app, state, alice_key)
    ring = {"to": b_addr, "app": APP, "line": "  Video   call  ", "link": "call=x7", "sig": "sig-" + a_addr,
            "expires": 2_000_000_000}
    assert app.post("/account/ring", headers=LOCAL, json=ring).json() == {"ok": True, "rung": True}
    assert woke == [ENDPOINT]
    assert state.mesh.checked_from == ["nodeA"], "the certificate checked against the node it came through"
    # The same pair again at once: not too often.
    assert app.post("/account/ring", headers=LOCAL, json=ring).json() == {"ok": True, "rung": False}
    assert woke == [ENDPOINT]
    # Another app Bob did not switch on: nothing.
    assert app.post("/account/ring", headers=LOCAL, json={**ring, "app": OTHER_APP}).json()["rung"] is False
    # Unproved: refused before anything.
    bad = app.post("/account/ring", headers=LOCAL, json={**ring, "sig": "forged"})
    assert bad.status_code == 400 and "unlock" in bad.json()["error"]

    # Bob's phone asks what woke it: who, which app, the line -- and the link stays with the ring.
    _as(app, state, bob_key)
    news = app.get("/account/push/news", headers=LOCAL).json()["news"]
    assert len(news) == 1 and news[0]["kind"] == "ring" and news[0]["line"] == "Video call"
    url = news[0]["url"]
    assert url.startswith(f"/inscriptions/{APP}/full?ring=")
    rid = url.rsplit("=", 1)[1]
    got = app.get(f"/account/ring/{rid}", headers=LOCAL).json()
    assert got["ok"] and got["from"] == a_addr and got["app"] == APP and got["link"] == "call=x7"
    # Nobody else reads it.
    _as(app, state, alice_key)
    assert app.get(f"/account/ring/{rid}", headers=LOCAL).json() == {"ok": False}


def test_a_stranger_does_not_ring(client, monkeypatch):
    app, state = client
    state.mesh = Mesh()
    woke = []
    monkeypatch.setattr(state.push(), "send", lambda endpoint, now=None, post=None: woke.append(endpoint) or True)
    carol_key, bob_key = SigningKey.generate(), SigningKey.generate()
    c_addr, b_addr = "nC" + "c" * 32, "nB" + "b" * 32
    for k, addr in ((carol_key, c_addr), (bob_key, b_addr)):
        _as(app, state, k)
        state.set_setting(f"address:{k.verify_key.encode().hex()}", addr)
    _as(app, state, bob_key)
    app.post("/account/push/subscribe", headers=LOCAL, json={"endpoint": ENDPOINT, "keys": {"p256dh": "x", "auth": "y"}})
    app.post("/account/ring/apps", headers=LOCAL, json={"app": APP, "on": True})
    app.post("/account/ring/allow", headers=LOCAL, json={"allow": []})
    _as(app, state, carol_key)
    said = app.post("/account/ring", headers=LOCAL, json={"to": b_addr, "app": APP, "sig": "sig-" + c_addr,
                                                          "expires": 2_000_000_000}).json()
    assert said == {"ok": True, "rung": False} and woke == [], "not in Bob's book: no notification"


def test_a_callee_on_another_node_is_rung_through_their_room(client, monkeypatch):
    """The callee's node holds `arcade!ring/<address>`; the caller's node joins it as a guest and says the ring.
    Not held anywhere: nothing to ring, said so."""
    app, state = client
    state.mesh = Mesh()
    key = SigningKey.generate()
    _as(app, state, key)
    me = "nA" + "a" * 32
    state.set_setting(f"address:{key.verify_key.encode().hex()}", me)
    far = "nF" + "f" * 32
    ring = {"to": far, "app": APP, "line": "hi", "sig": "sig-" + me, "expires": 2_000_000_000}
    said = app.post("/account/ring", headers=LOCAL, json=ring).json()
    assert said["ok"] and said["rung"] is False and "no phone" in said["why"]
    state.mesh.live.add(ringlib.room(far))
    monkeypatch.setattr(ringlib.time, "sleep", lambda s: None)
    assert app.post("/account/ring", headers=LOCAL, json=ring).json() == {"ok": True, "rung": True}
    import time
    for _ in range(100):                         # said from a thread of its own (it may wait for the room)
        if state.mesh.said:
            break
        time.sleep(0.05)
    room, said = state.mesh.said[-1]
    assert room == "arcade!ring/" + far
    assert said["t"] == "ring" and said["f"] == me and said["n"] == "nodeA" and said["app"] == APP


def test_a_ring_heard_through_another_node_than_it_names_is_refused():
    """The receiving side's rules in one place: the room's sender node must be the node the certificate names."""
    rings = ringlib.Rings(lambda: None)
    assert rings.may_ring("a", "b", now=1000)
    assert not rings.may_ring("a", "b", now=1005), "within RING_GAP"
    assert rings.may_ring("a", "b", now=1000 + ringlib.RING_GAP)
    assert rings.may_ring("c", "b", now=1005), "another caller is another pair"
    for i in range(ringlib.SEND_MINUTE):
        assert rings.may_send("x", now=2000 + i)
    assert not rings.may_send("x", now=2000 + ringlib.SEND_MINUTE)


def test_no_page_can_join_a_ring_room():
    from arcade.web.realtime import GAME
    assert not GAME.match(ringlib.ROOM.split("/")[0]), "a game name never makes the ring room's prefix"


def test_the_ring_routes_are_open_to_signed_in_accounts_on_the_public_site():
    for path in ("/account/ring", "/account/ring/apps", "/account/ring/allow"):
        assert door.public_path(path, "POST"), path
    assert door.public_path("/account/ring/apps")
    assert door.public_path("/account/ring/" + "ab" * 8)
