"""UDP beside TCP on a mesh link (arcade/mesh/udp.py): two real nodes over loopback, and a path pair over a bad wire."""

import asyncio
import os
import random
import time

import nacl.signing
import pytest

from arcade.mesh import stun, udp
from arcade.mesh.link import LinkError
from arcade.mesh.node import MeshNode


class _Taker:
    """An extension that records frames of its own kind, in order."""
    kinds = ("zz",)

    def __init__(self):
        self.got = []
        self.node = None

    async def link_up(self, link): pass
    async def link_down(self, peer_id): pass
    async def rebalance(self): pass

    async def on_frame(self, link, header, blob):
        self.got.append((header.get("i"), bytes(blob)))


async def _until(check, timeout=8.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if check():
            return True
        await asyncio.sleep(0.01)
    return bool(check())


class _Two:
    """Two nodes, the lower id first, linked by TCP; with UDP on each unless told otherwise."""

    def __init__(self, second_udp=True):
        self.second_udp = second_udp

    async def __aenter__(self):
        keys = sorted((nacl.signing.SigningKey.generate() for _ in range(2)), key=lambda k: bytes(k.verify_key).hex())
        loop = asyncio.get_running_loop()
        self.stun, _ = await loop.create_datagram_endpoint(lambda: stun._Server({}), local_addr=("127.0.0.1", 0))
        self.servers = [self.stun.get_extra_info("sockname")[:2]]
        self.a = MeshNode(keys[0], "t", listen_host="127.0.0.1", listen_port=0)
        self.b = MeshNode(keys[1], "t", listen_host="127.0.0.1", listen_port=0)
        self.ua = udp.UdpShare(lambda: self.servers, try_after=0.05)
        self.ub = udp.UdpShare(lambda: self.servers, try_after=0.05)
        self.ta, self.tb = _Taker(), _Taker()
        self.a.add_extension(self.ua)
        self.a.add_extension(self.ta)
        if self.second_udp:
            self.b.add_extension(self.ub)
        self.b.add_extension(self.tb)
        await self.a.start()
        await self.b.start()
        self.a.add_address("127.0.0.1", self.b.listen_port)
        assert await _until(lambda: self.b.node_id in self.a.links and self.a.node_id in self.b.links)
        self.la, self.lb = self.a.links[self.b.node_id], self.b.links[self.a.node_id]
        return self

    async def up(self):
        assert await _until(lambda: self.la.path is not None and self.lb.path is not None), self.ua.status()

    async def __aexit__(self, *exc):
        await self.a.stop()
        await self.b.stop()
        self.stun.close()


def _tcp_writes(link):
    """Count frames by the connection, by kind."""
    seen = []
    write = link._write

    async def counted(header, blob=b""):
        seen.append(header.get("t"))
        await write(header, blob)
    link._write = counted
    return seen


def test_a_link_moves_to_udp_and_frames_arrive_in_order_without_touching_tcp():
    async def run():
        async with _Two() as t:
            await t.up()
            seen = _tcp_writes(t.la)
            for i in range(200):
                await t.la.send({"t": "zz", "i": i}, b"x" * 50)
            assert await _until(lambda: len(t.tb.got) == 200)
            assert [g[0] for g in t.tb.got] == list(range(200))
            assert "zz" not in seen, "every frame went by UDP"
            assert t.ub.status()[0]["frames_in"] >= 200
    asyncio.run(run())


def test_both_directions_and_a_full_size_frame_are_cut_up_and_put_back():
    async def run():
        async with _Two() as t:
            await t.up()
            big = os.urandom(60_000)
            await t.la.send({"t": "zz", "i": 1}, big)
            await t.lb.send({"t": "zz", "i": 2}, big[::-1])
            assert await _until(lambda: t.tb.got and t.ta.got)
            assert t.tb.got == [(1, big)] and t.ta.got == [(2, big[::-1])]
    asyncio.run(run())


def test_when_udp_dies_nothing_is_lost_or_repeated_and_the_link_stays(monkeypatch):
    """The wire is cut with frames in flight, both ways. The backup carries what was not delivered, then the rest."""
    monkeypatch.setattr(udp, "DEAD_WITH_DATA", 0.3)

    async def run():
        async with _Two() as t:
            await t.up()
            pa, pb = t.la.path, t.lb.path
            pa.sendto = pb.sendto = lambda *a: None          # the wire goes
            for i in range(300):
                await t.la.send({"t": "zz", "i": i}, b"y" * 2000)
                await t.lb.send({"t": "zz", "i": i}, b"z" * 2000)
            for i in range(300, 320):
                await t.la.send({"t": "zz", "i": i})
                await t.lb.send({"t": "zz", "i": i})
            assert await _until(lambda: len(t.tb.got) == 320 and len(t.ta.got) == 320), (len(t.tb.got), len(t.ta.got))
            assert [g[0] for g in t.tb.got] == list(range(320))
            assert [g[0] for g in t.ta.got] == list(range(320))
            assert await _until(lambda: t.la.path is None and t.lb.path is None and not t.ua.paths and not t.ub.paths)
            assert t.a.links[t.b.node_id] is t.la and t.b.links[t.a.node_id] is t.lb, "the same link throughout"
    asyncio.run(run())


def test_a_failed_path_is_opened_again(monkeypatch):
    monkeypatch.setattr(udp, "DEAD_IDLE", 0.2)
    monkeypatch.setattr(udp, "RETRY_FIRST", 0.01)

    async def run():
        async with _Two() as t:
            await t.up()
            first = t.la.path
            first.sendto = t.lb.path.sendto = lambda *a: None
            assert await _until(lambda: t.la.path is None and t.ua.paths == {})
            await asyncio.sleep(0.05)
            await t.ua.rebalance()
            await t.up()
            assert t.la.path is not first
            await t.la.send({"t": "zz", "i": 7})
            assert await _until(lambda: t.tb.got == [(7, b"")])
    asyncio.run(run())


def test_only_the_backup_going_ends_the_link_and_the_path_with_it():
    async def run():
        async with _Two() as t:
            await t.up()
            path = t.la.path
            t.lb.writer.close()
            assert await _until(lambda: t.b.node_id not in t.a.links or t.a.links[t.b.node_id] is not t.la)
            assert path.dead
    asyncio.run(run())


def test_a_forged_or_repeated_packet_changes_nothing():
    async def run():
        async with _Two() as t:
            await t.up()
            pb = t.lb.incoming or t.ub.paths[t.a.node_id]
            taken = []
            wire = t.la.path.sendto

            def tap(packet, addr):
                taken.append(packet)
                wire(packet, addr)
            t.la.path.sendto = tap
            await t.la.send({"t": "zz", "i": 1}, b"real")
            assert await _until(lambda: len(t.tb.got) == 1)
            data = next(p for p in taken if len(p) > 60)
            where = pb.addr
            pb.received(data, ("9.9.9.9", 1))                                # replayed, from elsewhere
            bad = data[:-1] + bytes([data[-1] ^ 1])
            pb.received(udp.HEAD.pack(udp.MAGIC, pb.cid_in, 10 ** 6) + bad[udp.HEAD.size:], ("9.9.9.9", 1))
            await asyncio.sleep(0.05)
            assert t.tb.got == [(1, b"real")] and pb.bad == 1
            assert pb.addr == where, "only a packet that opens moves the path"
    asyncio.run(run())


def test_a_node_without_udp_stays_on_its_connection(monkeypatch):
    monkeypatch.setattr(udp, "ANSWER_WAIT", 0.2)

    async def run():
        async with _Two(second_udp=False) as t:
            assert await _until(lambda: t.b.node_id in t.ua.retry)
            await t.la.send({"t": "zz", "i": 1})
            assert await _until(lambda: t.tb.got == [(1, b"")])
            assert t.la.path is None and not t.ua.paths
    asyncio.run(run())


def _pair(wire):
    k1, k2 = os.urandom(32), os.urandom(32)
    got = [[], []]
    downs = []

    def maker(i, out, inn):
        async def deliver(frame):
            got[i].append(frame)
        return udp.Path(out, inn, lambda d, a: wire(i, d), deliver, lambda p, why: downs.append(why))
    return maker(0, k1, k2), maker(1, k2, k1), got, downs


@pytest.mark.parametrize("loss", [0.0, 0.05, 0.25])
def test_a_bad_wire_loses_nothing_and_keeps_the_order(loss):
    rnd = random.Random(7)

    async def run():
        loop = asyncio.get_running_loop()
        paths = []

        def wire(i, data):
            if rnd.random() < loss:
                return
            loop.call_later(rnd.uniform(0, 0.01), paths[1 - i].received, data, ("127.0.0.1", 1000 + i))
        a, b, got, downs = _pair(wire)
        paths += [a, b]
        a.addr, b.addr = ("127.0.0.1", 1001), ("127.0.0.1", 1000)
        a.start([b.addr])
        b.start([a.addr])
        a.released.set()
        b.released.set()
        assert await _until(lambda: a.usable and b.usable)
        count = 60 if loss > 0.1 else 300                    # 25% loss is slow on purpose: the window stays small
        frames = [bytes([i % 256]) * rnd.randint(1, 5000) for i in range(count)]
        for f in frames:
            await a.send_frame(f)
        assert await _until(lambda: len(got[1]) == count, 30.0), (len(got[1]), downs)
        assert got[1] == frames and not downs
        a.close()
        b.close()
    asyncio.run(run())


def test_a_machine_without_so_reuseport_still_links_and_moves_to_udp(monkeypatch):
    """Windows (PR #52): its Python has no SO_REUSEPORT at all."""
    from arcade.mesh import node as nodelib
    monkeypatch.setattr(nodelib, "_REUSE_PORT", False)

    async def run():
        async with _Two() as t:
            await t.up()
            await t.la.send({"t": "zz", "i": 1})
            assert await _until(lambda: t.tb.got == [(1, b"")])
    asyncio.run(run())


def _house(lan_works, firewall=False):
    """Two paths in one house: a router that loops back at the outside address, and (when it works) the LAN. With a
    firewall, b drops a LAN packet until b has sent to that address itself."""
    key_a, key_b = bytes(range(32)), bytes(range(1, 33))
    outside_a, outside_b, lan_a, lan_b = ("129.222.44.94", 1), ("129.222.44.94", 2), ("192.168.1.20", 3), ("192.168.1.20", 4)
    loop = asyncio.get_running_loop()

    async def nothing(frame):
        pass

    def send_a(packet, to):
        if to == outside_b:
            loop.call_soon(b.received, packet, outside_a)
        elif to == lan_b and lan_works and (not firewall or opened):
            loop.call_soon(b.received, packet, lan_a)

    def send_b(packet, to):
        if to == outside_a:
            loop.call_soon(a.received, packet, outside_b)
        elif to == lan_a:
            opened.append(1)
            loop.call_soon(a.received, packet, lan_b)
    opened = []
    a = udp.Path(key_a, key_b, send_a, nothing, lambda *_: None, peer="b")
    b = udp.Path(key_b, key_a, send_b, nothing, lambda *_: None, peer="a")
    a.addresses = (outside_a, outside_b, lan_a, lan_b)
    return a, b


def test_two_arcades_in_one_house_stay_on_the_lan_when_the_router_loops_back():
    """2026-11-02, a test machine and .25: only the answering side offers its home address, the router's loop back answered
    first, and the link ran at 60 ms instead of 1 ms."""
    async def run(lan_works):
        a, b = _house(lan_works)
        outside_a, outside_b, _, lan_b = a.addresses
        b.start([outside_a])                             # the answer's side: it has only the outside address
        await asyncio.sleep(0.05)
        a.start([outside_b, lan_b])                      # the asker: outside first, as an offer lists them
        await asyncio.sleep(0.8)
        seen = a.addr, b.addr
        a.down("done")
        b.down("done")
        await asyncio.sleep(0.05)
        return seen
    at_a, at_b = asyncio.run(run(True))
    assert at_a == ("192.168.1.20", 4) and at_b == ("192.168.1.20", 3), (at_a, at_b)
    at_a, at_b = asyncio.run(run(False))
    assert at_a == ("129.222.44.94", 2) and at_b == ("129.222.44.94", 1), "no LAN: the loop back is fine"


def test_a_host_firewall_lets_the_lan_through_once_both_sides_aim():
    """A test machine's ufw: nothing came in from .25 until a test machine had sent to .25. The asker's "home" frame gives the
    answering side something to aim at."""
    async def run():
        a, b = _house(True, firewall=True)
        outside_a, outside_b, lan_a, lan_b = a.addresses
        b.start([outside_a])
        await asyncio.sleep(0.05)
        a.start([outside_b, lan_b])
        await asyncio.sleep(0.5)
        before = a.addr, b.addr
        b.aim([lan_a])
        await asyncio.sleep(0.6)
        seen = a.addr, b.addr
        a.down("done")
        b.down("done")
        await asyncio.sleep(0.05)
        return before, seen
    before, seen = asyncio.run(run())
    assert before == (("129.222.44.94", 2), ("129.222.44.94", 1)), before
    assert seen == (("192.168.1.20", 4), ("192.168.1.20", 3)), seen


def test_a_remote_home_address_is_neither_offered_nor_dialed():
    """The one UDP socket's offer and its targets, without a link: the filter udp.py actually calls."""
    async def run():
        share = udp.UdpShare(lan_dial=lambda: "auto")
        always = udp.UdpShare(lan_dial=lambda: "on")
        for one in (share, always):
            one.mapped = (time.monotonic(), [["129.222.44.64", 40000]])
            one.outside = ["129.222.44.64"]
            one._lan_extra = [["192.168.1.20", 32805]]
        try:
            remote = await share.offer_for([["156.67.28.112", 41658], ["192.168.1.20", 41658]])
            same = await share.offer_for([["129.222.44.64", 40000]])
            blind = await share.offer_for(None)
            asked = await always.offer_for(None)
        finally:
            for one in (share, always):
                if one.sock is not None and one.sock.transport is not None:
                    one.sock.transport.close()
        raw = [["156.67.28.112", 41658], ["192.168.1.20", 32805], ["192.168.1.20", 41658],
               ["10.1.2.3", 9], ["100.64.12.1", 9], ["169.254.1.1", 9], ["127.0.0.1", 9]]
        return remote, same, blind, asked, share.dial_targets(raw), always.dial_targets(raw)
    remote, same, blind, asked, targets, allowed = asyncio.run(run())
    assert remote == blind == [["129.222.44.64", 40000]]
    assert same == [["129.222.44.64", 40000], ["192.168.1.20", 32805]]
    assert asked == [["129.222.44.64", 40000], ["192.168.1.20", 32805]]
    assert targets == [("156.67.28.112", 41658)]
    assert ("192.168.1.20", 32805) in allowed and ("127.0.0.1", 9) not in allowed


def test_on_windows_a_closed_port_does_not_break_the_next_receive(monkeypatch):
    from arcade.mesh import punch
    calls = []

    class _Sock:
        def ioctl(self, code, value):
            calls.append((code, value))
    monkeypatch.setattr(punch.socket, "SIO_UDP_CONNRESET", 0x9800000C, raising=False)
    punch.no_reset(_Sock())
    assert calls == [(0x9800000C, False)]
