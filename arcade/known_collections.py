"""Known collections of Doginals, Pepinals and Bellinals: which inscriptions make up each one, as Nintondo's public
search lists them, kept here and shared between arcades over the mesh (mesh/collectionshare.py).

2026-10-26, SadFrog: "I'd like to have pepinals, doginals and bellinals collections pulled from known collections and
searchable from our indexes and arcades - the known collections should be shared across arcades to be agreed upon."
Shared how: "over the mesh". Each arcade holds the lists, arcades compare a fingerprint of each collection, and Explore
says "agreed by N arcades". An arcade may take a list from a linked arcade, checked against its fingerprint, instead of
asking the source.

Importing from the source is the operator's own choice (Admin -> Ords, setting `known_collections_import`, off by
default): the operator's side stopped reading ords from outside APIs on 2026-10-15, so nothing here asks Nintondo unless
an operator switched it on. An arcade with it off still holds what linked arcades share.

What a list is worth: a fingerprint covers the inscription ids alone, so two arcades that agree hold the same pieces.
The names ("Name #k") and positions travel with them and are shown, never checked.

The fingerprint (every arcade must work it out exactly so):

    fp(chain, slug, ids) = sha256( chain "\\n" slug "\\n" + "\\n".join(sorted(ids)) ).hexdigest()

`chain` is the arcade's own name of the chain ("doge", "main", "bel"), `slug` Nintondo's collection slug, each id a
lowercase "<txid>i<n>", sorted as plain strings, utf-8. A chain's digest covers all its collections:

    digest(chain) = sha256( "\\n".join(sorted(slug + " " + fp for every collection held)) ).hexdigest()
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger("arcade.known_collections")

#: The arcade's chains with a Nintondo search host: Doginals, Pepinals, Bellinals.
SOURCES = {
    "doge": "https://doge-mainnet-search.nintondo.io",
    "main": "https://pepe-mainnet-search.nintondo.io",
    "bel": "https://bells-mainnet-search.nintondo.io",
}
CHAINS = tuple(SOURCES)
SETTING = "known_collections_import"

ID = re.compile(r"^[0-9a-f]{64}i[0-9]{1,6}$")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
POSITION = re.compile(r"#\s*([0-9]{1,9})\s*$")

MAX_ITEMS = 100_000          # pieces of one collection, at most (Dogecoin's biggest, "bone", has 57,951)
MAX_TOTAL = 1_500_000        # pieces of every collection together (Dogecoin's 278 hold about 700,000)
MAX_COLLECTIONS = 5_000      # collections of one chain
NAME_MAX = 120

PACE = 1.0                   # seconds between two requests to one host, at least
PAGE_SIZE = 100              # the search's most a page (500 is refused with a 422)
LIST_PAGE_SIZE = 50
REFRESH = 24 * 3600          # a chain's listing read again once a day
BACKOFF_MAX = 3600.0
USER_AGENT = "DogecoinArcade known-collections (+https://github.com/dogecoinarcade)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS kc (
    chain      TEXT NOT NULL,
    slug       TEXT NOT NULL,
    name       TEXT NOT NULL,
    supply     INTEGER,
    cover      TEXT NOT NULL DEFAULT '',     -- the first piece's inscription id
    count      INTEGER NOT NULL,             -- pieces held here
    fp         TEXT NOT NULL,                -- fingerprint() of the pieces' ids
    source     TEXT NOT NULL,                -- "nintondo", or "mesh:<arcade id>"
    sig        TEXT NOT NULL DEFAULT '',     -- the source's listing line when fetched (supply|first|last|size)
    fetched_at REAL NOT NULL,
    PRIMARY KEY (chain, slug)
);
CREATE TABLE IF NOT EXISTS kc_item (
    chain      TEXT NOT NULL,
    slug       TEXT NOT NULL,
    id         TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',     -- "Name #k"
    position   INTEGER,                      -- k
    number     INTEGER,                      -- its inscription number, as the source said
    PRIMARY KEY (chain, slug, id)
);
CREATE INDEX IF NOT EXISTS kc_item_id ON kc_item (chain, id);
CREATE INDEX IF NOT EXISTS kc_item_order ON kc_item (chain, slug, position, number);
-- the importer's own progress, so a restart goes on where it stopped
CREATE TABLE IF NOT EXISTS kc_listing (
    chain TEXT NOT NULL, slug TEXT NOT NULL, name TEXT NOT NULL, supply INTEGER, sig TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (chain, slug));
CREATE TABLE IF NOT EXISTS kc_pending (
    chain TEXT NOT NULL, slug TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, position INTEGER, number INTEGER,
    PRIMARY KEY (chain, slug, id));
CREATE TABLE IF NOT EXISTS kc_progress (
    chain TEXT PRIMARY KEY, phase TEXT NOT NULL, page INTEGER NOT NULL DEFAULT 1, slug TEXT, pages INTEGER,
    count INTEGER, round_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
"""


def fingerprint(chain: str, slug: str, ids: Iterable[str]) -> str:
    """A collection's fingerprint: the same pieces on the same chain under the same slug, the same hash, in any order."""
    body = f"{chain}\n{slug}\n" + "\n".join(sorted(ids))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def chain_digest(rows: Iterable[tuple[str, str]]) -> str:
    """One chain's collections in one hash: (slug, fp) pairs, in any order."""
    return hashlib.sha256("\n".join(sorted(f"{s} {f}" for s, f in rows)).encode("utf-8")).hexdigest()


def position_of(name: str) -> int | None:
    m = POSITION.search(name or "")
    return int(m.group(1)) if m else None


def clean_items(items: Iterable[Any]) -> list[dict]:
    """Pieces as {id, name, position, number}: ids shaped like inscription ids, each once; anything else left out."""
    out, seen = [], set()
    for it in items:
        if isinstance(it, (list, tuple)):
            it = dict(zip(("id", "name", "position", "number"), it))
        if not isinstance(it, dict):
            continue
        iid = str(it.get("id") or "").lower()
        if not ID.match(iid) or iid in seen:
            continue
        seen.add(iid)
        name = str(it.get("name") or "")[:NAME_MAX]
        pos, num = it.get("position"), it.get("number")
        out.append({"id": iid, "name": name,
                    "position": pos if isinstance(pos, int) and not isinstance(pos, bool) else position_of(name),
                    "number": num if isinstance(num, int) and not isinstance(num, bool) else None})
    return out


class KnownCollections:
    """The store: collections and their pieces, by chain."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        with self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextlib.contextmanager
    def _db(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---------------------------------------------------------------- writing
    def put(self, chain: str, slug: str, name: str, supply: int | None, items: Iterable[Any], source: str,
            sig: str = "", now: float | None = None) -> str:
        """Keep a collection's whole list (in place of what was held); its fingerprint. Refused past the bounds."""
        if chain not in CHAINS or not SLUG.match(slug or ""):
            raise ValueError("not a known chain and slug")
        rows = clean_items(items)
        if len(rows) > MAX_ITEMS:
            raise ValueError(f"{len(rows)} pieces, more than {MAX_ITEMS:,}")
        fp = fingerprint(chain, slug, (r["id"] for r in rows))
        ordered = sorted(rows, key=lambda r: (r["position"] is None, r["position"] or 0, r["number"] or 0, r["id"]))
        with self._lock, self._db() as conn:
            held = conn.execute("SELECT COUNT(*) FROM kc_item WHERE NOT (chain=? AND slug=?)", (chain, slug)).fetchone()[0]
            if held + len(rows) > MAX_TOTAL:
                raise ValueError(f"more than {MAX_TOTAL:,} pieces in all")
            if conn.execute("SELECT COUNT(*) FROM kc WHERE chain=? AND slug<>?", (chain, slug)).fetchone()[0] \
                    >= MAX_COLLECTIONS:
                raise ValueError(f"more than {MAX_COLLECTIONS:,} collections on {chain}")
            conn.execute("DELETE FROM kc_item WHERE chain=? AND slug=?", (chain, slug))
            conn.executemany("INSERT INTO kc_item (chain, slug, id, name, position, number) VALUES (?,?,?,?,?,?)",
                             [(chain, slug, r["id"], r["name"], r["position"], r["number"]) for r in ordered])
            conn.execute("INSERT OR REPLACE INTO kc (chain, slug, name, supply, cover, count, fp, source, sig, "
                         "fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (chain, slug, str(name or slug)[:NAME_MAX], supply if isinstance(supply, int) else None,
                          ordered[0]["id"] if ordered else "", len(rows), fp, str(source)[:80], str(sig)[:80],
                          time.time() if now is None else now))
        return fp

    # ---------------------------------------------------------------- reading
    def collection(self, chain: str, slug: str) -> dict | None:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM kc WHERE chain=? AND slug=?", (chain, slug)).fetchone()
        return dict(row) if row else None

    def collections(self, chain: str, q: str = "", limit: int = 60) -> list[dict]:
        """A chain's collections whose name or slug holds `q` (any case), biggest first."""
        like = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM kc WHERE chain=? AND (lower(name) LIKE ? ESCAPE '\\' OR "
                                "lower(slug) LIKE ? ESCAPE '\\') ORDER BY count DESC, slug LIMIT ?",
                                (chain, like, like, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def items(self, chain: str, slug: str, offset: int = 0, limit: int = 48) -> list[dict]:
        with self._db() as conn:
            rows = conn.execute("SELECT id, name, position, number FROM kc_item WHERE chain=? AND slug=? "
                                "ORDER BY position IS NULL, position, number, id LIMIT ? OFFSET ?",
                                (chain, slug, int(limit), int(offset))).fetchall()
        return [dict(r) for r in rows]

    def all_items(self, chain: str, slug: str) -> list[list]:
        """Every piece as [id, name, position, number], in order: what a linked arcade is sent."""
        with self._db() as conn:
            rows = conn.execute("SELECT id, name, position, number FROM kc_item WHERE chain=? AND slug=? "
                                "ORDER BY position IS NULL, position, number, id", (chain, slug)).fetchall()
        return [list(r) for r in rows]

    def ids(self, chain: str, slug: str) -> list[str]:
        with self._db() as conn:
            return [r[0] for r in conn.execute("SELECT id FROM kc_item WHERE chain=? AND slug=? ORDER BY id",
                                               (chain, slug))]

    def in_collection(self, chain: str, iid: str) -> dict | None:
        """Which known collection holds this inscription: {slug, collection, name, position, source}, or None."""
        with self._db() as conn:
            row = conn.execute("SELECT i.slug, i.name, i.position, k.name AS collection, k.source FROM kc_item i "
                               "JOIN kc k ON k.chain=i.chain AND k.slug=i.slug WHERE i.chain=? AND i.id=? "
                               "ORDER BY k.count LIMIT 1", (chain, iid)).fetchone()
        return dict(row) if row else None

    def index(self, chain: str) -> list[dict]:
        """A chain's collections as linked arcades are told them: slug, fp, name, supply, count, sig."""
        with self._db() as conn:
            rows = conn.execute("SELECT slug, fp, name, supply, count, sig, source, fetched_at FROM kc WHERE chain=? "
                                "ORDER BY slug", (chain,)).fetchall()
        return [dict(r) for r in rows]

    def digest(self, chain: str) -> str:
        with self._db() as conn:
            return chain_digest((r[0], r[1]) for r in conn.execute("SELECT slug, fp FROM kc WHERE chain=?", (chain,)))

    def counts(self) -> dict:
        with self._db() as conn:
            return {r[0]: {"collections": r[1], "pieces": r[2] or 0}
                    for r in conn.execute("SELECT chain, COUNT(*), SUM(count) FROM kc GROUP BY chain")}


# --------------------------------------------------------------------------------------------- the importer

def fetch_json(method: str, url: str, body: dict | None = None, timeout: float = 30.0) -> dict:
    """One request to the source: a plain User-Agent and json both ways (it refuses urllib's own in some cases)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:            # noqa: S310 -- fixed https hosts only
        raw = r.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("answer too large")
    got = json.loads(raw.decode("utf-8"))
    if not isinstance(got, dict):
        raise ValueError("not a json object")
    return got


def _sig(c: dict) -> str:
    nums = c.get("numbers") if isinstance(c.get("numbers"), list) else []
    first, last = (nums + [None, None])[:2]
    return f"{c.get('supply')}|{first}|{last}|{c.get('size')}"


class Importer:
    """Nintondo's lists of each chain, a request at a time: the listing (50 collections a page), then each collection
    new or changed since (100 pieces a page). At most one request a second to each host; every page's progress kept, so
    a restart goes on from it; once a chain is through, it rests a day and reads its listing again, fetching only the
    collections whose line in it (supply, first and last number, size) changed."""

    def __init__(self, store: KnownCollections, enabled: Callable[[], bool], fetch: Callable[..., dict] = fetch_json,
                 clock: Callable[[], float] = time.monotonic, now: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, sources: dict | None = None):
        self.store, self.enabled, self.fetch = store, enabled, fetch
        self.clock, self.now, self.sleep = clock, now, sleep
        self.sources = dict(sources or SOURCES)
        self._last: dict[str, float] = {}         # host -> when it was last asked (clock)
        self._backoff: dict[str, tuple[float, int]] = {}   # host -> (not before, failures in a row)
        self.requests = 0

    # ---------------------------------------------------------------- progress
    def _progress(self, chain: str) -> dict:
        with self.store._db() as conn:
            row = conn.execute("SELECT * FROM kc_progress WHERE chain=?", (chain,)).fetchone()
        return dict(row) if row else {"chain": chain, "phase": "list", "page": 1, "slug": None, "pages": None,
                                      "count": None, "round_at": 0.0, "error": ""}

    def _save(self, st: dict) -> None:
        with self.store._db() as conn:
            conn.execute("INSERT OR REPLACE INTO kc_progress (chain, phase, page, slug, pages, count, round_at, error) "
                         "VALUES (?,?,?,?,?,?,?,?)", (st["chain"], st["phase"], st["page"], st["slug"], st["pages"],
                                                      st["count"], st["round_at"], st["error"][:300]))

    # ---------------------------------------------------------------- one step
    def step(self) -> float:
        """At most one request; the seconds until the next one may be made."""
        if not self.enabled():
            return 30.0
        waits = []
        for chain in self.sources:
            for _ in range(MAX_COLLECTIONS + 2):          # steps that ask nothing (a collection unchanged) go on at once
                st = self._progress(chain)
                if st["phase"] == "rest":
                    left = st["round_at"] + REFRESH - self.now()
                    if left > 0:
                        waits.append(left)
                        break
                    st.update(phase="list", page=1, slug=None, pages=None, count=None)
                    with self.store._db() as conn:
                        conn.execute("DELETE FROM kc_listing WHERE chain=?", (chain,))
                    self._save(st)
                host = self.sources[chain]
                t = self.clock()
                until, _fails = self._backoff.get(host, (0.0, 0))
                wait = max(until - t, self._last.get(host, -1e18) + PACE - t)
                if wait > 0:
                    waits.append(wait)
                    break
                try:
                    asked = self._one(chain, st, host)
                except Exception as exc:                     # noqa: BLE001 -- said in the status, tried again later
                    fails = _fails + 1
                    self._backoff[host] = (self.clock() + min(BACKOFF_MAX, 30.0 * 2 ** (fails - 1)), fails)
                    st["error"] = f"{type(exc).__name__}: {exc}"
                    self._save(st)
                    log.info("known collections: %s: %s", chain, st["error"])
                    asked = True
                else:
                    self._backoff.pop(host, None)
                if asked:
                    return 0.0
        return max(0.05, min(waits)) if waits else 30.0

    def _ask(self, host: str, method: str, path: str, body: dict | None = None) -> dict:
        self._last[host] = self.clock()
        self.requests += 1
        return self.fetch(method, host + path, body)

    def _one(self, chain: str, st: dict, host: str) -> bool:
        """The next thing to do on this chain; True when a request was made."""
        if st["phase"] == "list":
            got = self._ask(host, "GET", f"/pub/collections?page_size={LIST_PAGE_SIZE}&page={int(st['page'])}")
            rows = []
            for c in got.get("collections") or []:
                if not isinstance(c, dict) or not SLUG.match(str(c.get("slug") or "")):
                    continue
                supply = c.get("supply") if isinstance(c.get("supply"), int) else None
                rows.append((chain, c["slug"], str(c.get("name") or c["slug"])[:NAME_MAX], supply, _sig(c)))
            with self.store._db() as conn:
                conn.executemany("INSERT OR REPLACE INTO kc_listing (chain, slug, name, supply, sig, done) "
                                 "VALUES (?,?,?,?,?,0)", rows[:MAX_COLLECTIONS])
            pages = int(got.get("pages") or 0)
            if not rows or st["page"] >= pages or st["page"] >= MAX_COLLECTIONS // LIST_PAGE_SIZE:
                st.update(phase="items", page=1, slug=None, pages=None, count=None)
            else:
                st["page"] += 1
            st["error"] = ""
            self._save(st)
            return True
        # phase "items": the collection being fetched, or the next one new or changed
        if not st["slug"]:
            with self.store._db() as conn:
                row = conn.execute("SELECT l.slug, l.supply FROM kc_listing l LEFT JOIN kc k ON k.chain=l.chain AND "
                                   "k.slug=l.slug WHERE l.chain=? AND l.done=0 AND (k.sig IS NULL OR k.sig<>l.sig) "
                                   "ORDER BY l.slug LIMIT 1", (chain,)).fetchone()
            if row is None:
                st.update(phase="rest", round_at=self.now(), slug=None, page=1, error="")
                self._save(st)
                return False
            if (row["supply"] or 0) > MAX_ITEMS:
                self._done(chain, row["slug"], f"{row['slug']}: {row['supply']:,} pieces, more than {MAX_ITEMS:,}")
                return False
            with self.store._db() as conn:
                conn.execute("DELETE FROM kc_pending WHERE chain=? AND slug=?", (chain, row["slug"]))
            st.update(slug=row["slug"], page=1, pages=None, count=None)
            self._save(st)
        slug = st["slug"]
        got = self._ask(host, "POST", "/pub/search", {"collection": slug, "page": int(st["page"]),
                                                      "page_size": PAGE_SIZE})
        found = []
        for x in got.get("inscriptions") or []:
            if not isinstance(x, dict):
                continue
            meta = ((x.get("collection") or {}).get("meta") or {}) if isinstance(x.get("collection"), dict) else {}
            name = str(meta.get("name") or "") if isinstance(meta, dict) else ""
            found.append({"id": x.get("id"), "name": name, "number": x.get("number")})
        found = clean_items(found)
        with self.store._db() as conn:
            conn.executemany("INSERT OR IGNORE INTO kc_pending (chain, slug, id, name, position, number) "
                             "VALUES (?,?,?,?,?,?)",
                             [(chain, slug, r["id"], r["name"], r["position"], r["number"]) for r in found])
            held = conn.execute("SELECT COUNT(*) FROM kc_pending WHERE chain=? AND slug=?", (chain, slug)).fetchone()[0]
        pages, count = int(got.get("pages") or 0), got.get("count")
        st.update(pages=pages, count=count if isinstance(count, int) else st["count"], error="")
        if held > MAX_ITEMS:
            self._done(chain, slug, f"{slug}: more than {MAX_ITEMS:,} pieces")
            st.update(slug=None, page=1)
            self._save(st)
            return True
        if found and st["page"] < pages:
            st["page"] += 1
            self._save(st)
            return True
        self._finish(chain, slug, st)
        return True

    def _finish(self, chain: str, slug: str, st: dict) -> None:
        with self.store._db() as conn:
            items = [dict(r) for r in conn.execute("SELECT id, name, position, number FROM kc_pending WHERE chain=? "
                                                   "AND slug=?", (chain, slug))]
            line = conn.execute("SELECT name, supply, sig FROM kc_listing WHERE chain=? AND slug=?",
                                (chain, slug)).fetchone()
        name, supply, sig = (line["name"], line["supply"], line["sig"]) if line else (slug, None, "")
        # Pieces added while it was read shift the pages: a list short of what the source counts is kept, and its line
        # left unsaid so tomorrow's round reads it again.
        if isinstance(st.get("count"), int) and len(items) != st["count"]:
            sig = ""
        try:
            self.store.put(chain, slug, name, supply, items, "nintondo", sig=sig)
        except ValueError as exc:
            st["error"] = f"{slug}: {exc}"
        self._done(chain, slug, None)
        st.update(slug=None, page=1, pages=None, count=None)
        self._save(st)

    def _done(self, chain: str, slug: str, why: str | None) -> None:
        if why:
            log.info("known collections: %s: skipped %s", chain, why)
        with self.store._db() as conn:
            conn.execute("UPDATE kc_listing SET done=1 WHERE chain=? AND slug=?", (chain, slug))
            conn.execute("DELETE FROM kc_pending WHERE chain=? AND slug=?", (chain, slug))

    # ---------------------------------------------------------------- running
    def run(self, stop: Callable[[], bool] = lambda: False) -> None:
        while not stop():
            try:
                wait = self.step()
            except Exception:                                # noqa: BLE001 -- never stop for good
                log.exception("known collections")
                wait = 60.0
            self.sleep(min(wait, 30.0))

    def status(self) -> dict:
        counts = self.store.counts()
        out = {"on": bool(self.enabled()), "requests": self.requests, "chains": []}
        with self.store._db() as conn:
            listed = {r[0]: (r[1], r[2] or 0) for r in conn.execute(
                "SELECT l.chain, COUNT(*), SUM(l.done=0 AND (k.sig IS NULL OR k.sig<>l.sig)) FROM kc_listing l "
                "LEFT JOIN kc k ON k.chain=l.chain AND k.slug=l.slug GROUP BY l.chain")}
        for chain in self.sources:
            st = self._progress(chain)
            total, waiting = listed.get(chain, (0, 0))
            out["chains"].append({"net": chain, "phase": st["phase"], "page": st["page"], "pages": st["pages"],
                                  "slug": st["slug"], "listed": total, "waiting": waiting, "error": st["error"],
                                  "round_at": st["round_at"] or None,
                                  "next_round": (st["round_at"] + REFRESH) if st["phase"] == "rest" else None,
                                  **counts.get(chain, {"collections": 0, "pieces": 0})})
        return out
