"""Arcades comparing their indexes over the mesh (2026-10-17: "share their index with each other so
that we can verify everyone has the same index"). The fingerprints are arcade/index_digest.py's.

On the wire (frames of kind "ix" on an ordinary link, never flooded):

    tip  {nets: {net: {h, at, d[, s]}}}   indexed height, the settled checkpoint, the fingerprint of
                                         floor..checkpoint, and the consensus hash at h
    q    {id, net, what: "d", lo, hi}     the fingerprint of a range, to halve towards a difference
    q    {id, net, what: "rows", h}       what one block did, to show the difference
    a    {id[, d][, rows][, err]}

Each link compares every chain both arcades index at the lower of their two checkpoints. Equal: they
agree up to there. Different: the range is halved, a question at a time, to the first block whose rows
differ, and both sides' rows for it are kept for the admin panel. Same height and a different
consensus hash (balances, the book, properties) is said too.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Callable

log = logging.getLogger("arcade.mesh")

MAX_ROWS = 200                 # rows of one block sent to show a difference
ROW_CHARS = 600
QUESTIONS_PER_MINUTE = 120     # from one peer
ASK_SECONDS = 30.0


class IndexCheck:
    """Attached to a MeshNode. `fingerprints()` gives {net: index_digest.Fingerprints} for the chains
    this arcade indexes (read again every minute)."""

    kinds = ("ix",)

    def __init__(self, fingerprints: Callable[[], dict]):
        self.fingerprints = fingerprints
        self.node = None
        self.mine: dict[str, dict] = {}                      # net -> my tip
        self.theirs: dict[str, dict[str, dict]] = {}         # peer -> net -> its tip
        self.results: dict[str, dict[str, dict]] = {}        # net -> peer -> what the comparison found
        self._ids = itertools.count(1)
        self._waiting: dict[int, tuple[str, asyncio.Future]] = {}
        self._running: dict[tuple[str, str], asyncio.Task] = {}
        self._asked: dict[str, list[float]] = {}
        self._work: asyncio.Semaphore | None = None          # made on the mesh's loop

    # ---------------------------------------------------------------- hooks
    async def link_up(self, link) -> None:
        if self._work is None:
            self._work = asyncio.Semaphore(1)
        if not self.mine:
            self.mine = await asyncio.to_thread(self._tips)
        await link.send({"t": "ix", "op": "tip", "nets": self.mine})

    async def link_down(self, peer_id: str) -> None:
        self.theirs.pop(peer_id, None)
        self._asked.pop(peer_id, None)
        for key in [k for k in self._running if k[0] == peer_id]:
            self._running.pop(key).cancel()
        for rid, (peer, fut) in list(self._waiting.items()):
            if peer == peer_id and not fut.done():
                fut.cancel()

    async def rebalance(self) -> None:
        if self.node is None:
            return
        now = await asyncio.to_thread(self._tips)
        if now != self.mine:
            self.mine = now
            for link in list(self.node.links.values()):
                try:
                    await link.send({"t": "ix", "op": "tip", "nets": now})
                except Exception:                              # noqa: BLE001
                    pass
            for peer in list(self.theirs):
                self._compare_all(peer)

    def _tips(self) -> dict:
        out = {}
        try:
            prints = self.fingerprints() or {}
        except Exception:                                      # noqa: BLE001
            return {}
        for net, fp in prints.items():
            try:
                tip = fp.tip()
            except Exception as exc:                           # noqa: BLE001 -- that index is not ready
                log.debug("indexcheck %s: %s", net, exc)
                continue
            if tip:
                out[net] = tip
        return out

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        op, peer = header.get("op"), link.peer_id
        if op == "tip":
            nets = header.get("nets")
            if isinstance(nets, dict):
                self.theirs[peer] = {str(n): v for n, v in nets.items() if isinstance(v, dict)}
                self._compare_all(peer)
        elif op == "q":
            self.node._spawn(self._answer(link, header))
        elif op == "a":
            got = self._waiting.get(header.get("id"))
            if got is not None and got[0] == peer and not got[1].done():
                got[1].set_result(header)

    # ---------------------------------------------------------------- answering
    async def _answer(self, link, header: dict) -> None:
        rid, net, peer = header.get("id"), str(header.get("net")), link.peer_id
        if not isinstance(rid, int):
            return
        now = time.monotonic()
        asked = [t for t in self._asked.get(peer, []) if t > now - 60]
        self._asked[peer] = asked + [now]
        fp = (await asyncio.to_thread(self.fingerprints)).get(net) if len(asked) < QUESTIONS_PER_MINUTE else None
        said: dict = {"t": "ix", "op": "a", "id": rid}
        try:
            if fp is None:
                raise ValueError("not answered")
            async with self._work:
                if header.get("what") == "d":
                    said["d"] = await asyncio.to_thread(fp.digest, int(header["lo"]), int(header["hi"]))
                elif header.get("what") == "rows":
                    rows = await asyncio.to_thread(fp.rows, int(header["h"]))
                    said["rows"] = [r[:ROW_CHARS] for r in rows[:MAX_ROWS]]
                else:
                    raise ValueError("no such question")
        except Exception as exc:                               # noqa: BLE001
            said = {"t": "ix", "op": "a", "id": rid, "err": str(exc)[:120]}
        try:
            await link.send(said)
        except Exception:                                      # noqa: BLE001
            pass

    # ---------------------------------------------------------------- comparing
    async def _ask(self, peer: str, question: dict) -> dict:
        link = self.node.links.get(peer)
        if link is None:
            raise ConnectionError("gone")
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._waiting[rid] = (peer, fut)
        try:
            await link.send({"t": "ix", "op": "q", "id": rid, **question})
            got = await asyncio.wait_for(fut, ASK_SECONDS)
        finally:
            self._waiting.pop(rid, None)
        if got.get("err"):
            raise ValueError(got["err"])
        return got

    def _compare_all(self, peer: str) -> None:
        for net in set(self.mine) & set(self.theirs.get(peer, {})):
            key = (peer, net)
            if key in self._running and not self._running[key].done():
                continue
            self._running[key] = self.node._spawn(self._compare(peer, net))

    async def _compare(self, peer: str, net: str) -> None:
        mine, theirs = self.mine.get(net), self.theirs.get(peer, {}).get(net)
        fp = (await asyncio.to_thread(self.fingerprints)).get(net)
        if not mine or not theirs or fp is None or not isinstance(theirs.get("at"), int):
            return
        result = {"checked": time.time(), "height": theirs.get("h")}
        try:
            at = min(mine["at"], theirs["at"])
            if at == mine["at"]:
                ours = mine["d"]
            else:
                ours = await asyncio.to_thread(fp.digest, fp.floor, at)
            if at == theirs["at"]:
                other = str(theirs.get("d"))
            else:
                other = (await self._ask(peer, {"net": net, "what": "d", "lo": fp.floor, "hi": at}))["d"]
            if ours == other:
                result.update(agree_upto=at)
            else:
                first = await self._first_difference(peer, net, fp, fp.floor, at)
                theirs_rows = (await self._ask(peer, {"net": net, "what": "rows", "h": first})).get("rows") or []
                ours_rows = [r[:ROW_CHARS] for r in (await asyncio.to_thread(fp.rows, first))[:MAX_ROWS]]
                result.update(differs_at=first, ours=ours_rows, theirs=theirs_rows,
                              agree_upto=first - 1 if first > fp.floor else None)
                # Said once per difference (2026-10-18: the same block was logged every ten seconds); the admin
                # page shows it for as long as it lasts.
                was = self.results.get(net, {}).get(peer, {}).get("differs_at")
                if was != first:
                    log.warning("indexcheck: %s's %s index differs from this one at block %d", peer[:12], net, first)
            if mine.get("h") == theirs.get("h") and mine.get("s") and theirs.get("s"):
                result["state"] = "same" if mine["s"] == theirs["s"] else "differs"
        except (asyncio.CancelledError, ConnectionError):
            return
        except Exception as exc:                               # noqa: BLE001 -- said, and tried again next minute
            result["error"] = str(exc)[:200]
        self.results.setdefault(net, {})[peer] = result

    async def _first_difference(self, peer: str, net: str, fp, lo: int, hi: int) -> int:
        """The lowest block in lo..hi whose fingerprint differs, knowing lo..hi as a whole does."""
        while lo < hi:
            mid = (lo + hi) // 2
            ours = await asyncio.to_thread(fp.digest, lo, mid)
            other = (await self._ask(peer, {"net": net, "what": "d", "lo": lo, "hi": mid}))["d"]
            if ours != other:
                hi = mid
            else:
                lo = mid + 1
        return lo

    def status(self) -> dict:
        """For the admin panel: per chain, this arcade's checkpoint and what every linked arcade's showed."""
        out = {}
        for net in sorted(set(self.mine) | set(self.results)):
            rows = {p: r for p, r in self.results.get(net, {}).items() if self.node is None or p in self.node.links}
            agree = [r for r in rows.values() if "differs_at" not in r and "error" not in r and r.get("state") != "differs"]
            out[net] = {"at": (self.mine.get(net) or {}).get("at"), "height": (self.mine.get(net) or {}).get("h"),
                        "compared": len(rows), "agree": len(agree),
                        "agree_upto": max((r.get("agree_upto") or 0 for r in agree), default=None),
                        "differ": [{"peer": p[:12], **{k: r[k] for k in ("differs_at", "ours", "theirs", "state", "error")
                                                        if k in r}}
                                   for p, r in rows.items() if r not in agree]}
        return out
