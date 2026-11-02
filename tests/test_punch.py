"""The punch probe (arcade/mesh/punch.py, 2026-10-30): two linked arcades map one UDP socket each through two STUN
servers, swap addresses over the link, and send to each other at the same time; each side says what got through."""

import asyncio
import ipaddress
import struct

from arcade.mesh import punch, stun


class _Link:
    """A mesh link that hands frames straight to the other side's extension."""

    def __init__(self, peer_id):
        self.peer_id, self.other = peer_id, None

    async def send(self, header, blob=b""):
        await self.other.on_frame(self.back, header, blob)


class _Node:
    def __init__(self):
        self.links = {}


def _pair(duration=1.0, servers=()):
    a = punch.PunchShare(lambda: list(servers), duration=duration, public_stun=())
    b = punch.PunchShare(lambda: list(servers), duration=duration, public_stun=())
    a.node, b.node = _Node(), _Node()
    to_b, to_a = _Link("bb" * 32), _Link("aa" * 32)
    to_b.other, to_b.back = b, to_a
    to_a.other, to_a.back = a, to_b
    a.node.links[to_b.peer_id] = to_b
    b.node.links[to_a.peer_id] = to_a
    return a, b


async def _stun_servers():
    loop = asyncio.get_running_loop()
    out = []
    for host in ("127.0.0.1", "127.0.0.2"):
        t, _ = await loop.create_datagram_endpoint(lambda: stun._Server({}), local_addr=(host, 0))
        out.append(t)
    return out, [t.get_extra_info("sockname")[:2] for t in out]


def test_two_nodes_punch_through_and_both_say_so():
    async def run():
        transports, servers = await _stun_servers()
        try:
            a, b = _pair(servers=servers)
            rec = await a.run("bb" * 32)
            return rec, b.results
        finally:
            for t in transports:
                t.close()
    rec, theirs = asyncio.run(run())
    assert rec["ok"] and rec["direct"], rec
    assert len(rec["mine"]["maps"]) == 2 and all(m["host"] for m in rec["mine"]["maps"]), "two STUN answers"
    assert rec["mine"]["heard"] and rec["theirs"]["heard"], "each got an answer to its own packets"
    assert rec["theirs"]["got_from"], "the other side's packets count is passed back"
    assert theirs and theirs[-1]["role"] == "answered" and theirs[-1]["direct"]


def test_the_nat_kind_comes_from_the_two_stun_answers():
    same = [{"server": "1.1.1.1:3478", "host": "129.222.44.64", "port": 40000},
            {"server": "2.2.2.2:3478", "host": "129.222.44.64", "port": 40000}]
    assert punch.nat_kind(same, "192.168.1.20") == "same-port"
    moved = [dict(same[0]), dict(same[1], port=40001)]
    assert punch.nat_kind(moved, "192.168.1.20") == "new-port"
    assert punch.nat_kind(same[:1], "192.168.1.20") == "unknown"
    assert punch.nat_kind([dict(m, host="156.67.28.112") for m in same], "156.67.28.112") == "open"


def test_a_node_answers_one_probe_at_a_time_and_says_no_to_quick_repeats():
    async def run():
        a, b = _pair(duration=0.3)
        first = await a.run("bb" * 32)
        again = await a.run("bb" * 32)
        return first, again
    first, again = asyncio.run(run())
    assert first["ok"], first
    assert not again["ok"] and "busy" in again["detail"], again


def test_only_ipv4_addresses_and_only_a_few_are_aimed_at():
    raw = [["10.0.0.1", 5], ["::1", 5], ["10.0.0.2", 0], ["10.0.0.3", "x"], ["host.example", 5],
           ["10.0.0.4", 6], ["10.0.0.5", 7], ["10.0.0.6", 8], ["10.0.0.7", 9]]
    got = punch._addrs(raw)
    assert got == [("10.0.0.1", 5)], "only the first MAX_ADDRS are even looked at, junk dropped"
    assert len(punch._addrs([["10.0.0.%d" % i, 9] for i in range(1, 10)])) == punch.MAX_ADDRS


def test_not_linked_says_so():
    a, _ = _pair()
    rec = asyncio.run(a.run("cc" * 32))
    assert not rec["ok"] and "not linked" in rec["detail"]


def test_an_old_arcades_ipv6_mapped_answer_is_the_same_ipv4_address():
    """Arcades before the 2026-10-29 STUN fix answer ::ffff:81e0:d389 for 129.224.211.137 (seen in the first live
    probe, 2026-10-30: Starlink was called "new-port" because of it)."""
    class _Old(stun._Server):
        def datagram_received(self, data, addr):
            packet = stun.answer(data, addr)
            tid = data[8:20]
            value = struct.pack("!BBH", 0, 2, addr[1] ^ (stun.MAGIC >> 16)) + (
                int(ipaddress.ip_address("::ffff:" + addr[0]))
                ^ int.from_bytes(struct.pack("!I", stun.MAGIC) + tid, "big")).to_bytes(16, "big")
            attrs = struct.pack("!HH", stun.XOR_MAPPED_ADDRESS, len(value)) + value
            self.transport.sendto(struct.pack("!HHI", stun.BINDING_SUCCESS, len(attrs), stun.MAGIC) + tid + attrs,
                                  addr)

    async def run():
        loop = asyncio.get_running_loop()
        new, _ = await loop.create_datagram_endpoint(lambda: stun._Server({}), local_addr=("127.0.0.1", 0))
        old, _ = await loop.create_datagram_endpoint(lambda: _Old({}), local_addr=("127.0.0.2", 0))
        s = punch.Session("x" * 12, "a")
        await s.open()
        try:
            await s.map([new.get_extra_info("sockname")[:2], old.get_extra_info("sockname")[:2]])
            return s.maps
        finally:
            s.close()
            new.close()
            old.close()
    maps = asyncio.run(run())
    assert [m["host"] for m in maps] == ["127.0.0.1", "127.0.0.1"], maps
    assert punch.nat_kind(maps, "10.9.9.9") == "same-port"


# The addresses from the report this fixes: a dial-out node was sending at them on its own LAN.
_REMOTE = [("156.67.28.112", 41658), ("192.168.1.20", 32805), ("192.168.1.20", 41658),
           ("10.1.2.3", 9), ("172.16.4.4", 9), ("100.64.12.1", 9), ("169.254.8.8", 9), ("127.0.0.1", 80)]
_OURS = ["129.222.44.64"]


def test_private_link_local_loopback_and_cgnat_are_not_public_addresses():
    assert punch.routable_public("8.8.8.8") and punch.routable_public("129.222.44.64")
    assert punch.routable_public("203.0.113.8") is False           # documentation range, not a place to send
    assert punch.routable_public("198.51.100.9") is False
    for host in ("192.168.1.20", "192.168.1.20", "10.0.0.1", "172.16.0.1", "172.31.255.1",
                 "127.0.0.1", "169.254.1.1", "100.64.0.1", "100.127.255.254", "0.0.0.0", "224.0.0.1"):
        assert punch.routable_public(host) is False, host
    assert punch.routable_public("100.128.0.1") and punch.routable_public("172.32.0.1")
    assert punch.routable_public("11.0.0.1")
    assert punch.lan_dial_mode(None) == punch.lan_dial_mode("") == punch.lan_dial_mode("nope") == "auto"
    assert punch.lan_dial_mode("on") == punch.lan_dial_mode(True) == "on"
    assert punch.lan_dial_mode("off") == punch.lan_dial_mode(False) == "off"

    def broken():
        raise RuntimeError("settings could not be read")
    assert punch.call_mode(broken) == "auto"


def test_a_peer_on_another_network_is_not_dialed_at_its_home_address():
    """What the socket was doing: 192.168.1.20 and 192.168.1.20, plus the other non-global ranges."""
    assert punch.dialable(_REMOTE, _OURS, "auto") == [("156.67.28.112", 41658)]
    assert punch.same_outside(_OURS, _REMOTE) is False
    assert punch.include_lan(_OURS, _REMOTE, "auto") is False
    assert punch.include_lan(_OURS, [], "auto") is False, "the asker has not seen the peer yet"
    # A shared private address is somebody else's LAN that uses the same number, not proof of one NAT.
    assert punch.same_outside(["192.168.1.20"], [("192.168.1.20", 32805)]) is False
    assert punch.spray_at([("192.168.1.20", 32805), ("156.67.28.112", 2)], _OURS) == "156.67.28.112"
    assert punch.spray_at([("192.168.1.20", 32805)], _OURS) is None
    assert punch.spray_at([("127.0.0.1", 9)], ["127.0.0.1"]) == "127.0.0.1"


def test_the_same_public_address_means_the_home_network_is_this_one():
    peer = [("129.222.44.64", 40000), ("192.168.1.20", 32805), ("192.168.1.20", 41658),
            ("100.64.1.1", 9), ("169.254.1.1", 9), ("127.0.0.1", 9)]
    assert punch.dialable(peer, _OURS, "auto") == [
        ("129.222.44.64", 40000), ("192.168.1.20", 32805), ("192.168.1.20", 41658),
        ("100.64.1.1", 9), ("169.254.1.1", 9)]
    assert punch.include_lan(_OURS, peer, "auto") is True
    # Loopback is dialed when this node itself mapped to it, and not because a peer wrote it down.
    assert punch.dialable([("127.0.0.1", 3478), ("192.168.1.20", 1)], ["127.0.0.1"], "auto") == [("127.0.0.1", 3478)]


def test_the_operator_can_allow_or_refuse_home_network_addresses():
    assert punch.dialable(_REMOTE, _OURS, "off") == [("156.67.28.112", 41658)]
    assert punch.include_lan(_OURS, [("129.222.44.64", 1)], "off") is False
    allowed = punch.dialable(_REMOTE, _OURS, "on")
    assert ("192.168.1.20", 32805) in allowed and ("192.168.1.20", 41658) in allowed
    assert ("100.64.12.1", 9) in allowed and ("169.254.8.8", 9) in allowed
    assert ("127.0.0.1", 80) not in allowed, "on does not aim at this machine's loopback"
    assert punch.dialable([("224.0.0.1", 9), ("0.0.0.0", 9), ("255.255.255.255", 9)], _OURS, "on") == []
    assert punch.include_lan(_OURS, [], "on") is True


def test_tcp_targets_follow_the_same_rule_as_udp():
    """What the TCP dialer was doing after UDP gave up: 192.168.1.20:8421, another operator's home LAN.
    The public endpoint is kept. The home one is kept only when that public address is ours."""
    other = [
        ("156.67.28.112", 8421),
        ("192.168.1.20", 8421),
        ("192.168.1.20", 8421),
        ("10.1.2.3", 8421),
        ("172.16.4.4", 8421),
        ("100.64.12.1", 8421),
        ("169.254.8.8", 8421),
        ("::ffff:192.168.1.20", 8421),
        ("wss://192.168.1.20/mesh", 443),
        ("wss://node.trycloudflare.com/mesh", 443),
        ("2606:4700:4700::1111", 8421),
        ("fd00::25", 8421),
        ("fe80::25", 8421),
    ]
    kept = punch.tcp_targets(other, _OURS, "auto")
    assert ("156.67.28.112", 8421) in kept
    assert ("wss://node.trycloudflare.com/mesh", 443) in kept
    assert ("2606:4700:4700::1111", 8421) in kept
    for blocked in (("192.168.1.20", 8421), ("192.168.1.20", 8421), ("10.1.2.3", 8421),
                    ("172.16.4.4", 8421), ("100.64.12.1", 8421), ("169.254.8.8", 8421),
                    ("::ffff:192.168.1.20", 8421), ("wss://192.168.1.20/mesh", 443),
                    ("fd00::25", 8421), ("fe80::25", 8421)):
        assert blocked not in kept, blocked
    # Judged on its own, the home address has no public sibling, so it is not this NAT.
    assert punch.tcp_targets([("192.168.1.20", 8421)], _OURS, "auto") == []

    same = [("129.222.44.64", 8421), ("192.168.1.20", 8421), ("192.168.1.20", 8421),
            ("100.64.1.1", 8421), ("169.254.1.1", 8421), ("fd00::25", 8421), ("fe80::1", 8421),
            ("127.0.0.1", 8421)]
    allowed = punch.tcp_targets(same, _OURS, "auto")
    assert ("129.222.44.64", 8421) in allowed
    assert ("192.168.1.20", 8421) in allowed and ("192.168.1.20", 8421) in allowed
    assert ("100.64.1.1", 8421) in allowed and ("169.254.1.1", 8421) in allowed
    assert ("fd00::25", 8421) in allowed and ("fe80::1", 8421) in allowed
    assert ("127.0.0.1", 8421) not in allowed, "a public STUN answer means a peer's loopback is this machine"

    assert ("192.168.1.20", 8421) not in punch.tcp_targets(same, _OURS, "off")
    assert ("192.168.1.20", 8421) in punch.tcp_targets(other, _OURS, "on")
    # Two nodes on one machine, before STUN has a public answer, still dial loopback.
    assert punch.tcp_targets([("127.0.0.1", 8421), ("::1", 8421)], [], "auto") == [
        ("127.0.0.1", 8421), ("::1", 8421)]
    assert punch.tcp_targets([("127.0.0.1", 8421)], [], "off") == []
    assert punch.is_home_host("192.168.1.20") and not punch.is_home_host("156.67.28.112")
    assert punch.is_home_host("wss://192.168.1.20/mesh") and not punch.is_home_host("127.0.0.1")
    assert punch.is_home_host("fe80::25") and not punch.is_home_host("2606:4700:4700::1111")


def test_an_offer_keeps_the_home_address_for_a_peer_on_this_nat(monkeypatch):
    monkeypatch.setattr(punch, "local_ipv4", lambda: "192.168.1.20")
    me = punch.Session("n", "b")
    me.local_port = 32805
    me.maps = [{"host": "129.222.44.64", "port": 40000, "server": "1.1.1.1:3478"}]
    assert me.offer() == [["129.222.44.64", 40000]]
    assert me.offer(include_lan=True) == [["129.222.44.64", 40000], ["192.168.1.20", 32805]]
    share = punch.PunchShare(lan_dial=lambda: "auto")
    assert share._say(me, [["156.67.28.112", 41658]]) == [["129.222.44.64", 40000]]
    assert share._say(me, [["129.222.44.64", 40001]]) == [["129.222.44.64", 40000], ["192.168.1.20", 32805]]
    assert share._aim([["156.67.28.112", 41658], ["192.168.1.20", 32805], ["192.168.1.20", 41658]], me) == [
        ("156.67.28.112", 41658)]
    assert punch.PunchShare(lan_dial=lambda: "off")._say(me, [["129.222.44.64", 1]]) == [["129.222.44.64", 40000]]
    assert punch.PunchShare(lan_dial=lambda: "on")._say(me, None) == [
        ["129.222.44.64", 40000], ["192.168.1.20", 32805]]


def test_the_probe_punches_the_mapped_address_and_not_the_home_network(monkeypatch):
    monkeypatch.setattr(punch, "local_ipv4", lambda: "192.168.1.20")

    async def run():
        transports, servers = await _stun_servers()
        sent = []
        try:
            a, b = _pair(servers=servers)

            def watch(link):
                real = link.send

                async def capture(header, blob=b""):
                    if header.get("op") in ("try", "ready"):
                        sent.append(header.get("a"))
                    await real(header, blob)
                link.send = capture
            watch(a.node.links["bb" * 32])
            watch(b.node.links["aa" * 32])
            return await a.run("bb" * 32), sent
        finally:
            for t in transports:
                t.close()
    rec, sent = asyncio.run(run())
    assert rec["ok"] and rec["direct"], rec
    assert sent, "both sides sent an offer"
    for offer in sent:
        assert offer and all(str(addr[0]).startswith("127.") for addr in offer), offer
        assert not any(addr[0] == "192.168.1.20" for addr in offer)
