"""Known collections between arcades (arcade/known_collections.py; 2026-10-26, SadFrog: "the known collections should
be shared across arcades to be agreed upon", shared "over the mesh").

Each arcade says one digest per chain; where two differ, one asks the other for that chain's index (each collection's
slug and fingerprint); a collection is agreed by this arcade (when it holds the same fingerprint) and every linked arcade
holding the same one. An arcade that lacks a collection, or holds a fingerprint fewer arcades agree on than another one,
fetches the pieces from a linked arcade that holds the other, works the fingerprint out itself and keeps the list only
when it is the one announced (source "mesh:<arcade id>").

On the wire (frames of kind "kc" on an ordinary link, never flooded; every one under the link's 64 KB):

    hi  {v, d: {net: digest}}                          this arcade's digest of each chain (link up; when it changes)
    ix  {net}                                          send me that chain's index
    il  {net, seq, end, rows: [[slug, fp, name, supply, count, sig]...]}    the index, in parts of ROWS_PER_FRAME
    ig  {id, net, slug, fp}                            send me that collection's pieces, if you hold it at that fp
    ib  {id, seq, end[, err]} + a slice of the pieces  the answer: zlib of json [[id, name, position, number]...],
                                                       in parts of PART; err "gone" when not held at that fp,
                                                       "busy" when asked too often (asked again a minute later)

A frame names no other arcade: what a link carries is that arcade's own word (the link proves who it is).
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
import zlib
from typing import Any, Callable

from .. import known_collections as kclib

log = logging.getLogger("arcade.mesh")

VERSION = 1
ROWS_PER_FRAME = 100           # an "il" frame: a row is at most ~420 bytes
PART = 60 * 1024               # a slice of packed pieces in one "ib" frame
MAX_PACKED = 16 * 1024 * 1024  # one collection's pieces as sent, at most
MAX_INFLATED = 32 * 1024 * 1024
HELLO_EVERY = 600.0            # the digests said again at least this often, unasked
PEER_TTL = 6 * 3600.0          # what a linked arcade said, forgotten after this
FETCH_TIMEOUT = 300.0          # a fetch that has not ended by then is let go
BAD_FOR = 3600.0               # a peer that sent a list not matching its fingerprint is not asked for it again so soon
ANSWERS_PER_MINUTE = 30        # "ix" and "ig" one linked arcade may ask of this one
SERVE_AT_ONCE = 2
MAX_PEERS = 256


def pack(rows: list[list]) -> bytes:
    return zlib.compress(json.dumps(rows, separators=(",", ":")).encode(), 6)


def unpack(data: bytes) -> list | None:
    """Pieces as sent, or None: not zlib, past MAX_INFLATED (never inflated further), or not a json list."""
    d = zlib.decompressobj()
    try:
        out = d.decompress(data, MAX_INFLATED + 1)
    except zlib.error:
        return None
    if len(out) > MAX_INFLATED or d.unconsumed_tail or not d.eof:
        return None
    try:
        got = json.loads(out.decode("utf-8"))
    except ValueError:
        return None
    return got if isinstance(got, list) else None


def _row(r: Any) -> dict | None:
    """One "il" row, checked: [slug, fp, name, supply, count, sig]."""
    if not isinstance(r, list) or len(r) < 5:
        return None
    slug, fp, name, supply, count = r[:5]
    sig = r[5] if len(r) > 5 else ""
    if not isinstance(slug, str) or not kclib.SLUG.match(slug) or not isinstance(fp, str) or len(fp) != 64:
        return None
    try:
        int(fp, 16)
    except ValueError:
        return None
    return {"slug": slug, "fp": fp.lower(), "name": str(name or slug)[:kclib.NAME_MAX],
            "supply": supply if isinstance(supply, int) and not isinstance(supply, bool) else None,
            "count": count if isinstance(count, int) and not isinstance(count, bool) else 0,
            "sig": str(sig or "")[:80]}


class CollectionShare:
    kinds = ("kc",)

    def __init__(self, store: Callable[[], Any], clock: Callable[[], float] = time.monotonic):
        self._store = store                     # the arcade's KnownCollections (made when first asked), or None
        self.clock = clock
        self.node = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.peers: dict[str, dict] = {}        # peer -> {net: {"digest", "at", "rows": {slug: row}}}
        self._incoming: dict[tuple, list] = {}  # (peer, net) -> rows of an index still coming
        self._said: dict[str, tuple[dict, float]] = {}   # peer -> (digests said to it, when)
        self._asked: dict[str, list[float]] = {}
        self._bad: dict[tuple, float] = {}      # (peer, net, slug, fp) -> until
        self._ids = itertools.count(1)
        self._fetch: dict | None = None         # the one fetch in flight
        self._serving = None
        self.fetched: list[tuple] = []          # (net, slug, peer, ok) of the last fetches, for the status

    def store(self):
        try:
            return self._store()
        except Exception:                                      # noqa: BLE001
            return None

    def _linked(self) -> dict:
        return dict(getattr(self.node, "links", None) or {})

    # ---------------------------------------------------------------- what this arcade holds
    def digests(self) -> dict:
        store = self.store()
        return {net: store.digest(net) for net in kclib.CHAINS} if store is not None else {}

    def agreement(self, net: str, slug: str, fp: str | None = None) -> int:
        """Arcades agreeing on this collection at `fp` (this arcade's own when not named): this one, if it holds that
        fingerprint, and every linked arcade that said it holds the same."""
        store = self.store()
        mine = store.collection(net, slug) if store is not None else None
        fp = fp or (mine or {}).get("fp")
        if not fp:
            return 0
        n = 1 if mine is not None and mine["fp"] == fp else 0
        now, linked = self.clock(), self._linked()
        for peer, nets in self.peers.items():
            said = nets.get(net)
            if peer in linked and said and now - said["at"] < PEER_TTL and (said["rows"].get(slug) or {}).get("fp") == fp:
                n += 1
        return n

    def agreements(self, net: str) -> dict:
        """{slug: arcades agreeing} for every collection this arcade holds on a chain."""
        store = self.store()
        if store is None:
            return {}
        mine = {r["slug"]: r["fp"] for r in store.index(net)}
        out = {slug: 1 for slug in mine}
        now, linked = self.clock(), self._linked()
        for peer, nets in self.peers.items():
            said = nets.get(net)
            if peer not in linked or not said or now - said["at"] >= PEER_TTL:
                continue
            for slug, fp in mine.items():
                if (said["rows"].get(slug) or {}).get("fp") == fp:
                    out[slug] += 1
        return out

    def wanted(self) -> list[dict]:
        """Collections to fetch from a linked arcade, most agreed first: {net, slug, fp, peers, row}."""
        store = self.store()
        if store is None:
            return []
        now, linked = self.clock(), self._linked()
        out = []
        for net in kclib.CHAINS:
            mine = {r["slug"]: r for r in store.index(net)}
            offers: dict[str, dict[str, list]] = {}
            rows: dict[tuple, dict] = {}
            for peer, nets in self.peers.items():
                said = nets.get(net)
                if peer not in linked or not said or now - said["at"] >= PEER_TTL:
                    continue
                for slug, row in said["rows"].items():
                    offers.setdefault(slug, {}).setdefault(row["fp"], []).append(peer)
                    rows.setdefault((slug, row["fp"]), row)
            for slug, by_fp in offers.items():
                have = mine.get(slug)
                agree = {fp: len(ps) + (1 if have is not None and have["fp"] == fp else 0) for fp, ps in by_fp.items()}
                best = max(sorted(agree), key=lambda fp: agree[fp])
                if have is not None:
                    if have["fp"] == best:
                        continue
                    if agree[best] <= agree.get(have["fp"], 1):
                        continue                               # no more arcades agree on the other: keep ours
                    if have["source"] == "nintondo" and time.time() - have["fetched_at"] < kclib.REFRESH:
                        continue                               # read from the source today: its own word stands
                peers = [p for p in by_fp[best] if self._bad.get((p, net, slug, best), 0) <= now]
                if peers:
                    out.append({"net": net, "slug": slug, "fp": best, "peers": peers, "row": rows[(slug, best)],
                                "agree": agree[best]})
        out.sort(key=lambda w: (-w["agree"], w["net"], w["slug"]))
        return out

    # ---------------------------------------------------------------- hooks
    async def link_up(self, link) -> None:
        self.loop = asyncio.get_running_loop()
        await self._hello(link)

    async def link_down(self, peer_id: str) -> None:
        self._said.pop(peer_id, None)
        if self._fetch is not None and self._fetch["peer"] == peer_id:
            self._fetch = None

    async def rebalance(self) -> None:
        self.loop = asyncio.get_running_loop()
        now = self.clock()
        for peer in [p for p, nets in self.peers.items()
                     if p not in self._linked() and all(now - s["at"] >= PEER_TTL for s in nets.values())]:
            self.peers.pop(peer, None)
        for key in [k for k, until in self._bad.items() if until <= now]:
            self._bad.pop(key, None)
        digests = await asyncio.to_thread(self.digests)
        for peer, link in self._linked().items():
            said, at = self._said.get(peer, ({}, -1e18))
            if said != digests or now - at >= HELLO_EVERY:
                await self._hello(link, digests)
        await self._next_fetch()

    async def on_frame(self, link, header: dict, blob: bytes) -> None:
        op, peer = header.get("op"), link.peer_id
        try:
            if op == "hi":
                await self._heard_hello(link, header)
            elif op == "ix":
                if self._too_many(peer):
                    return
                net = header.get("net")
                if net in kclib.CHAINS:
                    asyncio.ensure_future(self._send_index(link, net))
            elif op == "il":
                await self._index_part(link, header)
            elif op == "ig":
                if self._too_many(peer):
                    await self._send(link, {"op": "ib", "id": header.get("id"), "seq": 0, "end": True,
                                            "err": "busy"})
                    return
                asyncio.ensure_future(self._serve(link, header))
            elif op == "ib":
                await self._items_part(link, header, blob)
        except Exception:                                      # noqa: BLE001 -- not a word this version reads
            log.debug("kc frame", exc_info=True)

    # ---------------------------------------------------------------- digests and indexes
    async def _hello(self, link, digests: dict | None = None) -> None:
        digests = digests if digests is not None else await asyncio.to_thread(self.digests)
        if not digests:
            return
        self._said[link.peer_id] = (digests, self.clock())
        await self._send(link, {"op": "hi", "v": VERSION, "d": digests})

    async def _heard_hello(self, link, header: dict) -> None:
        said = header.get("d")
        if not isinstance(said, dict):
            return
        peer = link.peer_id
        if peer not in self.peers and len(self.peers) >= MAX_PEERS:
            return
        store = self.store()
        mine = await asyncio.to_thread(self.digests)
        nets = self.peers.setdefault(peer, {})
        now = self.clock()
        for net in kclib.CHAINS:
            digest = said.get(net)
            if not isinstance(digest, str) or len(digest) != 64:
                continue
            held = nets.get(net)
            if held is not None and held["digest"] == digest:
                held["at"] = now                               # nothing new: what it said stands
                continue
            if digest == mine.get(net) and store is not None:  # the same as ours: its index is ours, nothing to ask
                rows = {r["slug"]: _row([r["slug"], r["fp"], r["name"], r["supply"], r["count"], r["sig"]])
                        for r in await asyncio.to_thread(store.index, net)}
                nets[net] = {"digest": digest, "at": now, "rows": rows}
                continue
            await self._send(link, {"op": "ix", "net": net})
        if not nets:
            self.peers.pop(peer, None)

    async def _send_index(self, link, net: str) -> None:
        store = self.store()
        if store is None:
            return
        rows = [[r["slug"], r["fp"], r["name"], r["supply"], r["count"], r["sig"]]
                for r in await asyncio.to_thread(store.index, net)]
        parts = [rows[i:i + ROWS_PER_FRAME] for i in range(0, len(rows), ROWS_PER_FRAME)] or [[]]
        for seq, part in enumerate(parts):
            await self._send(link, {"op": "il", "net": net, "seq": seq, "end": seq == len(parts) - 1, "rows": part})

    async def _index_part(self, link, header: dict) -> None:
        net, peer = header.get("net"), link.peer_id
        if net not in kclib.CHAINS or not isinstance(header.get("rows"), list) or not isinstance(header.get("seq"), int):
            return
        key = (peer, net)
        if header["seq"] == 0:
            self._incoming[key] = []
        got = self._incoming.get(key)
        if got is None:
            return
        got.extend(r for r in (_row(x) for x in header["rows"][:ROWS_PER_FRAME]) if r is not None)
        if len(got) > kclib.MAX_COLLECTIONS:
            self._incoming.pop(key, None)
            return
        if not header.get("end"):
            return
        self._incoming.pop(key, None)
        rows = {r["slug"]: r for r in got}
        digest = kclib.chain_digest((s, r["fp"]) for s, r in rows.items())
        if peer not in self.peers and len(self.peers) >= MAX_PEERS:
            return
        self.peers.setdefault(peer, {})[net] = {"digest": digest, "at": self.clock(), "rows": rows}
        await self._next_fetch()

    # ---------------------------------------------------------------- pieces
    async def _serve(self, link, header: dict) -> None:
        rid, net, slug, fp = header.get("id"), header.get("net"), header.get("slug"), header.get("fp")
        if not isinstance(rid, int):
            return
        if self._serving is None:
            self._serving = asyncio.Semaphore(SERVE_AT_ONCE)
        async with self._serving:
            store = self.store()
            held = (await asyncio.to_thread(store.collection, net, slug)
                    if store is not None and net in kclib.CHAINS and isinstance(slug, str) else None)
            if held is None or held["fp"] != fp:
                await self._send(link, {"op": "ib", "id": rid, "seq": 0, "end": True, "err": "gone"})
                return
            data = await asyncio.to_thread(lambda: pack(store.all_items(net, slug)))
            parts = [data[i:i + PART] for i in range(0, len(data), PART)] or [b""]
            for seq, part in enumerate(parts):
                await self._send(link, {"op": "ib", "id": rid, "seq": seq, "end": seq == len(parts) - 1}, part)

    async def _next_fetch(self) -> None:
        """Ask for the next collection wanted, when none is coming."""
        now = self.clock()
        if self._fetch is not None:
            if now - self._fetch["at"] < FETCH_TIMEOUT:
                return
            self._fetch = None
        wanted = await asyncio.to_thread(self.wanted)
        linked = self._linked()
        for w in wanted:
            peer = next((p for p in w["peers"] if p in linked), None)
            if peer is None:
                continue
            rid = next(self._ids)
            self._fetch = {"id": rid, "peer": peer, "net": w["net"], "slug": w["slug"], "fp": w["fp"], "row": w["row"],
                           "parts": [], "size": 0, "at": now}
            await self._send(linked[peer], {"op": "ig", "id": rid, "net": w["net"], "slug": w["slug"], "fp": w["fp"]})
            return

    async def _items_part(self, link, header: dict, blob: bytes) -> None:
        w = self._fetch
        if w is None or header.get("id") != w["id"] or link.peer_id != w["peer"]:
            return
        if header.get("err") == "busy":                        # asked too often: tried again at the next rebalance
            self._fetch = None
            return
        if header.get("err") or header.get("seq") != len(w["parts"]) or w["size"] + len(blob) > MAX_PACKED:
            self._failed(w, str(header.get("err") or "out of order or too large"))
            await self._next_fetch()
            return
        w["parts"].append(blob)
        w["size"] += len(blob)
        if not header.get("end"):
            return
        self._fetch = None
        ok = await asyncio.to_thread(self._keep, w, b"".join(w["parts"]))
        if not ok:
            self._failed(w, "the pieces do not match the fingerprint")
        await self._next_fetch()

    def _keep(self, w: dict, data: bytes) -> bool:
        """The pieces kept when, and only when, their ids hash to the fingerprint the peer announced."""
        rows = unpack(data)
        if rows is None or len(rows) > kclib.MAX_ITEMS:
            return False
        items = kclib.clean_items(rows)
        if len(items) != len(rows):
            return False                                       # a malformed or repeated piece: not what was said
        if kclib.fingerprint(w["net"], w["slug"], (i["id"] for i in items)) != w["fp"]:
            return False
        store = self.store()
        if store is None:
            return False
        row = w["row"]
        try:
            store.put(w["net"], w["slug"], row["name"], row["supply"], items, f"mesh:{w['peer'][:12]}", sig=row["sig"])
        except ValueError as exc:
            log.info("known collections: %s %s from %s not kept: %s", w["net"], w["slug"], w["peer"][:12], exc)
            return False
        self.fetched = (self.fetched + [(w["net"], w["slug"], w["peer"], True)])[-50:]
        log.info("known collections: %s %s (%d pieces) from arcade %s", w["net"], w["slug"], len(items), w["peer"][:12])
        return True

    def _failed(self, w: dict, why: str) -> None:
        self._fetch = None
        self._bad[(w["peer"], w["net"], w["slug"], w["fp"])] = self.clock() + BAD_FOR
        self.fetched = (self.fetched + [(w["net"], w["slug"], w["peer"], False)])[-50:]
        log.info("known collections: %s %s from %s refused: %s", w["net"], w["slug"], w["peer"][:12], why)

    # ---------------------------------------------------------------- sending
    def _too_many(self, peer: str) -> bool:
        now = self.clock()
        asked = [t for t in self._asked.get(peer, []) if t > now - 60]
        self._asked[peer] = asked + [now]
        return len(asked) >= ANSWERS_PER_MINUTE

    async def _send(self, link, frame: dict, blob: bytes = b"") -> None:
        try:
            if blob:
                await link.send({"t": "kc", **frame}, blob)
            else:
                await link.send({"t": "kc", **frame})
        except Exception:                                      # noqa: BLE001 -- the link went
            pass
