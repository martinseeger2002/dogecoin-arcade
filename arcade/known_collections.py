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

On demand (2026-10-26, SadFrog: "can you make the collection search more in depth with sorting and filters and lazy
loading one at a time on demand"): every collection of Nintondo's runs to about 885,000 pieces, some 310 MB, so by
default (setting `known_collections_mode` "demand") the importer reads only the listings (cheap: a few pages a chain,
each collection's line with its owners, height, numbers and supply), and a collection's pieces when somebody opens it
on Explore (a want, table `kc_want`), plus the ones already held whose line changed. "all" is the old way: every
collection, wanted ones first.

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
MODE_SETTING = "known_collections_mode"
MODES = ("demand", "all")

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
MESH_FIRST = 60.0            # a want a linked arcade can meet is left to it this long before Nintondo is asked
WANT_TTL = 7 * 24 * 3600     # a want nobody met is forgotten after a week
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
-- (the listing's other details, added 2026-10-26, are LISTING_COLUMNS below: a file from before gets them at open)
CREATE TABLE IF NOT EXISTS kc_listing (
    chain TEXT NOT NULL, slug TEXT NOT NULL, name TEXT NOT NULL, supply INTEGER, sig TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (chain, slug));
-- collections somebody opened on Explore and this arcade does not hold yet (2026-10-26: "lazy loading ... on demand")
CREATE TABLE IF NOT EXISTS kc_want (
    chain TEXT NOT NULL, slug TEXT NOT NULL, asked_at REAL NOT NULL, PRIMARY KEY (chain, slug));
CREATE TABLE IF NOT EXISTS kc_pending (
    chain TEXT NOT NULL, slug TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, position INTEGER, number INTEGER,
    PRIMARY KEY (chain, slug, id));
CREATE TABLE IF NOT EXISTS kc_progress (
    chain TEXT PRIMARY KEY, phase TEXT NOT NULL, page INTEGER NOT NULL DEFAULT 1, slug TEXT, pages INTEGER,
    count INTEGER, round_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
"""


#: Nintondo's listing line kept whole (2026-10-26): added in place to a file that lacks them, never dropped.
LISTING_COLUMNS = (
    ("owners", "INTEGER"),                       # holders
    ("height", "INTEGER"),                       # the block of its first piece
    ("created", "INTEGER"),                      # when, unix seconds
    ("first", "INTEGER"), ("last", "INTEGER"),   # its lowest and highest inscription number
    ("size", "INTEGER"),                         # bytes, all pieces
    ("stale", "INTEGER NOT NULL DEFAULT 0"),     # not seen yet in the round's listing (dropped when it ends)
    ("skipped", "TEXT NOT NULL DEFAULT ''"),     # why this round's fetch kept nothing ("more than 100,000 pieces")
)


def mode_of(said: Any) -> str:
    """The setting's value as one of MODES: "demand" unless the operator chose "all"."""
    return said if said in MODES else "demand"


def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v == v and abs(v) < 1e18:
        return int(v)
    return None


def _when(v: Any) -> int | None:
    """`created` as unix seconds: a number (milliseconds when that big) or an ISO date; None otherwise."""
    n = _int(v)
    if n is None and isinstance(v, str) and v:
        import datetime
        try:
            d = datetime.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return int(d.timestamp())
    return n // 1000 if n is not None and n > 100_000_000_000 else n


def listing_details(c: dict) -> dict:
    """One line of Nintondo's /pub/collections as kept: owners, height, created, first, last, size."""
    nums = c.get("numbers") if isinstance(c.get("numbers"), list) else []
    first, last = (nums + [None, None])[:2]
    return {"owners": _int(c.get("owners")), "height": _int(c.get("height")), "created": _when(c.get("created")),
            "first": _int(first), "last": _int(last), "size": _int(c.get("size"))}


def sig_details(sig: str) -> dict:
    """first, last and size from a listing line's signature ("supply|first|last|size"), for a list a linked arcade
    sent without the line itself."""
    parts = (str(sig or "").split("|") + ["", "", "", ""])[:4]

    def num(x):
        return int(x) if re.match(r"^-?[0-9]{1,15}$", x) else None
    return {"first": num(parts[1]), "last": num(parts[2]), "size": num(parts[3])}


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
            have = {r[1] for r in conn.execute("PRAGMA table_info(kc_listing)")}
            for col, kind in LISTING_COLUMNS:            # a file from before 2026-10-26: its rows kept, columns added
                if col not in have:
                    conn.execute(f"ALTER TABLE kc_listing ADD COLUMN {col} {kind}")

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
            conn.execute("DELETE FROM kc_want WHERE chain=? AND slug=?", (chain, slug))     # held: the want is met
        return fp

    # ---------------------------------------------------------------- wants (2026-10-26: "on demand")
    def want(self, chain: str, slug: str, now: float | None = None) -> None:
        """Somebody opened this collection: fetch it. Asked again, it keeps its place (oldest first)."""
        if chain not in CHAINS or not SLUG.match(slug or ""):
            raise ValueError("not a known chain and slug")
        now = time.time() if now is None else now
        with self._lock, self._db() as conn:
            conn.execute("DELETE FROM kc_want WHERE asked_at < ?", (now - WANT_TTL,))
            if conn.execute("SELECT COUNT(*) FROM kc_want WHERE chain=?", (chain,)).fetchone()[0] >= MAX_COLLECTIONS:
                return
            conn.execute("INSERT OR IGNORE INTO kc_want (chain, slug, asked_at) VALUES (?,?,?)", (chain, slug, now))

    def unwant(self, chain: str, slug: str) -> None:
        with self._db() as conn:
            conn.execute("DELETE FROM kc_want WHERE chain=? AND slug=?", (chain, slug))

    def wants(self, chain: str) -> dict:
        """{slug: asked at} of a chain's wants."""
        with self._db() as conn:
            return {r[0]: r[1] for r in conn.execute("SELECT slug, asked_at FROM kc_want WHERE chain=?", (chain,))}

    def listed(self, chain: str, slug: str) -> dict | None:
        """The collection's line in the source's listing, as last read."""
        with self._db() as conn:
            row = conn.execute("SELECT * FROM kc_listing WHERE chain=? AND slug=?", (chain, slug)).fetchone()
        return dict(row) if row else None

    def fetching(self, chain: str, slug: str) -> int:
        """Pieces of a collection read so far by a fetch still going."""
        with self._db() as conn:
            return conn.execute("SELECT COUNT(*) FROM kc_pending WHERE chain=? AND slug=?", (chain, slug)).fetchone()[0]

    def catalog(self, chain: str) -> list[dict]:
        """Every collection of a chain this arcade knows of: each line of the listing, held or not, and each held list
        the listing lacks (a linked arcade's). One row each: the held list's facts with the listing's details."""
        with self._db() as conn:
            listed = {r["slug"]: dict(r) for r in conn.execute("SELECT * FROM kc_listing WHERE chain=?", (chain,))}
            held = {r["slug"]: dict(r) for r in conn.execute("SELECT * FROM kc WHERE chain=?", (chain,))}
        out = []
        for slug in set(listed) | set(held):
            line, mine = listed.get(slug), held.get(slug)
            row = {"chain": chain, "slug": slug, "imported": mine is not None}
            if line is not None:
                row.update(name=line["name"], supply=line["supply"],
                           **{k: line.get(k) for k in ("owners", "height", "created", "first", "last", "size")},
                           listed=True, skipped=line.get("skipped") or "")
            else:
                row.update(name=mine["name"], supply=mine["supply"], owners=None, height=None, created=None,
                           listed=False, skipped="", **sig_details(mine["sig"]))
            if mine is not None:
                row.update(count=mine["count"], cover=mine["cover"], fp=mine["fp"], source=mine["source"],
                           fetched_at=mine["fetched_at"])
                if row["supply"] is None:
                    row["supply"] = mine["supply"]
            else:
                row.update(count=0, cover="", fp="", source="nintondo", fetched_at=None)
            out.append(row)
        return out

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
    """Nintondo's lists of each chain, a request at a time: the listing (50 collections a page), then the collections to
    fetch (100 pieces a page). At most one request a second to each host; every page's progress kept, so a restart goes
    on from it; once a chain is through, it rests a day and reads its listing again.

    Which collections (2026-10-26, "lazy loading one at a time on demand"): in mode "demand" the ones somebody opened
    (oldest want first) and the ones held whose line in the listing (supply, first and last number, size) changed; in
    mode "all" the wanted ones first, then every one new or changed. A want is served while a chain rests too (phase
    "want", the day's round left as it was). A want a linked arcade can meet (`offered(chain, slug)`) is left to the
    mesh for MESH_FIRST seconds: faster, and no load on Nintondo."""

    def __init__(self, store: KnownCollections, enabled: Callable[[], bool], fetch: Callable[..., dict] = fetch_json,
                 clock: Callable[[], float] = time.monotonic, now: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] | None = None, sources: dict | None = None,
                 mode: Callable[[], str] = lambda: "demand",
                 offered: Callable[[str, str], bool] | None = None):
        self.store, self.enabled, self.fetch = store, enabled, fetch
        self.clock, self.now, self.sleep = clock, now, sleep
        self.mode, self.offered = mode, offered
        self.sources = dict(sources or SOURCES)
        self._last: dict[str, float] = {}         # host -> when it was last asked (clock)
        self._backoff: dict[str, tuple[float, int]] = {}   # host -> (not before, failures in a row)
        self.requests = 0
        self.wake = threading.Event()             # set by a new want: the wait between steps cut short

    def _mode(self) -> str:
        try:
            return mode_of(self.mode())
        except Exception:                                    # noqa: BLE001
            return "demand"

    def _next_want(self, chain: str) -> tuple[dict | None, float | None]:
        """The oldest want to fetch now from the source, and the seconds until one left to the mesh is ours. Wants met
        (held) or tried this round and not kept (skipped) are cleared on the way."""
        now = self.now()
        with self.store._db() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT w.slug, w.asked_at, l.supply, l.done, k.slug IS NOT NULL AS held FROM kc_want w "
                "JOIN kc_listing l ON l.chain=w.chain AND l.slug=w.slug LEFT JOIN kc k ON k.chain=w.chain AND "
                "k.slug=w.slug WHERE w.chain=? ORDER BY w.asked_at, w.slug", (chain,))]
        wait = None
        for r in rows:
            if r["held"] or r["done"]:
                self.store.unwant(chain, r["slug"])
                continue
            left = r["asked_at"] + MESH_FIRST - now
            if left > 0 and self.offered is not None:
                try:
                    theirs = bool(self.offered(chain, r["slug"]))
                except Exception:                            # noqa: BLE001
                    theirs = False
                if theirs:
                    wait = left if wait is None else min(wait, left)
                    continue
            return r, wait
        return None, wait

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
                        want, later = self._next_want(chain)
                        if want is None:                     # resting, nobody waiting
                            waits.append(left if later is None else min(left, later))
                            break
                        st.update(phase="want", page=1, slug=None, pages=None, count=None)
                    else:
                        # The listing is read again over the old lines, so search keeps them meanwhile; a line the new
                        # listing lacks is dropped when it ends.
                        st.update(phase="list", page=1, slug=None, pages=None, count=None)
                        with self.store._db() as conn:
                            conn.execute("UPDATE kc_listing SET done=0 WHERE chain=?", (chain,))
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
                d = listing_details(c)
                rows.append((chain, c["slug"], str(c.get("name") or c["slug"])[:NAME_MAX], supply, _sig(c),
                             d["owners"], d["height"], d["created"], d["first"], d["last"], d["size"]))
            with self.store._db() as conn:
                if st["page"] == 1:                          # a round's listing begins: every old line not seen yet
                    conn.execute("UPDATE kc_listing SET stale=1 WHERE chain=?", (chain,))
                conn.executemany(
                    "INSERT INTO kc_listing (chain, slug, name, supply, sig, done, owners, height, created, first, "
                    "last, size, stale, skipped) VALUES (?,?,?,?,?,0,?,?,?,?,?,?,0,'') ON CONFLICT (chain, slug) DO "
                    "UPDATE SET name=excluded.name, supply=excluded.supply, sig=excluded.sig, done=0, "
                    "owners=excluded.owners, height=excluded.height, created=excluded.created, first=excluded.first, "
                    "last=excluded.last, size=excluded.size, stale=0, skipped=''", rows[:MAX_COLLECTIONS])
            pages = int(got.get("pages") or 0)
            if not rows or st["page"] >= pages or st["page"] >= MAX_COLLECTIONS // LIST_PAGE_SIZE:
                with self.store._db() as conn:          # the round's listing read: lines it no longer has, dropped
                    if conn.execute("SELECT 1 FROM kc_listing WHERE chain=? AND stale=0 LIMIT 1", (chain,)).fetchone():
                        conn.execute("DELETE FROM kc_listing WHERE chain=? AND stale=1", (chain,))
                st.update(phase="items", page=1, slug=None, pages=None, count=None)
            else:
                st["page"] += 1
            st["error"] = ""
            self._save(st)
            return True
        # phase "items" (the round) or "want" (resting, somebody waiting): the collection being fetched, or the next
        if not st["slug"]:
            row, _later = self._next_want(chain)
            if row is None and st["phase"] == "items":
                held_only = "" if self._mode() == "all" else "k.sig IS NOT NULL AND "
                with self.store._db() as conn:
                    row = conn.execute("SELECT l.slug, l.supply FROM kc_listing l LEFT JOIN kc k ON k.chain=l.chain "
                                       "AND k.slug=l.slug WHERE l.chain=? AND l.done=0 AND " + held_only +
                                       "(k.sig IS NULL OR k.sig<>l.sig) ORDER BY l.slug LIMIT 1", (chain,)).fetchone()
            if row is None:
                if st["phase"] == "items":
                    st.update(phase="rest", round_at=self.now(), slug=None, page=1, error="")
                else:                                        # the wants met: back to the rest, its day unchanged
                    st.update(phase="rest", slug=None, page=1)
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
        why = None
        try:
            self.store.put(chain, slug, name, supply, items, "nintondo", sig=sig)
        except ValueError as exc:
            st["error"] = why = f"{slug}: {exc}"
        self._done(chain, slug, why)
        st.update(slug=None, page=1, pages=None, count=None)
        self._save(st)

    def _done(self, chain: str, slug: str, why: str | None) -> None:
        if why:
            log.info("known collections: %s: skipped %s", chain, why)
        with self.store._db() as conn:
            conn.execute("UPDATE kc_listing SET done=1, skipped=? WHERE chain=? AND slug=?", (why or "", chain, slug))
            conn.execute("DELETE FROM kc_pending WHERE chain=? AND slug=?", (chain, slug))
            conn.execute("DELETE FROM kc_want WHERE chain=? AND slug=?", (chain, slug))     # stored, or skipped

    # ---------------------------------------------------------------- running
    def run(self, stop: Callable[[], bool] = lambda: False) -> None:
        while not stop():
            try:
                wait = self.step()
            except Exception:                                # noqa: BLE001 -- never stop for good
                log.exception("known collections")
                wait = 60.0
            self.pause(min(wait, 30.0))

    def pause(self, seconds: float) -> None:
        """The wait between steps; a new want (`wake`) ends it early."""
        if self.sleep is not None:
            self.sleep(seconds)
        elif self.wake.wait(seconds):
            self.wake.clear()

    def status(self) -> dict:
        counts = self.store.counts()
        mode = self._mode()
        out = {"on": bool(self.enabled()), "mode": mode, "requests": self.requests, "chains": []}
        with self.store._db() as conn:
            listed = {r[0]: (r[1], r[2] or 0, r[3] or 0) for r in conn.execute(
                "SELECT l.chain, COUNT(*), SUM(l.done=0 AND (k.sig IS NULL OR k.sig<>l.sig)), "
                "SUM(l.done=0 AND k.sig IS NOT NULL AND k.sig<>l.sig) FROM kc_listing l "
                "LEFT JOIN kc k ON k.chain=l.chain AND k.slug=l.slug GROUP BY l.chain")}
            wanted = {r[0]: r[1] for r in conn.execute("SELECT chain, COUNT(*) FROM kc_want GROUP BY chain")}
        for chain in self.sources:
            st = self._progress(chain)
            total, every, changed = listed.get(chain, (0, 0, 0))
            waiting = every if mode == "all" else changed + wanted.get(chain, 0)
            out["chains"].append({"net": chain, "phase": st["phase"], "page": st["page"], "pages": st["pages"],
                                  "slug": st["slug"], "listed": total, "waiting": waiting,
                                  "wanted": wanted.get(chain, 0), "error": st["error"],
                                  "round_at": st["round_at"] or None,
                                  "next_round": (st["round_at"] + REFRESH) if st["phase"] == "rest" else None,
                                  **counts.get(chain, {"collections": 0, "pieces": 0})})
        return out
