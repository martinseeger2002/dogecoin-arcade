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

`chain` is the arcade's own name of the chain ("doge", "main", "bel", "ltc"), `slug` Nintondo's collection slug (on
Litecoin "<source>:<its key>", below), each id a lowercase "<txid>i<n>", sorted as plain strings, utf-8. A chain's
digest covers all its collections:

    digest(chain) = sha256( "\\n".join(sorted(slug + " " + fp for every collection held)) ).hexdigest()

Litecoin from several sites (2026-11-07, SadFrog: "we want to make sure the explore for ordinals collections on litecoin
picks up chikun.market and liteverse.io's index as well as the new https://liteord.doginals.store/inscriptions"). Nintondo
has no Litecoin search, so each site is a source of its own (MULTI_SOURCES), with a small adapter (listing -> lines,
collection -> pieces), and its lists are kept under the slug "<source>:<the site's key>": "chikun:punks",
"liteverse:12", "liteord:<parent inscription id>". Why in the slug rather than a source column: the slug is already
the key of every table, of the mesh's "il" rows and of the fingerprint, so the same collection from two sites is two
rows that never collide or flap, two arcades agree per site, and the formula, the wire and the files stay as they
were (Litecoin is new, so no old row is renamed). Two sites with a same-named collection are shown side by side, never
merged; Explore says which other site lists one by that name. What each site offers (read 2026-11-07):

  - chikun.market (api.chikun.market/v1): `/collections`, every collection in one answer (47: name, supply, cover).
    It publishes no collection's pieces (only the ones listed for sale), so its lines are kept "listed only".
  - liteverse.io: `/collections` in one answer (39) and `/collections/<id>/inscriptions`, a collection's whole list
    (ids, numbers, names). Keyed by its numeric id: its slugs repeat ("historical-genesis" twice) and hold "*". Its
    terms (section 6) bar automated access without its leave, so its own switch is off until the operator turns it on.
  - LiteOrd (liteord.doginals.store, a public read-only API, 5 requests a second without a key): no collection list;
    a parent inscription's children, the ord way (`/r/children/<id>/inscriptions/<page>`, 100 a page). A visitor opens
    one by searching a parent's id on Explore.
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
import urllib.error
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
#: Chains whose collections come from several sites, each its own source (2026-11-07): {chain: {source: host}}.
MULTI_SOURCES = {
    "ltc": {
        "chikun": "https://api.chikun.market",
        "liteverse": "https://liteverse.io",
        "liteord": "https://liteord.doginals.store",
    },
}
#: How Explore and Admin name each source.
SOURCE_LABELS = {"nintondo": "Nintondo", "chikun": "chikun.market", "liteverse": "liteverse.io", "liteord": "LiteOrd"}
CHAINS = tuple(SOURCES) + tuple(MULTI_SOURCES)
SETTING = "known_collections_import"
MODE_SETTING = "known_collections_mode"
MODES = ("demand", "all")
#: Which Litecoin sites the importer reads when the import is on (Admin -> Ords): a list of source names. Not set,
#: chikun.market and LiteOrd; liteverse.io only when the operator ticks it (its terms, section 6: no automated access
#: without its leave).
LTC_SETTING = "known_collections_ltc_sources"
LTC_DEFAULT = ("chikun", "liteord")

ID = re.compile(r"^[0-9a-f]{64}i[0-9]{1,6}$")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
SOURCE_SLUG = re.compile(r"^([a-z][a-z0-9]{0,15}):([A-Za-z0-9][A-Za-z0-9._-]{0,99})$")
#: A source's own keys, where narrower than SLUG: liteverse.io's numeric ids, LiteOrd's parent inscription ids.
SOURCE_KEYS = {"liteverse": re.compile(r"^[1-9][0-9]{0,17}$"), "liteord": ID}
POSITION = re.compile(r"#\s*([0-9]{1,9})\s*$")
MAX_OPENED = 500             # LiteOrd parents opened by visitors, at most (the oldest neither held nor wanted goes)

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
    ("cover", "TEXT NOT NULL DEFAULT ''"),       # the site's cover inscription, for a line not fetched (2026-11-07)
)


def mode_of(said: Any) -> str:
    """The setting's value as one of MODES: "demand" unless the operator chose "all"."""
    return said if said in MODES else "demand"


def ltc_sources(said: Any) -> tuple[str, ...]:
    """The Litecoin sites switched on (LTC_SETTING): the names said that are sources, LTC_DEFAULT when never set."""
    if not isinstance(said, (list, tuple)):
        return LTC_DEFAULT
    return tuple(s for s in MULTI_SOURCES["ltc"] if s in said)


def slug_ok(chain: str, slug: Any) -> bool:
    """A collection's slug as this chain keeps it: Nintondo's on its chains, "<source>:<key>" of a known source on a
    chain with several (2026-11-07). A source this arcade does not know (a newer arcade's) is not taken."""
    if not isinstance(slug, str):
        return False
    if chain in MULTI_SOURCES:
        m = SOURCE_SLUG.match(slug)
        return bool(m) and m.group(1) in MULTI_SOURCES[chain] and bool(SOURCE_KEYS.get(m.group(1), SLUG).match(m.group(2)))
    return chain in SOURCES and bool(SLUG.match(slug))


def source_of(chain: str, slug: str) -> str:
    """The site that lists a collection: "nintondo", or the slug's "<source>:" on a chain with several."""
    if chain in MULTI_SOURCES and ":" in (slug or ""):
        return slug.split(":", 1)[0]
    return "nintondo"


def source_link(chain: str, slug: str) -> str:
    """The collection's page on the site that lists it ("" when it has none)."""
    src, key = source_of(chain, slug), slug.split(":", 1)[-1]
    if src == "chikun":
        return f"https://chikun.market/collections/{key}"
    if src == "liteverse":
        return f"https://liteverse.io/market/collection?id={key}"
    if src == "liteord":
        return f"https://liteord.doginals.store/inscription/{key}"
    return ""


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
        if chain not in CHAINS or not slug_ok(chain, slug):
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
        if chain not in CHAINS or not slug_ok(chain, slug):
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

    def open_line(self, chain: str, slug: str, name: str) -> bool:
        """A line for a collection no site lists but one can read when asked (a LiteOrd parent a visitor opened,
        2026-11-07), so it is fetched like any other. At most MAX_OPENED a chain: the oldest neither held nor wanted
        makes room. False when it cannot be kept."""
        if chain not in MULTI_SOURCES or not slug_ok(chain, slug):
            return False
        with self._lock, self._db() as conn:
            if conn.execute("SELECT 1 FROM kc_listing WHERE chain=? AND slug=?", (chain, slug)).fetchone():
                return True
            prefix = slug.split(":", 1)[0] + ":"
            mine = "chain=? AND substr(slug,1,?)=?"
            if conn.execute(f"SELECT COUNT(*) FROM kc_listing WHERE {mine}",
                            (chain, len(prefix), prefix)).fetchone()[0] >= MAX_OPENED:
                conn.execute(f"DELETE FROM kc_listing WHERE rowid IN (SELECT l.rowid FROM kc_listing l WHERE l.{mine} "
                             "AND NOT EXISTS (SELECT 1 FROM kc k WHERE k.chain=l.chain AND k.slug=l.slug) AND NOT EXISTS "
                             "(SELECT 1 FROM kc_want w WHERE w.chain=l.chain AND w.slug=l.slug) ORDER BY l.rowid LIMIT 1)",
                             (chain, len(prefix), prefix))
                if conn.execute(f"SELECT COUNT(*) FROM kc_listing WHERE {mine}",
                                (chain, len(prefix), prefix)).fetchone()[0] >= MAX_OPENED:
                    return False
            conn.execute("INSERT INTO kc_listing (chain, slug, name, supply, sig, done) VALUES (?,?,?,NULL,'',0)",
                         (chain, slug, str(name or slug)[:NAME_MAX]))
        return True

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
                row.update(count=mine["count"], cover=mine["cover"] or (line or {}).get("cover") or "", fp=mine["fp"],
                           source=mine["source"], fetched_at=mine["fetched_at"])
                if row["supply"] is None:
                    row["supply"] = mine["supply"]
            else:
                row.update(count=0, cover=(line or {}).get("cover") or "", fp="", source=source_of(chain, slug),
                           fetched_at=None)
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

    def in_collections(self, chain: str, ids: list[str]) -> dict[str, dict]:
        """`in_collection` for many at once (the Wallet's Doginals, 2026-11-06): {id: {slug, collection, name, position,
        source}}, one query per 500 ids; an id in two collections gets the smaller one, as `in_collection` does."""
        best: dict[str, tuple[int, dict]] = {}
        with self._db() as conn:
            for at in range(0, len(ids), 500):
                part = list(ids[at:at + 500])
                for row in conn.execute(
                        "SELECT i.id, i.slug, i.name, i.position, k.name AS collection, k.source, k.count FROM kc_item i "
                        f"JOIN kc k ON k.chain=i.chain AND k.slug=i.slug WHERE i.chain=? AND i.id IN "
                        f"({','.join('?' * len(part))})", (chain, *part)):
                    if row["id"] not in best or row["count"] < best[row["id"]][0]:
                        best[row["id"]] = (row["count"], {"slug": row["slug"], "name": row["name"],
                                                          "position": row["position"], "collection": row["collection"],
                                                          "source": row["source"]})
        return {iid: got for iid, (_, got) in best.items()}

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

def fetch_json(method: str, url: str, body: dict | None = None, timeout: float = 30.0) -> Any:
    """One request to the source: a plain User-Agent and json both ways (it refuses urllib's own in some cases). A json
    object, or a list (liteverse.io answers lists, 2026-11-07); each adapter checks the shape it expects."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:            # noqa: S310 -- fixed https hosts only
        raw = r.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("answer too large")
    got = json.loads(raw.decode("utf-8"))
    if not isinstance(got, (dict, list)):
        raise ValueError("not a json object or list")
    return got


class Gone(Exception):
    """The source cannot give this collection's pieces (it publishes none, or does not know it yet): the line is kept
    with the reason, as one past the bounds is, and the host is not backed off."""


def _obj(got: Any, who: str = "the source") -> dict:
    if not isinstance(got, dict):
        raise ValueError(f"{who}: not a json object")
    return got


def _count(v: Any) -> int | None:
    """A whole number from a site that sends numbers as strings too (chikun.market: "supply": "100")."""
    if isinstance(v, str) and re.match(r"^[0-9]{1,15}$", v.strip()):
        return int(v.strip())
    n = _int(v)
    return n if n is not None and n >= 0 else None


def _sig(c: dict) -> str:
    nums = c.get("numbers") if isinstance(c.get("numbers"), list) else []
    first, last = (nums + [None, None])[:2]
    return f"{c.get('supply')}|{first}|{last}|{c.get('size')}"


def _line(key: str, name: Any, supply: int | None = None, sig: str = "", cover: str = "", skipped: str = "",
          **facts) -> dict:
    """One line of a source's listing as the importer keeps it."""
    return {"key": key, "name": str(name or key)[:NAME_MAX], "supply": supply, "sig": str(sig)[:80],
            "cover": cover if ID.match(cover or "") else "", "skipped": skipped,
            **{k: facts.get(k) for k in ("owners", "height", "created", "first", "last", "size")}}


class Adapter:
    """One site: `listing(ask, page)` -> (lines, more), each line as `_line` makes it; `items(ask, key, page)` ->
    (pieces as clean_items keeps them, more, the site's own count or None, its pages or None). `ask(method, path,
    body)` is one paced request to the host. An answer not shaped as the site sends it raises (said in the status,
    tried again after the backoff); pages start at 1."""
    name = ""
    lists = True                 # the site has a listing of its collections
    pieces = True                # the site publishes a collection's pieces

    def __init__(self, host: str):
        self.host = host.rstrip("/")

    def listing(self, ask: Callable, page: int) -> tuple[list[dict], bool]:
        return [], False

    def items(self, ask: Callable, key: str, page: int) -> tuple[list[dict], bool, int | None, int | None]:
        raise Gone(f"{SOURCE_LABELS.get(self.name, self.name)} publishes no collection's pieces")


class NintondoAdapter(Adapter):
    """Nintondo's public search (2026-10-26): `/pub/collections` pages of 50, `/pub/search` pages of 100 pieces."""
    name = "nintondo"

    def listing(self, ask, page):
        got = _obj(ask("GET", f"/pub/collections?page_size={LIST_PAGE_SIZE}&page={int(page)}"), "Nintondo")
        rows = []
        for c in got.get("collections") or []:
            if not isinstance(c, dict) or not SLUG.match(str(c.get("slug") or "")):
                continue
            supply = c.get("supply") if isinstance(c.get("supply"), int) else None
            rows.append(_line(c["slug"], c.get("name"), supply, _sig(c), **listing_details(c)))
        pages = int(got.get("pages") or 0)
        return rows, bool(rows) and page < pages and page < MAX_COLLECTIONS // LIST_PAGE_SIZE

    def items(self, ask, key, page):
        got = _obj(ask("POST", "/pub/search", {"collection": key, "page": int(page), "page_size": PAGE_SIZE}), "Nintondo")
        found = []
        for x in got.get("inscriptions") or []:
            if not isinstance(x, dict):
                continue
            meta = ((x.get("collection") or {}).get("meta") or {}) if isinstance(x.get("collection"), dict) else {}
            name = str(meta.get("name") or "") if isinstance(meta, dict) else ""
            found.append({"id": x.get("id"), "name": name, "number": x.get("number")})
        found = clean_items(found)
        pages, count = int(got.get("pages") or 0), got.get("count")
        return found, bool(found) and page < pages, count if isinstance(count, int) else None, pages


class ChikunAdapter(Adapter):
    """api.chikun.market/v1 (read 2026-11-07): `/collections`, every collection in one answer, {status, message,
    result: [{collection, name, supply, owners, image, ...}]}, numbers as strings. It publishes no collection's pieces
    (its pages show only the ones for sale), so each line is kept listed only, with that said."""
    name, pieces = "chikun", False
    NO_PIECES = "chikun.market lists this collection but does not publish its pieces"

    def listing(self, ask, page):
        got = _obj(ask("GET", "/v1/collections"), "chikun.market")
        if not isinstance(got.get("result"), list):
            raise ValueError("chikun.market: no list of collections")
        rows = []
        for c in got["result"][:MAX_COLLECTIONS]:
            if not isinstance(c, dict) or not SLUG.match(str(c.get("collection") or "")):
                continue
            supply = _count(c.get("supply"))
            cover = re.search(r"/content/([0-9a-f]{64}i[0-9]{1,6})$", str(c.get("image") or ""))
            rows.append(_line(c["collection"], c.get("name"), supply, f"{supply}", cover.group(1) if cover else "",
                              self.NO_PIECES, owners=_count(c.get("owners")) or None))   # it says 0 holders of each
        return rows, False

    def items(self, ask, key, page):
        raise Gone(self.NO_PIECES)


class LiteverseAdapter(Adapter):
    """liteverse.io (read 2026-11-07): `/collections`, all in one answer ([{id, name, display_name, supply,
    ordinal_count, inscription_icon, slug, ...}]), and `/collections/<id>/inscriptions`, a collection's whole list in
    one answer ([{inscription_id, inscription_number, name, collection_id, ...}], about 1.2 KB a piece). Keyed by the
    numeric id: its slugs repeat and hold "*"."""
    name = "liteverse"

    def listing(self, ask, page):
        got = ask("GET", "/collections")
        if not isinstance(got, list):
            raise ValueError("liteverse.io: not a list of collections")
        rows = []
        for c in got[:MAX_COLLECTIONS]:
            cid = c.get("id") if isinstance(c, dict) else None
            if not isinstance(cid, int) or isinstance(cid, bool) or not 0 < cid < 10 ** 18:
                continue
            supply, held = _count(c.get("supply")), _count(c.get("ordinal_count"))
            rows.append(_line(str(cid), c.get("display_name") or c.get("name"), supply, f"{supply}|{held}",
                              str(c.get("inscription_icon") or "")))
        return rows, False

    def items(self, ask, key, page):
        if not re.match(r"^[1-9][0-9]{0,17}$", key):
            raise Gone("not a liteverse.io collection")
        got = ask("GET", f"/collections/{key}/inscriptions")
        if not isinstance(got, list):
            raise ValueError("liteverse.io: not a list of pieces")
        found = []
        for x in got:
            if not isinstance(x, dict):
                continue
            if x.get("collection_id") != int(key):
                raise ValueError("liteverse.io: a piece of another collection in the list")
            name, pos = str(x.get("name") or ""), None
            if re.match(r"^[0-9]{1,9}$", name):               # Litecoin Punks' pieces are named "1", "2" ...
                name, pos = "", int(name)
            found.append({"id": x.get("inscription_id") or x.get("asset_id"), "name": name, "position": pos,
                          "number": _int(x.get("inscription_number"))})
        return clean_items(found), False, None, 1


class OrdChildrenAdapter(Adapter):
    """LiteOrd (liteord.doginals.store, read 2026-11-07): a read-only ord explorer with a documented API (5 requests a
    second per address without a key, burst 10) and no collection list. A collection there is a parent's children,
    as ord serves them (`/r/children/<id>/inscriptions/<page>`: {children: [{id, number, ...}], more, page}, 100 a
    page, from page 0). Its lines are the parents visitors opened (KnownCollections.open_line)."""
    name, lists = "liteord", False

    def items(self, ask, key, page):
        if not ID.match(key):
            raise Gone("not an inscription id")
        try:
            got = _obj(ask("GET", f"/r/children/{key}/inscriptions/{int(page) - 1}"), "LiteOrd")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise Gone("LiteOrd does not know this inscription (yet)") from None
            raise
        kids = got.get("children")
        if not isinstance(kids, list) or not isinstance(got.get("more"), bool):
            raise ValueError("LiteOrd: not ord's page of children")
        base = (int(page) - 1) * 100
        found = clean_items([{"id": x.get("id"), "name": "", "position": base + k + 1, "number": _int(x.get("number"))}
                             for k, x in enumerate(kids) if isinstance(x, dict)])
        return found, got["more"] and bool(found), None, None


ADAPTERS = {"nintondo": NintondoAdapter, "chikun": ChikunAdapter, "liteverse": LiteverseAdapter,
            "liteord": OrdChildrenAdapter}


class Feed:
    """One source of one chain as the importer reads it: its adapter, the slugs it keeps ("" on a Nintondo chain,
    "<source>:" on one with several) and its row in kc_progress (the chain, or "<chain>:<source>")."""

    def __init__(self, chain: str, adapter: Adapter, prefix: str = ""):
        self.chain, self.adapter, self.prefix = chain, adapter, prefix
        self.host = adapter.host
        self.key = f"{chain}:{adapter.name}" if prefix else chain

    def where(self, col: str = "slug") -> tuple[str, tuple]:
        """The SQL that keeps a query to this feed's slugs, and its parameters."""
        return (f" AND substr({col},1,{len(self.prefix)})=?", (self.prefix,)) if self.prefix else ("", ())


class Importer:
    """Each source's lists, a request at a time: Nintondo's for Dogecoin, Pepecoin and Bellscoin (the listing, 50
    collections a page, then the collections to fetch, 100 pieces a page), and each Litecoin site's (2026-11-07,
    MULTI_SOURCES, the ones the operator switched on). At most one request a second to each host; every page's progress
    kept, so a restart goes on from it; once a source is through, it rests a day and reads its listing again.

    Which collections (2026-10-26, "lazy loading one at a time on demand"): in mode "demand" the ones somebody opened
    (oldest want first) and the ones held whose line in the listing (supply, first and last number, size) changed; in
    mode "all" the wanted ones first, then every one new or changed. A want is served while a chain rests too (phase
    "want", the day's round left as it was). A want a linked arcade can meet (`offered(chain, slug)`) is left to the
    mesh for MESH_FIRST seconds: faster, and no load on the source."""

    def __init__(self, store: KnownCollections, enabled: Callable[[], bool], fetch: Callable[..., Any] = fetch_json,
                 clock: Callable[[], float] = time.monotonic, now: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] | None = None, sources: dict | None = None,
                 mode: Callable[[], str] = lambda: "demand",
                 offered: Callable[[str, str], bool] | None = None, multi: dict | None = None,
                 switched: Callable[[], Iterable[str]] | None = None):
        self.store, self.enabled, self.fetch = store, enabled, fetch
        self.clock, self.now, self.sleep = clock, now, sleep
        self.mode, self.offered = mode, offered
        self.sources = dict(SOURCES if sources is None else sources)
        multi = MULTI_SOURCES if multi is None and sources is None else (multi or {})
        self.switched = switched                  # the Litecoin sites switched on (LTC_SETTING); None: every one
        self.feeds = [Feed(chain, NintondoAdapter(host)) for chain, host in self.sources.items()]
        self.feeds += [Feed(chain, ADAPTERS[name](host), name + ":") for chain, by in multi.items()
                       for name, host in by.items()]
        self._last: dict[str, float] = {}         # host -> when it was last asked (clock)
        self._backoff: dict[str, tuple[float, int]] = {}   # host -> (not before, failures in a row)
        self.requests = 0
        self.wake = threading.Event()             # set by a new want: the wait between steps cut short

    def _mode(self) -> str:
        try:
            return mode_of(self.mode())
        except Exception:                                    # noqa: BLE001
            return "demand"

    def _on(self, feed: Feed) -> bool:
        """A Nintondo chain is read whenever the import is on; a Litecoin site when it is switched on as well."""
        if not feed.prefix or self.switched is None:
            return True
        try:
            return feed.adapter.name in set(self.switched())
        except Exception:                                    # noqa: BLE001
            return feed.adapter.name in LTC_DEFAULT

    def _next_want(self, feed: Feed) -> tuple[dict | None, float | None]:
        """The oldest want to fetch now from the source, and the seconds until one left to the mesh is ours. Wants met
        (held) or tried this round and not kept (skipped) are cleared on the way."""
        now, chain = self.now(), feed.chain
        where, args = feed.where("w.slug")
        with self.store._db() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT w.slug, w.asked_at, l.supply, l.done, k.slug IS NOT NULL AS held FROM kc_want w "
                "JOIN kc_listing l ON l.chain=w.chain AND l.slug=w.slug LEFT JOIN kc k ON k.chain=w.chain AND "
                "k.slug=w.slug WHERE w.chain=?" + where + " ORDER BY w.asked_at, w.slug", (chain, *args))]
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
    def _progress(self, feed: Feed | str) -> dict:
        """A feed's progress (by the feed, or its key: the chain, or "<chain>:<source>")."""
        key = feed if isinstance(feed, str) else feed.key
        with self.store._db() as conn:
            row = conn.execute("SELECT * FROM kc_progress WHERE chain=?", (key,)).fetchone()
        return dict(row) if row else {"chain": key, "phase": "list", "page": 1, "slug": None, "pages": None,
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
        for feed in self.feeds:
            if not self._on(feed):
                continue
            chain, where, args = feed.chain, *feed.where()
            for _ in range(MAX_COLLECTIONS + 2):          # steps that ask nothing (a collection unchanged) go on at once
                st = self._progress(feed)
                if st["phase"] == "rest":
                    left = st["round_at"] + REFRESH - self.now()
                    if left > 0:
                        want, later = self._next_want(feed)
                        if want is None:                     # resting, nobody waiting
                            waits.append(left if later is None else min(left, later))
                            break
                        st.update(phase="want", page=1, slug=None, pages=None, count=None)
                    else:
                        # The listing is read again over the old lines, so search keeps them meanwhile; a line the new
                        # listing lacks is dropped when it ends. A source with no listing (LiteOrd) tries its lines
                        # again, the ones it did not know yesterday too.
                        st.update(phase="list", page=1, slug=None, pages=None, count=None)
                        with self.store._db() as conn:
                            conn.execute("UPDATE kc_listing SET done=0" + ("" if feed.adapter.lists else ", skipped=''")
                                         + " WHERE chain=?" + where, (chain, *args))
                    self._save(st)
                host = feed.host
                t = self.clock()
                until, _fails = self._backoff.get(host, (0.0, 0))
                wait = max(until - t, self._last.get(host, -1e18) + PACE - t)
                if wait > 0:
                    waits.append(wait)
                    break
                try:
                    asked = self._one(feed, st)
                except Exception as exc:                     # noqa: BLE001 -- said in the status, tried again later
                    fails = _fails + 1
                    self._backoff[host] = (self.clock() + min(BACKOFF_MAX, 30.0 * 2 ** (fails - 1)), fails)
                    st["error"] = f"{type(exc).__name__}: {exc}"
                    self._save(st)
                    log.info("known collections: %s: %s", feed.key, st["error"])
                    asked = True
                else:
                    self._backoff.pop(host, None)
                if asked:
                    return 0.0
        return max(0.05, min(waits)) if waits else 30.0

    def _ask(self, host: str, method: str, path: str, body: dict | None = None) -> Any:
        self._last[host] = self.clock()
        self.requests += 1
        return self.fetch(method, host + path, body)

    def _one(self, feed: Feed, st: dict) -> bool:
        """The next thing to do on this source; True when a request was made."""
        chain, ad = feed.chain, feed.adapter
        where, args = feed.where()

        def ask(method: str, path: str, body: dict | None = None) -> Any:
            return self._ask(feed.host, method, path, body)
        if st["phase"] == "list":
            if not ad.lists:                                 # nothing to list: its lines are the ones opened here
                st.update(phase="items", page=1, slug=None, pages=None, count=None, error="")
                self._save(st)
                return False
            lines, more = ad.listing(ask, int(st["page"]))
            rows = [(chain, feed.prefix + r["key"], r["name"], r["supply"], r["sig"], 1 if r["skipped"] else 0,
                     r["owners"], r["height"], r["created"], r["first"], r["last"], r["size"], r["skipped"], r["cover"])
                    for r in lines if slug_ok(chain, feed.prefix + r["key"])]
            with self.store._db() as conn:
                if st["page"] == 1:                          # a round's listing begins: every old line not seen yet
                    conn.execute("UPDATE kc_listing SET stale=1 WHERE chain=?" + where, (chain, *args))
                conn.executemany(
                    "INSERT INTO kc_listing (chain, slug, name, supply, sig, done, owners, height, created, first, "
                    "last, size, stale, skipped, cover) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?) ON CONFLICT (chain, "
                    "slug) DO UPDATE SET name=excluded.name, supply=excluded.supply, sig=excluded.sig, "
                    "done=excluded.done, owners=excluded.owners, height=excluded.height, created=excluded.created, "
                    "first=excluded.first, last=excluded.last, size=excluded.size, stale=0, skipped=excluded.skipped, "
                    "cover=excluded.cover", rows[:MAX_COLLECTIONS])
            if not more:
                with self.store._db() as conn:          # the round's listing read: lines it no longer has, dropped
                    if conn.execute("SELECT 1 FROM kc_listing WHERE chain=? AND stale=0" + where + " LIMIT 1",
                                    (chain, *args)).fetchone():
                        conn.execute("DELETE FROM kc_listing WHERE chain=? AND stale=1" + where, (chain, *args))
                st.update(phase="items", page=1, slug=None, pages=None, count=None)
            else:
                st["page"] += 1
            st["error"] = ""
            self._save(st)
            return True
        # phase "items" (the round) or "want" (resting, somebody waiting): the collection being fetched, or the next
        if not st["slug"]:
            row, _later = self._next_want(feed)
            if row is None and st["phase"] == "items" and ad.pieces:
                held_only = "" if self._mode() == "all" else "k.sig IS NOT NULL AND "
                lw, la = feed.where("l.slug")
                with self.store._db() as conn:
                    row = conn.execute("SELECT l.slug, l.supply FROM kc_listing l LEFT JOIN kc k ON k.chain=l.chain "
                                       "AND k.slug=l.slug WHERE l.chain=? AND l.done=0 AND " + held_only +
                                       "(k.sig IS NULL OR k.sig<>l.sig)" + lw + " ORDER BY l.slug LIMIT 1",
                                       (chain, *la)).fetchone()
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
            if not ad.pieces:                                # a want of a listed-only line: said, never asked
                self._done(chain, row["slug"], ChikunAdapter.NO_PIECES if ad.name == "chikun" else "no pieces published")
                return False
            with self.store._db() as conn:
                conn.execute("DELETE FROM kc_pending WHERE chain=? AND slug=?", (chain, row["slug"]))
            st.update(slug=row["slug"], page=1, pages=None, count=None)
            self._save(st)
        slug = st["slug"]
        try:
            found, more, count, pages = ad.items(ask, slug[len(feed.prefix):], int(st["page"]))
        except Gone as exc:                                  # nothing to read there: kept as skipped, with why
            self._done(chain, slug, str(exc))
            st.update(slug=None, page=1, pages=None, count=None, error="")
            self._save(st)
            return True
        with self.store._db() as conn:
            conn.executemany("INSERT OR IGNORE INTO kc_pending (chain, slug, id, name, position, number) "
                             "VALUES (?,?,?,?,?,?)",
                             [(chain, slug, r["id"], r["name"], r["position"], r["number"]) for r in found])
            held = conn.execute("SELECT COUNT(*) FROM kc_pending WHERE chain=? AND slug=?", (chain, slug)).fetchone()[0]
        st.update(pages=pages, count=count if isinstance(count, int) else st["count"], error="")
        if held > MAX_ITEMS:
            self._done(chain, slug, f"{slug}: more than {MAX_ITEMS:,} pieces")
            st.update(slug=None, page=1)
            self._save(st)
            return True
        if more:
            st["page"] += 1
            self._save(st)
            return True
        self._finish(feed, slug, st)
        return True

    def _finish(self, feed: Feed, slug: str, st: dict) -> None:
        chain = feed.chain
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
            self.store.put(chain, slug, name, supply, items, feed.adapter.name, sig=sig)
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
        """The switch, the mode and each source's progress: one row per chain read from Nintondo, one per Litecoin site
        (with `source`, its `label` and whether it is switched on)."""
        mode = self._mode()
        out = {"on": bool(self.enabled()), "mode": mode, "requests": self.requests, "chains": [],
               "ltc_sources": [f.adapter.name for f in self.feeds if f.prefix and self._on(f)]}
        with self.store._db() as conn:
            for feed in self.feeds:
                lw, la = feed.where("l.slug")
                total, every, changed = conn.execute(
                    "SELECT COUNT(*), SUM(l.done=0 AND (k.sig IS NULL OR k.sig<>l.sig)), "
                    "SUM(l.done=0 AND k.sig IS NOT NULL AND k.sig<>l.sig) FROM kc_listing l "
                    "LEFT JOIN kc k ON k.chain=l.chain AND k.slug=l.slug WHERE l.chain=?" + lw,
                    (feed.chain, *la)).fetchone()
                every, changed = (every or 0, changed or 0) if feed.adapter.pieces else (0, 0)
                ww, wa = feed.where()
                wanted = conn.execute("SELECT COUNT(*) FROM kc_want WHERE chain=?" + ww, (feed.chain, *wa)).fetchone()[0]
                held, pieces = conn.execute("SELECT COUNT(*), SUM(count) FROM kc WHERE chain=?" + ww,
                                            (feed.chain, *wa)).fetchone()
                st = self._progress(feed)
                waiting = every if mode == "all" else changed + wanted
                row = {"net": feed.chain, "phase": st["phase"], "page": st["page"], "pages": st["pages"],
                       "slug": st["slug"], "listed": total, "waiting": waiting, "wanted": wanted, "error": st["error"],
                       "round_at": st["round_at"] or None,
                       "next_round": (st["round_at"] + REFRESH) if st["phase"] == "rest" else None,
                       "collections": held, "pieces": pieces or 0}
                if feed.prefix:
                    row.update(source=feed.adapter.name, label=SOURCE_LABELS.get(feed.adapter.name, feed.adapter.name),
                               switched=self._on(feed), pieces_published=feed.adapter.pieces)
                out["chains"].append(row)
        return out
