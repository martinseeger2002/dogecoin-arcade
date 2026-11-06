"""The Wallet's Doginals one page at a time (2026-11-06, the operator: "build it", for #91's "load a big wallet's Doginals
one page at a time").

Before this the browser did all of the work: it fetched every coin on the address, asked about every small coin's
transaction a hundred at a time (/account/doginal-txs, which read all of the address's coins again on every call), then
asked for every inscription's number and collection 500 at a time (/account/doginal-meta), and only then sorted and drew
page 1. A wallet of about 7,100 coins was some 85 calls before anything showed.

Now the arcade builds the address's list once, on a thread of its own, and the browser asks for one page:

- the coins are read once, and only the small ones (Doginal postage) are looked at;
- what sits on each one is the inscription guard's own answer (doginals.InscriptionGuard.held, the same answer that
  keeps an inscribed coin out of an ordinary send, kept by the guard for good);
- each inscription's number, block, content type, size and protocol are asked of the most complete ord index
  (`numbers`, 500 at a time) and kept here for good (`Numbers`): a number never changes;
- the collections are looked up for all of them at once, one query per few hundred, in both stores;
- the sorted list is kept for FRESH_FOR seconds and built again behind the page after that (the old one is served
  meanwhile), or at once when the Wallet says it has just sent one.

A build that is still going is served as it stands: `done` false, and how far it got. The browser asks again.
"""
from __future__ import annotations

import contextlib
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

#: Coins above this many satoshis are never looked at: they are not Doginal postage (doginals.POSTAGE_MAX).
POSTAGE = 1_000_000
#: A page holds at most this many.
PAGE_MAX = 96
PAGE_DEFAULT = 30
#: An address's list is built again when it is older than this (a block on Dogecoin is about a minute).
FRESH_FOR = 60.0
#: Addresses kept at once (the least recently asked is dropped first).
KEEP = 64
#: Ids asked of the index at once (at most doginal_index.NUMBERS_MAX). The answer comes back over a mesh link, one frame of
#: at most 64 KB (mesh/link.MAX_FRAME), about 190 bytes an id: 500 was 94 KB, never delivered, so .25 waited 15 s a
#: question and then read its own quarter-walked copy off a busy disk (2026-11-06). 200 is about 38 KB.
NUMBERS_AT_ONCE = 200
#: Transactions the guard reads side by side while a list is built for the first time.
GUARD_THREADS = 4
#: How often a build in progress is published to the pages (transactions checked).
PUBLISH_EVERY = 250

#: Not pictures or pages (2026-10-22: "filter out txt json and js meme types"): shown under Text.
NOT_ART = re.compile(r"^\s*(text/plain|application/json|text/javascript|application/(x-)?javascript)\b", re.I)

KINDS = ("all", "art", "text")


class Numbers:
    """Each inscription's number, block, content type, size and protocol, kept for good (they never change)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS num (net TEXT NOT NULL, id TEXT NOT NULL, number INTEGER NOT NULL, "
                       "height INTEGER, ctype TEXT, size INTEGER, proto TEXT, PRIMARY KEY (net, id))")

    @contextlib.contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, net: str, ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        with self._db() as db:
            for at in range(0, len(ids), 500):
                part = ids[at:at + 500]
                for iid, number, height, ctype, size, proto in db.execute(
                        f"SELECT id, number, height, ctype, size, proto FROM num WHERE net=? AND id IN "
                        f"({','.join('?' * len(part))})", (net, *part)):
                    out[iid] = {"number": number, "height": height, "content_type": ctype, "size": size,
                                "protocol": proto}
        return out

    def put(self, net: str, rows: dict[str, dict]) -> None:
        keep = [(net, iid, int(r["number"]), r.get("height"), r.get("content_type"), r.get("size"), r.get("protocol"))
                for iid, r in rows.items() if isinstance(r, dict) and r.get("number") is not None]
        if not keep:
            return
        with self._lock, self._db() as db:
            db.executemany("INSERT OR REPLACE INTO num VALUES (?,?,?,?,?,?,?)", keep)


class Source:
    """What one address's list is built from (the app supplies these; tests stand in for them)."""

    def __init__(self, coins: Callable[[], list[dict]], guard: Any, numbers: Callable[[list[str]], dict],
                 collections: Callable[[list[str]], dict], token_protocol: str = "", reach: Callable[[], Any] = None):
        self.coins = coins                  # () -> [{txid, vout, value}], the address's coins now
        self.guard = guard                  # .held(txid) -> {vout: [{id, offset, unknown, wait, h, ctype}]}; .ctype_of
        self.numbers = numbers              # (ids) -> {id: {number, height, content_type, size, protocol}}
        self.collections = collections      # (ids) -> {id: {name, item, position, slug | root, source}}
        self.token_protocol = token_protocol
        self.reach = reach or (lambda: None)
        self.ctypes: dict[str, str | None] = {}     # kept types read this build (`_items`)


class Pages:
    """The lists, one per (chain, address), each built on a thread of its own."""

    def __init__(self, numbers: Numbers, unknown: type[BaseException] = Exception):
        self.numbers = numbers
        self.unknown = unknown              # what the guard raises when it cannot say just now (doginals.Unknown)
        self._lists: OrderedDict = OrderedDict()
        self._building: set = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ the list
    def get(self, key: tuple, source: Source, fresh: bool = False, wait: float = 1.5,
            now: Callable[[], float] = time.time) -> dict:
        """The list for `key` as it stands: built if there is none, built again behind it when the address's coins are
        not the ones it was built from (a send, a coin received), when it is old, or when `fresh`; waiting `wait`
        seconds at most for a build that has just begun. The coins are read here, every time: one indexed read."""
        coins = [c for c in source.coins() if int(c.get("value") or 0) <= POSTAGE]
        sig = coins_sig(coins)
        with self._lock:
            held = self._lists.get(key)
            if held is not None:
                self._lists.move_to_end(key)
            changed = held is not None and held.get("sig") != sig
            stale = held is None or fresh or changed or (held["done"] and now() - held["at"] > FRESH_FOR)
            start = stale and key not in self._building
            if start:
                self._building.add(key)
        if start:
            done = threading.Event()
            threading.Thread(target=self._build, args=(key, source, coins, done, now), daemon=True,
                             name=f"doginal-pages-{key[0]}").start()
            if held is None or fresh or changed:
                done.wait(wait)
        with self._lock:
            got = self._lists.get(key) or {"done": False, "phase": "coins", "items": [], "checked": 0, "of": 0,
                                           "failed": 0, "unread": 0, "waiting": 0, "unnumbered": 0, "at": now(),
                                           "error": ""}
            return {**got, "building": key in self._building}

    def forget(self, key: tuple) -> None:
        with self._lock:
            self._lists.pop(key, None)

    def _publish(self, key: tuple, doc: dict) -> None:
        with self._lock:
            self._lists[key] = doc
            self._lists.move_to_end(key)
            while len(self._lists) > KEEP:
                self._lists.popitem(last=False)

    def _build(self, key: tuple, source: Source, coins: list[dict], done: threading.Event,
               now: Callable[[], float]) -> None:
        net = key[0]
        with self._lock:
            had = self._lists.get(key)

        def partial(doc: dict) -> None:
            # A list built before stays shown while the new one is built behind it: steps are shown only for a first.
            if had is None or not had.get("done"):
                self._publish(key, doc)
        try:
            self._publish(key, self.build(net, source, partial, now, coins=coins))
        except Exception as exc:                          # noqa: BLE001 -- said on the page, built again next time
            with self._lock:
                old = self._lists.get(key)
            self._publish(key, {**(old or {"items": [], "checked": 0, "of": 0, "failed": 0, "unread": 0,
                                           "waiting": 0, "unnumbered": 0}),
                                "done": True, "phase": "error", "at": now(), "error": str(exc)[:200]})
        finally:
            with self._lock:
                self._building.discard(key)
            done.set()

    def build(self, net: str, source: Source, publish: Callable[[dict], None] = lambda doc: None,
              now: Callable[[], float] = time.time, coins: list[dict] | None = None) -> dict:
        """The whole list for one address: [{id, txid, vout, value, offset, number, ...}] in number order."""
        if coins is None:
            coins = [c for c in source.coins() if int(c.get("value") or 0) <= POSTAGE]
        txids = list(dict.fromkeys(c["txid"] for c in coins))
        by_tx: dict[str, list[dict]] = {}
        for c in coins:
            by_tx.setdefault(c["txid"], []).append(c)
        held: dict[str, dict | None] = {}
        nums: dict[str, dict] = {}
        asked: set[str] = set()
        waiting_for: list[str] = []

        def one(txid: str):
            try:
                return txid, source.guard.held(txid)
            except self.unknown:
                return txid, None

        def number(items: list[dict], last: bool = False) -> None:
            # Numbers as the check goes, not only at the end, so even a list still being read on a slow node is in
            # number order (2026-11-06: "still not showing my lowest number inscriptions first"): the cache
            # at once, the index for the rest in full questions of 500, what is left over at the end.
            ids = [g["id"] for g in items if g["id"] not in asked]
            asked.update(ids)
            nums.update(self.numbers.get(net, ids))
            waiting_for.extend(i for i in ids if i not in nums)
            while waiting_for and (last or len(waiting_for) >= NUMBERS_AT_ONCE) and not doc["error"]:
                part = waiting_for[:NUMBERS_AT_ONCE]
                del waiting_for[:NUMBERS_AT_ONCE]
                try:
                    got = source.numbers(part) or {}
                except Exception as exc:                  # noqa: BLE001 -- no copy could say just now: asked next time
                    doc["error"] = f"the index could not say just now: {exc}"[:200]
                    return
                got = {str(k).strip().lower(): v for k, v in got.items()
                       if isinstance(v, dict) and v.get("number") is not None}
                self.numbers.put(net, got)
                nums.update(got)

        doc = {"done": False, "phase": "checking", "sig": coins_sig(coins), "items": [], "checked": 0, "of": len(txids), "failed": 0,
               "unread": 0, "waiting": 0, "unnumbered": 0, "at": now(), "error": "", "reach": source.reach()}
        publish(dict(doc))
        # What the guard already knows, read in one go (2026-11-06: "the wallet takes too long to load the
        # initial load"): one connection a transaction under the guard's lock was minutes on .25's disk for a big
        # wallet. Only the never-asked, and those still waiting to be read, go to the guard one by one.
        try:
            kept = source.guard.kept_many(txids) if hasattr(source.guard, "kept_many") else {}
        except Exception:                                 # noqa: BLE001 -- asked one by one instead
            kept = {}
        for txid, said in (kept or {}).items():
            if not any(i.get("unknown") for on in said.values() for i in on):
                held[txid] = said
        rest = [t for t in txids if t not in held]
        if held:
            so_far = self._items(net, coins, held, {}, {}, source)[0]
            number(so_far)
            publish({**doc, "checked": len(held), "items": self._items(net, coins, held, nums, {}, source)[0]})
        with ThreadPoolExecutor(GUARD_THREADS) as pool:
            for n, (txid, said) in enumerate(pool.map(one, rest), len(held) + 1):
                held[txid] = said
                if n % PUBLISH_EVERY == 0:
                    so_far = self._items(net, coins, held, {}, {}, source)[0]
                    number(so_far)
                    publish({**doc, "checked": n, "items": self._items(net, coins, held, nums, {}, source)[0]})
        doc["checked"] = len(txids)
        items, unread, waiting, failed = self._items(net, coins, held, nums, {}, source)
        doc.update(items=items, unread=unread, waiting=waiting, failed=failed, phase="numbers")
        number(items, last=True)
        items = self._items(net, coins, held, nums, {}, source)[0]
        doc["items"] = items
        publish(dict(doc))
        ids = [g["id"] for g in items]
        doc["phase"] = "collections"
        try:
            colls = source.collections(ids) or {}
        except Exception:                                 # noqa: BLE001 -- shown without them, built again next time
            colls = {}
        items = self._items(net, coins, held, nums, colls, source)[0]
        doc.update(items=items, phase="done", done=True, at=now(),
                   unnumbered=sum(1 for g in items if g["number"] is None))
        return doc

    def _items(self, net: str, coins: list[dict], held: dict, nums: dict, colls: dict, source: Source):
        """The inscriptions on these coins, in number order (those without a number yet last), and the counts of small
        coins that could not be read (`unread`), wait for the index (`waiting`) or whose transaction failed."""
        items, unread, waiting, failed = [], 0, 0, set()
        token_p = (source.token_protocol or "").lower()
        for c in coins:
            on = held.get(c["txid"], "?")
            if on == "?":
                continue                                  # not checked yet
            if on is None:
                failed.add(c["txid"])
                continue
            for i in on.get(int(c["vout"])) or on.get(str(c["vout"])) or []:
                if i.get("unknown") or not i.get("id"):
                    unread += 1
                    waiting += 1 if i.get("wait") else 0
                    continue
                iid = str(i["id"]).strip().lower()
                m = nums.get(iid) or {}
                ctype = m.get("content_type") or i.get("ctype")
                if not ctype:
                    # Only what is kept on disk, once a build: reading the content from the chain here was a whole
                    # inscription fetched for every coin the index had not numbered yet, again at every step (the
                    # stall watch caught the build in it on .25, 2026-11-06). The index's number brings the type.
                    if iid not in source.ctypes:
                        try:
                            source.ctypes[iid] = source.guard.ctype_of(iid, read=False)
                        except TypeError:                 # a guard without `read`
                            source.ctypes[iid] = None
                        except Exception:                 # noqa: BLE001
                            source.ctypes[iid] = None
                    ctype = source.ctypes[iid]
                proto = m.get("protocol")
                items.append({"id": iid, "txid": c["txid"], "vout": int(c["vout"]), "value": int(c.get("value") or 0),
                              "offset": i.get("offset"), "number": m.get("number"), "height": m.get("height"),
                              "content_type": ctype, "size": m.get("size"),
                              "text": bool(ctype and NOT_ART.match(ctype)),
                              "token": bool(proto) and str(proto).lower() == token_p and bool(token_p),
                              "collection": colls.get(iid)})
        items.sort(key=lambda g: (g["number"] is None, g["number"] if g["number"] is not None else 0, g["id"]))
        return items, unread, waiting, len(failed)


def coins_sig(coins: list[dict]) -> str:
    """Which coins a list was built from: it is built again when they change."""
    import hashlib
    return hashlib.sha256("\n".join(sorted(f"{c['txid']}:{int(c['vout'])}" for c in coins)).encode()).hexdigest()


def coll_key(k: dict | None) -> str:
    return str(k.get("slug") or k.get("root") or k.get("name") or "") if k else ""


def view(doc: dict, kind: str = "all", coll: str = "", order: str = "old", page: int = 0, size: int = PAGE_DEFAULT,
         go: str = "") -> dict:
    """One page of a list, with everything the bar and the status lines show: the counts of each kind, the collections
    with how many of each, how many have no collection, how far the build is, and the page holding `go` (#number or
    id) when it is asked for."""
    kind = kind if kind in KINDS else "all"
    size = max(1, min(PAGE_MAX, int(size or PAGE_DEFAULT)))
    items = doc.get("items") or []
    tokens = sum(1 for g in items if g["token"])
    found = [g for g in items if not g["token"]]
    arts = sum(1 for g in found if not g["text"])
    colls: dict[str, dict] = {}
    loose = 0
    for g in found:
        k = g.get("collection")
        if not k:
            loose += 1 if g["number"] is not None else 0
            continue
        key = coll_key(k)
        held = colls.setdefault(key, {"key": key, "name": k.get("name") or key, "n": 0})
        held["n"] += 1
    shown = [g for g in found if kind == "all" or (kind == "text") == g["text"]]
    if coll == "-":
        shown = [g for g in shown if g["number"] is not None and not g.get("collection")]
    elif coll:
        shown = [g for g in shown if coll_key(g.get("collection")) == coll]
    if order == "new":
        numbered = [g for g in shown if g["number"] is not None]
        shown = numbered[::-1] + [g for g in shown if g["number"] is None]
    pages = max(1, -(-len(shown) // size))
    focus = None
    missed = False
    q = str(go or "").strip().lower().lstrip("#").replace(",", "")
    if q:
        at = next((n for n, g in enumerate(shown)
                   if g["id"] == q or (q.lstrip("-").isdigit() and g["number"] is not None and str(g["number"]) == q)),
                  None)
        if at is None:
            missed = True
        else:
            page, focus = at // size, shown[at]["id"]
    page = min(max(0, int(page or 0)), pages - 1)
    return {"done": bool(doc.get("done")), "building": bool(doc.get("building")), "phase": doc.get("phase"),
            "checked": doc.get("checked", 0), "of": doc.get("of", 0), "failed": doc.get("failed", 0),
            "unread": doc.get("unread", 0), "waiting": doc.get("waiting", 0), "reach": doc.get("reach"),
            "unnumbered": sum(1 for g in found if g["number"] is None), "error": doc.get("error") or "",
            "counts": {"all": len(found), "art": arts, "text": len(found) - arts, "tokens": tokens},
            "collections": sorted(colls.values(), key=lambda c: c["name"].lower()), "loose": loose,
            "view": {"kind": kind, "coll": coll, "order": "new" if order == "new" else "old"},
            "page": page, "pages": pages, "size": size, "total": len(shown), "focus": focus, "missed": missed,
            "items": shown[page * size:(page + 1) * size]}
