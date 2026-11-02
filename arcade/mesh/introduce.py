"""Links with no TCP under them (2026-10-31, the operator: "Can we have the nodes run through UDP as well?").

udp.py moves a link to UDP once it exists. Two nodes behind NAT often have no way to make one: neither can be dialled,
so they relied on a tunnel. This makes the link over UDP from the first packet.

**The introduction.** Each sends the other a fresh X25519 key and where to aim. It goes hop by hop over the mesh, along
the way the other's SUBs came (node._via), so any two nodes on the mesh can meet without sharing a neighbour. A
node with no links at all has no mesh to ask: it seals the introduction to the other's mesh key and publishes it on the
chain's machine layer (broadcast topic "mesh/intro/v1"), as the operator suggested, and the answer comes back the same way.
Every introduction is signed by the node's mesh key.

**The punch.** Both send at once. A NAT that keeps one outside port per socket ("same-port") is the easy case. A NAT
that picks a new port per destination ("new-port") cannot be aimed at, so its side opens HARD_SOCKETS sockets and sends
from each, and the other side guesses ports until a guess lands on one (udp.py). Two new-port NATs cannot meet this way;
they stay on the mesh's other links.

**The link.** The two then run the ordinary handshake over the path (udp.Stream). The path's keys come from the
introduction, which a node on the way could have swapped, but the handshake inside proves both mesh keys and encrypts
again, so such a node gets nothing and the link simply fails. There is no TCP backup: if the path dies the link does, as a
TCP link would, and the nodes meet again.

Frames "ui": {"op": "hi" | "yes", "to", "from", "n", "x", "a", "nat", "ts", "sig", "hops"}. Older arcades pass none of this
on and never act on it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import time

import nacl.bindings as sodium
import nacl.exceptions
import nacl.public
import nacl.signing

from . import punch as punchlib
from . import udp
from .link import LinkError, handshake

log = logging.getLogger("arcade.mesh.introduce")

TOPIC = "mesh/intro/v1"
MAX_HOPS = 8
FRESH = 600.0                  # seconds an introduction is believed
HEARD_WITHIN = 120.0           # a node is worth meeting if its SUB came this recently
RETRY = 300.0                  # seconds before meeting the same node again; doubles
RETRY_MAX = 6 * 3600.0
SPRAY_FOR = 22.0               # seconds a new-port punch goes on
AT_ONCE = 2                    # nodes met in one round
RATE_EVERY = 1.0               # seconds between introductions passed on for one sender
LONELY_AFTER = 120.0           # seconds with no links before the chain is asked
CHAIN_EVERY = 1800.0           # one publish in this long: it spends coins
CHAIN_TARGETS = 3
CHAIN_WAIT = 300.0             # a chain introduction takes longer to be answered
ROUND_EVERY = 20.0


def _signed_part(header: dict) -> bytes:
    return json.dumps({k: v for k, v in header.items() if k not in ("sig", "hops", "t")},
                      separators=(",", ":"), sort_keys=True).encode()


def sign(key: nacl.signing.SigningKey, header: dict) -> dict:
    header["sig"] = key.sign(_signed_part(header)).signature.hex()
    return header


def verify(header: dict) -> bool:
    try:
        nacl.signing.VerifyKey(bytes.fromhex(str(header["from"]))).verify(
            _signed_part(header), bytes.fromhex(str(header["sig"])))
    except (KeyError, ValueError, TypeError, nacl.exceptions.BadSignatureError, nacl.exceptions.ValueError):
        return False
    return abs(time.time() - float(header.get("ts", 0))) < FRESH


def seal_to(mesh_key_hex: str, header: dict) -> str:
    """A sealed box to a node's mesh key (Ed25519, converted): only that node can read who wants to meet it."""
    public = sodium.crypto_sign_ed25519_pk_to_curve25519(bytes.fromhex(mesh_key_hex))
    return base64.b64encode(nacl.public.SealedBox(nacl.public.PublicKey(public)).encrypt(
        json.dumps(header, separators=(",", ":")).encode())).decode()


def open_sealed(key: nacl.signing.SigningKey, sealed: str) -> dict | None:
    try:
        private = sodium.crypto_sign_ed25519_sk_to_curve25519(bytes(key) + bytes(key.verify_key))
        plain = nacl.public.SealedBox(nacl.public.PrivateKey(private)).decrypt(base64.b64decode(sealed))
        said = json.loads(plain)
    except (ValueError, TypeError, nacl.exceptions.CryptoError):
        return None
    return said if isinstance(said, dict) else None


def path_keys(mine: nacl.public.PrivateKey, theirs: bytes, nonce: bytes, i_am_lower: bool) -> tuple[bytes, bytes]:
    shared = sodium.crypto_scalarmult(bytes(mine), theirs)
    low = hashlib.blake2b(shared + nonce, digest_size=32, person=b"arcm-intro-low").digest()
    high = hashlib.blake2b(shared + nonce, digest_size=32, person=b"arcm-intro-high").digest()
    return (low, high) if i_am_lower else (high, low)


class Introducer:
    """A mesh extension beside udp.UdpShare. `publish(topic, body)` and `read(topic)` are the chain's machine layer,
    `announced()` lists mesh keys the chain knows; all three optional."""

    kinds = ("ui",)

    def __init__(self, share: udp.UdpShare, publish=None, read=None, announced=None, round_every: float = ROUND_EVERY):
        self.share = share
        self.publish, self.read, self.announced = publish, read, announced
        self.round_every = round_every
        self.node = None
        self.opening: dict[str, dict] = {}      # peer -> {"n", "eph", "chain"}: asked, not yet answered
        self.cooling: dict[str, tuple[float, int]] = {}
        self.passed: dict[str, float] = {}      # sender -> when one of its introductions was last passed on
        self.seen_chain: set[str] = set()
        self.lonely_since: float | None = None
        self.last_chain = 0.0
        self.paths: dict[str, udp.Path] = {}
        self.sockets: dict[str, list] = {}
        self.task: asyncio.Task | None = None
        self.tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self.tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("introduce", exc_info=t.exception())
        task.add_done_callback(done)

    # ---------------------------------------------------------------- the extension

    async def link_up(self, link) -> None:
        if self.task is None:
            self.task = asyncio.get_running_loop().create_task(self._rounds())

    async def link_down(self, peer_id: str) -> None:
        pass

    async def rebalance(self) -> None:
        if self.task is None:                   # a node that never had a link still needs its rounds
            self.task = asyncio.get_running_loop().create_task(self._rounds())

    async def on_stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
        for task in list(self.tasks):
            task.cancel()
        for path in list(self.paths.values()):
            path.close()
        for peer in list(self.sockets):
            self._close_sockets(peer)

    async def _rounds(self) -> None:
        while True:
            try:
                await self.round()
            except asyncio.CancelledError:
                raise
            except Exception:                  # noqa: BLE001 -- the loop must not die
                log.exception("introduce")
            await asyncio.sleep(self.round_every)

    async def round(self) -> None:
        await self._read_chain()
        node = self.node
        if node.links:
            self.lonely_since = None
        elif self.lonely_since is None:
            self.lonely_since = time.monotonic()
        if node.links and node._outbound() < node.outbound_target:
            for peer in self._worth_meeting()[:AT_ONCE]:
                await self.meet(peer)
        if (not node.links and self.lonely_since is not None
                and time.monotonic() - self.lonely_since >= LONELY_AFTER):
            await self._ask_chain()

    def _due(self, peer: str) -> bool:
        return (peer != self.node.node_id and peer not in self.node.links and peer not in self.opening
                and peer not in self.paths and time.monotonic() >= self.cooling.get(peer, (0, 0))[0])

    def _worth_meeting(self) -> list[str]:
        now = time.time()
        found = [p for p, at in self.node._heard.items() if now - at < HEARD_WITHIN and self._due(p)]
        random.shuffle(found)
        return found

    def _cool(self, peer: str) -> None:
        tries = self.cooling.get(peer, (0, 0))[1]
        self.cooling[peer] = (time.monotonic() + min(RETRY_MAX, RETRY * 2 ** tries), tries + 1)

    # ---------------------------------------------------------------- saying hello

    async def _what_i_say(self, op: str, peer: str, nonce: str, eph: nacl.public.PrivateKey) -> dict:
        # No peer addresses here: a "hi" has not seen them, and a "yes" is relayed, so a home-network address would
        # be shown to every node on the way. The direct UDP path (udp.py) still shares one with a peer on this NAT.
        offer = await self.share.offer_for(None)
        return sign(self.node.key, {"t": "ui", "op": op, "to": peer, "from": self.node.node_id, "n": nonce,
                                    "x": bytes(eph.public_key).hex(), "a": offer, "nat": self.share.nat,
                                    "ts": int(time.time()), "hops": 0})

    async def meet(self, peer: str, chain: bool = False) -> None:
        """Ask `peer` to meet."""
        if not self._due(peer):
            return
        eph, nonce = nacl.public.PrivateKey.generate(), os.urandom(8).hex()
        self.opening[peer] = {"n": nonce, "eph": eph, "chain": chain, "at": time.monotonic()}
        self._cool(peer)
        header = await self._what_i_say("hi", peer, nonce, eph)
        if not (await self._forward(header) if not chain else await self._publish_to(peer, header)):
            self.opening.pop(peer, None)

    async def _forward(self, header: dict) -> bool:
        to, node = str(header.get("to", "")), self.node
        link = node.links.get(to) or node.links.get(node._via.get(to, ""))
        if link is None:
            return False
        try:
            await link.send(header)
        except (LinkError, ConnectionError, OSError):
            return False
        return True

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        to = str(header.get("to", ""))
        if to != self.node.node_id:
            sender, hops = str(header.get("from", ""))[:64], header.get("hops")
            now = time.monotonic()
            if isinstance(hops, int) and hops < MAX_HOPS and now - self.passed.get(sender, 0) >= RATE_EVERY:
                self.passed[sender] = now
                if len(self.passed) > 10_000:
                    self.passed.clear()
                await self._forward({**header, "hops": hops + 1})
            return
        await self._heard(header)

    async def _heard(self, header: dict, chain: bool = False) -> None:
        if not verify(header):
            return
        peer, op = str(header["from"]), header.get("op")
        if peer in self.node.links or peer in self.paths:
            return
        if op == "hi":
            mine = self.opening.get(peer)
            if mine is not None and self.node.node_id < peer:
                return                                  # both asked: the lower id's question stands
            self.opening.pop(peer, None)
            inbound = sum(1 for link in self.node.links.values() if not link.outbound)
            if inbound >= self.node.max_inbound:
                return
            eph = nacl.public.PrivateKey.generate()
            answer = await self._what_i_say("yes", peer, str(header.get("n", ""))[:16], eph)
            sent = await self._publish_to(peer, answer) if chain else await self._forward(answer)
            if sent:
                self._punch(peer, eph, header, outbound=False)
        elif op == "yes":
            mine = self.opening.get(peer)
            if mine is not None and mine["n"] == header.get("n"):
                del self.opening[peer]
                self._punch(peer, mine["eph"], header, outbound=True)

    # ---------------------------------------------------------------- the punch

    def _punch(self, peer: str, eph, header: dict, outbound: bool) -> None:
        targets = self.share.dial_targets(header.get("a"))
        theirs, mine = str(header.get("nat")), self.share.nat
        if not targets or (theirs == "new-port" and mine == "new-port"):
            log.info("mesh: no way to punch to %s (%s, %s)", peer[:12], mine, theirs)
            return
        try:
            nonce, x = bytes.fromhex(str(header["n"])), bytes.fromhex(str(header["x"]))
            key_out, key_in = path_keys(eph, x, nonce, self.node.node_id < peer)
        except (KeyError, ValueError, TypeError):
            return

        async def nothing(frame: bytes) -> None:
            pass

        path = udp.Path(key_out, key_in, self.share.sock.transport.sendto, nothing, self._down, peer=peer)
        path.on_confirmed = lambda p: self._spawn(self._link(p, outbound))
        stream = udp.Stream(path)
        path.stream = stream
        self.share.sock.paths[path.cid_in] = path
        self.paths[peer] = path
        if theirs == "new-port":                       # guess at the far NAT's port, on its public address
            host = punchlib.spray_at(targets, self.share.outside)
            if host is not None:
                path.spray_host, path.knock_for = host, SPRAY_FOR
            path.start(targets)
        elif mine == "new-port":                       # be guessed at
            path.knock_for = SPRAY_FOR
            self._spawn(self._open_fan(peer, path, targets))
        else:
            path.start(targets)

    async def _open_fan(self, peer: str, path: udp.Path, targets: list) -> None:
        loop = asyncio.get_running_loop()
        made: list = []
        self.sockets[peer] = made                      # kept from the start: on_stop may come while these open
        for _ in range(udp.HARD_SOCKETS):
            try:
                transport, proto = await loop.create_datagram_endpoint(udp._Socket, sock=punchlib.udp_socket())
            except OSError:
                break
            proto.paths = self.share.sock.paths
            made.append(transport)
        path.fan = list(made)

        def through(keep) -> None:
            for transport in made:
                if transport is not keep:
                    transport.close()
            self.sockets[peer] = [keep] if keep in made else []
        path.on_through = through
        path.start(targets)

    def _close_sockets(self, peer: str) -> None:
        for transport in self.sockets.pop(peer, []):
            transport.close()

    def _down(self, path: udp.Path, why: str) -> None:
        self.share.sock.paths.pop(path.cid_in, None) if self.share.sock is not None else None
        if self.paths.get(path.peer) is path:
            del self.paths[path.peer]
        self._close_sockets(path.peer)
        path.stream.end()
        log.debug("mesh: udp link to %s: %s", path.peer[:12], why)

    async def _link(self, path: udp.Path, outbound: bool) -> None:
        node, stream = self.node, path.stream
        if path.peer in node.links:
            path.down("already linked")
            return
        try:
            link = await handshake(stream, stream, node.key, node.network, outbound, node.listen_port,
                                   initiator=node.node_id < path.peer)
        except LinkError as exc:
            log.info("mesh: udp link to %s refused: %s", path.peer[:12], exc)
            path.down("handshake")
            return
        if link.peer_id != path.peer:                  # somebody between swapped the introduction
            log.warning("mesh: udp path to %s answered as %s", path.peer[:12], link.peer_id[:12])
            path.down("wrong node")
            return
        self.cooling.pop(path.peer, None)
        log.info("mesh: linked by udp alone to %s at %s", path.peer[:12], path.addr)
        await node._run_link(link)
        path.down("link ended")

    # ---------------------------------------------------------------- the chain

    async def _publish_to(self, peer: str, header: dict) -> bool:
        if self.publish is None:
            return False
        body = {"to": peer[:16], "s": seal_to(peer, {k: v for k, v in header.items() if k != "hops"})}
        try:
            await asyncio.to_thread(self.publish, TOPIC, body)
        except Exception as exc:                       # noqa: BLE001 -- no coins, no Core: nothing to do
            log.info("mesh: chain introduction not sent: %s", exc)
            return False
        return True

    async def _ask_chain(self) -> None:
        if self.publish is None or self.announced is None or time.monotonic() - self.last_chain < CHAIN_EVERY and self.last_chain:
            return
        try:
            rows = await asyncio.to_thread(self.announced)
        except Exception:                              # noqa: BLE001
            return
        keys = [str(r.get("key")) for r in rows if len(str(r.get("key"))) == 64]
        random.shuffle(keys)
        self.last_chain = time.monotonic()
        for peer in [k for k in keys if self._due(k)][:CHAIN_TARGETS]:
            await self.meet(peer, chain=True)

    async def _read_chain(self) -> None:
        if self.read is None:
            return
        try:
            rows = await asyncio.to_thread(self.read, TOPIC)
        except Exception:                              # noqa: BLE001
            return
        for row in rows or []:
            mark = str(row.get("txid") or row.get("s"))
            body = row.get("body") if isinstance(row.get("body"), dict) else row
            if mark in self.seen_chain or body.get("to") != self.node.node_id[:16]:
                continue
            self.seen_chain.add(mark)
            said = open_sealed(self.node.key, str(body.get("s", "")))
            if said is not None and said.get("to") == self.node.node_id:
                await self._heard(said, chain=True)

    def status(self) -> list[dict]:
        return [{"peer": p, "up": path.usable, "at": f"{path.addr[0]}:{path.addr[1]}" if path.addr else None}
                for p, path in self.paths.items()]
