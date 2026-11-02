"""Links over UDP from the first packet (arcade/mesh/introduce.py)."""

import asyncio

import nacl.signing
import nacl.public

from arcade.mesh import introduce, stun, udp
from arcade.mesh.node import MeshNode


class _Taker:
    kinds = ("zz",)
    node = None

    def __init__(self):
        self.got = []

    async def link_up(self, link): pass
    async def link_down(self, peer_id): pass
    async def rebalance(self): pass

    async def on_frame(self, link, header, blob):
        self.got.append(header.get("i"))


async def _until(check, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if check():
            return True
        await asyncio.sleep(0.02)
    return bool(check())


def _node(servers, listen=False, **kw):
    n = MeshNode(nacl.signing.SigningKey.generate(), "t", listen_host="127.0.0.1" if listen else None,
                 listen_port=0 if listen else None)
    n.share = udp.UdpShare(lambda: servers, try_after=0.05)
    n.intro = introduce.Introducer(n.share, round_every=3600, **kw)
    n.taker = _Taker()
    n.add_extension(n.share)
    n.add_extension(n.intro)
    n.add_extension(n.taker)
    return n


async def _stun():
    loop = asyncio.get_running_loop()
    t, _ = await loop.create_datagram_endpoint(lambda: stun._Server({}), local_addr=("127.0.0.1", 0))
    return t, [t.get_extra_info("sockname")[:2]]


def test_two_nodes_that_cannot_be_dialled_meet_and_stay_linked_when_the_introducer_goes():
    async def run():
        t, servers = await _stun()
        c, a, b = _node(servers, listen=True), _node(servers), _node(servers)
        for n in (c, a, b):
            await n.start()
        for n in (a, b):
            n.add_address("127.0.0.1", c.listen_port)
        assert await _until(lambda: len(c.links) == 2)
        assert await _until(lambda: b.node_id in a._via and a.node_id in b._via), "each heard the other through c"
        await a.intro.meet(b.node_id)
        assert await _until(lambda: b.node_id in a.links and a.node_id in b.links), a.intro.status()
        link = a.links[b.node_id]
        assert getattr(link.writer, "is_path", False), "no TCP under this link"
        await link.send({"t": "zz", "i": 1}, b"x" * 30_000)
        assert await _until(lambda: b.taker.got == [1])
        await c.stop()
        await asyncio.sleep(0.1)
        await b.links[a.node_id].send({"t": "zz", "i": 2})
        assert await _until(lambda: a.taker.got == [2]), "the link needs nobody else"
        await a.stop()
        await b.stop()
        t.close()
    asyncio.run(run())


def test_nodes_with_no_links_at_all_meet_over_the_chains_machine_layer():
    async def run():
        t, servers = await _stun()
        wall = []

        def publish(topic, body):
            wall.append({"txid": str(len(wall)), "topic": topic, "body": body})
            return "tx"
        read = lambda topic: list(wall)                                       # noqa: E731
        a, b = _node(servers, publish=publish, read=read), _node(servers, publish=publish, read=read)
        a.intro.announced = lambda: [{"key": b.node_id}]
        await a.start()
        await b.start()
        await a.share._offer()
        await b.share._offer()
        await a.intro._ask_chain()
        assert len(wall) == 1 and b.node_id not in str(wall[0]["body"]["s"]), "only the target can read who is asking"
        assert a.node_id[:16] not in str(wall[0])
        await b.intro._read_chain()
        assert len(wall) == 2, "the answer goes back the same way"
        await a.intro._read_chain()
        assert await _until(lambda: b.node_id in a.links and a.node_id in b.links)
        await a.stop()
        await b.stop()
        t.close()
    asyncio.run(run())


def test_a_nat_that_picks_a_new_port_is_reached_by_guessing(monkeypatch):
    """The easy side knows only the far address; the hard side's many sockets wait to be hit. The hard side sends nothing
    to the easy side here, so only a guess can connect them."""
    monkeypatch.setattr(udp, "SPRAY_EACH", 64)
    monkeypatch.setattr(introduce, "SPRAY_FOR", 8.0)

    async def run():
        t, servers = await _stun()
        c, a, b = _node(servers, listen=True), _node(servers), _node(servers)
        for n in (c, a, b):
            await n.start()
        for n in (a, b):
            n.add_address("127.0.0.1", c.listen_port)
        assert await _until(lambda: b.node_id in a._via and a.node_id in b._via)
        await a.share._offer()
        await b.share._offer()
        b.share.nat = "new-port"
        b.share.mapped = (b.share.mapped[0], [["127.0.0.1", 9]])              # its one mapped port leads nowhere
        real_fan = b.intro._open_fan

        async def silent(peer, path, targets):
            await real_fan(peer, path, [])
        b.intro._open_fan = silent
        await a.intro.meet(b.node_id)
        assert await _until(lambda: b.node_id in a.links and a.node_id in b.links, 12.0)
        assert len(b.intro.sockets[a.node_id]) == 1, "the socket that got through stays, the rest close"
        for n in (a, b, c):
            await n.stop()
        t.close()
    asyncio.run(run())


def test_two_new_port_nats_do_not_try():
    async def run():
        t, servers = await _stun()
        a = _node(servers)
        await a.start()
        await a.share._offer()
        a.share.nat = "new-port"
        a.intro._punch("bb" * 32, nacl.public.PrivateKey.generate(),
                       {"a": [["1.2.3.4", 5]], "nat": "new-port", "n": "00" * 8, "x": "00" * 32}, True)
        assert not a.intro.paths
        await a.stop()
        t.close()
    asyncio.run(run())


def test_an_introduction_is_not_aimed_at_another_networks_home_address():
    async def run():
        t, servers = await _stun()
        a = _node(servers)
        await a.start()
        await a.share._offer()
        a.share.outside = ["129.222.44.64"]
        a.share.nat = "same-port"
        eph = nacl.public.PrivateKey.generate()
        peer = "bb" * 32
        a.intro._punch(peer, eph, {
            "a": [["192.168.1.20", 32805], ["192.168.1.20", 41658], ["156.67.28.112", 41658]],
            "nat": "new-port", "n": "cd" * 8, "x": bytes(eph.public_key).hex(),
        }, True)
        path = a.intro.paths[peer]
        targets, spray = list(path.targets), path.spray_host
        path.close()
        await a.stop()
        t.close()
        return targets, spray
    targets, spray = asyncio.run(run())
    assert targets == [("156.67.28.112", 41658)], targets
    assert spray == "156.67.28.112"


def test_a_forged_or_old_introduction_is_ignored():
    key, other = nacl.signing.SigningKey.generate(), nacl.signing.SigningKey.generate()
    import time
    good = introduce.sign(key, {"t": "ui", "op": "hi", "to": "x", "from": bytes(key.verify_key).hex(), "n": "1",
                                "a": [["1.2.3.4", 5]], "ts": int(time.time()), "hops": 0})
    assert introduce.verify(good) and introduce.verify({**good, "hops": 3}), "a hop count is not signed"
    assert not introduce.verify({**good, "a": [["6.6.6.6", 5]]}), "a changed address"
    assert not introduce.verify({**good, "from": bytes(other.verify_key).hex()})
    old = introduce.sign(key, {"from": bytes(key.verify_key).hex(), "ts": int(time.time()) - 3600})
    assert not introduce.verify(old)


def test_only_the_target_opens_a_sealed_introduction():
    a, b = nacl.signing.SigningKey.generate(), nacl.signing.SigningKey.generate()
    sealed = introduce.seal_to(bytes(a.verify_key).hex(), {"hello": 1})
    assert introduce.open_sealed(a, sealed) == {"hello": 1}
    assert introduce.open_sealed(b, sealed) is None
