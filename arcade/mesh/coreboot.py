"""A chain's Core data copied between linked arcades, automatically (arcade/core_copy.py says what is copied and
checked; 2026-11-04: "The bootstrap should work automatically from the coins and chains tab just like the
installer does now", from "Any linked arcade").

On the wire (frames of kind "cb" on a DIRECT link only -- 50 GB never rides through a third arcade):

    hi  {nets: {net: {h, ver, gen, can, sn}}}   this arcade's installed, shared Core of each chain: its height, Core
                                   version, genesis, whether it can make a copy (synced), and the copy it holds
                                   (`sn`: {id, h, hash, bytes, n}, or null)
    sq  {net}                      this arcade is far behind: make a copy
    g   {id, net, snap, what[, f, i]}   copy `snap`'s manifest (what "manifest", zlib) or piece i of file f ("piece")
    gb  {id, seq, end[, err][, z]} + a slice   the answer, in parts under a frame's size

Said when a link comes up and once a minute. The work -- RPC to the Core, making a copy, writing pieces, installing --
runs on threads of its own; serving runs off the mesh loop, SERVING_AT_ONCE pieces at a time and PIECES_PER_MINUTE per
asker, so links, rooms and calls never wait behind it.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import shutil
import threading
import time
import zlib
from typing import Any, Callable

from .. import core_copy as cc

log = logging.getLogger("arcade.mesh")

PART = 60 * 1024
SERVING_AT_ONCE = 2
PIECES_PER_MINUTE = 60                  # 8 MB each: up to 8 MB/s to one asker
MANIFEST_MAX = 64 * 1024 * 1024
WAIT_SECONDS = 300
HEARD_FRESH = 600
ASKED_FRESH = 15 * 60
ASK_EVERY = 10 * 60
TICK = 1.0
INFO_EVERY = 60.0
#: Disk kept free besides the copy.
SPARE_BYTES = 2 * 10 ** 9


def _inflate(data: bytes, limit: int) -> bytes | None:
    d = zlib.decompressobj()
    try:
        out = d.decompress(data, limit + 1)
    except zlib.error:
        return None
    if len(out) > limit or d.unconsumed_tail or not d.eof:
        return None
    return out


class CoreBoot:
    kinds = ("cb",)

    def __init__(self, state, core_of: Callable[[str], Any] | None = None, networks: dict | None = None):
        from ..config import NETWORKS
        self.state = state
        self.networks = networks if networks is not None else NETWORKS
        self.core_of = core_of or (lambda net: cc.Core(self._installed(net), self.networks[net]))
        self.node = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._ids = itertools.count(1)
        self._wait: dict[int, dict] = {}
        self._served: dict[str, list[float]] = {}
        self._serving: asyncio.Semaphore | None = None
        self.heard: dict[str, dict] = {}                 # peer -> {at, nets}
        self.asked: dict[str, dict[str, float]] = {}     # net -> {peer: when it asked for a copy}
        self.info: dict[str, dict] = {}                  # net -> this Core's {blocks, headers, synced, ver, gen}
        self.jobs: dict[str, dict] = {}                  # net -> what the Coins & chains tab shows
        self._snaps: dict[str, cc.Snapshots] = {}
        self._fetch: dict[str, cc.Fetch] = {}
        self._flight: dict[str, dict[int, float]] = {}   # net -> {piece number: asked at}
        self._asked_at: dict[str, float] = {}
        self._busy: set[str] = set()                     # nets with a thread working (making, installing)
        self._info_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- which chains
    def _settings(self) -> dict:
        try:
            return self.state.settings() or {}
        except Exception:                                  # noqa: BLE001
            return {}

    def _installed(self, net: str) -> dict:
        return dict(self._settings().get(f"core_installed:{net}") or {})

    def nets(self) -> list[str]:
        """Chains whose Core this arcade installed and runs here."""
        from ..cores import HERE, mode
        s = self._settings()
        return [k.split(":", 1)[1] for k, v in s.items()
                if k.startswith("core_installed:") and isinstance(v, dict) and k.split(":", 1)[1] in self.networks
                and mode(s, k.split(":", 1)[1]) == HERE]

    def _sharing(self, net: str) -> bool:
        from ..cores import sharing
        return sharing(self._settings(), net)

    def snaps(self, net: str) -> cc.Snapshots:
        s = self._snaps.get(net)
        if s is None:
            s = self._snaps[net] = cc.Snapshots(net, self.networks[net], self._installed(net), self.core_of(net))
        return s

    def fetch(self, net: str) -> cc.Fetch:
        f = self._fetch.get(net)
        if f is None:
            f = self._fetch[net] = cc.Fetch(cc.chain_dir(self._installed(net), self.networks[net]))
        return f

    def _refused(self, net: str) -> list:
        return list(self._settings().get(f"core_copy_refused:{net}") or [])

    # ---------------------------------------------------------------- the thread
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="core-copy", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(TICK):
            try:
                self.step()
            except Exception:                              # noqa: BLE001 -- tried again next tick
                log.warning("core copy", exc_info=True)

    def step(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        nets = self.nets()
        if not nets:
            return
        if now - self._info_at >= INFO_EVERY:
            self._info_at = now
            for net in nets:
                self._read_info(net)
        for net in nets:
            if net in self._busy:
                continue
            self._make_if_asked(net, now)
            self._receive(net, now)

    def _read_info(self, net: str) -> None:
        core = self.core_of(net)
        try:
            bc = core.call("getblockchaininfo", timeout=30)
            got = {"blocks": int(bc.get("blocks", 0)), "headers": int(bc.get("headers", 0)),
                   "synced": int(bc.get("blocks", 0)) >= int(bc.get("headers", 0)) - 2
                   and float(bc.get("verificationprogress", 0)) >= 0.999 and not bc.get("pruned")}
            old = self.info.get(net) or {}
            if old.get("gen") and old.get("ver"):
                got["gen"], got["ver"] = old["gen"], old["ver"]
            else:
                got["gen"] = core.call("getblockhash", 0, timeout=30)
                got["ver"] = int(core.call("getnetworkinfo", timeout=30).get("version", 0))
            self.info[net] = got
        except Exception:                                  # noqa: BLE001 -- the node is down or starting
            self.info.pop(net, None)

    def _spawn(self, net: str, fn, *args) -> None:
        self._busy.add(net)

        def run():
            try:
                fn(*args)
            except Exception:                              # noqa: BLE001
                log.warning("core copy %s", net, exc_info=True)
            finally:
                self._busy.discard(net)
                self._info_at = 0.0
        threading.Thread(target=run, name=f"core-copy-{net}", daemon=True).start()

    # ---------------------------------------------------------------- making
    def _make_if_asked(self, net: str, now: float) -> None:
        snaps = self.snaps(net)
        snaps.tidy()
        asks = {p: t for p, t in (self.asked.get(net) or {}).items() if now - t < ASKED_FRESH}
        self.asked[net] = asks
        info = self.info.get(net) or {}
        if not asks or not self._sharing(net) or not info.get("synced") or snaps.fresh() or snaps.making:
            return
        if not cc.conf_txindex(self._installed(net)):
            return
        folder = snaps.folder.parent
        if shutil.disk_usage(folder).free < SPARE_BYTES:
            return
        self.asked[net] = {}

        def make():
            with cc.held(snaps.root.with_name(snaps.root.name + ".lock")) as mine:
                if mine:
                    snaps.make()
            self._say_all()
        self._spawn(net, make)

    def _say_all(self) -> None:
        loop = self.loop
        if loop is not None:
            asyncio.run_coroutine_threadsafe(self.rebalance(), loop)

    # ---------------------------------------------------------------- fetching
    def _job(self, net: str, **kw) -> None:
        self.jobs[net] = {**self.jobs.get(net, {}), **kw}

    def _offers(self, net: str, now: float) -> list[tuple[str, dict]]:
        mine = self.info.get(net) or {}
        refused = self._refused(net)
        out = []
        for peer, h in self.heard.items():
            said = (h.get("nets") or {}).get(net)
            if now - h.get("at", 0) > HEARD_FRESH or not isinstance(said, dict) or peer in refused:
                continue
            if said.get("gen") != mine.get("gen") or said.get("ver") != mine.get("ver"):
                continue
            if self._link(peer) is None:
                continue
            out.append((peer, said))
        return out

    def _receive(self, net: str, now: float) -> None:
        f = self.fetch(net)
        info = self.info.get(net)
        if f.active:
            if f.state.get("phase") in ("swapping", "checking") or not f.missing():
                self._spawn(net, self._install, net)
                return
            self._pull(net, f, now)
            return
        if not info or self._settings().get("core_copy_auto", True) is False:
            return
        if info["headers"] - info["blocks"] < cc.WORTH_BLOCKS and info["headers"] > 0:
            self.jobs.pop(net, None) if (self.jobs.get(net) or {}).get("state") == "waiting" else None
            return
        offers = self._offers(net, now)
        ready = sorted(((p, s) for p, s in offers if isinstance(s.get("sn"), dict)
                        and int(s["sn"].get("h", 0)) - info["blocks"] >= cc.WORTH_BLOCKS),
                       key=lambda x: -int(x[1]["sn"]["h"]))
        if ready:
            peer, said = ready[0]
            self._spawn(net, self._choose, net, peer, said["sn"])
            return
        makers = sorted(((p, s) for p, s in offers if s.get("can") and int(s.get("h", 0)) - info["blocks"]
                         >= cc.WORTH_BLOCKS), key=lambda x: -int(x[1].get("h", 0)))
        if makers:
            if now - self._asked_at.get(net, -1e9) >= ASK_EVERY:
                self._asked_at[net] = now
                self._post(makers[0][0], {"op": "sq", "net": net})
            self._job(net, state="waiting", step=f"asked {self._who(makers[0][0])} for a copy of its chain data")
        elif (self.jobs.get(net) or {}).get("state") in (None, "waiting"):
            self._job(net, state="waiting", step="no linked arcade has a copy of this chain's data to give: "
                                                 "the node syncs on its own")

    def _choose(self, net: str, peer: str, sn: dict) -> None:
        """Check an offer against this node's own headers and disk, then ask for its manifest."""
        core = self.core_of(net)
        why = cc.header_check(core, {"genesis": (self.info.get(net) or {}).get("gen"),
                                     "version": (self.info.get(net) or {}).get("ver"),
                                     "height": sn.get("h"), "hash": sn.get("hash")})
        if why:
            self._job(net, state="waiting", step=f"the copy on {self._who(peer)}: {why}")
            if "waiting" not in why:
                self._refuse(net, peer)
            return
        folder = cc.chain_dir(self._installed(net), self.networks[net]).parent
        need = int(sn.get("bytes", 0)) + SPARE_BYTES
        if shutil.disk_usage(folder).free < need:
            self._job(net, state="waiting", step=f"a copy takes {need / 1e9:.0f} GB; this disk has less free")
            return
        self._job(net, state="working", step=f"asking {self._who(peer)} for its copy", peer=peer)
        self._get(peer, net, {"what": "manifest", "snap": sn["id"]})

    def got_manifest(self, peer: str, net: str, sid: str, manifest: dict) -> None:
        f = self.fetch(net)
        if f.active:
            return
        sn = (((self.heard.get(peer) or {}).get("nets") or {}).get(net) or {}).get("sn") or {}
        if sn.get("id") != sid or manifest.get("hash") != sn.get("hash") or manifest.get("height") != sn.get("h"):
            return
        if not manifest.get("txindex"):
            self._job(net, state="waiting", step=f"the copy on {self._who(peer)} has no transaction index")
            self._refuse(net, peer)
            return
        try:
            with cc.held(f.root.with_name(f.root.name + ".lock")) as mine:
                if mine:
                    f.begin(sid, peer, manifest)
        except cc.CopyError as exc:
            self._job(net, state="failed", step=str(exc))
            self._refuse(net, peer)

    def _pull(self, net: str, f: cc.Fetch, now: float) -> None:
        peer = f.state["maker"]
        have, total = f.progress()
        flight = self._flight.setdefault(net, {})
        for n, t in list(flight.items()):
            if now - t > cc.PIECE_SECONDS:
                flight.pop(n, None)
        link = self._link(peer)
        sn = (((self.heard.get(peer) or {}).get("nets") or {}).get(net) or {}).get("sn") or {}
        last = f.state.setdefault("got_at", time.time())
        if link is None or sn.get("id") != f.state["id"]:
            if time.time() - last > cc.FETCH_STALL and (self.heard.get(peer) and sn.get("id") != f.state["id"]):
                log.info("core copy %s: %s no longer offers the copy; let go", net, peer)
                f.drop()
                self._job(net, state="waiting", step=f"{self._who(peer)} no longer has the copy")
                return
            self._job(net, state="working", step=f"waiting for {self._who(peer)} to be linked again",
                      done=have, total=total, peer=peer)
            return
        self._job(net, state="working", step=f"copying the chain data from {self._who(peer)}", done=have,
                  total=total, peer=peer)
        for n in f.missing():
            if len(flight) >= cc.IN_FLIGHT:
                break
            if n in flight:
                continue
            fi, pi = f.where(n)
            flight[n] = now
            self._get(peer, net, {"what": "piece", "snap": f.state["id"], "f": fi, "i": pi})

    def got_piece(self, peer: str, net: str, sid: str, fi: int, pi: int, data: bytes) -> None:
        f = self.fetch(net)
        if not f.active or f.state.get("id") != sid or f.state.get("maker") != peer:
            return
        n = f.number(fi, pi)
        if n is None:
            return
        self._flight.get(net, {}).pop(n, None)
        if f.got(n, data):
            f.state["got_at"] = time.time()
        else:
            log.warning("core copy %s: a piece from %s is not the manifest's", net, peer)

    def piece_failed(self, net: str, fi: int, pi: int) -> None:
        f = self.fetch(net)
        n = f.number(fi, pi) if f.active else None
        if n is not None:
            self._flight.get(net, {}).pop(n, None)

    def _install(self, net: str) -> None:
        f = self.fetch(net)
        peer = f.state.get("maker", "")
        with cc.held(f.root.with_name(f.root.name + ".lock")) as mine:
            if not mine:
                return
            if not f.active:
                f.load()                                   # after a restart: from its state.json
            if not f.active:
                return
            f.save(force=True)
            try:
                got = f.install(self.core_of(net), lambda s: self._job(net, state="working", step=s, peer=peer))
            except Exception as exc:                       # noqa: BLE001 -- said on the page; the old data is back
                self._job(net, state="failed", step=f"the copy from {self._who(peer)} was refused: {exc}"[:300])
                self._refuse(net, peer)
                return
        self.state.set_setting(f"core_copied:{net}", {"from": peer, "height": got["height"], "hash": got["hash"],
                                                      "at": int(time.time())})
        self._job(net, state="done", step=f"copied from {self._who(peer)} at block {got['height']:,}; its coin set "
                                          f"is trusted, the node syncs the rest itself", peer=peer)
        log.info("core copy %s: installed the copy from %s at %s", net, peer, got["height"])

    def _refuse(self, net: str, peer: str) -> None:
        refused = self._refused(net)
        if peer not in refused:
            self.state.set_setting(f"core_copy_refused:{net}", refused + [peer])

    def _who(self, peer: str) -> str:
        try:
            tag = ((self.state.mesh.operators.peers or {}).get(peer) or {}).get("tag")
        except Exception:                                  # noqa: BLE001
            tag = None
        return f"@{tag}" if tag else f"arcade {peer[:12]}"

    def status(self) -> dict:
        """For the Coins & chains tab: per chain, the fetch under way or done, and the copy this arcade holds."""
        out = {}
        for net in self.nets():
            row = dict(self.jobs.get(net) or {})
            done = self._settings().get(f"core_copied:{net}")
            if done and not row:
                row = {"state": "done", "step": f"chain data copied from {self._who(done.get('from', ''))} at block "
                                                f"{int(done.get('height', 0)):,}; its coin set is trusted"}
            s = self._snaps.get(net)
            if s is not None:
                if s.making:
                    row["making"] = s.said or "making a copy for a linked arcade"
                elif s.offer():
                    row["holds"] = s.offer()
            if row:
                out[net] = row
        return out

    # ---------------------------------------------------------------- the wire
    def _link(self, peer: str):
        node = self.node
        if node is None:
            return None
        links = getattr(node, "links", None)
        return links.get(peer) if isinstance(links, dict) else None

    def _hello(self) -> dict:
        nets = {}
        for net in self.nets():
            info = self.info.get(net)
            if not info or not self._sharing(net):
                continue
            sn = self.snaps(net).offer()
            if sn is not None:
                sn = {**sn, "hash": self.snaps(net).current()[1]["hash"]}
            nets[net] = {"h": info["blocks"], "ver": info.get("ver"), "gen": info.get("gen"),
                         "can": bool(info.get("synced")) and cc.conf_txindex(self._installed(net)), "sn": sn}
        return {"t": "cb", "op": "hi", "nets": nets}

    async def link_up(self, link) -> None:
        self.loop = asyncio.get_running_loop()
        self.start()
        try:
            await link.send(await asyncio.to_thread(self._hello))
        except Exception:                                  # noqa: BLE001
            log.debug("cb link_up", exc_info=True)

    async def link_down(self, peer_id: str) -> None:
        self.heard.pop(peer_id, None)

    async def rebalance(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.start()
        hello = await asyncio.to_thread(self._hello)
        links = getattr(self.node, "links", None)
        for link in list(links.values()) if isinstance(links, dict) else []:
            try:
                await link.send(hello)
            except Exception:                              # noqa: BLE001
                pass

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        if self._link(link.peer_id) is not link:
            return                                         # direct links only
        op, peer = header.get("op"), link.peer_id
        if op == "gb":
            await self._part(link, header, blob)
        elif op == "hi":
            nets = header.get("nets")
            if isinstance(nets, dict):
                self.heard[peer] = {"at": time.monotonic(), "nets": {str(k): v for k, v in nets.items()
                                                                     if isinstance(v, dict)}}
        elif op == "sq":
            net = str(header.get("net") or "")
            if net in self.nets():
                self.asked.setdefault(net, {})[peer] = time.monotonic()
        elif op == "g":
            asyncio.ensure_future(self._send(link, header))

    async def _send(self, link, header: dict) -> None:
        rid, what, sid, net = header.get("id"), header.get("what"), header.get("snap"), str(header.get("net") or "")
        if not isinstance(rid, int) or not isinstance(sid, str) or net not in self.nets() or not self._sharing(net):
            return
        snaps = self.snaps(net)
        now = time.monotonic()
        served = [t for t in self._served.get(link.peer_id, []) if t > now - 60]
        busy = what == "piece" and len(served) >= PIECES_PER_MINUTE
        self._served[link.peer_id] = served if busy or what != "piece" else served + [now]
        if self._serving is None:
            self._serving = asyncio.Semaphore(SERVING_AT_ONCE)
        try:
            if busy:
                await link.send({"t": "cb", "op": "gb", "id": rid, "seq": 0, "end": True, "err": "busy"})
                return
            async with self._serving:
                z = False
                if what == "manifest":
                    m = snaps.manifest(sid)
                    data = None if m is None else await asyncio.to_thread(
                        lambda: zlib.compress(json.dumps(m, sort_keys=True, separators=(",", ":")).encode(), 6))
                    z = True
                elif what == "piece" and isinstance(header.get("f"), int) and isinstance(header.get("i"), int):
                    data = await asyncio.to_thread(snaps.piece, sid, header["f"], header["i"])
                else:
                    data = None
                if data is None:
                    await link.send({"t": "cb", "op": "gb", "id": rid, "seq": 0, "end": True, "err": "gone"})
                    return
                parts = [data[i:i + PART] for i in range(0, len(data), PART)] or [b""]
                for seq, part in enumerate(parts):
                    await link.send({"t": "cb", "op": "gb", "id": rid, "seq": seq, "end": seq == len(parts) - 1,
                                     **({"z": 1} if z else {})}, part)
        except Exception:                                  # noqa: BLE001 -- the link went
            log.debug("cb send", exc_info=True)

    def _post(self, peer: str, frame: dict) -> bool:
        loop, link = self.loop, self._link(peer)
        if loop is None or link is None:
            return False
        try:
            asyncio.run_coroutine_threadsafe(link.send({"t": "cb", **frame}), loop)
        except RuntimeError:
            return False
        return True

    def _get(self, peer: str, net: str, ask: dict) -> None:
        now = time.monotonic()
        for rid, w in list(self._wait.items()):
            if now - w["at"] > WAIT_SECONDS:
                self._wait.pop(rid, None)
        rid = next(self._ids)
        self._wait[rid] = {"net": net, "peer": peer, **ask, "parts": [], "size": 0, "at": now}
        if not self._post(peer, {"op": "g", "net": net, "id": rid, **ask}):
            self._wait.pop(rid, None)

    async def _part(self, link, header: dict, blob: bytes) -> None:
        w = self._wait.get(header.get("id"))
        if w is None or w["peer"] != link.peer_id:
            return
        limit = MANIFEST_MAX if w["what"] == "manifest" else cc.PIECE
        if header.get("err") or header.get("seq") != len(w["parts"]) or w["size"] + len(blob) > limit:
            self._wait.pop(header.get("id"), None)
            if w["what"] == "piece":
                self.piece_failed(w["net"], int(w["f"]), int(w["i"]))
            return
        w["parts"].append(blob)
        w["size"] += len(blob)
        if not header.get("end"):
            return
        self._wait.pop(header.get("id"), None)
        data = b"".join(w["parts"])
        if w["what"] == "manifest":
            raw = await asyncio.to_thread(_inflate, data, MANIFEST_MAX)
            try:
                manifest = json.loads(raw.decode()) if raw is not None else None
            except ValueError:
                manifest = None
            if isinstance(manifest, dict):
                await asyncio.to_thread(self.got_manifest, w["peer"], w["net"], w["snap"], manifest)
        else:
            await asyncio.to_thread(self.got_piece, w["peer"], w["net"], w["snap"], int(w["f"]), int(w["i"]), data)
