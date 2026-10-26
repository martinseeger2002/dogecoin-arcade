"""Known collections of Doginals, Pepinals and Bellinals (2026-10-26, SadFrog: "I'd like to have pepinals, doginals and
bellinals collections pulled from known collections and searchable from our indexes and arcades - the known collections
should be shared across arcades to be agreed upon"; shared "over the mesh"). arcade/known_collections.py (the store, the
fingerprint, the importer from Nintondo's public search) and arcade/mesh/collectionshare.py (digests, indexes, agreement,
pieces fetched in parts and checked). Nothing here reaches the internet: Nintondo is faked."""
import asyncio
import hashlib
import json
import pathlib
import sys
import urllib.parse

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_ord_explorer import EDGE, walking                                     # noqa: F401,E402
from test_doginal_index import small                                            # noqa: F401,E402
from test_web import app_state, client                                          # noqa: F401,E402

from arcade import known_collections as kc                                      # noqa: E402
from arcade.mesh import collectionshare as cs                                   # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}
WRITE = {"x-arcade-admin": "1"}


def _id(k: int, n: int = 0) -> str:
    return hashlib.sha256(str(k).encode()).hexdigest() + f"i{n}"


def _pieces(name: str, ids: list[str], first_number: int = 1000) -> list[dict]:
    return [{"id": i, "name": f"{name} #{k + 1}", "number": first_number + k} for k, i in enumerate(ids)]


# --------------------------------------------------------------------------- the store and its fingerprints

def test_a_fingerprint_is_the_sorted_ids_bound_to_chain_and_slug():
    ids = [_id(k) for k in range(5)]
    fp = kc.fingerprint("doge", "frogs", ids)
    assert fp == hashlib.sha256(("doge\nfrogs\n" + "\n".join(sorted(ids))).encode()).hexdigest(), "as documented"
    assert kc.fingerprint("doge", "frogs", list(reversed(ids))) == fp, "any order"
    assert kc.fingerprint("main", "frogs", ids) != fp, "bound to the chain"
    assert kc.fingerprint("doge", "frogs2", ids) != fp, "and to the slug"
    assert kc.fingerprint("doge", "frogs", ids[:4]) != fp
    assert kc.chain_digest([("a", "1" * 64), ("b", "2" * 64)]) == kc.chain_digest([("b", "2" * 64), ("a", "1" * 64)])


def test_the_store_keeps_a_list_finds_a_piece_and_searches_by_name(tmp_path):
    store = kc.KnownCollections(tmp_path / "kc.sqlite")
    ids = [_id(k) for k in range(60)]
    items = _pieces("Doge Frog", ids)[::-1]                                  # the source lists them newest first
    items.append({"id": "not an id", "name": "x"})
    items.append(dict(items[0]))                                              # the same piece twice
    fp = store.put("doge", "doge-frogs", "Doge Frog", 60, items, "nintondo", sig="60|1|60|9")
    assert fp == kc.fingerprint("doge", "doge-frogs", ids)
    row = store.collection("doge", "doge-frogs")
    assert row["count"] == 60 and row["cover"] == ids[0] and row["source"] == "nintondo" and row["fp"] == fp
    page = store.items("doge", "doge-frogs", offset=48, limit=48)
    assert [p["position"] for p in page] == list(range(49, 61)) and page[0]["name"] == "Doge Frog #49"
    assert store.in_collection("doge", ids[6]) == {"slug": "doge-frogs", "name": "Doge Frog #7", "position": 7,
                                                   "collection": "Doge Frog", "source": "nintondo"}
    assert store.in_collection("main", ids[6]) is None, "a chain's own"
    store.put("doge", "cats", "Bell Cats", 2, _pieces("Cat", [_id(100), _id(101)]), "mesh:abc")
    assert [c["slug"] for c in store.collections("doge", "frog")] == ["doge-frogs"]
    assert [c["slug"] for c in store.collections("doge", "")] == ["doge-frogs", "cats"], "biggest first"
    assert store.collections("doge", "%") == [], "a wildcard is plain text"
    assert store.digest("doge") == kc.chain_digest([("doge-frogs", fp), ("cats", store.collection("doge", "cats")["fp"])])
    store.put("doge", "doge-frogs", "Doge Frog", 60, items[:10], "nintondo")
    assert store.collection("doge", "doge-frogs")["count"] == 10 and store.in_collection("doge", ids[6]) is None, \
        "a list kept again replaces the old one"
    with pytest.raises(ValueError):
        store.put("btc", "x", "x", 1, [], "nintondo")


def test_the_store_is_bounded(tmp_path, monkeypatch):
    store = kc.KnownCollections(tmp_path / "kc.sqlite")
    monkeypatch.setattr(kc, "MAX_ITEMS", 5)
    monkeypatch.setattr(kc, "MAX_TOTAL", 8)
    with pytest.raises(ValueError):
        store.put("doge", "big", "Big", 6, _pieces("Big", [_id(k) for k in range(6)]), "nintondo")
    store.put("doge", "a", "A", 5, _pieces("A", [_id(k) for k in range(5)]), "nintondo")
    with pytest.raises(ValueError):
        store.put("doge", "b", "B", 4, _pieces("B", [_id(k) for k in range(10, 14)]), "nintondo")
    assert store.collection("doge", "b") is None


# --------------------------------------------------------------------------- the importer (a faked Nintondo)

class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Nintondo:
    """Nintondo's public search, one host per chain, as confirmed 2026-10-26: /pub/collections pages of 50 (any
    page_size asked), /pub/search pages of up to 100 pieces, newest first."""

    def __init__(self, clock, chains):
        self.clock, self.chains, self.calls = clock, chains, []
        self.fail = 0

    def __call__(self, method, url, body=None):
        u = urllib.parse.urlsplit(url)
        host = f"{u.scheme}://{u.netloc}"
        self.calls.append((host, method, u.path + ("?" + u.query if u.query else ""), json.dumps(body, sort_keys=True),
                           self.clock()))
        if self.fail:
            self.fail -= 1
            raise OSError("connection reset")
        colls = self.chains[host]
        if u.path == "/pub/collections":
            q = urllib.parse.parse_qs(u.query)
            size, page = int(q["page_size"][0]), int(q["page"][0])
            rows = sorted(colls)
            pages = -(-len(rows) // size)
            shown = rows[(page - 1) * size: page * size]
            return {"collections": [{"id": k, "name": c["name"], "slug": slug, "supply": len(c["items"]),
                                     "numbers": [c["items"][0]["number"], c["items"][-1]["number"]] if c["items"] else [],
                                     "size": 100 * len(c["items"]), "owners": c.get("owners", 3),
                                     "height": c.get("height", 4_000_000 + k), "created": c.get("created")}
                                    for k, (slug, c) in enumerate((s, colls[s]) for s in shown)],
                    "count": len(rows), "pages": pages}
        assert u.path == "/pub/search" and method == "POST" and body["page_size"] <= 100
        c = colls[body["collection"]]
        items = list(reversed(c["items"]))
        size, page = body["page_size"], body["page"]
        shown = items[(page - 1) * size: page * size]
        return {"inscriptions": [{"id": i["id"], "number": i["number"], "file_type": "PNG",
                                  "collection": {"name": c["name"], "slug": body["collection"],
                                                 "meta": {"name": i["name"]}}} for i in shown],
                "count": len(items), "pages": -(-len(items) // size)}


def _source(slugs: dict) -> dict:
    """{slug: n pieces} -> a faked chain's collections."""
    out, k = {}, 0
    for slug, n in slugs.items():
        name = slug.replace("-", " ").title()
        out[slug] = {"name": name, "items": _pieces(name, [_id(k + j) for j in range(n)], 5000 + k)}
        k += n
    return out


DOGE, PEPE = kc.SOURCES["doge"], kc.SOURCES["main"]


def _importer(tmp_path, clock, fake, on=lambda: True, mode="all", offered=None):
    """The tests before 2026-10-26 read every collection: mode "all" (the default setting is "demand")."""
    store = kc.KnownCollections(tmp_path / "kc.sqlite")
    return store, kc.Importer(store, on, fetch=fake, clock=clock, now=clock, sleep=clock.sleep,
                              sources={"doge": DOGE, "main": PEPE}, mode=lambda: mode if isinstance(mode, str) else mode(),
                              offered=offered)


def _run(imp, clock, steps):
    """Steps with the clock moved on by each wait, until every chain rests (a wait of hours)."""
    for _ in range(steps):
        wait = imp.step()
        if wait > 3600:
            return
        clock.t += wait


def test_the_importer_pages_everything_at_a_request_a_second_per_host(tmp_path):
    clock = Clock()
    fake = Nintondo(clock, {DOGE: _source({f"c{k:02d}": 3 for k in range(55)} | {"bone": 250}),
                            PEPE: _source({"council-of-69": 69})})
    store, imp = _importer(tmp_path, clock, fake)
    _run(imp, clock, 400)
    assert store.counts() == {"doge": {"collections": 56, "pieces": 55 * 3 + 250},
                              "main": {"collections": 1, "pieces": 69}}
    bone = store.collection("doge", "bone")
    assert bone["source"] == "nintondo" and bone["fp"] == kc.fingerprint("doge", "bone", [_id(55 * 3 + j) for j in range(250)])
    assert [r["name"] for r in store.items("doge", "bone", 0, 2)] == ["Bone #1", "Bone #2"], "in their own order"
    for host in (DOGE, PEPE):
        times = [c[4] for c in fake.calls if c[0] == host]
        assert all(b - a >= kc.PACE for a, b in zip(times, times[1:])), f"{host}: at most one request a second"
    assert len([c for c in fake.calls if c[0] == DOGE and c[2].startswith("/pub/collections")]) == 2, "50 a page"
    assert len([c for c in fake.calls if c[0] == DOGE and '"bone"' in c[3]]) == 3, "100 pieces a page"
    both = sorted(c[4] for c in fake.calls)
    assert any(b - a < kc.PACE for a, b in zip(both, both[1:])), "two hosts are asked side by side"
    assert all(c["phase"] == "rest" for c in imp.status()["chains"])


def test_the_importer_goes_on_where_it_stopped_after_a_restart(tmp_path):
    clock = Clock()
    fake = Nintondo(clock, {DOGE: _source({"a": 150, "b": 250}), PEPE: _source({})})
    store, imp = _importer(tmp_path, clock, fake)
    _run(imp, clock, 4)                                       # the listing and part of the first collection
    assert store.counts() == {}
    _store, again = _importer(tmp_path, clock, fake)          # started again on the same file
    _run(again, clock, 50)
    assert store.counts()["doge"] == {"collections": 2, "pieces": 400}
    asked = [(c[0], c[2], c[3]) for c in fake.calls]
    assert len(asked) == len(set(asked)), "no page asked twice"


def test_a_day_later_only_a_changed_collection_is_read_again(tmp_path):
    clock = Clock()
    chain = _source({"a": 20, "b": 20, "c": 20})
    fake = Nintondo(clock, {DOGE: chain, PEPE: _source({})})
    store, imp = _importer(tmp_path, clock, fake)
    _run(imp, clock, 30)
    before = len(fake.calls)
    clock.t += 3600
    _run(imp, clock, 10)
    assert len(fake.calls) == before, "resting until the day is over"
    chain["b"]["items"].append({"id": _id(999), "name": "B #21", "number": 99_999})
    clock.t += kc.REFRESH
    _run(imp, clock, 30)
    searched = [json.loads(c[3])["collection"] for c in fake.calls[before:] if c[2] == "/pub/search"]
    assert searched == ["b"], "only the one whose line changed"
    assert store.collection("doge", "b")["count"] == 21 and store.in_collection("doge", _id(999))["name"] == "B #21"


def test_the_importer_asks_nothing_while_switched_off_and_waits_after_an_error(tmp_path):
    clock = Clock()
    fake = Nintondo(clock, {DOGE: _source({"a": 5}), PEPE: _source({})})
    on = {"v": False}
    store, imp = _importer(tmp_path, clock, fake, on=lambda: on["v"])
    _run(imp, clock, 20)
    assert fake.calls == [], "Martin's rule stands until the operator switches it on"
    on["v"] = True
    fake.fail = 1
    _run(imp, clock, 1)
    assert "connection reset" in imp.status()["chains"][0]["error"]
    failed_at = fake.calls[-1][4]
    _run(imp, clock, 30)
    doge = [c[4] for c in fake.calls if c[0] == DOGE]
    assert doge[1] - failed_at >= 30, "a host that failed is left alone a while"
    assert store.counts()["doge"]["pieces"] == 5


# --------------------------------------------------------------------------- the mesh: two (or three) fake arcades

class Link:
    def __init__(self, peer_id):
        self.peer_id, self.far, self.frames = peer_id, None, []

    async def send(self, header, blob=b""):
        assert len(json.dumps(header)) + len(blob) < 64 * 1024, "every frame under the link's 64 KB"
        self.frames.append(header.get("op"))
        await self.far.share.on_frame(self.far, header, blob)


class Node:
    def __init__(self):
        self.links = {}


class Arcade:
    def __init__(self, tmp_path, name, mode="all"):
        self.name = name
        self.store = kc.KnownCollections(tmp_path / f"{name}.sqlite")
        self.share = cs.CollectionShare(lambda: self.store, mode=lambda: mode)
        self.share.node = Node()


def _link(a: Arcade, b: Arcade):
    """a's link to b (what a sends, b hears on its own link from a) and the other way round."""
    to_b, to_a = Link(b.name), Link(a.name)
    to_b.far, to_a.far = to_a, to_b
    to_a.share, to_b.share = b.share, a.share                # a link's far end is heard by the other arcade
    a.share.node.links[b.name] = to_b
    b.share.node.links[a.name] = to_a
    return to_b, to_a


async def _settle():
    """Until every answer sent on its own (ix, ig) has been heard."""
    me = asyncio.current_task()
    for _ in range(500):
        await asyncio.sleep(0.01)
        if not [t for t in asyncio.all_tasks() if t is not me and not t.done()]:
            return


def _frogs(store, n=30, source="nintondo", extra=()):
    ids = [_id(k) for k in range(n)] + list(extra)
    return store.put("doge", "frogs", "Frogs", n, _pieces("Frogs", ids), source, sig=f"{n}|x")


def test_two_arcades_compare_digests_swap_indexes_and_fetch_in_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "PART", 300)                      # a list in many parts
    monkeypatch.setattr(cs, "ROWS_PER_FRAME", 2)              # an index in parts
    a, b = Arcade(tmp_path, "a" * 64), Arcade(tmp_path, "b" * 64)
    fp = _frogs(a.store)
    for k in range(4):
        a.store.put("main", f"pep-{k}", f"Pep {k}", 3, _pieces(f"Pep {k}", [_id(500 + 10 * k + j) for j in range(3)]),
                    "nintondo")
    to_b, to_a = _link(a, b)

    async def go():
        await a.share.link_up(to_b)
        await b.share.link_up(to_a)
        await _settle()
        for _ in range(8):                                    # one collection a fetch
            await b.share.rebalance()
            await _settle()
    asyncio.run(go())
    got = b.store.collection("doge", "frogs")
    assert got is not None and got["fp"] == fp and got["source"] == "mesh:" + "a" * 12 and got["sig"] == "30|x"
    assert b.store.counts() == a.store.counts(), "every list, the same"
    assert b.store.items("doge", "frogs", 0, 3) == a.store.items("doge", "frogs", 0, 3)
    assert to_b.frames.count("ib") > 3 and to_b.frames.count("il") >= 2, "pieces and the index came in parts"
    assert a.share.agreements("doge") == {"frogs": 2} and b.share.agreements("main") == {f"pep-{k}": 2 for k in range(4)}
    assert b.share.agreement("doge", "frogs") == 2
    assert a.store.digest("doge") == b.store.digest("doge")
    assert to_a.frames.count("ix") >= 1, "the arcade with nothing asked for each chain's index"
    b.share.node.links.clear()
    assert b.share.agreements("doge") == {"frogs": 1}, "only linked arcades count"


def test_a_tampered_list_is_refused(tmp_path):
    a, b = Arcade(tmp_path, "a" * 64), Arcade(tmp_path, "b" * 64)
    _frogs(a.store)
    real = a.store.all_items

    def tampered(net, slug):
        rows = real(net, slug)
        rows[3] = [_id(777), "Frogs #4", 4, 1003]             # one piece swapped for another
        return rows
    a.store.all_items = tampered
    to_b, to_a = _link(a, b)

    async def go():
        await a.share.link_up(to_b)
        await b.share.link_up(to_a)
        await _settle()
        await b.share.rebalance()
        await _settle()
    asyncio.run(go())
    assert b.store.collection("doge", "frogs") is None, "pieces whose hash is not the one announced are never kept"
    assert b.share.fetched[-1][:2] == ("doge", "frogs") and b.share.fetched[-1][3] is False
    assert to_a.frames.count("ig") == 1, "and that arcade is not asked for it again so soon"
    assert b.share.wanted() == []


def test_a_list_most_arcades_agree_on_replaces_an_older_one(tmp_path):
    a, b, c = (Arcade(tmp_path, x * 64) for x in "abc")
    new = _frogs(a.store, extra=[_id(900)])
    _frogs(c.store, extra=[_id(900)])
    old = _frogs(b.store, source="mesh:" + "d" * 12)
    pairs = [(a, b), (c, b), (a, c)]
    links = [_link(x, y) for x, y in pairs]

    async def go():
        for (x, y), (to_y, to_x) in zip(pairs, links):
            await x.share.link_up(to_y)
            await y.share.link_up(to_x)
        await _settle()
        await b.share.rebalance()
        await _settle()
    asyncio.run(go())
    assert b.store.collection("doge", "frogs")["fp"] == new, "two arcades of three agree on the newer list"
    assert b.share.agreements("doge") == {"frogs": 3}


def test_a_tie_or_a_list_read_from_the_source_today_stays(tmp_path):
    a, b = Arcade(tmp_path, "a" * 64), Arcade(tmp_path, "b" * 64)
    _frogs(a.store, extra=[_id(900)])
    mine = _frogs(b.store, source="mesh:" + "d" * 12)
    to_b, to_a = _link(a, b)

    async def go():
        await a.share.link_up(to_b)
        await b.share.link_up(to_a)
        await _settle()
        await b.share.rebalance()
        await _settle()
    asyncio.run(go())
    assert b.store.collection("doge", "frogs")["fp"] == mine, "one against one: each keeps its own"
    assert b.share.agreements("doge") == {"frogs": 1}


# --------------------------------------------------------------------------- Explore and Admin

def test_explore_lists_a_known_collection_pages_it_and_names_a_piece(walking):
    app, state = walking
    store = state.known_collections()
    ids = ["01" * 32 + "i0", "02" * 32 + "i0", "03" * 32 + "i0"]
    store.put("main", "pepe-frogs", "Pepe Frogs", 3, _pieces("Pepe Frog", ids), "nintondo")
    said = app.get("/explore/api/collections?net=main&q=frog", headers=EDGE).json()["collections"]
    assert [(c["kind"], c["name"], c["source"], c["agreed"], c["count"]) for c in said] == \
        [("known", "Pepe Frogs", "nintondo", 1, 3)], "searchable, with its source and how many arcades agree"
    assert app.get("/explore/api/collections?net=main&q=dogs", headers=EDGE).json()["collections"] == []
    assert app.get("/explore/api/collections?net=doge&q=frog", headers=EDGE).json()["collections"] == [], "its chain's"
    page = app.get("/explore/api/collection?net=main&slug=pepe-frogs&offset=1", headers=EDGE).json()
    assert page["collection"]["name"] == "Pepe Frogs" and [i["name"] for i in page["items"]] == \
        ["Pepe Frog #2", "Pepe Frog #3"] and page["items"][0]["id"] == ids[1]
    assert app.get("/explore/api/collection?net=main&slug=nope", headers=EDGE).status_code == 404
    assert app.get("/explore/api/collection?net=btc&slug=pepe-frogs", headers=EDGE).status_code == 400
    assert app.get("/explore/api/collection?net=main&slug=../x", headers=EDGE).status_code == 400
    seen = app.get("/explore/api/inscription?net=main&number=1", headers=EDGE).json()
    assert seen["collection"] == {"slug": "pepe-frogs", "collection": "Pepe Frogs", "name": "Pepe Frog #2",
                                  "position": 2, "source": "nintondo"}, "In collection: Pepe Frog #2"
    by_id = app.get(f"/explore/api/inscription?net=main&id={ids[2]}", headers=EDGE).json()
    assert by_id["collection"]["name"] == "Pepe Frog #3"
    store.put("main", "pepe-frogs", "Pepe Frogs", 3, [], "nintondo")
    assert "collection" not in app.get("/explore/api/inscription?net=main&number=1", headers=EDGE).json()
    text = app.get("/explore", headers=EDGE).text
    assert "agreed by" in text and "In collection" in text


def test_admin_switches_the_import_on_and_off(client):
    app, state = client
    assert state.setting(kc.SETTING) is None, "off by default"
    got = app.get("/admin/api/known-collections", headers=LOCAL).json()
    assert got["on"] is False and [c["net"] for c in got["chains"]] == ["doge", "main", "bel"]
    assert app.post("/admin/api/known-collections", headers=LOCAL, json={"import": True}).status_code == 403
    assert app.post("/admin/api/known-collections", headers={**LOCAL, **WRITE}, json={"import": "yes"}).status_code == 400
    import os
    os.environ["ARCADE_NO_KNOWN_COLLECTIONS"] = "1"           # no thread here: the switch alone
    try:
        assert app.post("/admin/api/known-collections", headers={**LOCAL, **WRITE},
                        json={"import": True}).json()["import"] is True
    finally:
        os.environ.pop("ARCADE_NO_KNOWN_COLLECTIONS", None)
    assert state.setting(kc.SETTING) is True and app.get("/admin/api/known-collections", headers=LOCAL).json()["on"]
    app.post("/admin/api/known-collections", headers={**LOCAL, **WRITE}, json={"import": False})
    assert state.setting(kc.SETTING) is False
    assert 'id="kcimport"' in app.get("/admin", headers=LOCAL).text


# --------------------------------------------------------------------------- on demand (2026-10-26)
#
# SadFrog: "can you make the collection search more in depth with sorting and filters and lazy loading one at a time on
# demand." Every collection of Nintondo's is about 885,000 pieces, so by default only the listings are read, and a
# collection's pieces when somebody opens it (a want).

def _searched(fake, since=0):
    return [json.loads(c[3])["collection"] for c in fake.calls[since:] if c[2] == "/pub/search"]


def test_the_listing_details_are_kept_and_an_old_file_is_upgraded_in_place(tmp_path):
    import sqlite3
    path = tmp_path / "kc.sqlite"
    old = sqlite3.connect(path)                               # a file as an arcade wrote it before 2026-10-26
    old.executescript("""CREATE TABLE kc_listing (chain TEXT NOT NULL, slug TEXT NOT NULL, name TEXT NOT NULL,
        supply INTEGER, sig TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (chain, slug));
        INSERT INTO kc_listing VALUES ('doge', 'old-frogs', 'Old Frogs', 7, '7|1|7|700', 1);""")
    old.commit()
    old.close()
    store = kc.KnownCollections(path)
    line = store.listed("doge", "old-frogs")
    assert line["name"] == "Old Frogs" and line["done"] == 1 and line["owners"] is None and line["skipped"] == "", \
        "its row kept, the new columns added"
    kc.KnownCollections(path)                                 # opened again: nothing added twice
    clock = Clock()
    chain = _source({"frogs": 4, "toads": 2})
    chain["frogs"].update(owners=41, height=5_000_123, created="2024-03-01T12:00:00Z")
    fake = Nintondo(clock, {DOGE: chain, PEPE: _source({})})
    imp = kc.Importer(store, lambda: True, fetch=fake, clock=clock, now=clock, sleep=clock.sleep,
                      sources={"doge": DOGE, "main": PEPE})
    _run(imp, clock, 30)
    frogs = store.listed("doge", "frogs")
    assert (frogs["owners"], frogs["height"], frogs["created"], frogs["first"], frogs["last"], frogs["size"]) == \
        (41, 5_000_123, 1709294400, 5000, 5003, 400), "every detail of the listing line"
    assert store.listed("doge", "old-frogs") is None, "a line the new listing lacks is dropped once it is read"
    assert _searched(fake) == [], "on demand by default: nothing opened, no pieces asked"
    assert {r["slug"]: r["imported"] for r in store.catalog("doge")} == {"frogs": False, "toads": False}
    assert kc.listing_details({"created": 1_709_294_400_000, "owners": True})["created"] == 1709294400, "milliseconds"
    assert kc.listing_details({"created": "soon", "owners": True}) == \
        {"owners": None, "height": None, "created": None, "first": None, "last": None, "size": None}


def test_on_demand_only_wanted_and_changed_held_collections_are_fetched(tmp_path, monkeypatch):
    clock = Clock()
    chain = _source({"a": 5, "b": 5, "c": 5, "huge": 9})
    fake = Nintondo(clock, {DOGE: chain, PEPE: _source({})})
    store, imp = _importer(tmp_path, clock, fake, mode="demand")
    store.put("doge", "b", "B", 5, chain["b"]["items"][:4], "nintondo", sig="4|old")   # held, its line since changed
    _run(imp, clock, 40)
    assert _searched(fake) == ["b"], "only the held one whose line changed"
    assert store.collection("doge", "b")["count"] == 5
    assert store.collection("doge", "a") is None and store.collection("doge", "c") is None
    before = len(fake.calls)
    store.want("doge", "c", now=clock())
    store.want("doge", "a", now=clock() + 1)
    store.want("doge", "c", now=clock() + 2)                  # asked again: keeps its place
    assert set(store.wants("doge")) == {"a", "c"}
    _run(imp, clock, 40)
    assert _searched(fake, before) == ["c", "a"], "wanted ones, oldest want first"
    assert store.collection("doge", "a")["count"] == 5 and store.wants("doge") == {}, "met wants are cleared"
    monkeypatch.setattr(kc, "MAX_ITEMS", 6)
    before = len(fake.calls)
    store.want("doge", "huge", now=clock())
    _run(imp, clock, 20)
    assert _searched(fake, before) == [] and store.wants("doge") == {}, "too big: skipped, the want cleared"
    assert "more than 6" in store.listed("doge", "huge")["skipped"]
    assert imp.status()["mode"] == "demand"


def test_mode_all_reads_every_collection_wanted_ones_first(tmp_path):
    clock = Clock()
    fake = Nintondo(clock, {DOGE: _source({"a": 3, "b": 3, "c": 3}), PEPE: _source({})})
    store, imp = _importer(tmp_path, clock, fake, mode="all")
    imp.step()                                                # the listing's one page
    store.want("doge", "c", now=clock())
    _run(imp, clock, 40)
    assert _searched(fake) == ["c", "a", "b"], "the wanted one, then the rest as before"
    assert store.counts()["doge"] == {"collections": 3, "pieces": 9}


def test_a_want_is_served_while_the_chain_rests(tmp_path):
    clock = Clock()
    fake = Nintondo(clock, {DOGE: _source({"a": 3, "b": 150}), PEPE: _source({})})
    store, imp = _importer(tmp_path, clock, fake, mode="demand")
    _run(imp, clock, 20)
    st = imp._progress("doge")
    assert st["phase"] == "rest" and store.counts() == {}
    rested_at = st["round_at"]
    clock.t += 3600
    store.want("doge", "b", now=clock())
    started = clock()
    _run(imp, clock, 20)
    assert store.collection("doge", "b")["count"] == 150 and clock() - started < 10, "fetched at once, not tomorrow"
    st = imp._progress("doge")
    assert st["phase"] == "rest" and st["round_at"] == rested_at, "the day's round left as it was"
    before = len(fake.calls)
    clock.t = rested_at + kc.REFRESH + 1
    _run(imp, clock, 20)
    assert any(c[2].startswith("/pub/collections") for c in fake.calls[before:]), "the next round on time"


def test_a_want_a_linked_arcade_holds_is_left_to_the_mesh_a_minute(tmp_path):
    clock = Clock()
    fake = Nintondo(clock, {DOGE: _source({"a": 3}), PEPE: _source({})})
    store, imp = _importer(tmp_path, clock, fake, mode="demand", offered=lambda net, slug: slug == "a")
    _run(imp, clock, 20)
    store.want("doge", "a", now=clock())
    asked_at = clock()
    for _ in range(10):
        clock.t += min(imp.step(), 30)
        if clock() - asked_at > kc.MESH_FIRST - 5:
            break
    assert _searched(fake) == [], "a linked arcade holds it: the mesh first"
    _run(imp, clock, 20)
    assert _searched(fake) == ["a"] and clock() - asked_at >= kc.MESH_FIRST, "then Nintondo, when the mesh did not"


def test_a_new_want_wakes_the_importer(tmp_path):
    import threading
    import time as _time
    store = kc.KnownCollections(tmp_path / "kc.sqlite")
    imp = kc.Importer(store, lambda: False)
    threading.Timer(0.1, imp.wake.set).start()
    t = _time.monotonic()
    imp.pause(20)
    assert _time.monotonic() - t < 5 and not imp.wake.is_set()


def test_on_demand_the_mesh_fetches_only_held_or_wanted_collections(tmp_path):
    a, c = Arcade(tmp_path, "a" * 64), Arcade(tmp_path, "c" * 64)
    b = Arcade(tmp_path, "b" * 64, mode="demand")
    new = _frogs(a.store, extra=[_id(900)])
    _frogs(c.store, extra=[_id(900)])
    for x in (a, c):
        x.store.put("doge", "toads", "Toads", 3, _pieces("Toads", [_id(300 + j) for j in range(3)]), "nintondo")
        x.store.put("doge", "newts", "Newts", 2, _pieces("Newts", [_id(400 + j) for j in range(2)]), "nintondo")
    _frogs(b.store, source="mesh:" + "d" * 12)               # b holds an older list of frogs
    pairs = [(a, b), (c, b)]
    links = [_link(x, y) for x, y in pairs]

    async def turn():
        await b.share.rebalance()
        await _settle()

    async def go():
        for (x, y), (to_y, to_x) in zip(pairs, links):
            await x.share.link_up(to_y)
            await y.share.link_up(to_x)
        await _settle()
        for _ in range(3):
            await turn()
    asyncio.run(go())
    assert b.store.collection("doge", "frogs")["fp"] == new, "a held one follows the agreement as before"
    assert b.store.collection("doge", "toads") is None and b.store.collection("doge", "newts") is None, \
        "nobody here opened them: not fetched"
    assert b.share.offered("doge", "toads") and not b.share.offered("doge", "frogs-nope")
    assert b.share.offers("doge")["toads"]["agree"] == 2
    b.store.want("doge", "toads")
    asyncio.run(turn())
    got = b.store.collection("doge", "toads")
    assert got is not None and got["source"].startswith("mesh:") and b.store.wants("doge") == {}, \
        "a want met from a linked arcade, and cleared"
    assert b.store.collection("doge", "newts") is None


def test_admin_chooses_on_demand_or_every_collection(client):
    app, state = client
    got = app.get("/admin/api/known-collections", headers=LOCAL).json()
    assert got["mode"] == "demand", "on demand by default"
    assert app.post("/admin/api/known-collections", headers=LOCAL, json={"mode": "all"}).status_code == 403
    for bad in ({"mode": "some"}, {"mode": 1}, {}, {"import": True, "mode": "x"}):
        assert app.post("/admin/api/known-collections", headers={**LOCAL, **WRITE}, json=bad).status_code == 400, bad
    said = app.post("/admin/api/known-collections", headers={**LOCAL, **WRITE}, json={"mode": "all"}).json()
    assert said == {"ok": True, "mode": "all"} and state.setting(kc.MODE_SETTING) == "all"
    assert app.get("/admin/api/known-collections", headers=LOCAL).json()["mode"] == "all"
    assert state.setting(kc.SETTING) is None, "the import switch untouched"
    app.post("/admin/api/known-collections", headers={**LOCAL, **WRITE}, json={"mode": "demand"})
    assert state.setting(kc.MODE_SETTING) == "demand"
    page = app.get("/admin", headers=LOCAL).text
    assert 'id="kcmode"' in page and 'value="demand"' in page
