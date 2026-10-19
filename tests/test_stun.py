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
