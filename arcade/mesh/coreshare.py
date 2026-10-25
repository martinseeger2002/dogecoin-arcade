"""Shared Cores: one arcade's Core answering for another arcade over the mesh.

2026-10-17: "use the mesh network as a core API allowing any operator to allow the rest of
The operators to use their core by clicking a check box in the admin panel ... both arcades could
offer all three but only each have two cores running." Every arcade runs Pepecoin testnet (the
Messenger and the mesh's own vouching live there) and at least one more Core of its choosing; any
chain it does not run it BORROWS from an arcade that shares that chain's Core.

On the wire (frames of kind "cr" on an ordinary mesh link, never flooded):

    offer  {nets: {net: {h, b}}}      the chains this arcade shares, with its tip (height, hash)
    sub    {nets: [...]}              a borrower asks for that chain's mempool as it arrives
    q      {id, net, seq, end, z} + a slice of the calls' JSON    a borrower's batch of calls; `z` 1: it reads a
                                                                  zlib-compressed answer (2026-10-22)
    a      {id, seq, end[, err][, z]} + a slice of the answers' JSON (zlib-compressed when `z` 1; an older sharer
                                      ignores `z` and answers raw, an older borrower never asks)
    tx     {net}  + raw tx bytes      a transaction that just entered the sharer's mempool
    gone   {net, ids}                 transactions that left it (mined, or dropped)

What a sharer answers is SERVED and nothing else: block, transaction, mempool and coin reads, and
broadcasting a transaction somebody already signed. Never a wallet call -- sharing a Core can never
spend, sign or reveal the sharer's own coins (a wallet's "is this address mine" is stripped from
validateaddress, which the borrower answers itself anyway). A sharer's Core is asked at most
CORE_AT_ONCE batches at a time, for all borrowers together, so its own pages never wait behind them
(dogecoind runs four RPC workers; 2026-10-17 a page of Doginals filled them all and the wallet waited).

What a borrower believes (BorrowedRpc): blocks and transactions are fetched RAW and read by
arcade/rawchain.py, which proves each is what was asked for and that the block carries its proof of
work. What cannot be proved from bytes -- which block is at a height, whether a coin is still
unspent, what is in the mempool -- is the sharer's word; with two sharers linked their tips are
compared, and a mismatch is said in the admin panel.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import threading
import time
import zlib
from collections import OrderedDict
from typing import Any, Callable

from .. import rawchain
from ..config import NETWORKS
from ..rpc import RpcClient, RpcError, RpcTransportError

log = logging.getLogger("arcade.mesh")

#: What a shared Core answers. Reads of the chain and the mempool, fee estimates, and broadcasting a
#: transaction that is already signed. Nothing that touches a wallet.
SERVED = frozenset({
    "getblockchaininfo", "getblockcount", "getbestblockhash", "getblockhash", "getblockheader", "getblock",
    "getrawtransaction", "getrawmempool", "getmempoolentry", "getmempoolinfo", "gettxout", "getdifficulty",
    "sendrawtransaction", "estimatefee", "estimatesmartfee", "verifymessage", "getnetworkinfo",
    # Not a Core's: the sharer's whole unspent set (KOTIA ONLY, arcade/utxoset.py `answer`), lent to an arcade that
    # borrows the chain instead of building its own through this Core (tbuuol 2026-10-26).
    "arcade_utxoset",
})
#: Work for a miner, and its answer (2026-10-18: mine testnet LTC with another arcade's Core). Answered
#: only for a chain of test coins (`may_mine`), and no more often than MINE_EVERY seconds per borrower and call:
#: a template is built from the sharer's mempool and a submitted block is checked whole, both real work.
MINED = frozenset({"getblocktemplate", "submitblock"})
MINE_EVERY = {"getblocktemplate": 2.0, "submitblock": 1.0}

#: Fields of getnetworkinfo a borrower may see (the relay fee); the rest describes the sharer's machine.
NETWORKINFO_FIELDS = ("version", "subversion", "protocolversion", "relayfee", "incrementalfee")

CHUNK = 48 * 1024            # bytes of an answer per frame (a frame is at most 64 KB)


def _inflate(data: bytes, most: int) -> bytes | None:
    """A compressed answer unpacked, or None: not zlib, or more than `most` (never inflated past it)."""
    d = zlib.decompressobj()
    try:
        out = d.decompress(data, most + 1)
    except zlib.error:
        return None
    if len(out) > most or d.unconsumed_tail or not d.eof:
        return None
    return out
RATE = 512 * 1024            # answer bytes per second to one borrower
AT_ONCE_PER_PEER = 4         # batches one borrower may have in flight
CORE_AT_ONCE = 2             # batches this arcade's Core works on for borrowers, all together
MAX_CALLS = 500              # calls in one batch
MAX_ANSWER = 64 * 1024 * 1024
MAX_ASKED = 8 * 1024 * 1024  # bytes of one batch of calls (a signed transaction can be 100 KB)
POOL_SECONDS = 2.0           # how often a sharer reads its mempool for new transactions
POOL_KEEP = 20_000           # transactions a borrower keeps per chain
REQUEST_SECONDS = 60.0
FAILING = 3                  # recent failures before a sharer goes behind the ones that answer, whatever its ping

def _test_coins(net: str) -> bool:
    from ..config import NETWORKS
    params = NETWORKS.get(net)
    return params is not None and not params.real_coins


#: The CoreShare of the running mesh, for ChainContext.rpc() (set by mesh.service.for_state).
CURRENT: "CoreShare | None" = None


class _Rate:
    def __init__(self, rate: float):
        self.rate, self.tokens, self.t = rate, rate, time.monotonic()

    async def take(self, n: int) -> None:
        while True:
            now = time.monotonic()
            self.tokens = min(self.rate, self.tokens + (now - self.t) * self.rate)
            self.t = now
            if self.tokens >= min(n, self.rate):
                self.tokens -= n
                return
            await asyncio.sleep((min(n, self.rate) - self.tokens) / self.rate)


def _sanitise(method: str, result: Any) -> Any:
    if method == "getnetworkinfo" and isinstance(result, dict):
        return {k: result[k] for k in NETWORKINFO_FIELDS if k in result}
    return result


class CoreShare:
    """Attached to a MeshNode (node.add_extension), on both sides of a shared Core.

    `serve(net, calls)` runs a batch on this machine's own Core for `net` (blocking: run in a thread)
    and returns one {"r": result} or {"e": [code, message]} per call. `shared()` names the chains this
    arcade shares right now, {net: {"h": height, "b": best hash}} -- read again every minute, so the
    admin panel's checkbox applies without a restart. `borrowing` is the chains this arcade borrows."""

    kinds = ("cr",)

    def __init__(self, serve: Callable[[str, list], list] | None, shared: Callable[[], dict],
                 borrowing: set[str] | frozenset[str] = frozenset(), rate: float = RATE,
                 pool_seconds: float = POOL_SECONDS, may_mine: Callable[[str], bool] | None = None):
        self.serve, self.shared, self.borrowing = serve, shared, set(borrowing)
        self.may_mine = may_mine or _test_coins
        self._mined_at: dict[tuple[str, str], float] = {}     # (peer, method) -> when it was last answered
        self.rate, self.pool_seconds = rate, pool_seconds
        self.node = None
        self.loop: asyncio.AbstractEventLoop | None = None
        # --- what others share with this arcade
        self.offers: dict[str, dict[str, dict]] = {}          # peer -> {net: {h, b}}
        self.pools: dict[str, OrderedDict[str, bytes]] = {}   # net -> txid -> raw (each proved)
        self._pool_from: dict[str, dict[str, set[str]]] = {}  # net -> txid -> sharers that pushed it
        self._waiting: dict[int, dict] = {}                   # request id -> {fut, parts, peer}
        self._ids = itertools.count(1)
        self.failures: dict[str, int] = {}                    # peer -> requests it failed lately
        # --- what this arcade shares
        self._offered: dict[str, dict] = {}
        self._subs: dict[str, set[str]] = {}                  # net -> peers that asked for its mempool
        self._busy: dict[str, int] = {}                       # peer -> batches in flight
        self._asked: dict[tuple[str, int], list] = {}         # (peer, id) -> slices of calls so far
        self._rates: dict[str, _Rate] = {}
        self._core: asyncio.Semaphore | None = None
        self._pool_task: asyncio.Task | None = None
        self.served: dict[str, int] = {}                      # peer -> batches answered (for the panel)

    # ---------------------------------------------------------------- the node's hooks
    async def link_up(self, link) -> None:
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
            self._core = asyncio.Semaphore(CORE_AT_ONCE)
        if self._pool_task is None and self.serve is not None:
            self._pool_task = self.node._spawn(self._pool_reader())
        self._offered = await asyncio.to_thread(self._shared_now)
        await link.send({"t": "cr", "op": "offer", "nets": self._offered})
        if self.borrowing:
            await link.send({"t": "cr", "op": "sub", "nets": sorted(self.borrowing)})

    async def link_down(self, peer_id: str) -> None:
        self.offers.pop(peer_id, None)
        self._busy.pop(peer_id, None)
        self._rates.pop(peer_id, None)
        for subs in self._subs.values():
            subs.discard(peer_id)
        for key in [k for k in self._asked if k[0] == peer_id]:
            self._asked.pop(key, None)
        for net, froms in self._pool_from.items():
            for txid in [t for t, peers in froms.items() if peer_id in peers]:
                self._forget(net, txid, peer_id)
        for rid, w in list(self._waiting.items()):
            if w["peer"] == peer_id and not w["fut"].done():
                w["fut"].set_exception(RpcTransportError("the sharing arcade went away"))

    async def rebalance(self) -> None:
        """Every minute: tell every link when what this arcade shares has changed."""
        if self.node is None:
            return
        now = await asyncio.to_thread(self._shared_now)
        if set(now) != set(self._offered) or any(now[n] != self._offered.get(n) for n in now):
            self._offered = now
            for link in list(self.node.links.values()):
                try:
                    await link.send({"t": "cr", "op": "offer", "nets": now})
                except Exception:                              # noqa: BLE001
                    pass

    def _shared_now(self) -> dict:
        try:
            got = self.shared() or {}
        except Exception:                                      # noqa: BLE001 -- shares nothing this minute
            return {}
        # "p": where this arcade is paid its tenth by those who use its Core (2026-10-18). "l": how many ord-index
        # lanes one borrower may read from it at once (2026-10-25): a big machine 4, a small one 1, so a small server's
        # disk is never asked for more than one block at a time per borrower.
        return {str(n): {"h": int(v.get("h", 0)), "b": str(v.get("b", "")), "l": lanes_lent(),
                         **({"p": str(v["p"])[:100]} if v.get("p") else {})}
                for n, v in got.items() if isinstance(v, dict)}

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        op = header.get("op")
        peer = link.peer_id
        if op == "offer":
            nets = header.get("nets")
            self.offers[peer] = {str(n): v for n, v in nets.items() if isinstance(v, dict)} \
                if isinstance(nets, dict) else {}
            wanted = sorted(self.borrowing & set(self.offers[peer]))
            if wanted:                                         # (again) when a sharer starts sharing
                await link.send({"t": "cr", "op": "sub", "nets": wanted})
        elif op == "sub":
            nets = header.get("nets") if isinstance(header.get("nets"), list) else []
            for net in nets:
                if isinstance(net, str) and net in self._offered:
                    self._subs.setdefault(net, set()).add(peer)
        elif op == "q":
            await self._on_request(link, header, blob)
        elif op == "a":
            self._on_answer(peer, header, blob)
        elif op == "tx":
            self._on_pushed(peer, header, blob)
        elif op == "gone":
            ids = header.get("ids") if isinstance(header.get("ids"), list) else []
            for txid in ids[:5000]:
                self._forget(str(header.get("net")), str(txid), peer)

    # ---------------------------------------------------------------- the sharer's side
    async def _on_request(self, link, header: dict, blob: bytes) -> None:
        rid, net, peer = header.get("id"), header.get("net"), link.peer_id
        if not isinstance(rid, int):
            return
        if self.serve is None or net not in self._offered:
            await link.send({"t": "cr", "op": "a", "id": rid, "seq": 0, "end": True,
                             "err": "this arcade does not share that chain"})
            return
        key = (peer, rid)
        parts = self._asked.setdefault(key, [])
        parts.append(blob)
        if sum(map(len, parts)) > MAX_ASKED:
            self._asked.pop(key, None)
            await link.send({"t": "cr", "op": "a", "id": rid, "seq": 0, "end": True, "err": "too large"})
            return
        if not header.get("end", True):
            return
        blob = b"".join(self._asked.pop(key))
        zipped = header.get("z") == 1                # the borrower reads a compressed answer (2026-10-22)
        if self._busy.get(peer, 0) >= AT_ONCE_PER_PEER:
            await link.send({"t": "cr", "op": "a", "id": rid, "seq": 0, "end": True, "err": "busy"})
            return
        try:
            calls = json.loads(blob.decode())
            if not isinstance(calls, list) or len(calls) > MAX_CALLS:
                raise ValueError
            calls = [(str(c[0]), list(c[1])) for c in calls]
        except Exception:                                      # noqa: BLE001
            await link.send({"t": "cr", "op": "a", "id": rid, "seq": 0, "end": True, "err": "not a batch"})
            return
        self._busy[peer] = self._busy.get(peer, 0) + 1
        self.node._spawn(self._answer(link, rid, net, calls, zipped))

    async def _answer(self, link, rid: int, net: str, calls: list, zipped: bool = False) -> None:
        peer = link.peer_id
        try:
            now = time.monotonic()
            refused = {}
            for i, (m, _) in enumerate(calls):
                if m in MINED:
                    if not self.may_mine(net):
                        refused[i] = f"{m} is answered for test coins only"
                    elif now - self._mined_at.get((peer, m), -1e9) < MINE_EVERY[m]:
                        refused[i] = f"{m} asked too often; once every {MINE_EVERY[m]:g} s"
                    else:
                        self._mined_at[(peer, m)] = now
                elif m not in SERVED:
                    refused[i] = f"a shared Core does not answer {m}"
            asked = [c for i, c in enumerate(calls) if i not in refused]
            async with self._core:
                got = await asyncio.to_thread(self.serve, net, asked) if asked else []
            answers, it = [], iter(got)
            for i, (method, _) in enumerate(calls):
                if i in refused:
                    answers.append({"e": [-32601, refused[i]]})
                else:
                    one = next(it)
                    if "r" in one:
                        one = {"r": _sanitise(method, one["r"])}
                    answers.append(one)
            data = json.dumps(answers, separators=(",", ":")).encode()
            # Raw blocks and transactions ride in it as hex, twice their size: compressed, the answer is about half
            # (2026-10-22: compress what arcades send each other).
            if zipped:
                data = await asyncio.to_thread(zlib.compress, data, 6)
            rate = self._rates.setdefault(peer, _Rate(self.rate))
            parts = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]
            for seq, part in enumerate(parts):
                await rate.take(len(part))
                await link.send({"t": "cr", "op": "a", "id": rid, "seq": seq, "end": seq == len(parts) - 1,
                                 **({"z": 1} if zipped else {})}, part)
            self.served[peer] = self.served.get(peer, 0) + 1
        except Exception as exc:                               # noqa: BLE001 -- the borrower tries elsewhere
            try:
                await link.send({"t": "cr", "op": "a", "id": rid, "seq": 0, "end": True, "err": str(exc)[:200]})
            except Exception:                                  # noqa: BLE001
                pass
        finally:
            self._busy[peer] = max(0, self._busy.get(peer, 1) - 1)

    async def _pool_reader(self) -> None:
        """Each shared chain's mempool, read every POOL_SECONDS: what is new goes, raw, to every borrower
        that asked for it (2026-10-17: "they should also broadcast mempool transactions as they hit")."""
        seen: dict[str, set[str]] = {}
        while True:
            await asyncio.sleep(self.pool_seconds)
            for net in list(self._offered):
                subs = [p for p in self._subs.get(net, ()) if p in self.node.links]
                if not subs:
                    seen.pop(net, None)
                    continue
                try:
                    async with self._core:
                        ids = await asyncio.to_thread(self.serve, net, [("getrawmempool", [])])
                    pool = set(ids[0].get("r") or [])
                    known = seen.get(net)
                    fresh = sorted(pool - known) if known is not None else sorted(pool)
                    left = sorted(known - pool) if known is not None else []
                    seen[net] = pool
                    for at in range(0, len(left), 1000):
                        for peer in subs:
                            link = self.node.links.get(peer)
                            if link is not None:
                                await link.send({"t": "cr", "op": "gone", "net": net, "ids": left[at:at + 1000]})
                    for at in range(0, len(fresh), 100):
                        batch = fresh[at:at + 100]
                        async with self._core:
                            raws = await asyncio.to_thread(self.serve, net, [("getrawtransaction", [t, 0])
                                                                             for t in batch])
                        for raw in raws:
                            data = bytes.fromhex(raw["r"]) if isinstance(raw.get("r"), str) else b""
                            if not data or len(data) > CHUNK:
                                continue                       # gone already, or too big: asked for when needed
                            for peer in subs:
                                link = self.node.links.get(peer)
                                if link is not None:
                                    await self._rates.setdefault(peer, _Rate(self.rate)).take(len(data))
                                    await link.send({"t": "cr", "op": "tx", "net": net}, data)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:                       # noqa: BLE001 -- the next pass tries again
                    log.debug("coreshare pool %s: %s", net, exc)

    # ---------------------------------------------------------------- the borrower's side
    def _on_pushed(self, peer: str, header: dict, blob: bytes) -> None:
        net = header.get("net")
        if net not in self.borrowing or net not in self.offers.get(peer, {}):
            return
        try:
            tx = rawchain.read_tx(blob, timed=getattr(NETWORKS.get(net), "tx_time", False))
        except Exception:                                      # noqa: BLE001 -- not a transaction: dropped
            return
        pool = self.pools.setdefault(net, OrderedDict())
        pool[tx.txid] = blob
        self._pool_from.setdefault(net, {}).setdefault(tx.txid, set()).add(peer)
        while len(pool) > POOL_KEEP:
            old, _ = pool.popitem(last=False)
            self._pool_from.get(net, {}).pop(old, None)

    def _forget(self, net: str, txid: str, peer: str) -> None:
        """`peer` no longer has `txid` in its mempool: forgotten once no sharer that pushed it still does."""
        froms = self._pool_from.get(net, {})
        peers = froms.get(txid)
        if peers is None:
            return
        peers.discard(peer)
        if not peers:
            froms.pop(txid, None)
            self.pools.get(net, {}).pop(txid, None)

    def _on_answer(self, peer: str, header: dict, blob: bytes) -> None:
        w = self._waiting.get(header.get("id"))
        if w is None or w["peer"] != peer or w["fut"].done():
            return
        if header.get("err"):
            w["fut"].set_exception(RpcTransportError(f"the sharing arcade said: {header['err']}"))
            return
        w["parts"].append(blob)
        w["size"] += len(blob)
        w["z"] = w.get("z") or header.get("z") == 1
        if w["size"] > MAX_ANSWER:
            w["fut"].set_exception(RpcTransportError("the answer is too large"))
        elif header.get("end"):
            data = b"".join(w["parts"])
            if w.get("z"):
                data = _inflate(data, MAX_ANSWER)
                if data is None:
                    w["fut"].set_exception(RpcTransportError("a compressed answer that does not unpack (or too large)"))
                    return
            try:
                w["fut"].set_result(json.loads(data.decode()))
            except Exception as exc:                           # noqa: BLE001
                w["fut"].set_exception(RpcTransportError(f"an answer that is not JSON: {exc}"))

    def sharers(self, net: str) -> list[str]:
        """Linked arcades sharing `net`: the fastest ping first among those answering (2026-10-19: "whichever
        node has the fastest ping should be the one that that arcade uses"), then any failing lately. One failure
        no longer sends a fast sharer behind a slow one for good -- it was never asked again, so never recovered."""
        if self.node is None:
            return []
        rtts = self.node.rtts()
        peers = [p for p, nets in self.offers.items() if net in nets and p in self.node.links]
        # Among the ones failing lately, the fewest failures first, then ping (2026-10-25: a sharer caught lying, +10,
        # could stay ahead of one that merely went quiet five times, by ping alone -- the coreshare liar test flaked).
        def order(p: str) -> tuple:
            fails = self.failures.get(p, 0)
            failing = fails >= FAILING
            return (failing, fails if failing else 0, rtts.get(p) if rtts.get(p) is not None else 9e9, fails)
        return sorted(peers, key=order)

    async def request(self, net: str, calls: list, timeout: float = REQUEST_SECONDS,
                      avoid: frozenset = frozenset()) -> tuple[str, list]:
        """One batch, asked of the best sharer (not one in `avoid`); another one if it fails or goes
        quiet. Returns who answered, and the answers."""
        tried = []
        for peer in [p for p in self.sharers(net) if p not in avoid]:
            link = self.node.links.get(peer)
            if link is None:
                continue
            rid = next(self._ids)
            fut = asyncio.get_running_loop().create_future()
            self._waiting[rid] = {"fut": fut, "parts": [], "size": 0, "peer": peer}
            try:
                data = json.dumps(calls, separators=(",", ":")).encode()
                parts = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]
                for seq, part in enumerate(parts):
                    await link.send({"t": "cr", "op": "q", "id": rid, "net": net, "seq": seq,
                                     "end": seq == len(parts) - 1, "z": 1}, part)
                got = await asyncio.wait_for(fut, timeout)
                if not isinstance(got, list) or len(got) != len(calls):
                    raise RpcTransportError("an answer of the wrong length")
                self.failures[peer] = max(0, self.failures.get(peer, 0) - 1)
                return peer, got
            except (asyncio.TimeoutError, RpcTransportError, ConnectionError, OSError) as exc:
                self.failures[peer] = self.failures.get(peer, 0) + 1
                tried.append(f"{peer[:12]}: {exc or 'no answer'}")
            finally:
                self._waiting.pop(rid, None)
        raise RpcTransportError(f"no arcade sharing {net} answered" + (f" ({'; '.join(tried)})" if tried else
                                " -- none is linked to this one yet"))

    def call(self, net: str, calls: list, timeout: float = REQUEST_SECONDS) -> list:
        """From any thread: one batch through the mesh's own loop."""
        return self.call_from(net, calls, timeout)[1]

    def call_from(self, net: str, calls: list, timeout: float = REQUEST_SECONDS,
                  avoid: frozenset = frozenset()) -> tuple[str, list]:
        """call(), saying which sharer answered, and never asking one in `avoid`."""
        if self.loop is None or self.node is None:
            raise RpcTransportError(f"no arcade sharing {net} is linked to this one yet")
        fut = asyncio.run_coroutine_threadsafe(self.request(net, calls, timeout, frozenset(avoid)), self.loop)
        try:
            return fut.result(timeout * 3 + 5)
        except TimeoutError as exc:
            fut.cancel()
            raise RpcTransportError(f"no arcade sharing {net} answered in time") from exc

    def payee(self, net: str, valid: Callable[[str], bool] = lambda a: True) -> str | None:
        """The address the arcade whose Core this one uses for `net` is paid at: the sharer asked first, or the
        next one that names an address `valid` accepts. None when none names one."""
        for peer in self.sharers(net):
            address = str(self.offers.get(peer, {}).get(net, {}).get("p") or "")
            if address and valid(address):
                return address
        return None

    def tips(self, net: str) -> dict[str, dict]:
        """What each linked sharer of `net` says its tip is."""
        return {p: dict(nets[net]) for p, nets in self.offers.items()
                if net in nets and self.node is not None and p in self.node.links}

    def status(self) -> dict:
        nets = sorted({n for nets in self.offers.values() for n in nets} | self.borrowing | set(self._offered))
        out = {}
        for net in nets:
            tips = self.tips(net)
            heights: dict[int, set[str]] = {}
            for t in tips.values():
                heights.setdefault(t["h"], set()).add(t["b"])
            out[net] = {"sharing": net in self._offered, "borrowing": net in self.borrowing,
                        "sharers": len(tips), "pool": len(self.pools.get(net, ())),
                        "borrowers": len([p for p in self._subs.get(net, ()) if self.node and p in self.node.links]),
                        # two sharers naming different blocks at the same height disagree about the chain
                        "disagree": any(len(v) > 1 for v in heights.values())}
        return out


# ------------------------------------------------------------------------------------------------ the client

WALLET_HINT = "this arcade borrows {label} from another arcade's Core, and a borrowed chain has no node wallet"


class OneSharer:
    """A CoreShare seen through one sharer only: a BorrowedRpc on it reads from that arcade and no other (an ord index
    gives each lending arcade a lane of its own; 2026-10-24: "borrow as many nodes as are available and run them
    each on their own lane"). Should that sharer fail, the call fails: the lane waits, the others go on."""

    def __init__(self, share: "CoreShare", peer: str):
        self._share, self.peer = share, peer

    def sharers(self, net: str) -> list[str]:
        return [self.peer] if self.peer in self._share.sharers(net) else []

    def call_from(self, net: str, calls: list, timeout: float = REQUEST_SECONDS,
                  avoid: frozenset = frozenset()) -> tuple[str, list]:
        if self.peer in avoid or self.peer not in self._share.sharers(net):
            raise RpcTransportError(f"arcade {self.peer[:12]} does not share {net} now")
        others = frozenset(p for p in self._share.sharers(net) if p != self.peer)
        return self._share.call_from(net, calls, timeout, others | frozenset(avoid))

    def __getattr__(self, name: str):
        return getattr(self._share, name)


#: Transactions already fanned out, so a copy one arcade sends another (which fans out in turn) goes round only once.
_FANNED: "OrderedDict[str, float]" = OrderedDict()
_FAN_LOCK = threading.Lock()
FAN_SECONDS = 20.0


def fan_out(net: str, raw: str) -> list[str]:
    """A signed transaction a node took, also sent to every linked arcade sharing `net`, each on its own thread (the operator
    2026-10-25): in several mempools at once it reaches the miners sooner, and one slow or badly connected node no
    longer leaves it unseen. Sending it twice is harmless -- a transaction cannot be spent twice; a node that has it
    already says so. Each transaction once per arcade (a sharer that takes a copy fans it out once too). Returns the
    arcades it went to."""
    share = CURRENT
    if share is None or not raw:
        return []
    key = hashlib.sha256(raw.encode()).hexdigest()
    now = time.time()
    with _FAN_LOCK:
        if key in _FANNED:
            return []
        _FANNED[key] = now
        while len(_FANNED) > 5000:
            _FANNED.popitem(last=False)
    peers = list(share.sharers(net))

    def one(peer: str) -> None:
        others = frozenset(p for p in peers if p != peer)
        try:
            share.call_from(net, [("sendrawtransaction", [raw])], FAN_SECONDS, others)
        except Exception as exc:                          # noqa: BLE001 -- a copy: whatever it says
            log.debug("coreshare: copy of a broadcast to %s: %s", peer[:12], exc)
    for peer in peers:
        threading.Thread(target=one, args=(peer,), name="fan-out", daemon=True).start()
    return peers


#: Lanes a sharer lends each borrower's ord index at once: a big machine (8+ cores) this many, a small one 1.
LANES_BIG = 4


def lanes_lent() -> int:
    import os
    return LANES_BIG if (os.cpu_count() or 1) >= 8 else 1


def lenders(net: str) -> list[tuple[str, int]]:
    """The linked arcades sharing `net` now, fastest first, each with the lanes it lends (2026-10-25: several
    lanes on each lender; an arcade that does not say lends one)."""
    if CURRENT is None:
        return []
    out = []
    for peer in CURRENT.sharers(net):
        try:
            lanes = int((CURRENT.offers.get(peer, {}).get(net) or {}).get("l") or 1)
        except (TypeError, ValueError):
            lanes = 1
        out.append((peer, max(1, min(lanes, 16))))
    return out


def lent_rpc(peer: str, params) -> "BorrowedRpc":
    """A client reading `params`' chain from `peer` only."""
    if CURRENT is None:
        raise RpcTransportError("the mesh is not running")
    return BorrowedRpc(OneSharer(CURRENT, peer), params)


class BorrowedRpc(RpcClient):
    """An RpcClient whose node is another arcade's, reached through the mesh (CoreShare).

    The arcade reads a borrowed chain exactly as it reads its own node: same calls, same answers. What
    comes back is checked where it can be (arcade/rawchain.py): getblock and getrawtransaction are
    fetched raw, proved, and decoded here; decoderawtransaction and validateaddress are answered here
    without asking anybody; a transaction pushed from the sharer's mempool is answered from memory. A
    wallet call fails the way it fails on a node with no wallet."""

    #: Verified blocks remembered, by hash: (height or None, bits), for the difficulty check.
    _KNOWN: dict[str, OrderedDict] = {}
    _KNOWN_LOCK = threading.Lock()

    def __init__(self, share: CoreShare | None, params, timeout: float = REQUEST_SECONDS):
        self._share = share
        self._params = params
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._creds = None

    def close(self) -> None:
        pass

    @property
    def net(self) -> str:
        return self._params.name

    @property
    def fan_net(self) -> str:
        return self._params.name

    def _post(self, payload: Any) -> Any:
        single = isinstance(payload, dict)
        items = [payload] if single else list(payload)
        answers = self._answer(items)
        return answers[0] if single else answers

    # ------------------------------------------------------------------ one batch
    def _answer(self, items: list[dict]) -> list[dict]:
        if self._share is None:
            raise RpcTransportError(f"this arcade borrows {self._params.label} through the mesh, "
                                    "and the mesh is not running")
        out: list[dict | None] = [None] * len(items)
        remote: list[tuple] = []            # (method, params)
        finish: list[tuple] = []            # (index, how, first remote slot, count, extra)
        for i, item in enumerate(items):
            method, params = str(item.get("method")), list(item.get("params") or [])
            rid = item.get("id")
            try:
                local = self._local(method, params)
            except RpcError as exc:
                out[i] = {"id": rid, "result": None, "error": {"code": exc.code, "message": exc.message}}
                continue
            if local is not _ASK:
                out[i] = {"id": rid, "result": local, "error": None}
                continue
            plan = self._plan(method, params)
            finish.append((i, rid, method, params, len(remote), len(plan)))
            remote.extend(plan)
        avoid: set[str] = set()
        while remote:
            # The whole batch from one sharer; one whose answer fails a check is marked and the batch
            # asked again of the next, until no sharer is left (then the check's error is the answer).
            peer, got = "", []
            at = 0
            while at < len(remote):                  # slices of at most MAX_CALLS, never splitting one call's plan
                end = at
                for f in finish:
                    if f[4] >= at and f[4] + f[5] - at <= MAX_CALLS:
                        end = max(end, f[4] + f[5])
                end = max(end, at + 1)
                if not got:
                    peer, part = self._share.call_from(self.net, remote[at:end], self._timeout, frozenset(avoid))
                else:
                    part = self._share.call_from(self.net, remote[at:end], self._timeout,
                                                 frozenset(set(self._share.sharers(self.net)) - {peer}))[1]
                got += part
                at = end
            caught = False
            for i, rid, method, params, at, n in finish:
                parts = got[at:at + n]
                bad = next((p["e"] for p in parts if "e" in p), None)
                if bad is not None:
                    code, message = (bad + [0, ""])[:2] if isinstance(bad, list) else (-1, str(bad))
                    out[i] = {"id": rid, "result": None, "error": {"code": code, "message": message}}
                    continue
                try:
                    result = self._check(method, params, [p.get("r") for p in parts])
                except rawchain.Unreadable as exc:            # honest as far as anyone can tell: not a failure
                    out[i] = {"id": rid, "result": None, "error": {"code": -5, "message": str(exc)}}
                    continue
                except rawchain.RawError as exc:
                    log.warning("coreshare: %s's answer to %s failed its check: %s", peer[:12], method, exc)
                    caught = True
                    out[i] = {"id": rid, "result": None,
                              "error": {"code": -1, "message": f"the sharing arcade's answer did not check out: {exc}"}}
                    continue
                out[i] = {"id": rid, "result": result, "error": None}
            if not caught:
                break
            self._share.failures[peer] = self._share.failures.get(peer, 0) + 10
            avoid.add(peer)
            if not [p for p in self._share.sharers(self.net) if p not in avoid]:
                break
        return out  # type: ignore[return-value]

    def _local(self, method: str, params: list) -> Any:
        """An answer that needs nobody, or _ASK."""
        if method not in SERVED and method not in MINED and method not in (
                "decoderawtransaction", "validateaddress", "getconnectioncount"):
            raise RpcError(-32601, "Method not found (" + WALLET_HINT.format(label=self._params.label) + ")")
        if method == "getconnectioncount":
            return len(self._share.sharers(self.net))
        if method == "decoderawtransaction":
            try:
                return rawchain.read_tx(bytes.fromhex(str(params[0])), timed=self._params.tx_time).verbose(
                    self._params, with_hex=False)
            except Exception as exc:                           # noqa: BLE001
                raise RpcError(-22, "TX decode failed") from exc
        if method == "validateaddress":
            return self._validate(str(params[0]) if params else "")
        if method == "getrawtransaction" and params:
            raw = self._share.pools.get(self.net, {}).get(str(params[0]))
            if raw is not None:
                tx = rawchain.read_tx(raw, timed=self._params.tx_time)
                return tx.verbose(self._params) if (params[1:] and params[1]) else raw.hex()
        return _ASK

    def _plan(self, method: str, params: list) -> list[tuple]:
        """The calls to send for one asked: raw where raw can be proved."""
        if method == "getblock":
            verbosity = params[1] if len(params) > 1 else 1
            verbosity = (1 if verbosity else 0) if isinstance(verbosity, bool) else int(verbosity)
            calls = [("getblock", [params[0], 0])]
            if verbosity:
                calls.append(("getblockheader", [params[0], True]))
            return calls
        if method == "getblockheader":
            verbose = params[1] if len(params) > 1 else True
            return [("getblockheader", [params[0], False])] + ([("getblockheader", [params[0], True])] if verbose else [])
        if method == "getrawtransaction":
            verbose = bool(params[1]) if len(params) > 1 else False
            return [("getrawtransaction", [params[0], 1 if verbose else 0])]
        return [(method, params)]

    def _check(self, method: str, params: list, got: list) -> Any:
        """The answer as the arcade expects it, proved where the bytes allow."""
        p = self._params
        if method == "getblock":
            block_hash = str(params[0])
            block = rawchain.read_block(bytes.fromhex(str(got[0])), block_hash, mweb=p.mweb, timed=p.tx_time,
                                        signed=p.block_sig, scrypt_below=p.scrypt_headers_below)
            self._work(block)
            if len(got) == 1:
                self._remember(block, None)
                return str(got[0])
            meta = got[1] if isinstance(got[1], dict) else {}
            verbosity = params[1] if len(params) > 1 else 1
            verbosity = (1 if verbosity else 0) if isinstance(verbosity, bool) else int(verbosity)
            self._remember(block, meta.get("height"))
            return block.verbose(p, height=meta.get("height"), verbosity=verbosity, extra=meta)
        if method == "getblockheader":
            header = rawchain.read_header(bytes.fromhex(str(got[0])), str(params[0]))
            self._work(header)
            if len(got) == 1:
                return str(got[0])
            meta = dict(got[1]) if isinstance(got[1], dict) else {}
            self._remember(header, meta.get("height"))
            meta.update(hash=header.hash, version=header.version, merkleroot=header.merkleroot, time=header.time,
                        nonce=header.nonce, bits=f"{header.bits:08x}")
            if header.prev != "00" * 32:
                meta["previousblockhash"] = header.prev
            return meta
        if method == "getrawtransaction":
            txid = str(params[0])
            if isinstance(got[0], dict):
                tx = rawchain.read_tx(bytes.fromhex(str(got[0].get("hex", ""))), txid, timed=p.tx_time)
                said = tx.verbose(p)
                for key in ("blockhash", "confirmations", "time", "blocktime"):
                    if key in got[0]:
                        said[key] = got[0][key]
                return said
            rawchain.read_tx(bytes.fromhex(str(got[0])), txid, timed=p.tx_time)
            return str(got[0])
        return got[0]

    def _work(self, block: rawchain.Block) -> None:
        rawchain.check_work(block, self._params.auxpow_chain_id, self._params.pow_algo,
                               self._params.auxpow_chain_mask)
        with self._KNOWN_LOCK:
            known = self._KNOWN.setdefault(self.net, OrderedDict())
            before = known.get(block.prev)
        if before is not None and rawchain.too_easy(block.bits, before[1], self._params):
            raise rawchain.RawError(f"block {block.hash}: its difficulty fell more than "
                                    f"{rawchain.MAX_EASIER}x in one block")

    def _remember(self, block: rawchain.Block, height: Any) -> None:
        with self._KNOWN_LOCK:
            known = self._KNOWN.setdefault(self.net, OrderedDict())
            known[block.hash] = (height, block.bits)
            while len(known) > 5000:
                known.popitem(last=False)

    def _validate(self, address: str) -> dict:
        from ..script import b58check_decode
        from ..txbuild import p2pkh_script
        p = self._params
        try:
            version, payload = b58check_decode(address)
        except Exception:                                      # noqa: BLE001
            return {"isvalid": False}
        if len(payload) != 20 or version not in (p.pubkeyhash_version, p.scripthash_version):
            return {"isvalid": False}
        script = (p2pkh_script(address).hex() if version == p.pubkeyhash_version
                  else "a914" + payload.hex() + "87")
        return {"isvalid": True, "address": address, "scriptPubKey": script, "ismine": False,
                "iswatchonly": False, "isscript": version == p.scripthash_version}


_ASK = object()
