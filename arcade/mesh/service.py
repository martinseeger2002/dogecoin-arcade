"""The mesh, running inside an arcade node: one thread, one event loop, one MeshNode.

The web application is synchronous by design (routes run in a thread pool and
nothing in them awaits), and the mesh is a set of long-lived sockets. So the
mesh gets a thread of its own with its own loop, and everything the web side
asks of it crosses over with `run_coroutine_threadsafe`. Nothing the mesh does
can block a page, and no page can stall the mesh.

**Where peers come from.** The chain, first: every mesh announcement this
node's index has read (announce.py), so every node starts from the same list
and none of it comes from a website. Then whatever `--mesh-peer` names (a
machine on the same network, which the chain cannot carry), and then what the
peers themselves pass on.

**Saying where this node can be reached, by itself.** A node that listens
works out its own public address and announces it on the chain, again whenever
it changes -- with no website asked "what is my IP": the Pepecoin peers each of
its Core nodes is connected to say what address they see it at (getpeerinfo's
`addrlocal`), and a majority of them is the answer. It announces only an address
that has been shown to lead back here: it dials itself there (which works when
the router forwards the port and lets a machine reach its own address), or asks
a mesh peer to dial it back. A node behind a router with no port forwarded
therefore announces nothing on IPv4, which is right: it can still play by
dialling out.

**Behind a router that cannot be changed (Starlink, carrier NAT).** When IPv4
cannot be proved, a node with an IPv6 address announces that instead, without
proof: no proof is possible from inside (dialling your own IPv6 address never
leaves the machine), and none is needed, because IPv6 peers are not dialled,
they are met (node.py): both sides knock from their mesh ports on the same beat
of the clock and each router lets the other in. `status()["meet"]` names it.

**And a tunnel, as the floor under all of that** (2026-10-12, the operator: "not reliant on
Cloudflare, but we can use Cloudflare to strengthen the network"). A node whose IPv4
cannot be proved also runs a Cloudflare quick tunnel -- `cloudflared tunnel --url`,
free, no account, pointing at the node's local WebSocket port (wsock.py) -- and
announces the wss:// address it gets (`arcade-mesh-ws`). Every node can dial that,
IPv4-only homes included. The tunnel carries ciphertext only: the mesh handshake and
frames run inside it unchanged. A new address every time cloudflared restarts, so it is
announced again then. It stays up even once an address is proved: it is also every
browser's https way in to this node (realtime.py, "any node can be a player's way in"). No cloudflared on the machine, or `--mesh-tunnel off`: no tunnel,
and nothing else changes. `status()["tunnel"]` names it.
`--mesh-announce IP:PORT` still names an address by hand and skips all of this.

**Who a player is.** A player is their chain address, proved by a certificate
their own key signed: `cert_message` binds the address to ONE mesh node and an
expiry, and every node checks it with its own Core's `verifymessage`, which
needs no library and trusts nobody. A certificate stolen from one node is
useless on another, and the address is what the @tag on the chain points at,
so every node shows the same name for the same player from its own index.
Somebody who is not signed in plays as a guest -- `guest-` and eight hex
digits -- and a game can tell, because a guest carries no certificate.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import nacl.signing

from .node import Event, MeshError, MeshNode

log = logging.getLogger("arcade.mesh")

DEFAULT_PORT = 8421
#: What Core prefixes to a message before it signs it (validation.cpp
#: strMessageMagic). Pepecoin's; a Dogecoin node would say "Dogecoin".
MESSAGE_MAGIC = "Pepecoin Signed Message:\n"
#: How long a certificate may be good for. A day: long enough that a player
#: signs once per sitting, short enough that a leaked one goes stale.
CERT_DAYS = 1
GUEST = re.compile(r"^guest-[0-9a-f]{8}$")
BOOTSTRAP_EVERY = 60.0
ANNOUNCE_EVERY = 24 * 3600.0
ANNOUNCED_WITHIN = 14 * 24 * 3600       # chain announcements older than this are skipped
CHECK_EVERY = 600.0                     # how often a node looks at its own address
TUNNEL_FIRST, TUNNEL_EVERY = 45.0, 60.0 # when the tunnel keeper first looks, and how often after
KEEP_PEERS = 30 * 24 * 3600             # how long a peer this node once reached is remembered
SAVE_PEERS_EVERY = 300.0
MIN_VOTES = 2                           # peers that must agree on what they see


def public_address(seen: list[str], versions: tuple[int, ...] = (4, 6)) -> str | None:
    """The address most peers say they see this node at, if at least MIN_VOTES
    agree. IPv4 first: an IPv6 address on a home network is often a temporary
    one that changes by itself, which would mean announcing again every day.
    `versions=(6,)` asks for the IPv6 one only."""
    import collections
    import ipaddress
    votes: dict[int, collections.Counter] = {4: collections.Counter(), 6: collections.Counter()}
    for raw in seen:
        host = str(raw or "").strip()
        if host.startswith("["):
            host = host[1:host.index("]")] if "]" in host else host[1:]
        elif host.count(":") == 1:
            host = host.split(":")[0]
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue
        if ip.is_global:
            votes[ip.version][str(ip)] += 1
    for version in versions:
        if votes[version]:
            host, n = votes[version].most_common(1)[0]
            if n >= MIN_VOTES:
                return host
    return None


def own_ipv6() -> str | None:
    """The global IPv6 address this machine would send from, asked of the kernel
    (a UDP socket 'connected' to a public address sends nothing). The fallback
    when Core's peers report no IPv6 address for this node."""
    import ipaddress
    import socket
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
            s.connect(("2001:4860:4860::8888", 9))
            host = s.getsockname()[0]
        return host if ipaddress.ip_address(host).is_global else None
    except OSError:
        return None


def cert_message(network: str, node_id: str, address: str, expires: int) -> str:
    """The exact words a player's key signs to be `address` on node `node_id`."""
    return f"arcade-realtime\n{network}\n{node_id}\n{address}\n{int(expires)}"


def load_or_make_key(path: Path) -> nacl.signing.SigningKey:
    """The node's mesh key: 32 random bytes, made once, readable only by this user."""
    if path.exists():
        return nacl.signing.SigningKey(bytes.fromhex(path.read_text().strip()))
    key = nacl.signing.SigningKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(bytes(key).hex() + "\n")
    return key


@dataclass
class Session:
    """One page's place in one room: what its token unlocks."""
    token: str
    room: str
    member: str
    cred: dict | None
    queue: asyncio.Queue


class MeshService:
    def __init__(self, network: str, key: nacl.signing.SigningKey, *,
                 listen_host: str | None = None, listen_port: int | None = DEFAULT_PORT,
                 peers: list[tuple[str, int]] | None = None,
                 verify: Callable[[str, str, str], bool] | None = None,
                 announced: Callable[[], list[dict]] | None = None,
                 announce: Callable[[str, int], Any] | None = None,
                 announce_at: tuple[str, int] | None = None,
                 observe: Callable[[], list[str]] | None = None,
                 public_port: int | None = None,
                 remember: Path | None = None,
                 own_v6: Callable[[], str | None] | None = None,
                 peers_file: Path | None = None,
                 max_inbound: int | None = None,
                 tunnel: str | None = None,
                 ws_port: int | None = None,
                 web_port: int | None = None):
        """`verify(address, signature, message)` asks a Core node; `announced()`
        lists the chain's mesh announcements; `announce(host, port)` publishes
        this node's; `announce_at` fixes the address by hand; `observe()` lists
        the addresses Core's peers see this node at; `public_port` is the port
        the router forwards (the listening port unless said); `remember` keeps
        the last announcement across restarts. All optional: a test runs a node
        with none of them."""
        self.network = network
        self.verify = verify
        self.announced = announced
        self.announce = announce
        self.announce_at = announce_at
        self.observe = observe
        self.public_port = public_port or listen_port
        self.remember = remember
        self.own_v6 = own_v6                    # this machine's IPv6 address, when Core's peers name none
        # Every peer this node has actually linked to, kept across restarts: a node
        # that restarts finds the mesh again from these, even if no announcement on
        # the chain is fresh and the node that announced last has gone (2026-10-12:
        # "not dependent on any node").
        self.peers_file = peers_file
        self.last_announced = self._recall()
        self.reachable: bool | None = None
        self.meet: str | None = None            # the IPv6 address peers meet this node at
        # The cloudflared program to run a quick tunnel with, or None (wsock.py, notes above).
        self.tunnel_cmd = tunnel
        self.core = None                        # corelink.CoreBridge, when Core rides the mesh
        self.tunnel_url: str | None = None
        self._tunnel_said: str | None = None    # the tunnel address last announced successfully
        self._announcing = threading.Lock()     # one announcement at a time: two at once spend the same coin
        self._tunnel_proc = None
        self.node = MeshNode(key, network, listen_host=listen_host, listen_port=listen_port,
                             peers=peers or [], vouch=self._vouch,
                             ws_port=(ws_port if ws_port is not None else 0) if tunnel else None,
                             web_port=web_port,
                             **({"max_inbound": max_inbound} if max_inbound else {}))
        self.node_id = self.node.node_id
        for host, port, node_id in self._load_peers():
            self.node.add_address(host, port, node_id)
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._sessions: dict[str, Session] = {}
        # Several pages of one member in one room (2026-10-09: ASHVALE and Ziibiing open
        # in two tabs as the same account shared ONE session -- one queue read by both
        # pages, and either page leaving took the other out of the room, so the two
        # kicked each other off and their saves failed meanwhile). The node still has
        # the member once; each page gets its own session and its own copy of every
        # event, fanned out from the member's queue, and the member leaves the room
        # only with its last page.
        self._fans: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> "MeshService":
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            ready.set()
            self.loop.run_forever()
            self.loop.close()

        self._thread = threading.Thread(target=run, name="arcade-mesh", daemon=True)
        self._thread.start()
        ready.wait()
        self._call(self.node.start(), timeout=10)
        self.loop.call_soon_threadsafe(self._spawn_background)
        return self

    def stop(self) -> None:
        if self.loop is None:
            return
        try:
            self.save_peers()
        except Exception:                              # noqa: BLE001 -- stopping matters more
            log.exception("mesh: saving peers")
        try:
            self._call(self._tunnel_stop(), timeout=15)
        except Exception:                              # noqa: BLE001
            pass
        try:
            self._call(self.node.stop(), timeout=10)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=10)
            self.loop = None

    def _spawn_background(self) -> None:
        self.node._spawn(self._bootstrapper())
        if self.peers_file is not None:
            self.node._spawn(self._peer_keeper())
        if self.announce is not None and (self.announce_at or self.observe):
            self.node._spawn(self._announcer())
        if self.tunnel_cmd and self.node.ws_port:
            self.node._spawn(self._tunneler())

    def _call(self, coro, timeout: float = 5.0):
        """Run a coroutine on the mesh's loop from any other thread, and wait."""
        if self.loop is None:
            coro.close()
            raise MeshError("the mesh is not running")
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    # ---------------------------------------------------------------- peers

    async def _bootstrapper(self) -> None:
        while True:
            if self.announced is not None:
                try:
                    rows = await asyncio.to_thread(self.announced)
                    for row in rows:
                        if row["key"] != self.node_id:
                            self.node.add_address(row["host"], int(row["port"]), row["key"])
                except Exception:                      # noqa: BLE001 -- try again later
                    log.exception("mesh bootstrap")
            await asyncio.sleep(BOOTSTRAP_EVERY)

    def _peer_rows(self) -> list[tuple[str, int, str, float]]:
        if self.peers_file is None:
            return []
        try:
            import json
            rows = json.loads(self.peers_file.read_text())
        except (OSError, ValueError):
            return []
        out = []
        for r in rows if isinstance(rows, list) else []:
            try:
                out.append((str(r["h"]), int(r["p"]), str(r["id"]), float(r["ok"])))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def _load_peers(self) -> list[tuple[str, int, str]]:
        now = time.time()
        return [(h, p, i) for h, p, i, ok in self._peer_rows() if now - ok < KEEP_PEERS and i != self.node_id]

    def save_peers(self) -> int:
        """Write the peers this node has reached (and still remembers) to peers_file."""
        if self.peers_file is None:
            return 0
        import json
        now = time.time()
        rows = {(h, p): {"h": h, "p": p, "id": i, "ok": ok}             # what is on file ...
                for h, p, i, ok in self._peer_rows()}
        for (h, p), e in list(self.node.addrs.items()):                   # ... and what this run reached
            if e.get("ok") and e.get("id") and e["id"] != self.node_id:
                rows[(h, p)] = {"h": h, "p": p, "id": e["id"], "ok": e["ok"]}
        keep = sorted((r for r in rows.values() if now - r["ok"] < KEEP_PEERS), key=lambda r: -r["ok"])[:200]
        tmp = self.peers_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(keep))
            tmp.replace(self.peers_file)
        except OSError:
            return 0
        return len(keep)

    async def _peer_keeper(self) -> None:
        while True:
            await asyncio.sleep(SAVE_PEERS_EVERY)
            try:
                await asyncio.to_thread(self.save_peers)
            except Exception:                          # noqa: BLE001
                log.exception("mesh: saving peers")

    def _recall(self) -> dict:
        try:
            import json
            return json.loads(self.remember.read_text()) if self.remember else {}
        except (OSError, ValueError):
            return {}

    def _keep(self, said: dict) -> None:
        self.last_announced = said
        if self.remember:
            import json
            try:
                self.remember.write_text(json.dumps(said))
            except OSError:
                pass

    async def where_am_i(self) -> tuple[str, int] | None:
        """The public address this node can be reached at, proved, or None."""
        if self.announce_at:
            return self.announce_at
        if self.node.listen_port is None or self.observe is None:
            return None
        try:
            seen = await asyncio.to_thread(self.observe)
        except Exception:                          # noqa: BLE001 -- ask again later
            seen = []
        port = int(self.public_port or self.node.listen_port)
        host = public_address(seen, (4,))
        if host is not None:
            self.reachable = await self.node.reaches_itself(host, port)
            if not self.reachable and self.node.links:
                said = await self.node.ask_dial_back(port)
                self.reachable = said["reached"]
            if self.reachable:
                self.meet = None
                return host, port
        # IPv4 cannot be reached from outside: meet over IPv6 instead (see the notes above).
        v6 = public_address(seen, (6,)) or (await asyncio.to_thread(self.own_v6) if self.own_v6 else None)
        self.meet = self.node.meet_from = v6
        return (v6, int(self.node.listen_port)) if v6 else None

    async def announce_if_due(self) -> bool:
        """Announce when the address is new, or once a day to stay fresh."""
        found = await self.where_am_i()
        if found is None:
            return False
        host, port = found
        last = self.last_announced or {}
        fresh = time.time() - float(last.get("at") or 0) < ANNOUNCE_EVERY
        if last.get("host") == host and last.get("port") == port and fresh:
            return False
        await asyncio.to_thread(self._announce_locked, host, port)
        log.info("mesh: announced this node at %s:%s", host, port)
        self._keep({"host": host, "port": port, "at": int(time.time())})
        return True

    async def _tunneler(self) -> None:
        """Keep a quick tunnel up while this node cannot be reached otherwise (see the notes)."""
        import re
        import tempfile
        await asyncio.sleep(TUNNEL_FIRST)               # after the first look at its own address
        home = tempfile.mkdtemp(prefix="arcade-tunnel-")  # no ~/.cloudflared/config.yml: a quick tunnel
        while True:
            try:
                # Kept up even when an address is proved (2026-10-12): the tunnel is also how a
                # BROWSER reaches this node over https, to use it as its way in (realtime.py).
                # Nodes dialling nodes still prefer a direct address (node.py _dialer).
                if self._tunnel_proc is None or self._tunnel_proc.returncode is not None:
                    self.tunnel_url = None
                    self._tunnel_proc = await asyncio.create_subprocess_exec(
                        self.tunnel_cmd, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.node.ws_port}",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                        env=dict(os.environ, HOME=home))
                    end = time.monotonic() + 60
                    while time.monotonic() < end and self.tunnel_url is None:
                        line = await asyncio.wait_for(self._tunnel_proc.stdout.readline(), 60)
                        if not line:
                            break
                        m = re.search(rb"https://[a-z0-9-]+\.trycloudflare\.com", line)
                        if m:
                            self.tunnel_url = "wss://" + m.group(0).decode()[8:] + "/mesh"
                    if self.tunnel_url:
                        self.node._spawn(self._drain_tunnel_output(self._tunnel_proc))
                        log.info("mesh: reachable through a tunnel at %s", self.tunnel_url)
                    else:
                        await self._tunnel_stop()
                # Announced until it sticks: a failure (2026-10-12 live: txn-mempool-conflict, the IPv6
                # announcement went in the same second) is tried again on the next round.
                if self.tunnel_url and self.tunnel_url != self._tunnel_said and self.announce is not None:
                    await asyncio.to_thread(self._announce_locked, self.tunnel_url, 443)
                    self._tunnel_said = self.tunnel_url
                    log.info("mesh: announced the tunnel %s", self.tunnel_url)
            except Exception as exc:                   # noqa: BLE001 -- try again later
                log.warning("mesh: tunnel: %s", exc)
            await asyncio.sleep(TUNNEL_EVERY)

    def _announce_locked(self, host: str, port: int):
        with self._announcing:
            return self.announce(host, port)

    async def _drain_tunnel_output(self, proc) -> None:
        try:
            while await proc.stdout.readline():
                pass                                        # cloudflared talks; nobody needs to listen
        except Exception:                                   # noqa: BLE001
            pass

    async def _tunnel_stop(self) -> None:
        proc, self._tunnel_proc, self.tunnel_url = self._tunnel_proc, None, None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                proc.kill()

    async def _announcer(self) -> None:
        await asyncio.sleep(30)                         # let the node settle first
        while True:
            try:
                await self.announce_if_due()
            except Exception as exc:                   # noqa: BLE001
                log.warning("mesh: could not announce this node: %s", exc)
            await asyncio.sleep(CHECK_EVERY)

    # ---------------------------------------------------------------- who is who

    async def _vouch(self, origin: str, member: str, cred: dict | None) -> bool:
        if cred is None:
            return bool(GUEST.match(member))
        try:
            address, expires, sig = str(cred["a"]), int(cred["x"]), str(cred["s"])
        except (KeyError, TypeError, ValueError):
            return False
        now = time.time()
        if address != member or not now < expires <= now + CERT_DAYS * 86400 + 3600:
            return False
        if self.verify is None:
            return False
        message = cert_message(self.network, origin, address, expires)
        return bool(await asyncio.to_thread(self.verify, address, sig, message))

    def check_cert(self, member: str, cred: dict) -> bool:
        """Is this certificate good for `member` on THIS node? (Asked by the web
        layer before it lets its own player in under that name.)"""
        return bool(self._call(self._vouch(self.node_id, member, cred), timeout=15))

    @staticmethod
    def new_guest() -> str:
        return "guest-" + secrets.token_hex(4)

    @staticmethod
    def expiry() -> int:
        """When a certificate made now should run out: a day, on the hour, so a
        page that asks twice in one sitting gets the same words to sign."""
        return (int(time.time()) // 3600 + 1) * 3600 + (CERT_DAYS * 86400 - 3600)

    # ---------------------------------------------------------------- the web side

    def online(self, node_id: str) -> bool:
        """Whether a node is on the mesh right now (its heartbeat, or a direct link).
        Read from the web thread: a dict lookup, no call into the mesh loop."""
        try:
            return bool(self.node.online(str(node_id)))
        except Exception:
            return False

    def status(self) -> dict:
        links = len(self.node.links)
        return {"linked": links > 0, "node": self.node_id, "network": self.network,
                "peers": links, "known": len(self.node.addrs),
                "rtt_ms": sorted(v for v in self.node.rtts().values() if v is not None),
                "listening": self.node.listen_port, "reachable": self.reachable, "meet": self.meet,
                "tunnel": self.tunnel_url,
                "core": self.core.status() if self.core is not None else None,
                "announced": {k: self.last_announced.get(k) for k in ("host", "port", "at")}
                if self.last_announced else None}

    def join(self, room: str, member: str, cred: dict | None) -> Session:
        key = (room, member)

        async def attach() -> asyncio.Queue:
            # On the mesh loop, so the fan and the node never race each other.
            fan = self._fans.get(key)
            if fan is None or key not in self.node.local:
                if fan is not None:
                    fan["task"].cancel()
                base = await self.node.join(room, member, cred)
                fan = {"base": base, "outs": [], "task": None}
                self._fans[key] = fan

                async def pump(f=fan):
                    while True:
                        ev = await f["base"].get()
                        for out in list(f["outs"]):
                            try:
                                out.put_nowait(ev)
                            except asyncio.QueueFull:   # a page not reading: it rejoins
                                pass
                fan["task"] = self.node._spawn(pump())   # the node cancels it when it stops
            out: asyncio.Queue = asyncio.Queue(maxsize=1000)
            fan["outs"].append(out)
            return out

        queue = self._call(attach())
        session = Session(secrets.token_urlsafe(18), room, member, cred, queue)
        with self._lock:
            self._sessions[session.token] = session
        return session

    async def _detach(self, s: Session) -> None:
        """One page gone; the member leaves the room only when it was the last."""
        key = (s.room, s.member)
        fan = self._fans.get(key)
        if fan is not None:
            fan["outs"] = [q for q in fan["outs"] if q is not s.queue]
            if fan["outs"]:
                return
            fan["task"].cancel()
            self._fans.pop(key, None)
        await self.node.leave(s.room, s.member)

    def present(self, room: str, member: str) -> bool:
        """Is `member` in `room` on this node right now?"""
        return (room, member) in self.node.local

    def session(self, token: str) -> Session:
        with self._lock:
            found = self._sessions.get(str(token or ""))
        if found is None or (found.room, found.member) not in self.node.local:
            with self._lock:
                self._sessions.pop(str(token or ""), None)
            raise MeshError("not in that room any more -- join again")
        return found

    def send(self, token: str, data: bytes) -> None:
        s = self.session(token)
        self._call(self.node.send(s.room, s.member, data))

    def leave(self, token: str) -> None:
        with self._lock:
            s = self._sessions.pop(str(token or ""), None)
        if s is not None and self.loop is not None:
            self._call(self._detach(s))

    def touch(self, token: str) -> None:
        with self._lock:
            s = self._sessions.get(str(token or ""))
        if s is not None and self.loop is not None:
            self.loop.call_soon_threadsafe(self.node.touch, s.room, s.member)

    def members(self, room: str) -> list[dict]:
        async def read():
            return self.node.members(room)
        return self._call(read())

    async def next_event(self, token: str, timeout: float) -> Event | None:
        """Awaited from the WEB server's loop: the next thing this page should hear."""
        s = self.session(token)

        async def get():
            try:
                return await asyncio.wait_for(s.queue.get(), timeout)
            except asyncio.TimeoutError:
                return None
        if self.loop is None:
            raise MeshError("the mesh is not running")
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(get(), self.loop))


def for_state(state, *, listen_host: str | None = None, listen_port: int | None = DEFAULT_PORT,
              peers: list[tuple[str, int]] | None = None,
              announce_at: tuple[str, int] | None = None,
              public_port: int | None = None, max_inbound: int | None = None,
              tunnel: str | None = None, core: bool = True,
              web_port: int | None = None) -> MeshService:
    """A MeshService wired to an arcade node: its key in the arcade's home, its
    peers from the messaging chain's index, and players checked by the
    messaging chain's Core -- the chain the @tags live on, so a player's
    address is the one their name points at."""
    key = load_or_make_key(Path(state.home) / "mesh.key")
    tags = state.messaging

    def verify(address: str, signature: str, message: str) -> bool:
        try:
            with tags.rpc() as rpc:
                return bool(rpc.call("verifymessage", address, signature, message))
        except Exception:                              # noqa: BLE001 -- unproven is refused
            return False

    def announced() -> list[dict]:
        if not state.store_path.exists():
            return []
        with state.store() as store:
            rows = store.mesh_peers(state.messaging.params.name,
                                    since=int(time.time()) - ANNOUNCED_WITHIN)
        return [{"host": r["host"], "port": r["port"], "key": r["mesh_key"]} for r in rows]

    def observe() -> list[str]:
        """Where the Pepecoin network sees this machine: each Core peer's addrlocal."""
        seen: list[str] = []
        for chain in (state.ledger, state.messaging):
            try:
                with chain.rpc() as rpc:
                    seen += [str(p.get("addrlocal") or "") for p in rpc.call("getpeerinfo")]
            except Exception:                          # noqa: BLE001 -- the other chain may answer
                continue
        return seen

    service = MeshService(
        f"arcade-{state.ledger.params.name}", key, listen_host=listen_host,
        listen_port=listen_port, peers=peers, verify=verify, announced=announced,
        announce=lambda host, port: state.announce_mesh(host, port, bytes(key.verify_key).hex()),
        announce_at=announce_at, observe=observe, public_port=public_port,
        remember=Path(state.home) / "mesh-announced.json", own_v6=own_ipv6,
        peers_file=Path(state.home) / "mesh-peers.json", max_inbound=max_inbound,
        tunnel=tunnel, web_port=web_port)
    if core:
        # Core to Core through the mesh (corelink.py): each chain's own peer port, and its addnode.
        from .corelink import CoreBridge
        by_name = {"main": state.ledger, "test": state.messaging}
        chains = {}
        for name, chain in by_name.items():
            try:
                chains[name] = int(chain.params.p2p_port)
            except Exception:                              # noqa: BLE001 -- a chain this node does not run
                continue

        def addnode(name: str, hostport: str, op: str):
            with by_name[name].rpc() as rpc:
                return rpc.call("addnode", hostport, op)
        service.core = CoreBridge(chains, addnode)
        service.node.add_extension(service.core)
    return service
