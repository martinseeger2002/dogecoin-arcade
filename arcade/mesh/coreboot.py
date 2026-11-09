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
# Raised 2026-11-09 (the operator: Arcade V1.0 fetched Strawberry's Pepecoin mainnet copy at about 6 MB/s, held by the old
# 2 and 60). SERVING_AT_ONCE is for everybody together; PIECES_PER_MINUTE is per asker.
SERVING_AT_ONCE = 6
PIECES_PER_MINUTE = 240                 # 8 MB each: up to 32 MB/s to one asker
MANIFEST_MAX = 64 * 1024 * 1024
WAIT_SECONDS = 300
HEARD_FRESH = 600
ASKED_FRESH = 15 * 60
ASK_EVERY = 10 * 60
TICK = 1.0
INFO_EVERY = 20.0
CHOOSE_EVERY = 60.0
#: How long after this arcade starts a chain with no offer is not yet said to have none (offers come once a minute).
LOOK_SECONDS = 90.0
#: Phases that are said as notifications too, and how.
NOTED = {"asked": "info", "copying": "info", "checking": "info", "done": "ok", "failed": "bad", "none": "info",
         "own": "info", "making": "info"}
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
        self.core_of = core_of or (lambda net: cc.core_for(self._installed(net), self.networks[net]))
        self._services: dict[str, tuple[float, dict | None]] = {}
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
        self._chose_at: dict[str, float] = {}
        self._samples: dict[str, list] = {}
        self._started = time.monotonic()
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

    def _service(self, net: str) -> dict | None:
        """The node the installer runs for `net` as a systemd user unit (core_copy.service_node), looked at once a
        minute: a Lite install's Pepecoin testnet takes its first chain data from linked arcades this way (the operator
        2026-11-04: "I would like the Lite install to get the initial Pepecoin test block state from one of the arcade
        peers")."""
        now = time.monotonic()
        kept = self._services.get(net)
        if kept is None or now - kept[0] > 60:
            kept = self._services[net] = (now, cc.service_node(net))
        return kept[1]

    def _installed(self, net: str) -> dict:
        got = self._settings().get(f"core_installed:{net}")
        return dict(got) if isinstance(got, dict) else dict(self._service(net) or {})

    def nets(self) -> list[str]:
        """Chains whose Core runs here and this arcade may stop and start: those it installed (Coins & chains), and
        those the installer runs as a user unit."""
        from ..cores import HERE, mode
        s = self._settings()
        out = [k.split(":", 1)[1] for k, v in s.items()
               if k.startswith("core_installed:") and isinstance(v, dict) and k.split(":", 1)[1] in self.networks
               and mode(s, k.split(":", 1)[1]) == HERE]
        for net in cc.SERVICE_UNITS:
            if net in self.networks and net not in out and mode(s, net) == HERE and self._service(net):
                out.append(net)
        return out

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
            from ..core_install import note
            label = getattr(self.networks.get(net), "label", net)
            note(self.state, net, f"{label}: making a copy of this node's chain data for "
                                  f"{', '.join(self._who(p) for p in asks)} (the node pauses for a few seconds)")
            with cc.held(snaps.root.with_name(snaps.root.name + ".lock")) as mine:
                if mine:
                    snaps.make(maker=self._me())
            note(self.state, net, f"{label}: the copy is ready for linked arcades", "ok")
            self._say_all()
        self._spawn(net, make)

    def _me(self) -> str:
        return str(getattr(self.node, "node_id", "") or "")

    def holders(self, net: str, sid: str) -> list[str]:
        """Linked arcades that hold copy `sid` whole and say so: its maker, and every arcade that installed it and keeps
        it to share (core_copy.seed). Pieces are asked of all of them at once, torrent-style (2026-11-04: "Or
        several of the peers torrent style"); each is checked against the manifest, whoever sent it."""
        now = time.monotonic()
        out = []
        for peer, h in self.heard.items():
            sn = (((h.get("nets") or {}).get(net) or {}).get("sn") or {})
            if sn.get("id") == sid and now - h.get("at", 0) <= HEARD_FRESH and self._link(peer) is not None:
                out.append(peer)
        return sorted(out)

    def _say_all(self) -> None:
        loop = self.loop
        if loop is not None:
            asyncio.run_coroutine_threadsafe(self.rebalance(), loop)

    # ---------------------------------------------------------------- fetching
    def _job(self, net: str, **kw) -> None:
        """What the Coins & chains row says. A new `phase` is also a notification (core_install.note)."""
        before = (self.jobs.get(net) or {}).get("phase")
        self.jobs[net] = {**self.jobs.get(net, {}), **kw}
        phase = kw.get("phase")
        if phase and phase != before and phase in NOTED:
            from ..core_install import note
            label = getattr(self.networks.get(net), "label", net)
            note(self.state, net, f"{label}: {kw.get('step', '')}", NOTED[phase])

    def _offers(self, net: str, now: float) -> list[tuple[str, dict]]:
        mine = self.info.get(net) or {}
        refused = self._refused(net)
        out = []
        for peer, h in self.heard.items():
            said = (h.get("nets") or {}).get(net)
            if now - h.get("at", 0) > HEARD_FRESH or not isinstance(said, dict) or peer in refused:
                continue
            if isinstance(said.get("sn"), dict) and said["sn"].get("maker") in refused:
                said = {k: v for k, v in said.items() if k != "sn"}      # a refused maker's copy, whoever holds it
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
        if not info:
            if (self.jobs.get(net) or {}).get("phase") in (None, "starting"):
                self._job(net, state="waiting", phase="starting", step="the node is starting")
            return
        if self._settings().get("core_copy_auto", True) is False or self._settings().get(f"core_copy_own:{net}"):
            self._job(net, state="waiting", phase="own", step="syncing on its own (no copy, as chosen)")
            return
        if info["headers"] - info["blocks"] < cc.WORTH_BLOCKS and info["headers"] > 0:
            if (self.jobs.get(net) or {}).get("state") == "waiting":
                self.jobs.pop(net, None)                   # caught up by itself (or never far behind)
            return
        offers = self._offers(net, now)
        best = max([int(s.get("h") or 0) for _, s in offers] + [0])
        if best and (self.jobs.get(net) or {}).get("phase") not in ("headers", "asked", "copying", "linking"):
            # The chain's height as linked arcades say it, for the headers step -- but never over the block of the copy
            # being taken, which is what that step waits for (2026-11-04: it said 915,108 for a copy at 915,080).
            self.jobs.setdefault(net, {})["target"] = best
        ready = sorted(((p, s) for p, s in offers if isinstance(s.get("sn"), dict)
                        and int(s["sn"].get("h", 0)) - info["blocks"] >= cc.WORTH_BLOCKS),
                       key=lambda x: -int(x[1]["sn"]["h"]))
        if ready:
            peer, said = ready[0]
            if now - self._chose_at.get(net, -1e9) >= CHOOSE_EVERY:     # an offer waiting on headers: once a minute
                self._chose_at[net] = now
                self._spawn(net, self._choose, net, peer, said["sn"])
            return
        makers = sorted(((p, s) for p, s in offers if s.get("can") and int(s.get("h", 0)) - info["blocks"]
                         >= cc.WORTH_BLOCKS), key=lambda x: -int(x[1].get("h", 0)))
        if makers:
            if now - self._asked_at.get(net, -1e9) >= ASK_EVERY:
                self._asked_at[net] = now
                self._post(makers[0][0], {"op": "sq", "net": net})
            self._job(net, state="waiting", phase="asked", peer=makers[0][0],
                      step=f"asked {self._who(makers[0][0])} for a copy of its chain data; it makes one (its node "
                           f"pauses for a few seconds), then the copy comes over")
        elif now - self._started < LOOK_SECONDS:
            # Just started (an install reloads the arcade): the linked arcades say what they have within a minute.
            self._job(net, state="waiting", phase="looking",
                      step="looking for a linked arcade that has a copy of this chain's data")
        elif (self.jobs.get(net) or {}).get("state") in (None, "waiting"):
            self._job(net, state="waiting", phase="none",
                      step="no linked arcade has a copy of this chain's data to give, so the node syncs on its own; "
                           "a copy is taken if one is offered later")

    def _choose(self, net: str, peer: str, sn: dict) -> None:
        """Check an offer against this node's own headers and disk, then ask for its manifest."""
        core = self.core_of(net)
        why = cc.header_check(core, {"genesis": (self.info.get(net) or {}).get("gen"),
                                     "version": (self.info.get(net) or {}).get("ver"),
                                     "height": sn.get("h"), "hash": sn.get("hash")})
        if why:
            waiting = "waiting" in why
            self._job(net, state="waiting", phase="headers" if waiting else "none", peer=peer,
                      target=int(sn.get("h") or 0), copy_bytes=int(sn.get("bytes") or 0),
                      step=(f"{self._who(peer)} made a copy at block {int(sn.get('h') or 0):,}; this node is learning "
                            f"the chain's headers up to it, to check the copy is of the real chain") if waiting
                      else f"the copy on {self._who(peer)}: {why}")
            if "waiting" not in why:
                self._refuse(net, peer)
            return
        folder = cc.chain_dir(self._installed(net), self.networks[net]).parent
        need = int(sn.get("bytes", 0)) + SPARE_BYTES
        if shutil.disk_usage(folder).free < need:
            self._job(net, state="waiting", phase="none",
                      step=f"a copy takes {need / 1e9:.0f} GB; this disk has less free, so the node syncs on its own")
            return
        self._job(net, state="working", phase="asked", step=f"asking {self._who(peer)} for its copy", peer=peer,
                  target=int(sn.get("h") or 0), copy_bytes=int(sn.get("bytes") or 0))
        self._get(peer, net, {"what": "manifest", "snap": sn["id"]})

    def got_manifest(self, peer: str, net: str, sid: str, manifest: dict) -> None:
        f = self.fetch(net)
        if f.active:
            return
        sn = (((self.heard.get(peer) or {}).get("nets") or {}).get(net) or {}).get("sn") or {}
        if sn.get("id") != sid or manifest.get("hash") != sn.get("hash") or manifest.get("height") != sn.get("h"):
            return
        if not manifest.get("txindex"):
            self._job(net, state="waiting", phase="none", step=f"the copy on {self._who(peer)} has no transaction "
                                                              f"index, so the node syncs on its own")
            self._refuse(net, peer)
            return
        try:
            with cc.held(f.root.with_name(f.root.name + ".lock")) as mine:
                if mine:
                    f.begin(sid, str(manifest.get("maker") or peer), manifest)
        except cc.CopyError as exc:
            self._job(net, state="failed", phase="failed", step=str(exc))
            self._refuse(net, peer)

    def _pull(self, net: str, f: cc.Fetch, now: float) -> None:
        sid, maker = f.state["id"], f.state["maker"]
        have, total = f.progress()
        flight = self._flight.setdefault(net, {})                  # piece number -> (asked at, of whom)
        for n, (t, _) in list(flight.items()):
            if now - t > cc.PIECE_SECONDS:
                flight.pop(n, None)
        holders = self.holders(net, sid)
        last = f.state.setdefault("got_at", time.time())
        if not holders:
            if time.time() - last > cc.FETCH_STALL and self.heard:
                log.info("core copy %s: no linked arcade offers the copy any more; let go", net)
                f.drop()
                self._job(net, state="waiting", phase="none", step="no linked arcade has the copy any more")
                return
            self._job(net, state="working", phase="linking", done=have, total=total, peer=maker, sources=0,
                      step=f"waiting for an arcade that has {self._who(maker)}'s copy to be linked again")
            return
        rate, eta = self._rate(net, have, total)
        self._job(net, state="working", phase="copying", done=have, total=total, peer=maker, rate=rate, eta=eta,
                  pieces=[len(f._done), len(f._pieces)], sources=len(holders),
                  step=f"copying the chain data, {self._who(maker)}'s copy, from "
                       + (self._who(holders[0]) if len(holders) == 1 else f"{len(holders)} linked arcades at once"))
        asked = {p: 0 for p in holders}
        for _, p in flight.values():
            if p in asked:
                asked[p] += 1
        paused = self.__dict__.setdefault("_paused", {})
        for n in f.missing():
            if n in flight:
                continue
            ready = [p for p in holders if asked[p] < cc.IN_FLIGHT and paused.get(p, 0) <= now]
            if not ready:
                break
            peer = min(ready, key=lambda p: (asked[p], p))
            fi, pi = f.where(n)
            flight[n] = (now, peer)
            asked[peer] += 1
            self._get(peer, net, {"what": "piece", "snap": sid, "f": fi, "i": pi})

    def got_piece(self, peer: str, net: str, sid: str, fi: int, pi: int, data: bytes) -> None:
        f = self.fetch(net)
        if not f.active or f.state.get("id") != sid:
            return
        n = f.number(fi, pi)
        if n is None:
            return
        self._flight.get(net, {}).pop(n, None)
        if f.got(n, data):                                         # checked against the manifest, whoever sent it
            f.state["got_at"] = time.time()
        else:
            log.warning("core copy %s: a piece from %s is not the manifest's", net, peer)
            self.__dict__.setdefault("_paused", {})[peer] = time.monotonic() + 300

    def piece_failed(self, net: str, fi: int, pi: int, peer: str = "", busy: bool = False) -> None:
        f = self.fetch(net)
        n = f.number(fi, pi) if f.active else None
        if n is not None:
            self._flight.get(net, {}).pop(n, None)
        if peer and busy:
            self.__dict__.setdefault("_paused", {})[peer] = time.monotonic() + 10

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
                got = f.install(self.core_of(net), lambda s: self._job(net, state="working", phase="checking",
                                                                        step=s, peer=peer))
            except Exception as exc:                       # noqa: BLE001 -- said on the page; the old data is back
                self._job(net, state="failed", phase="failed",
                          step=f"the copy from {self._who(peer)} was refused: {exc}. This node's own data is back "
                               f"and it syncs on its own"[:300])
                self._refuse(net, peer)
                return
        self.state.set_setting(f"core_copied:{net}", {"from": peer, "height": got["height"], "hash": got["hash"],
                                                      "at": int(time.time())})
        self._job(net, state="done", phase="done",
                  step=f"copied from {self._who(peer)} at block {got['height']:,} and checked here; its coin set is "
                       f"{self._who(peer)}'s word. The node syncs the rest itself", peer=peer)
        log.info("core copy %s: installed the copy from %s at %s", net, peer, got["height"])

    def _rate(self, net: str, have: int, total: int) -> tuple[float, int | None]:
        """Bytes a second over the last minute or so, and seconds left at that pace."""
        now = time.monotonic()
        samples = [x for x in self._samples.get(net, []) if now - x[0] < 90] + [(now, have)]
        self._samples[net] = samples
        t0, h0 = samples[0]
        rate = (have - h0) / (now - t0) if now - t0 >= 5 else 0.0
        return round(rate), (int((total - have) / rate) if rate > 0 else None)

    def own(self, net: str) -> None:
        """The operator chose "Sync on its own instead": no copy for this chain (a fetch under way is let go)."""
        self.state.set_setting(f"core_copy_own:{net}", True)
        f = self.fetch(net)
        if f.active and f.state.get("phase") == "fetching":
            f.drop()
        self._flight.pop(net, None)
        self._job(net, state="waiting", phase="own", step="syncing on its own (no copy, as chosen)")

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
                row = {"state": "done", "phase": "done",
                       "step": f"chain data copied from {self._who(done.get('from', ''))} at block "
                               f"{int(done.get('height', 0)):,}; its coin set is trusted", "peer": done.get("from")}
            if row.get("peer"):
                row["from"] = self._who(row["peer"])
            if self._installed(net).get("unit"):
                row["service"] = True                             # the installer's node: no Cancel here
            info = self.info.get(net)
            if info:
                row["node"] = {"blocks": info["blocks"], "headers": info["headers"]}
                # How fast the node's headers and blocks come, for each step's time left.
                if row.get("target") and info["headers"] < row["target"]:
                    row["eta_headers"] = self._rate(net + ":h", info["headers"], int(row["target"]))[1]
                if info["headers"] and info["blocks"] < info["headers"]:
                    row["eta_blocks"] = self._rate(net + ":b", info["blocks"], info["headers"])[1]
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
                self.piece_failed(w["net"], int(w["f"]), int(w["i"]), w["peer"], header.get("err") == "busy")
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
