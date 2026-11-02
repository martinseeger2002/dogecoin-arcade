"""The media server (arcade/mesh/turn.py): a relay spoken to as a browser speaks to it."""

import asyncio
import hashlib
import hmac
import os
import struct
import time

from arcade.mesh import stun, turn

SECRET = b"s" * 32


class _Client(asyncio.DatagramProtocol):
    def __init__(self):
        self.q = asyncio.Queue()

    def connection_made(self, transport):
        self.t = transport

    def datagram_received(self, data, addr):
        self.q.put_nowait(data)

    async def ask(self, packet, to):
        self.t.sendto(packet, to)
        return await asyncio.wait_for(self.q.get(), 3)


def _signed(kind, attrs, user, password, realm, nonce, tid=None):
    tid = tid or os.urandom(12)
    key = hashlib.md5(f"{user}:{realm}:{password}".encode()).digest()
    full = attrs + [(turn.A_USERNAME, user.encode()), (turn.A_REALM, realm.encode()), (turn.A_NONCE, nonce)]
    return turn.build(kind, tid, full, key)


async def _server(**kw):
    loop = asyncio.get_running_loop()
    srv = turn.TurnServer(SECRET, **kw)
    t, _ = await loop.create_datagram_endpoint(lambda: stun._Server({}, srv), local_addr=("127.0.0.1", 0))
    srv.transport = t
    return srv, t, t.get_extra_info("sockname")[:2]


async def _allocate(srv_addr, user, password):
    loop = asyncio.get_running_loop()
    _, c = await loop.create_datagram_endpoint(_Client, local_addr=("127.0.0.1", 0))
    want = [(turn.A_TRANSPORT, bytes([17, 0, 0, 0]))]
    first = turn.parse(await c.ask(turn.build(turn.ALLOCATE, os.urandom(12), want), srv_addr))
    assert first[0] == 0x110 | turn.ALLOCATE and first[2][turn.A_ERROR][2:4] == bytes([4, 1])
    realm, nonce = first[2][turn.A_REALM].decode(), first[2][turn.A_NONCE]
    tid = os.urandom(12)
    reply = turn.parse(await c.ask(_signed(turn.ALLOCATE, want, user, password, realm, nonce, tid), srv_addr))
    return c, reply, realm, nonce, tid


def _xor(host, port):
    import ipaddress
    return struct.pack("!BBHI", 0, 1, port ^ (stun.MAGIC >> 16), int(ipaddress.IPv4Address(host)) ^ stun.MAGIC)


def _relayed(reply, tid):
    v = reply[2][turn.A_RELAYED]
    return turn.peer_address(v)


def test_a_wrong_password_gets_no_relay_and_an_expired_login_neither():
    async def run():
        srv, t, addr = await _server()
        u, _ = turn.login(SECRET, "player")
        c, reply, *_ = await _allocate(addr, u, "not-the-password")
        assert reply[0] == 0x110 | turn.ALLOCATE and not srv.allocations
        old, pw = turn.login(SECRET, "player", ttl=-5)
        c2, reply2, *_ = await _allocate(addr, old, pw)
        assert reply2[0] == 0x110 | turn.ALLOCATE and not srv.allocations
        c.t.close(); c2.t.close(); t.close()
    asyncio.run(run())


def test_two_players_through_the_relay_both_ways_on_a_channel_and_by_indication():
    async def run():
        srv, t, addr = await _server()
        u, p = turn.login(SECRET, "a")
        c, reply, realm, nonce, tid = await _allocate(addr, u, p)
        assert reply[0] == 0x100 | turn.ALLOCATE
        relay = _relayed(reply, tid)
        loop = asyncio.get_running_loop()
        _, peer = await loop.create_datagram_endpoint(_Client, local_addr=("127.0.0.1", 0))
        peer_at = peer.t.get_extra_info("sockname")[:2]
        real_relay = ("127.0.0.1", relay[1])

        peer.t.sendto(b"before", real_relay)                  # no permission yet: dropped
        await asyncio.sleep(0.05)
        assert c.q.empty()

        # may_reach refuses this machine's own addresses, so ask as a client on a private address may
        srv_allowed = srv._allowed
        srv._allowed = lambda p, cl: True
        r = turn.parse(await c.ask(_signed(turn.PERMISSION, [(turn.A_PEER, _xor(*peer_at))], u, p, realm, nonce), addr))
        assert r[0] == 0x100 | turn.PERMISSION
        peer.t.sendto(b"hello", real_relay)
        got = turn.parse(await asyncio.wait_for(c.q.get(), 3))
        assert got[0] == 0x010 | turn.DATA and got[2][turn.A_DATA] == b"hello"

        r = turn.parse(await c.ask(_signed(turn.BIND, [(turn.A_PEER, _xor(*peer_at)),
                                                       (turn.A_CHANNEL, struct.pack("!HH", 0x4001, 0))],
                                           u, p, realm, nonce), addr))
        assert r[0] == 0x100 | turn.BIND
        c.t.sendto(struct.pack("!HH", 0x4001, 5) + b"world" + b"\0\0\0", addr)
        assert await asyncio.wait_for(peer.q.get(), 3) == b"world"
        peer.t.sendto(b"back", real_relay)
        data = await asyncio.wait_for(c.q.get(), 3)
        assert data[:4] == struct.pack("!HH", 0x4001, 4) and data[4:8] == b"back"
        srv._allowed = srv_allowed
        assert srv.bytes == len(b"hello") + len(b"world") + len(b"back")
        c.t.close(); peer.t.close(); srv.close(); t.close()
    asyncio.run(run())


def test_a_relay_is_no_way_into_a_private_network_or_this_machine():
    assert not turn.may_reach("127.0.0.1", "8.8.8.8")
    assert not turn.may_reach("192.168.1.20", "8.8.8.8")
    assert not turn.may_reach("10.0.0.5", "::ffff:8.8.8.8")
    assert not turn.may_reach("169.254.169.254", "192.168.1.9"), "cloud metadata"
    assert not turn.may_reach("8.8.4.4", "8.8.8.8", ("8.8.4.4",)), "its own address"
    assert turn.may_reach("8.8.4.4", "1.1.1.1")
    assert turn.may_reach("192.168.1.20", "192.168.1.9"), "a player on the home network, to the next room"


def test_the_month_limit_ends_the_service_and_a_login_cannot_hoard():
    async def run():
        srv, t, addr = await _server(limit_gb=lambda: 0.0)
        u, p = turn.login(SECRET, "a")
        c, reply, *_ = await _allocate(addr, u, p)
        assert reply[0] == 0x110 | turn.ALLOCATE and reply[2][turn.A_ERROR][2:4] == bytes([4, 86])
        assert not srv.available() and srv.status()["over"]
        c.t.close(); t.close()
    asyncio.run(run())


def test_a_month_of_usage_survives_a_restart(tmp_path):
    f = tmp_path / "u.json"
    s = turn.TurnServer(SECRET, usage_file=str(f))
    s.bytes = 123
    s._save()
    assert turn.TurnServer(SECRET, usage_file=str(f)).bytes == 123


def test_a_borrowed_relay_is_named_by_the_proof_not_the_answer():
    class _Link:
        def __init__(self, share, me): self.share, self.me, self.peer_id = share, me, "bb"
        async def send(self, header):
            await self.share.on_frame(self.me, header, b"")

    async def run():
        srv = turn.TurnServer(SECRET)
        srv.transport = object()
        asker, giver = turn.TurnShare(None, lambda: {"cc": [("203.0.113.9", 3478)]}), turn.TurnShare(srv)
        class _N: pass
        asker.node, giver.node = _N(), _N()
        asker.node.node_id = "aa" * 32
        back = _Link(asker, None)
        forward = _Link(giver, back)
        forward.peer_id = "cc"
        back.peer_id = "aa"
        asker.node.links = {"cc": forward}
        got = await asker.borrow("player")
        assert got and got[0]["urls"] == ["turn:203.0.113.9:3478?transport=udp"]
        name = got[0]["username"]
        assert turn.password_for(SECRET, name) == got[0]["credential"]
    asyncio.run(run())


def test_who_gets_which_relay():
    class _Stun:
        def __init__(self, hosts): self.hosts = hosts
        def mine(self): return self.hosts

    srv = turn.TurnServer(SECRET)
    srv.transport = object()
    far = [{"urls": ["turn:203.0.113.9:3478?transport=udp"], "username": "u", "credential": "c"}]
    # A public arcade: anybody, anywhere, reaches it, and nothing borrowed is offered beside it (2026-11-02:
    # a browser picked a far borrowed relay that lost a fifth of the sound).
    got = turn.player_relays(srv, _Stun([("198.51.100.1", 3478)]), 3478, "10.0.0.2", "8.8.8.8", "addr", far)
    assert [g["urls"] for g in got] == [["turn:198.51.100.1:3478?transport=udp"]]
    got = turn.player_relays(srv, _Stun([("198.51.100.1", 3478)]), 3478, "10.0.0.2", "10.0.0.7", "addr", far)
    assert len(got) == 1 and "turn:10.0.0.2:3478?transport=udp" in got[0]["urls"], "a player at home: its LAN too"
    # Behind NAT, a player away from home: its own relay is out of reach, the borrowed one is the way.
    assert turn.player_relays(srv, _Stun([]), 3478, "192.168.1.5", "8.8.8.8", "addr", far) == far
    # Behind NAT, a player at home: its own node too.
    got = turn.player_relays(srv, _Stun([]), 3478, "192.168.1.5", "192.168.1.9", "addr", far)
    assert got[0]["urls"] == ["turn:192.168.1.5:3478?transport=udp"] and got[1:] == far
    # No relay here at all, or its month is used up.
    assert turn.player_relays(None, None, None, None, "8.8.8.8", "addr", far) == far
    srv.limit_gb = lambda: 0.0
    assert turn.player_relays(srv, _Stun([("198.51.100.1", 3478)]), 3478, None, "8.8.8.8", "addr", far) == far


def test_relayed_packets_keep_their_pace_while_the_mesh_loop_is_busy():
    """2026-11-02: calls on an "Arcade relay" were "very lossy". The relay ran on the mesh loop, so every relayed
    packet waited behind whatever the mesh was doing: none lost, but delivered in clumps. It has a loop of its own now;
    a mesh loop blocked for 400 ms at a time must not hold up a 50-a-second stream through the relay."""
    import socket
    import nacl.signing
    from arcade.mesh.service import MeshService

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    svc = MeshService("arcade-test", nacl.signing.SigningKey.generate(), listen_host="127.0.0.1", listen_port=0)
    svc.stun_port = port
    svc.turn = turn.TurnServer(SECRET)
    svc.turn._allowed = lambda p, cl: True
    svc.start()
    try:
        for _ in range(50):
            if svc.turn.transport is not None:
                break
            time.sleep(0.05)
        addr = ("127.0.0.1", port)

        async def run():
            u, p = turn.login(SECRET, "a")
            c, reply, realm, nonce, tid = await _allocate(addr, u, p)
            assert reply[0] == 0x100 | turn.ALLOCATE
            relay = ("127.0.0.1", _relayed(reply, tid)[1])
            loop = asyncio.get_running_loop()
            _, peer = await loop.create_datagram_endpoint(_Client, local_addr=("127.0.0.1", 0))
            peer_at = peer.t.get_extra_info("sockname")[:2]
            r = turn.parse(await c.ask(_signed(turn.PERMISSION, [(turn.A_PEER, _xor(*peer_at))], u, p, realm, nonce),
                                       addr))
            assert r[0] == 0x100 | turn.PERMISSION
            arrived = []

            async def listen():
                while len(arrived) < 50:
                    await c.q.get()
                    arrived.append(time.monotonic())
            listening = asyncio.ensure_future(listen())
            for i in range(50):                       # one second of 20 ms packets, as Opus sends them
                if i in (10, 30):                     # the mesh busy for 400 ms, twice
                    svc.loop.call_soon_threadsafe(time.sleep, 0.4)
                peer.t.sendto(b"x" * 160, relay)
                await asyncio.sleep(0.02)
            await asyncio.wait_for(listening, 5)
            c.t.close(); peer.t.close()
            return max(b - a for a, b in zip(arrived, arrived[1:]))
        worst = asyncio.run(run())
        assert worst < 0.15, f"a {worst * 1000:.0f} ms gap: the relay waited for the mesh"
    finally:
        svc.stop()
