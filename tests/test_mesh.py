"""The arcade mesh: nodes linking by key, rooms across nodes, and what it refuses.

Every test runs real nodes on localhost sockets in one event loop. The timings
are shrunk (SUB_EVERY, IDLE, DIAL_EVERY) so a test takes about a second.
"""

from __future__ import annotations

import asyncio
import time

import nacl.signing
import pytest

from arcade.mesh import link as linklib
from arcade.mesh import node as meshlib
from arcade.mesh.node import MeshError, MeshNode

NET = "test-net"


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(meshlib, "SUB_EVERY", 0.15)
    monkeypatch.setattr(meshlib, "IDLE", 1.0)
    monkeypatch.setattr(meshlib, "DIAL_EVERY", 0.05)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


async def until(check, timeout=5.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def drain(queue, kind=None, timeout=3.0):
    """The next event (of `kind`, if given)."""
    end = time.monotonic() + timeout
    while True:
        event = await asyncio.wait_for(queue.get(), max(0.01, end - time.monotonic()))
        if kind is None or event.type == kind:
            return event


def mk(listen=True, peers=None, network=NET, target=meshlib.OUTBOUND_TARGET):
    return MeshNode(nacl.signing.SigningKey.generate(), network, listen_host="127.0.0.1",
                    listen_port=0 if listen else None, peers=peers or [],
                    outbound_target=target)


async def started(*nodes):
    for n in nodes:
        await n.start()
    return nodes


async def stop(*nodes):
    for n in nodes:
        await n.stop()


def addr(n):
    return ("127.0.0.1", n.listen_port)


# ------------------------------------------------------------------ links

def test_two_nodes_link_and_know_each_other_by_key():
    async def go():
        a = mk()
        await a.start()
        b = mk(listen=False, peers=[addr(a)])
        await b.start()
        try:
            await until(lambda: a.node_id in b.links and b.node_id in a.links, what="link")
            assert b.links[a.node_id].peer_key == bytes(a.key.verify_key)
            assert not a.links[b.node_id].outbound and b.links[a.node_id].outbound
        finally:
            await stop(b, a)
    run(go())


def test_nodes_of_different_networks_never_link():
    async def go():
        a = mk(network="pepecoin-main")
        await a.start()
        b = mk(listen=False, peers=[addr(a)], network="pepecoin-test")
        await b.start()
        try:
            await asyncio.sleep(0.5)
            assert not a.links and not b.links
            assert b.addrs[addr(a)]["fail"] >= 1
        finally:
            await stop(b, a)
    run(go())


def test_a_node_does_not_link_to_itself():
    async def go():
        a = mk()
        await a.start()
        a.add_address(*addr(a))
        try:
            await asyncio.sleep(0.4)
            assert not a.links
        finally:
            await stop(a)
    run(go())


def test_a_link_proof_cannot_be_made_without_the_key():
    """The handshake signs the transcript: an impostor holding someone else's
    PUBLIC key cannot complete it in their name."""
    async def go():
        a = mk()
        await a.start()
        victim = nacl.signing.SigningKey.generate()
        impostor = nacl.signing.SigningKey.generate()

        async def lie(reader, writer, signing_key, network, outbound, listen_port):
            eph = linklib.nacl.public.PrivateKey.generate()
            mine = linklib.Hello(network=network, ephemeral=bytes(eph.public_key))
            writer.write(linklib._frame(mine.encode()))
            await writer.drain()
            theirs = linklib.Hello.decode(await linklib._read_frame(reader))
            shared = linklib.sodium.crypto_scalarmult(bytes(eph), theirs.ephemeral)
            t = linklib.hashlib.blake2b(mine.encode() + theirs.encode(), digest_size=32).digest()
            link = linklib.Link(reader, writer, b"", True,
                                linklib._derive(shared, t, b"arcm-dialer"),
                                linklib._derive(shared, t, b"arcm-listener"), None)
            sig = impostor.sign(linklib._AUTH_CONTEXT + b"D" + t).signature
            await link.send({"t": "auth", "key": bytes(victim.verify_key).hex(), "sig": sig.hex()})
            await asyncio.sleep(0.3)
            await link.close()

        reader, writer = await asyncio.open_connection(*addr(a))
        try:
            await lie(reader, writer, impostor, NET, True, None)
            assert victim.verify_key.encode().hex() not in a.links
            assert not a.links
        finally:
            await stop(a)
    run(go())


# ------------------------------------------------------------------ rooms

def line():
    """a <- b <- c: b dials a, c dials b. c never hears of a directly unless gossip says."""
    async def build():
        a = mk(target=1)
        await a.start()
        b = mk(peers=[addr(a)], target=1)
        await b.start()
        c = mk(listen=False, peers=[addr(b)], target=1)
        await c.start()
        await until(lambda: len(b.links) == 2, what="line to form")
        return a, b, c
    return build()


def test_a_message_crosses_a_node_that_is_not_in_the_room():
    async def go():
        a, b, c = await line()
        try:
            qa = await a.join("game1/town", "alice")
            qc = await c.join("game1/town", "carol")
            # Each hears the other arrive, through b.
            assert (await drain(qa, "join")).member == "carol"
            assert (await drain(qc, "join")).member == "alice"
            await until(lambda: "game1/town" in a.remote and "game1/town" in c.remote,
                        what="routes")
            await a.send("game1/town", "alice", b'{"x":1,"y":2}')
            got = await drain(qc, "message")
            assert (got.member, got.node, got.data) == ("alice", a.node_id, b'{"x":1,"y":2}')
            assert not b.local                    # b relayed without being in it
            members = {m["member"] for m in c.members("game1/town")}
            assert members == {"alice", "carol"}
        finally:
            await stop(c, b, a)
    run(go())


def test_payload_is_never_read_any_bytes_go():
    async def go():
        a, b, c = await line()
        try:
            await a.join("g/r", "alice")
            qc = await c.join("g/r", "carol")
            await until(lambda: "g/r" in a.remote, what="route")
            blob = bytes(range(256)) * 2              # 512 bytes, not text, not JSON
            await a.send("g/r", "alice", blob)
            assert (await drain(qc, "message")).data == blob
        finally:
            await stop(c, b, a)
    run(go())


def test_rooms_are_separate():
    async def go():
        a, b, c = await line()
        try:
            await a.join("g/one", "alice")
            qc = await c.join("g/two", "carol")
            await asyncio.sleep(0.5)
            await a.send("g/one", "alice", b"hi")
            with pytest.raises(asyncio.TimeoutError):
                await drain(qc, "message", timeout=0.5)
        finally:
            await stop(c, b, a)
    run(go())


def test_a_triangle_delivers_once():
    async def go():
        a = mk()
        await a.start()
        b = mk(peers=[addr(a)])
        await b.start()
        c = mk(peers=[addr(a), addr(b)])
        await c.start()
        try:
            await until(lambda: len(a.links) == 2 and len(b.links) == 2 and len(c.links) == 2,
                        what="triangle")
            await a.join("g/r", "alice")
            qc = await c.join("g/r", "carol")
            await until(lambda: "g/r" in a.remote and "g/r" in c.remote, what="routes")
            for i in range(5):
                await a.send("g/r", "alice", bytes([i]))
            got = []
            with pytest.raises(asyncio.TimeoutError):
                while True:
                    got.append((await drain(qc, "message", timeout=0.6)).data)
            assert got == [bytes([i]) for i in range(5)]
        finally:
            await stop(c, b, a)
    run(go())


def test_two_members_on_one_node_hear_each_other():
    async def go():
        a = mk()
        await a.start()
        try:
            q1 = await a.join("g/r", "one")
            q2 = await a.join("g/r", "two")
            assert (await drain(q1, "join")).member == "two"
            await a.send("g/r", "one", b"x")
            assert (await drain(q2, "message")).data == b"x"
            with pytest.raises(asyncio.TimeoutError):
                await drain(q1, "message", timeout=0.3)   # no echo to the sender
        finally:
            await stop(a)
    run(go())


def test_leaving_and_vanishing_both_say_leave():
    async def go():
        a, b, c = await line()

        async def keep_carol():                  # her page stays open throughout
            while True:
                c.touch("g/r", "carol")
                await asyncio.sleep(0.1)
        toucher = None
        try:
            await a.join("g/r", "alice")
            qc = await c.join("g/r", "carol")
            toucher = asyncio.get_running_loop().create_task(keep_carol())
            await drain(qc, "join")
            await a.leave("g/r", "alice")
            assert (await drain(qc, "leave")).member == "alice"
            await a.join("g/r", "alice")
            assert (await drain(qc, "join")).member == "alice"
            await a.stop()                       # gone without a word
            ev = await drain(qc, "leave", timeout=4)
            assert ev.member == "alice"
            assert {m["member"] for m in c.members("g/r")} == {"carol"}
        finally:
            if toucher:
                toucher.cancel()
            await stop(c, b, a)
    run(go())


def test_a_member_whose_page_went_quiet_is_dropped():
    async def go():
        a, b, c = await line()
        try:
            await a.join("g/r", "alice")
            qc = await c.join("g/r", "carol")
            await drain(qc, "join")
            # carol keeps touching; alice's page is gone and nobody touches for her.
            for _ in range(20):
                c.touch("g/r", "carol")
                await asyncio.sleep(0.1)
            assert ("g/r", "alice") not in a.local
            assert (await drain(qc, "leave", timeout=2)).member == "alice"
            assert ("g/r", "carol") in c.local
        finally:
            await stop(c, b, a)
    run(go())


def test_when_a_node_in_the_middle_goes_down_the_room_carries_on():
    """a and d meet only through b or c. Take b down: the room keeps working via c."""
    async def go():
        a = mk(target=2)
        await a.start()
        b = mk(peers=[addr(a)], target=1)
        c = mk(peers=[addr(a)], target=1)
        await started(b, c)
        d = mk(listen=False, peers=[addr(b), addr(c)], target=2)
        await d.start()

        async def keep(node, member):
            while True:
                node.touch("g/r", member)
                await asyncio.sleep(0.1)
        keepers = []
        try:
            await until(lambda: len(d.links) == 2 and len(a.links) == 2, what="diamond")
            await a.join("g/r", "alice")
            qd = await d.join("g/r", "dave")
            keepers = [asyncio.get_running_loop().create_task(keep(a, "alice")),
                       asyncio.get_running_loop().create_task(keep(d, "dave"))]
            await until(lambda: "g/r" in a.remote and "g/r" in d.remote, what="routes")
            await b.stop()
            got = None
            end = time.monotonic() + 5
            while got is None and time.monotonic() < end:
                await a.send("g/r", "alice", b"still here")
                try:
                    got = await drain(qd, "message", timeout=0.3)
                except asyncio.TimeoutError:
                    pass
            assert got is not None and got.data == b"still here"
            # and nobody was told alice left while the path moved
            assert {m["member"] for m in d.members("g/r")} == {"alice", "dave"}
        finally:
            for k in keepers:
                k.cancel()
            await stop(d, c, b, a)
    run(go())


def test_a_node_learns_addresses_from_its_peers():
    async def go():
        a, b, c = await line()
        try:
            await until(lambda: addr(a) in c.addrs, what="gossip")
            assert c.addrs[addr(a)]["id"] == a.node_id
        finally:
            await stop(c, b, a)
    run(go())


# ------------------------------------------------------------------ refusals

def test_size_and_rate_limits():
    async def go():
        a = mk()
        await a.start()
        try:
            await a.join("g/r", "alice")
            with pytest.raises(MeshError, match="512"):
                await a.send("g/r", "alice", b"x" * 513)
            with pytest.raises(MeshError, match="join"):
                await a.send("g/r", "bob", b"x")
            sent = 0
            with pytest.raises(MeshError, match="too fast"):
                for _ in range(meshlib.BURST + 5):
                    await a.send("g/r", "alice", b"x")
                    sent += 1
            assert sent == meshlib.BURST
            for bad in ("", "x" * 129, "a\nb"):
                with pytest.raises(MeshError):
                    await a.join(bad, "alice")
        finally:
            await stop(a)
    run(go())


class _FakeLink:
    peer_id = "f" * 64


def _signed_pub(key, room="g/r", member="mallory", data=b"hi", seq=None, kind="msg"):
    header = {"t": "pub", "r": room, "o": bytes(key.verify_key).hex(),
              "q": seq or int(time.time() * 1_000_000), "m": member, "k": kind, "hops": 0}
    header["sig"] = key.sign(meshlib._canon(meshlib._signed_part(header)) + data).signature.hex()
    return header


def test_forged_altered_and_replayed_messages_are_dropped():
    async def go():
        a = mk(listen=False)
        await a.start()
        try:
            q = await a.join("g/r", "alice")
            await drain(q) if not q.empty() else None
            other = nacl.signing.SigningKey.generate()
            await a._on_pub(_FakeLink(), _signed_pub(other, data=b"", kind="join",
                                                     seq=int(time.time() * 1e6) - 5), b"")
            assert (await drain(q, "join")).member == "mallory"

            good = _signed_pub(other)
            await a._on_pub(_FakeLink(), dict(good), b"hi")
            assert (await drain(q, "message")).data == b"hi"

            await a._on_pub(_FakeLink(), dict(good), b"hi")          # the same one again
            altered = _signed_pub(other, seq=good["q"] + 1)
            await a._on_pub(_FakeLink(), altered, b"HI")             # bytes changed after signing
            impostor = _signed_pub(nacl.signing.SigningKey.generate(), seq=good["q"] + 2)
            impostor["o"] = good["o"]                                 # claims to be `other`
            await a._on_pub(_FakeLink(), impostor, b"hi")
            stale = _signed_pub(other, seq=int((time.time() - 3600) * 1_000_000))
            await a._on_pub(_FakeLink(), stale, b"hi")               # an hour old
            with pytest.raises(asyncio.TimeoutError):
                await drain(q, "message", timeout=0.3)

            # A forged copy arriving first does not get the real one thrown away.
            real = _signed_pub(other, seq=good["q"] + 10, data=b"real")
            fake = dict(real, sig="00" * 64)
            await a._on_pub(_FakeLink(), fake, b"real")
            await a._on_pub(_FakeLink(), dict(real), b"real")
            assert (await drain(q, "message")).data == b"real"
        finally:
            await stop(a)
    run(go())


def test_only_members_the_vouch_accepts_are_seen_or_heard():
    """The mesh asks the layer above about every remote member before showing it,
    and only nodes with players in the room ask."""
    async def go():
        asked = {"b": [], "c": []}

        def vouch_for(name):
            async def vouch(origin, member, cred):
                asked[name].append(member)
                return bool(cred) and cred.get("proof") == "ok:" + member
            return vouch

        a = mk(target=1)
        await a.start()
        b = mk(peers=[addr(a)], target=1)
        b.vouch = vouch_for("b")
        await b.start()
        c = mk(listen=False, peers=[addr(b)], target=1)
        c.vouch = vouch_for("c")
        await c.start()
        try:
            await until(lambda: len(b.links) == 2, what="line")
            await a.join("g/r", "alice", cred={"proof": "ok:alice"})
            await a.join("g/r", "mallory", cred={"proof": "ok:alice"})   # someone else's proof
            await a.join("g/r", "guest", cred=None)
            qc = await c.join("g/r", "carol", cred={"proof": "ok:carol"})
            assert (await drain(qc, "join")).member == "alice"
            await until(lambda: "g/r" in a.remote, what="route")
            await a.send("g/r", "mallory", b"trust me")
            await a.send("g/r", "alice", b"hello")
            got = await drain(qc, "message")
            assert (got.member, got.data, got.cred) == ("alice", b"hello", {"proof": "ok:alice"})
            assert {m["member"] for m in c.members("g/r")} == {"alice", "carol"}
            assert asked["b"] == []                     # b only forwards: it never asks
            assert "carol" not in asked["c"]            # c's own player is not re-checked
            assert {"alice", "mallory", "guest"} <= set(asked["c"])
        finally:
            await stop(c, b, a)
    run(go())


def test_a_node_that_ignores_its_own_rate_is_cut_off_by_the_next():
    async def go():
        a = mk(listen=False)
        await a.start()
        try:
            q = await a.join("g/r", "alice")
            other = nacl.signing.SigningKey.generate()
            await a._on_pub(_FakeLink(), _signed_pub(other, data=b"", kind="join",
                                                     seq=int(time.time() * 1e6) - 5), b"")
            for i in range(60):
                await a._on_pub(_FakeLink(), _signed_pub(other, data=b"x", seq=int(time.time() * 1e6) + i), b"x")
            got = 0
            while not q.empty():
                if q.get_nowait().type == "message":
                    got += 1
            assert got == int(meshlib.BURST * meshlib.FORWARD_SLACK)
        finally:
            await stop(a)
    run(go())


# ------------------------------------------------------------------ can I be reached

def test_a_node_knows_its_own_address_when_it_dials_it():
    async def go():
        a, b = mk(), mk()
        await started(a, b)
        try:
            assert await a.reaches_itself("127.0.0.1", a.listen_port)
            assert not await a.reaches_itself("127.0.0.1", b.listen_port), "that is b, not a"
            closed = mk()
            await closed.start()
            port = closed.listen_port
            await closed.stop()
            assert not await a.reaches_itself("127.0.0.1", port)
        finally:
            await stop(b, a)
    run(go())


def test_a_peer_dials_back_only_where_it_sees_the_asker():
    async def go():
        a = mk()
        await a.start()
        b = mk(peers=[addr(a)], target=1)
        await b.start()
        c = mk(listen=False, peers=[addr(a)], target=1)
        await c.start()
        try:
            await until(lambda: len(a.links) == 2, what="links")
            said = await b.ask_dial_back(b.listen_port, timeout=5)
            assert said == {"reached": True, "seen": ["127.0.0.1"]}
            # c does not listen: a dials where c's connection comes from, and fails.
            said = await c.ask_dial_back(45999, timeout=5)
            assert said["reached"] is False and said["seen"] == ["127.0.0.1"]
            # Asking again at once is not answered: once a minute per peer.
            said = await b.ask_dial_back(b.listen_port, timeout=0.5)
            assert said == {"reached": False, "seen": []}
        finally:
            await stop(c, b, a)
    run(go())
