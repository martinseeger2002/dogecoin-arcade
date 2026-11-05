"""A stratum server for the ASICs on this arcade's own network: solo mining, with work from a chain's node here
(2026-11-04, SadFrog: "a section to hookup local asics to mine the appropriate" chain).

An ASIC is pointed at stratum+tcp://<this machine>:<port>, with any worker name and password. It is given work built
from the node's `getblocktemplate`: a coinbase paying the operator's address whole, split around the 8 bytes of
extranonce the miner fills (4 ours, per connection, and 4 its own), the merkle branch, and the header's fields. Its
shares are checked here at the share difficulty (vardiff aims at one every few seconds), and one that meets the
block's own target is sent to the node as a block (`submitblock`). Stratum v1 (mining.subscribe, authorize, notify,
set_difficulty, submit), with mining.configure's version rolling (BIP 310) for the SHA-256 ASICs that roll it.

What it mines: a chain whose node runs here, whose blocks are not merge-mined (Dogecoin, Pepecoin and the other AuxPoW
chains need a parent's work: not here), with an algorithm an ASIC hashes: scrypt (Litecoin, DigiByte's scrypt) or
sha256d (DigiByte's SHA-256). DigiByte's node is asked for that algorithm's own template (getblocktemplate's second
argument), whose version carries the algorithm. Only miners on this network may connect, unless the operator says
otherwise: the port is a door to the node's work, and a stranger's shares cost this machine their checking.

Settings: `stratum` {on, net, algo, address, port, difficulty, anyone}."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import struct
import threading
import time
from typing import Any, Callable

from .blockminer import TAG, merkle_root, output_script, pow_hash, push, script_number, sha256d, target_of, varint

log = logging.getLogger(__name__)

SETTING = "stratum"
PORT = 3333
#: The hashes a share of difficulty 1 is worth: pools' own convention, by algorithm (Bitcoin's 0xFFFF << 208;
#: scrypt pools' 2^16 times easier).
DIFF1 = {"sha256d": 0xFFFF << 208, "scrypt": 0xFFFF << 224}
HASHES_PER_DIFF1 = {"sha256d": 2 ** 32, "scrypt": 2 ** 16}
#: Where a new miner starts, by algorithm, before vardiff finds its pace: a small scrypt ASIC's and a small SHA-256
#: one's.
START_DIFFICULTY = {"scrypt": 1024.0, "sha256d": 65536.0}
#: Vardiff: a share about this often, looked at this often, moving at most this much at a time.
SHARE_SECONDS, RETARGET_SECONDS, MOST_STEP = 8.0, 30.0, 4.0
#: A new template this often even when no block came (new transactions, a later time), and the node asked this often.
JOB_SECONDS, POLL_SECONDS = 30.0, 3.0
#: BIP 310: the version bits a miner may roll. DigiByte's algorithm bits (8-11) are outside it.
VERSION_MASK = 0x1FFFE000
EXTRANONCE2_SIZE = 4
ALGOS = ("scrypt", "sha256d")


class StratumError(ValueError):
    """The server could not be set up as asked, said for the operator."""


# --------------------------------------------------------------------------- what can be mined

def asic_algos(params) -> tuple[str, ...]:
    """The algorithms an ASIC mines `params`' chain with here: () for one that is merge-mined or whose algorithm no
    ASIC here hashes."""
    if getattr(params, "auxpow_chain_id", None) is not None:
        return ()
    algos = tuple(getattr(params, "algos", ()) or ()) or ((getattr(params, "pow_algo", "") or "scrypt"),)
    return tuple(a for a in algos if a in ALGOS)


# --------------------------------------------------------------------------- work

def merkle_branch(txids: list[bytes]) -> list[bytes]:
    """The hashes the coinbase (index 0) is folded with, bottom up, to the merkle root of [coinbase] + txids."""
    steps, level = [], [b""] + list(txids)
    while len(level) > 1:
        steps.append(level[1])
        rest = level[2:]
        if len(rest) % 2:
            rest.append(rest[-1])
        level = [b""] + [sha256d(rest[i] + rest[i + 1]) for i in range(0, len(rest), 2)]
    return steps


def fold(coinbase_hash: bytes, branch: list[bytes]) -> bytes:
    root = coinbase_hash
    for step in branch:
        root = sha256d(root + step)
    return root


class Job:
    """One template as stratum work: the coinbase split around the extranonce, and what the block needs after."""

    def __init__(self, job_id: str, template: dict, pay_script: bytes, algo: str):
        self.id, self.algo, self.template = job_id, algo, template
        self.height = int(template["height"])
        self.prev = template["previousblockhash"]
        self.version = int(template["version"])
        self.bits = int(template["bits"], 16)
        self.ntime = int(template["curtime"])
        self.target = int(template["target"], 16) if template.get("target") else target_of(self.bits)
        reward = int(template["coinbasevalue"])
        # BIP34's height as CScript() << height writes it: OP_1 to OP_16 for 1 to 16, a number push above.
        height = bytes([0x50 + self.height]) if 1 <= self.height <= 16 else push(script_number(self.height))
        head, tail = height + bytes([8]), push(TAG)
        size = len(head) + 8 + len(tail)
        outs = [(reward, pay_script)]
        self.commitment = template.get("default_witness_commitment")
        if self.commitment:
            outs.append((0, bytes.fromhex(self.commitment)))
        self.coinb1 = (struct.pack("<i", 1) + varint(1) + b"\x00" * 32 + b"\xff\xff\xff\xff" + varint(size) + head)
        self.coinb2 = (tail + b"\xff\xff\xff\xff" + varint(len(outs))
                       + b"".join(struct.pack("<q", v) + varint(len(s)) + s for v, s in outs) + b"\x00\x00\x00\x00")
        txs = template.get("transactions") or []
        self.branch = merkle_branch([bytes.fromhex(t["txid"])[::-1] for t in txs])
        self.txdata = b"".join(bytes.fromhex(t["data"]) for t in txs)
        self.count = 1 + len(txs)
        self.mweb = template.get("mweb")
        self.made = time.time()

    def notify(self, clean: bool) -> list:
        prev = bytes.fromhex(self.prev)[::-1]
        words = b"".join(prev[i:i + 4][::-1] for i in range(0, 32, 4))
        return [self.id, words.hex(), self.coinb1.hex(), self.coinb2.hex(), [b.hex() for b in self.branch],
                f"{self.version:08x}", f"{self.bits:08x}", f"{self.ntime:08x}", clean]

    def header(self, extranonce1: bytes, extranonce2: bytes, ntime: int, nonce: int, version: int) -> tuple[bytes, bytes]:
        """(the 80-byte header, the coinbase) a miner's answer makes."""
        coinbase = self.coinb1 + extranonce1 + extranonce2 + self.coinb2
        root = fold(sha256d(coinbase), self.branch)
        head = (struct.pack("<I", version & 0xFFFFFFFF) + bytes.fromhex(self.prev)[::-1] + root
                + struct.pack("<I", ntime) + struct.pack("<I", self.bits) + struct.pack("<I", nonce))
        return head, coinbase

    def block(self, header: bytes, coinbase: bytes) -> bytes:
        if self.commitment:          # the witness reserved value the commitment is made with
            coinbase = coinbase[:4] + b"\x00\x01" + coinbase[4:-4] + b"\x01\x20" + b"\x00" * 32 + coinbase[-4:]
        blob = header + varint(self.count) + coinbase + self.txdata
        if self.mweb:                # Litecoin: the block's MWEB part after its transactions
            blob += b"\x01" + bytes.fromhex(self.mweb)
        return blob


# --------------------------------------------------------------------------- the server

class Miner:
    """One connected ASIC."""

    def __init__(self, server: "Server", writer: asyncio.StreamWriter, n: int):
        self.server, self.writer = server, writer
        self.peer = (writer.get_extra_info("peername") or ("?", 0))[0]
        self.extranonce1 = struct.pack(">I", n)
        self.worker, self.subscribed, self.mask = "", False, 0
        self.difficulty = float(server.start_difficulty)
        self.shares, self.rejected, self.since, self.window = 0, 0, time.time(), []
        self.hashes, self.last = 0.0, 0.0
        self.seen: set = set()

    def send(self, obj: dict) -> None:
        self.writer.write((json.dumps(obj) + "\n").encode())

    def hashrate(self) -> float:
        span = max(time.time() - self.since, 1.0)
        return self.hashes / span

    def as_row(self) -> dict:
        return {"peer": self.peer, "worker": self.worker, "difficulty": self.difficulty, "shares": self.shares,
                "rejected": self.rejected, "hashrate": round(self.hashrate()), "last_share": self.last or None}


class Server:
    """The stratum server, on a thread and event loop of its own. `call(method, params)` asks the chain's node."""

    def __init__(self, call: Callable[[str, list], Any], params, algo: str, address: str, port: int = PORT,
                 host: str = "0.0.0.0", difficulty: float | None = None, anyone: bool = False,
                 rules: tuple[str, ...] = ("segwit",)):
        if algo not in asic_algos(params):
            raise StratumError(f"{params.label} is not mined with {algo} by an ASIC here: "
                               + (", ".join(asic_algos(params)) or "it is merge-mined, or its algorithm is not one"))
        self.call, self.params, self.algo, self.address = call, params, algo, address
        self.pay = output_script(address, params)
        self.port, self.host, self.anyone = int(port), host, bool(anyone)
        self.start_difficulty = float(difficulty or START_DIFFICULTY[algo])
        self.rules = list(rules) + (["mweb"] if getattr(params, "mweb", False) else [])
        self.multi = bool(getattr(params, "algos", ()))
        self.jobs: dict[str, Job] = {}
        self.job: Job | None = None
        self.miners: list[Miner] = []
        self.blocks: list[dict] = []
        self.error = ""
        self.loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._n = 0
        self._ready = threading.Event()
        self.thread: threading.Thread | None = None
        self.started = time.time()

    # --- running ---------------------------------------------------------------------------------------------------

    def start(self, wait: float = 10.0) -> "Server":
        self.thread = threading.Thread(target=self._run, name=f"arcade-stratum-{self.params.name}", daemon=True)
        self.thread.start()
        self._ready.wait(wait)
        if self.error:
            raise StratumError(self.error)
        return self

    def stop(self) -> None:
        loop = self.loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if self.thread is not None:
            self.thread.join(timeout=5)

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive() and not self.error)

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._open())
        except Exception as exc:                           # noqa: BLE001 -- said in status, and to start()
            self.error = f"could not listen on port {self.port}: {exc}"
            self._ready.set()
            return
        self._ready.set()
        poller = self.loop.create_task(self._poll())
        try:
            self.loop.run_forever()
        finally:
            poller.cancel()
            for m in list(self.miners):
                m.writer.close()
            if self._server is not None:
                self._server.close()
            try:
                self.loop.run_until_complete(asyncio.sleep(0))
            except Exception:                              # noqa: BLE001
                pass
            self.loop.close()

    async def _open(self) -> None:
        # A reload starts the new worker beside the old one (web/supervisor.py), which still holds the port for a
        # moment: tried again for a while rather than given up at once.
        deadline = time.time() + 60
        while True:
            try:
                self._server = await asyncio.start_server(self._serve, self.host, self.port, reuse_address=True)
                return
            except OSError:
                if time.time() > deadline:
                    raise
                await asyncio.sleep(2)

    async def _ask(self, method: str, params: list) -> Any:
        return await self.loop.run_in_executor(None, self.call, method, params)

    async def _template(self) -> dict:
        args: list = [{"rules": self.rules}]
        if self.multi:
            args.append(self.algo)                         # DigiByte: this algorithm's own template
        return await self._ask("getblocktemplate", args)

    async def _poll(self) -> None:
        while True:
            try:
                template = await self._template()
                fresh = self.job is None or template["previousblockhash"] != self.job.prev
                if fresh or time.time() - self.job.made >= JOB_SECONDS:
                    self._new_job(template, clean=fresh)
                self.error = ""
            except Exception as exc:                       # noqa: BLE001 -- said in status, asked again
                self.error = f"the node did not give work: {exc}"[:300]
            await asyncio.sleep(POLL_SECONDS)

    def _new_job(self, template: dict, clean: bool) -> None:
        self._n += 1
        job = Job(f"{self._n:x}", template, self.pay, self.algo)
        if clean:
            self.jobs.clear()
        self.jobs[job.id] = job
        while len(self.jobs) > 16:
            self.jobs.pop(next(iter(self.jobs)))
        self.job = job
        for m in list(self.miners):
            if m.subscribed:
                m.send({"id": None, "method": "mining.notify", "params": job.notify(clean)})

    # --- one miner -------------------------------------------------------------------------------------------------

    def _allowed(self, peer: str) -> bool:
        if self.anyone:
            return True
        try:
            ip = ipaddress.ip_address(peer)
        except ValueError:
            return False
        ip = getattr(ip, "ipv4_mapped", None) or ip
        return ip.is_private or ip.is_loopback or ip.is_link_local

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._n += 1
        miner = Miner(self, writer, self._n)
        if not self._allowed(miner.peer):
            log.info("stratum: refused %s, not on this network", miner.peer)
            writer.close()
            return
        self.miners.append(miner)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                if len(line) > 16384:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    break
                self._answer(miner, msg)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if miner in self.miners:
                self.miners.remove(miner)
            writer.close()

    def _answer(self, miner: Miner, msg: dict) -> None:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or []
        if method == "mining.subscribe":
            miner.subscribed = True
            miner.send({"id": mid, "result": [[["mining.set_difficulty", "1"], ["mining.notify", "1"]],
                                              miner.extranonce1.hex(), EXTRANONCE2_SIZE], "error": None})
        elif method == "mining.authorize":
            miner.worker = str(params[0] if params else "")[:64]
            miner.send({"id": mid, "result": True, "error": None})
            miner.send({"id": None, "method": "mining.set_difficulty", "params": [miner.difficulty]})
            if self.job is not None:
                miner.send({"id": None, "method": "mining.notify", "params": self.job.notify(True)})
        elif method == "mining.configure":
            asked = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}
            mask = VERSION_MASK & int(str(asked.get("version-rolling.mask", "ffffffff")), 16)
            miner.mask = mask if "version-rolling" in (params[0] if params else []) else 0
            miner.send({"id": mid, "result": {"version-rolling": bool(miner.mask),
                                              "version-rolling.mask": f"{miner.mask:08x}"}, "error": None})
        elif method == "mining.suggest_difficulty":
            try:
                miner.difficulty = max(float(params[0]), 1e-12)
            except (TypeError, ValueError, IndexError):
                pass
            miner.send({"id": mid, "result": True, "error": None})
            miner.send({"id": None, "method": "mining.set_difficulty", "params": [miner.difficulty]})
        elif method == "mining.extranonce.subscribe":
            miner.send({"id": mid, "result": True, "error": None})
        elif method == "mining.submit":
            ok, why = self._submit(miner, params)
            miner.send({"id": mid, "result": ok, "error": None if ok else [why[0], why[1], None]})
            self._vardiff(miner)
        else:
            miner.send({"id": mid, "result": None, "error": [20, f"unknown method {method}", None]})

    def _submit(self, miner: Miner, params: list) -> tuple[bool, tuple[int, str]]:
        try:
            _, job_id, en2, ntime, nonce = (str(p) for p in params[:5])
            rolled = int(str(params[5]), 16) if len(params) > 5 and params[5] else None
            job = self.jobs.get(job_id)
            if job is None:
                miner.rejected += 1
                return False, (21, "job not found (stale)")
            extranonce2 = bytes.fromhex(en2)
            if len(extranonce2) != EXTRANONCE2_SIZE:
                miner.rejected += 1
                return False, (20, "extranonce2 is the wrong size")
            version = job.version
            if rolled is not None:
                if rolled & ~miner.mask:
                    miner.rejected += 1
                    return False, (20, "version bits outside the mask")
                version = (job.version & ~miner.mask) | (rolled & miner.mask)
            key = (job_id, en2, ntime, nonce, version)
            if key in miner.seen:
                miner.rejected += 1
                return False, (22, "duplicate share")
            miner.seen.add(key)
            if len(miner.seen) > 50_000:
                miner.seen.clear()
            header, coinbase = job.header(miner.extranonce1, extranonce2, int(ntime, 16), int(nonce, 16), version)
        except (ValueError, TypeError, IndexError):
            miner.rejected += 1
            return False, (20, "that share does not read")
        value = pow_hash(header, self.algo)
        if value > DIFF1[self.algo] / miner.difficulty:
            miner.rejected += 1
            return False, (23, "low difficulty share")
        miner.shares += 1
        miner.hashes += miner.difficulty * HASHES_PER_DIFF1[self.algo]
        miner.last = time.time()
        miner.window.append(miner.last)
        if value <= job.target:
            self._found(job, header, coinbase, miner)
        return True, (0, "")

    def _found(self, job: Job, header: bytes, coinbase: bytes, miner: Miner) -> None:
        block = job.block(header, coinbase)
        found = {"height": job.height, "hash": sha256d(header)[::-1].hex(), "worker": miner.worker,
                 "peer": miner.peer, "at": time.time(), "result": "sending"}
        self.blocks.append(found)

        async def send() -> None:
            try:
                said = await self._ask("submitblock", [block.hex()])
                found["result"] = said or "accepted"
                log.info("stratum: block %s at %s from %s: %s", found["hash"], job.height, miner.worker,
                         found["result"])
            except Exception as exc:                       # noqa: BLE001 -- said in status
                found["result"] = f"refused: {exc}"[:200]
            try:
                self._new_job(await self._template(), clean=True)
            except Exception:                              # noqa: BLE001 -- the poller asks again
                pass
        asyncio.ensure_future(send())

    def _vardiff(self, miner: Miner) -> None:
        now = time.time()
        miner.window = [t for t in miner.window if now - t <= RETARGET_SECONDS * 2]
        if now - getattr(miner, "_tuned", miner.since) < RETARGET_SECONDS or len(miner.window) < 2:
            return
        miner._tuned = now
        rate = len(miner.window) / (RETARGET_SECONDS * 2)
        wanted = miner.difficulty * rate * SHARE_SECONDS
        wanted = min(max(wanted, miner.difficulty / MOST_STEP), miner.difficulty * MOST_STEP)
        if abs(wanted - miner.difficulty) / miner.difficulty > 0.3:
            miner.difficulty = wanted
            miner.send({"id": None, "method": "mining.set_difficulty", "params": [wanted]})

    def status(self) -> dict:
        rows = [m.as_row() for m in list(self.miners)]
        return {"running": self.running, "net": self.params.name, "chain": self.params.label, "algo": self.algo,
                "address": self.address, "port": self.port, "anyone": self.anyone,
                "url": f"stratum+tcp://{_lan_address()}:{self.port}", "miners": rows,
                "hashrate": round(sum(r["hashrate"] for r in rows)), "height": self.job.height if self.job else None,
                "blocks": list(self.blocks[-10:]), "error": self.error,
                "minutes": round((time.time() - self.started) / 60, 1)}


def _lan_address() -> str:
    """This machine's address on its network, for the URL an ASIC is given (no packet is sent)."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return "this-machine"


# --------------------------------------------------------------------------- the arcade's one server

class Service:
    """The arcade's stratum server, as the operator set it in `stratum` (Admin -> Mining, `stratum` in the console)."""

    def __init__(self, state):
        self.state = state
        self.server: Server | None = None
        self.error = ""
        self._lock = threading.Lock()

    def conf(self) -> dict:
        got = self.state.setting(SETTING) or {}
        return got if isinstance(got, dict) else {}

    def configure(self, **changes) -> dict:
        """Save the operator's choice and start or stop the server to match it."""
        conf = {**self.conf(), **{k: v for k, v in changes.items() if v is not None}}
        self.state.set_setting(SETTING, conf)
        return self.apply()

    def apply(self) -> dict:
        with self._lock:
            conf = self.conf()
            if self.server is not None:
                self.server.stop()
                self.server = None
            self.error = ""
            if not conf.get("on"):
                return self.status()
            try:
                chain = next((c for c in self.state.all_chains if c.network == conf.get("net")), None)
                if chain is None:
                    raise StratumError(f"this arcade has no {conf.get('net')} chain")
                if chain.borrowed:
                    raise StratumError(f"{chain.label} is borrowed here: an ASIC mines a chain whose node runs here")
                algo = conf.get("algo") or (asic_algos(chain.params) or ("",))[0]
                address = str(conf.get("address") or "").strip()
                if not address:
                    raise StratumError("an address for the blocks to pay")

                def call(method: str, params: list) -> Any:
                    with chain.rpc() as rpc:
                        return rpc.call(method, *params)
                self.server = Server(call, chain.params, algo, address, port=int(conf.get("port") or PORT),
                                     difficulty=conf.get("difficulty"), anyone=bool(conf.get("anyone"))).start()
            except Exception as exc:                       # noqa: BLE001 -- said in status
                self.error = str(exc)[:300]
                self.server = None
                log.warning("stratum not started: %s", exc)
            return self.status()

    def stop(self) -> None:
        with self._lock:
            if self.server is not None:
                self.server.stop()
                self.server = None

    def status(self) -> dict:
        conf = self.conf()
        live = self.server.status() if self.server is not None else {"running": False}
        return {**live, "on": bool(conf.get("on")), "conf": {k: conf.get(k) for k in
                                                                ("net", "algo", "address", "port", "difficulty",
                                                                 "anyone")},
                **({"error": self.error} if self.error else {})}
