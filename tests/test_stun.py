"""The nodes' STUN for the WebRTC fast lane (arcade/mesh/stun.py; 2026-10-18: "Nodes, then public").
A Binding answer any browser understands, a node's STUN offered only after another node reached it, and
/realtime/ice listing the nodes' STUN before the public one."""

import asyncio
import pathlib
import sys

from arcade.mesh import stun


def test_a_binding_answer_carries_the_senders_address_both_families():
    for host, port in (("203.0.113.7", 50123), ("2001:db8::42", 3478)):
        packet, tid = stun.binding_request()
        reply = stun.answer(packet, (host, port))
        assert stun.mapped_address(reply, tid) == (host, port)
    assert stun.answer(b"\x00" * 20, ("1.2.3.4", 1)) is None, "not a Binding request: no answer"
    assert stun.answer(b"GET / HTTP/1.1\r\n\r\n....", ("1.2.3.4", 1)) is None


def test_the_server_answers_a_real_probe():
    async def run():
        loop = asyncio.get_running_loop()
        t, _ = await loop.create_datagram_endpoint(lambda: stun._Server({}), local_addr=("127.0.0.1", 0))
        port = t.get_extra_info("sockname")[1]
        try:
            return await stun.probe("127.0.0.1", port), port
        finally:
            t.close()
    said, _port = asyncio.run(run())
    assert said and said[0] == "127.0.0.1"
    assert asyncio.run(stun.probe("127.0.0.1", 9, timeout=0.3)) is None, "nobody there: None, quickly"


class _Link:
    def __init__(self, peer_id, inbox):
        self.peer_id, self.inbox = peer_id, inbox

    async def send(self, header):
        self.inbox.append(header)


def test_a_node_offers_its_stun_only_after_another_node_reached_it():
    async def run():
        loop = asyncio.get_running_loop()
        t, _ = await loop.create_datagram_endpoint(lambda: stun._Server({}), local_addr=("127.0.0.1", 0))
        port = t.get_extra_info("sockname")[1]
        a = stun.StunShare(lambda: ["127.0.0.1", "192.0.2.99"], port=port, allow_private=True)
        b = stun.StunShare(None, allow_private=True)
        to_b, to_a = [], []

        class _Node:
            links = {}
        a.node, b.node = _Node(), _Node()
        a_link, b_link = _Link("b", to_b), _Link("a", to_a)     # a's link to b, b's link to a
        a.node.links = {"b": a_link}
        await a.link_up(a_link)
        assert a.mine() == [] and [h["op"] for h in to_b] == ["probe"], "nothing offered before it is proved"
        await b.on_frame(b_link, to_b.pop(0), b"")
        probed = to_a.pop(0)
        assert probed["ok"] == [["127.0.0.1", port]], "the address nobody answers on is not proved"
        await a.on_frame(a_link, probed, b"")
        assert a.mine() == [("127.0.0.1", port)]
        have = to_b.pop(0)
        assert have["op"] == "have"
        await b.on_frame(b_link, have, b"")
        assert b.known() == [("127.0.0.1", port)]
        # A "probed" nobody asked for proves nothing.
        await a.on_frame(a_link, {"t": "st", "op": "probed", "n": "forged", "ok": [["198.51.100.1", 1]]}, b"")
        assert ("198.51.100.1", 1) not in a.mine()
        await b.link_down("a")
        assert b.known() == []
        t.close()
    asyncio.run(run())


def test_private_addresses_are_never_offered_on_the_real_mesh():
    s = stun.StunShare(None)
    asyncio.run(s.on_frame(_Link("x", []), {"t": "st", "op": "have",
                                             "s": [["192.168.1.20", 3478], ["8.8.4.4", 3478]]}, b""))
    assert s.known() == [("8.8.4.4", 3478)]


def test_ice_lists_the_nodes_stun_then_the_public_one(tmp_path):
    from fastapi.testclient import TestClient

    from arcade.web.app import create_app
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from test_realtime_web import make_state

    state = make_state(tmp_path, "node")

    class _Mesh:
        node_id = "n"
        loop = object()
    _Mesh.stun = stun.StunShare(None)
    _Mesh.stun.offers["peer"] = [("2001:db8::1", 3478), ("203.0.113.5", 3478)]
    state.mesh = _Mesh()
    client = TestClient(create_app(state))
    said = client.get("/realtime/ice").json()
    assert [s["urls"] for s in said["iceServers"]] == [
        "stun:[2001:db8::1]:3478", "stun:203.0.113.5:3478", "stun:stun.cloudflare.com:3478"]
    state.set_setting("realtime_public_stun", False)
    assert [s["urls"] for s in client.get("/realtime/ice").json()["iceServers"]] == [
        "stun:[2001:db8::1]:3478", "stun:203.0.113.5:3478"], "the operator turned the public one off"


class _Answer:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def test_cloudflare_relay_logins_in_either_shape():
    """THE RELAY (web/realtime.py, 2026-10-23 "Cloudflare TURN"): Cloudflare's answer is a list of servers
    (generate-ice-servers) or one object (the older generate); only those with a login are the relay."""
    import pytest

    from arcade.web.realtime import turn_servers
    from arcade.mesh.node import MeshError
    asked = []
    listed = {"iceServers": [{"urls": ["stun:stun.cloudflare.com:3478"]},
                             {"urls": ["turn:turn.cloudflare.com:3478?transport=udp",
                                       "turns:turn.cloudflare.com:443?transport=tcp"],
                              "username": "u1", "credential": "c1"}]}

    def post(url, **kw):
        asked.append((url, kw))
        return _Answer(201, listed)
    got = turn_servers("ab" * 16, "tok", ttl=60, post=post)
    assert got == [{"urls": listed["iceServers"][1]["urls"], "username": "u1", "credential": "c1"}]
    assert asked[0][0].endswith("/turn/keys/" + "ab" * 16 + "/credentials/generate-ice-servers")
    assert asked[0][1]["headers"]["Authorization"] == "Bearer tok" and asked[0][1]["json"] == {"ttl": 60}
    one = {"iceServers": {"urls": ["turn:turn.cloudflare.com:3478"], "username": "u2", "credential": "c2"}}
    assert turn_servers("ab" * 16, "tok", post=lambda url, **kw: _Answer(201, one))[0]["username"] == "u2"
    with pytest.raises(MeshError):
        turn_servers("ab" * 16, "bad", post=lambda url, **kw: _Answer(401, {}))
    with pytest.raises(MeshError, match="cannot find specified key.*TURN key's own id"):
        turn_servers("ab" * 16, "tok", post=lambda url, **kw: _Answer(404, {"error": "cannot find specified key"}))


def test_the_relay_goes_to_players_who_are_somebody_and_not_to_guests(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from arcade.web import realtime
    from arcade.web.app import create_app
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from test_realtime_web import make_state

    state = make_state(tmp_path, "node")

    class _Mesh:
        node_id = "n"
        loop = object()
    _Mesh.stun = stun.StunShare(None)
    state.mesh = _Mesh()
    made = []

    def fake(key_id, token, ttl=realtime.TURN_TTL, post=None):
        made.append((key_id, token))
        return [{"urls": ["turn:turn.cloudflare.com:3478"], "username": "u", "credential": "c"}]
    monkeypatch.setattr(realtime, "turn_servers", fake)
    client = TestClient(create_app(state))
    assert client.get("/realtime/ice").json()["relay"] is False, "no key: no relay"
    state.set_setting("turn", {"key_id": "ab" * 16, "token": "tok"})
    monkeypatch.setattr(type(state), "derived_address", "nOperator" + "a" * 25, raising=False)
    said = client.get("/realtime/ice").json()
    assert said["relay"] is True and said["iceServers"][-1]["username"] == "u", said
    client.get("/realtime/ice")
    assert len(made) == 1, "kept, not asked of Cloudflare every time"
    # A stranger on the public site (a guest) gets no relay.
    public = TestClient(create_app(state), base_url="https://app.example.com")
    monkeypatch.setattr(realtime, "turn_servers", fake)
    said = public.get("/realtime/ice", headers={"Host": "app.example.com", "X-Forwarded-For": "203.0.113.9"}).json()
    assert said.get("relay") is not True


def test_the_meter_reads_this_months_egress_for_the_key():
    """THE METER (2026-10-23: "a usage meter on my node so that I don't go over the limit"): Cloudflare's
    GraphQL, this calendar month, this key; billed by egress."""
    import datetime

    import pytest

    from arcade.mesh.node import MeshError
    from arcade.web.realtime import turn_usage
    asked = []
    body = {"data": {"viewer": {"accounts": [{"callsTurnUsageAdaptiveGroups": [
        {"sum": {"egressBytes": 2_500_000_000, "ingressBytes": 100}}, {"sum": {"egressBytes": 500_000_000}}]}]}}}

    def post(url, **kw):
        asked.append(kw)
        return _Answer(200, body)
    got = turn_usage("cd" * 16, "an", "ab" * 16, today=datetime.date(2026, 10, 23), post=post)
    assert got == {"egress": 3_000_000_000, "ingress": 100, "from": "2026-10-01", "to": "2026-10-23"}
    v = asked[0]["json"]["variables"]
    assert v == {"account": "cd" * 16, "key": "ab" * 16, "from": "2026-10-01", "to": "2026-10-23"}
    assert asked[0]["headers"]["Authorization"] == "Bearer an"
    with pytest.raises(MeshError, match="not authorized"):
        turn_usage("cd" * 16, "bad", "ab" * 16, post=lambda url, **kw: _Answer(200, {"errors": [{"message": "not authorized"}]}))


def test_at_the_limit_the_relay_is_no_longer_handed_out(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from arcade.web import realtime
    from arcade.web.app import create_app
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from test_realtime_web import make_state

    state = make_state(tmp_path, "node")

    class _Mesh:
        node_id = "n"
        loop = object()
    _Mesh.stun = stun.StunShare(None)
    state.mesh = _Mesh()
    monkeypatch.setattr(type(state), "derived_address", "nOperator" + "a" * 25, raising=False)
    monkeypatch.setattr(realtime, "turn_servers", lambda *a, **k: [{"urls": ["turn:x"], "username": "u", "credential": "c"}])
    used = {"egress": 0}
    monkeypatch.setattr(realtime, "turn_usage", lambda *a, **k: {"egress": used["egress"], "ingress": 0,
                                                                  "from": "2026-10-01", "to": "2026-10-23"})
    state.set_setting("turn", {"key_id": "ab" * 16, "token": "t", "account_id": "cd" * 16, "analytics": "an",
                               "limit_gb": 10})
    client = TestClient(create_app(state))
    assert client.get("/realtime/ice").json()["relay"] is True
    meter = state.relay_meter(fresh=True)
    assert meter["metered"] and meter["used_gb"] == 0 and not meter["over"]
    used["egress"] = 10_500_000_000
    assert state.relay_meter(fresh=True)["over"] is True
    assert client.get("/realtime/ice").json()["relay"] is False, "over the month's limit: direct links only"


def test_the_operator_chooses_which_apps_get_the_relay(tmp_path, monkeypatch):
    """2026-10-23: "what apps on my arcade are allowed to use my relay?" -- "Apps I choose, any account".
    None chosen yet: every app (nothing breaks); chosen: only those pages."""
    from fastapi.testclient import TestClient

    from arcade.web import realtime
    from arcade.web.app import create_app
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from test_realtime_web import make_state

    state = make_state(tmp_path, "node")

    class _Mesh:
        node_id = "n"
        loop = object()
    _Mesh.stun = stun.StunShare(None)
    state.mesh = _Mesh()
    monkeypatch.setattr(type(state), "derived_address", "nOperator" + "a" * 25, raising=False)
    monkeypatch.setattr(realtime, "turn_servers", lambda *a, **k: [{"urls": ["turn:x"], "username": "u", "credential": "c"}])
    call, chess = "a7" * 32, "c5" * 32
    state.set_setting("turn", {"key_id": "ab" * 16, "token": "t"})
    client = TestClient(create_app(state))
    assert client.get(f"/realtime/ice?app={chess}").json()["relay"] is True, "none chosen: every app"
    state.set_setting("turn", {"key_id": "ab" * 16, "token": "t", "apps": [call]})
    assert client.get(f"/realtime/ice?app={call}").json()["relay"] is True
    assert client.get(f"/realtime/ice?app={chess}").json()["relay"] is False, "not chosen: direct links only"
    assert client.get("/realtime/ice").json()["relay"] is False, "an asker that names no app gets none"
