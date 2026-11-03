"""Relay hops (arcade/mesh/relay.py, 2026-11-02: "relay hops ... like they do with Meshtastic"): two arcades with
no direct link exchange frames through the arcades between them, signed end to end and limited at every hop."""
import asyncio

from arcade.mesh import relay as R

from test_mesh import addr, fast, mk, run, started, until  # noqa: F401


class Echo:
    """An extension riding "ix" frames (one of the carried kinds): it keeps what it got, and answers "ping"."""
    kinds = ("ix",)

    def __init__(self):
        self.got, self.up = [], []
        self.node = None

    async def link_up(self, link):
        self.up.append(link.peer_id)

    async def link_down(self, peer_id):
        pass

    async def rebalance(self):
        pass

    async def on_frame(self, link, header, blob):
        self.got.append((link.peer_id, header, blob, getattr(link, "relayed", False)))
        if header.get("op") == "ping":
            await link.send({"t": "ix", "op": "pong"}, b"back")


def test_two_arcades_with_no_direct_link_talk_through_the_one_between(fast):
    async def go():
        a, b, c = mk(target=1), mk(target=2), mk(target=1)
        ea, ec = Echo(), Echo()
        a.add_extension(ea)
        c.add_extension(ec)
        await started(a, b, c)
        try:
            a.add_address(*addr(b))
            c.add_address(*addr(b))
            await until(lambda: b.node_id in a.links and b.node_id in c.links, what="both linked to b")
            await until(lambda: c.node_id in a._via and a.node_id in c._via, timeout=5, what="each heard of the other")
            assert c.node_id not in a.links
            link = await a.relays.open(c.node_id, wait=5)
            assert link is not None and a.reach(c.node_id) is link and c.node_id in a.reachable()
            await until(lambda: a.node_id in ec.up, what="c sees a relayed link up")
            await link.send({"t": "ix", "op": "ping"}, b"x" * 5000)
            await until(lambda: ea.got, what="the answer came back")
            peer, header, blob, relayed = ea.got[0]
            assert peer == c.node_id and header["op"] == "pong" and blob == b"back" and relayed
            assert ec.got[0][2] == b"x" * 5000, "the blob too, whole"
            assert b.relays.carried > 5000, "b carried it"
        finally:
            for n in (a, b, c):
                await n.stop()
    run(go())


def test_a_carried_frame_changed_on_the_way_is_thrown_away(fast):
    async def go():
        a, b, c = mk(target=1), mk(target=2), mk(target=1)
        ec = Echo()
        c.add_extension(ec)
        await started(a, b, c)
        try:
            a.add_address(*addr(b))
            c.add_address(*addr(b))
            await until(lambda: c.node_id in a._via and a.node_id in c._via, timeout=5, what="each heard of the other")
            assert await a.relays.open(c.node_id, wait=5) is not None
            real = b.relays._carry

            async def tamper(came, header, blob):         # b changes what it carries
                header = dict(header, **{"in": {"t": "ix", "op": "forged"}})
                await real(came, header, b"evil")
            b.relays._carry = tamper
            await a.relays.send(c.node_id, {"t": "ix", "op": "real"}, b"ok")
            await asyncio.sleep(0.5)
            assert not ec.got, "neither the changed frame nor its blob reached c"
        finally:
            for n in (a, b, c):
                await n.stop()
    run(go())


def test_a_relaying_arcade_limits_what_it_carries_for_one_sender(fast, monkeypatch):
    monkeypatch.setattr(R, "RELAY_RATE", 10_000)

    async def go():
        a, b, c = mk(target=1), mk(target=2), mk(target=1)
        ec = Echo()
        c.add_extension(ec)
        await started(a, b, c)
        try:
            a.add_address(*addr(b))
            c.add_address(*addr(b))
            await until(lambda: c.node_id in a._via and a.node_id in c._via, timeout=5, what="each heard of the other")
            assert await a.relays.open(c.node_id, wait=5) is not None
            for _ in range(20):
                await a.relays.send(c.node_id, {"t": "ix", "op": "bulk"}, b"y" * 20_000)
            await asyncio.sleep(0.5)
            assert 0 < len(ec.got) < 20, "some got through, the rest were over b's limit"
        finally:
            for n in (a, b, c):
                await n.stop()
    run(go())
