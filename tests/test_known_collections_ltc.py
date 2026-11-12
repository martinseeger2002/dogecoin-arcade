"""Litecoin's known collections from several sites (2026-11-07, SadFrog: "we want to make sure the explore for ordinals
collections on litecoin picks up chikun.market and liteverse.io's index as well as the new
https://liteord.doginals.store/inscriptions"). arcade/known_collections.py: an adapter per site (listing -> lines,
collection -> pieces), each site's lists kept under "<source>:<key>", fingerprints and the mesh share unchanged.

The answers are the sites' own, recorded 2026-11-07 and trimmed (tests/fixtures/ltc_collections/): chikun.market's
`/v1/collections` (4 of 47, its royalty addresses and links left out), liteverse.io's `/collections` (5 of 39: a slug it
gives twice, a slug with "*", Litecoin Punks, Sad Frog; creator fields blanked) and `/collections/12/inscriptions` (the
first 5 of Litecoin Punks' 100), and ord's `/r/children/<parent>/inscriptions/<page>` (3 and 2 of a parent's children;
read from ord.chikun.market, the same ord LiteOrd serves `/r/*` from, as LiteOrd's own index had not reached the first
Litecoin inscription yet). Nothing here reaches the internet."""
import asyncio
import io
import json
import pathlib
import sys
import urllib.error
import urllib.parse

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_ord_explorer import EDGE, walking                                     # noqa: F401,E402
from test_doginal_index import small                                            # noqa: F401,E402
from test_web import app_state, client                                          # noqa: F401,E402
from test_known_collections import LOCAL, WRITE, Arcade, Clock, _id, _link, _pieces, _settle   # noqa: E402

from arcade import known_collections as kc                                      # noqa: E402
from arcade.mesh import collectionshare as cs                                   # noqa: E402

FIX = pathlib.Path(__file__).resolve().parent / "fixtures" / "ltc_collections"
CHIKUN, LITEVERSE, LITEORD = (kc.MULTI_SOURCES["ltc"][s] for s in ("chikun", "liteverse", "liteord"))
PARENT = "f14af2c5df58488deadc29bee047b165b3e1c9bb4dddb5c1d01201465ffcbeabi0"


def fx(name: str):
    return json.loads((FIX / name).read_text())


class Sites:
    """The three sites, answering from the recorded fixtures; every request noted (host, path, when)."""

    def __init__(self, clock):
        self.clock, self.calls = clock, []
        self.fail: dict[str, Exception] = {}

    def __call__(self, method, url, body=None):
        u = urllib.parse.urlsplit(url)
        host, path = f"{u.scheme}://{u.netloc}", u.path
        self.calls.append((host, path, self.clock()))
        assert method == "GET" and body is None
        if host in self.fail:
            raise self.fail[host]
        if host == CHIKUN and path == "/v1/collections":
            return fx("chikun_collections.json")
        if host == LITEVERSE and path == "/collections":
            return fx("liteverse_collections.json")
        if host == LITEVERSE and path == "/collections/12/inscriptions":
            return fx("liteverse_inscriptions_12.json")
        if host == LITEORD and path.startswith(f"/r/children/{PARENT}/inscriptions/"):
            return fx(f"ord_children_{int(path.rsplit('/', 1)[1])}.json")
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b'{"error":"not found"}'))


def _ask(fake, host):
    return lambda method, path, body=None: fake(method, host + path, body)


def _importer(tmp_path, clock, fake, switched=None, mode="demand"):
    store = kc.KnownCollections(tmp_path / "kc.sqlite")
    return store, kc.Importer(store, lambda: True, fetch=fake, clock=clock, now=clock, sleep=clock.sleep, sources={},
                              multi=kc.MULTI_SOURCES, mode=lambda: mode, switched=switched)


def _run(imp, clock, steps=60):
    for _ in range(steps):
        wait = imp.step()
        if wait > 3600:
            return
        clock.t += wait


# --------------------------------------------------------------------------- each site's adapter, on its own answers

def test_chikun_lists_its_collections_and_says_it_publishes_no_pieces():
    clock = Clock()
    ad = kc.ChikunAdapter(CHIKUN)
    lines, more = ad.listing(_ask(Sites(clock), CHIKUN), 1)
    by = {r["key"]: r for r in lines}
    assert not more and set(by) == {"punks", "sadfrog", "2048", "litebars-10-percent"}, "every one in a single answer"
    punks = by["punks"]
    assert (punks["name"], punks["supply"], punks["sig"]) == ("Litecoin Punks", 100, "100"), "its numbers come as strings"
    assert punks["cover"] == "ebc07ae334fbd2753a32550dd28341af90c2219885b401a529307ed8f093fd17i0", "the image's inscription"
    assert punks["owners"] is None, "it says 0 holders of every collection: not shown as a fact"
    assert punks["skipped"] == ad.NO_PIECES and not ad.pieces
    with pytest.raises(kc.Gone):
        ad.items(_ask(Sites(clock), CHIKUN), "punks", 1)
    with pytest.raises(ValueError):
        ad.listing(lambda *a, **k: {"status": 0, "message": "Error"}, 1)


def test_liteverse_lists_by_its_numeric_id_and_reads_a_whole_collection():
    clock = Clock()
    ad = kc.LiteverseAdapter(LITEVERSE)
    lines, more = ad.listing(_ask(Sites(clock), LITEVERSE), 1)
    by = {r["key"]: r for r in lines}
    assert not more and set(by) == {"1", "12", "27", "44", "45"}
    assert by["44"]["name"] == by["45"]["name"] == "HISTORICAL GENESIS", "its own slug repeats: the id keeps them apart"
    assert by["1"]["name"] == "Ah sh*t, here we ordinal again" and kc.slug_ok("ltc", "liteverse:1"), \
        "a slug with '*' is no matter: the key is the id"
    assert (by["12"]["supply"], by["12"]["sig"]) == (100, "100|100")
    pieces, more, count, _pages = ad.items(_ask(Sites(clock), LITEVERSE), "12", 1)
    assert not more and count is None and len(pieces) == 5
    assert pieces[0] == {"id": "f2b6adf7b2d0f128fb14817ff37f5f36e9176b17571e20c49871875553f937b4i0", "name": "",
                         "position": 1, "number": 642}, "a piece named '1' is Litecoin Punks #1"
    assert all(kc.ID.match(p["id"]) for p in pieces)
    other = fx("liteverse_inscriptions_12.json")
    other[2]["collection_id"] = 13
    with pytest.raises(ValueError):
        ad.items(lambda *a, **k: other, "12", 1)
    with pytest.raises(ValueError):
        ad.items(lambda *a, **k: {"error": "busy"}, "12", 1)
    with pytest.raises(kc.Gone):
        ad.items(lambda *a, **k: [], "../x", 1)


def test_liteord_reads_a_parents_children_the_ord_way_page_by_page():
    clock = Clock()
    fake = Sites(clock)
    ad = kc.OrdChildrenAdapter(LITEORD)
    assert not ad.lists and ad.listing(_ask(fake, LITEORD), 1) == ([], False) and not fake.calls, \
        "LiteOrd has no list of collections: nothing asked"
    first, more, _c, _p = ad.items(_ask(fake, LITEORD), PARENT, 1)
    assert more and [p["position"] for p in first] == [1, 2, 3] and first[0]["number"] == 26845160
    second, more, _c, _p = ad.items(_ask(fake, LITEORD), PARENT, 2)
    assert not more and [p["position"] for p in second] == [101, 102], "page 2 of ord's (page 1, from 0): 100 a page"
    assert [c[1] for c in fake.calls] == [f"/r/children/{PARENT}/inscriptions/0", f"/r/children/{PARENT}/inscriptions/1"]
    with pytest.raises(kc.Gone, match="does not know"):
        ad.items(_ask(fake, LITEORD), _id(5), 1)              # not indexed there (yet): a 404
    with pytest.raises(ValueError):
        ad.items(lambda *a, **k: {"ids": []}, PARENT, 1)


# --------------------------------------------------------------------------- slugs, fingerprints, the importer

def test_litecoin_slugs_carry_their_source_and_bind_the_fingerprint(tmp_path):
    assert "ltc" in kc.CHAINS
    assert kc.slug_ok("ltc", "chikun:punks") and kc.slug_ok("ltc", "liteverse:12") and kc.slug_ok("ltc", "liteord:" + PARENT)
    for bad in ("punks", "nintondo:punks", "othersite:punks", "chikun:", "chikun:../x", ":punks", None,
                "liteverse:punks", "liteverse:012", "liteord:nope", "liteord:" + PARENT.upper()):
        assert not kc.slug_ok("ltc", bad), bad
    assert kc.slug_ok("doge", "bone") and not kc.slug_ok("doge", "chikun:punks"), "Nintondo's chains keep plain slugs"
    ids = [_id(k) for k in range(5)]
    assert kc.fingerprint("ltc", "chikun:12", ids) != kc.fingerprint("ltc", "liteverse:12", ids), \
        "the same pieces from two sites: two fingerprints, never one row"
    assert kc.fingerprint("ltc", "liteverse:12", ids) == kc.fingerprint("ltc", "liteverse:12", reversed(ids))
    store = kc.KnownCollections(tmp_path / "kc.sqlite")
    with pytest.raises(ValueError):
        store.put("ltc", "punks", "Punks", 5, _pieces("Punk", ids), "liteverse")
    with pytest.raises(ValueError):
        store.put("doge", "chikun:punks", "Punks", 5, _pieces("Punk", ids), "chikun")
    assert kc.source_of("ltc", "liteverse:12") == "liteverse" and kc.source_of("doge", "bone") == "nintondo"
    assert kc.source_link("ltc", "liteverse:12") == "https://liteverse.io/market/collection?id=12"
    assert kc.source_link("ltc", "chikun:punks") == "https://chikun.market/collections/punks"
    assert kc.ltc_sources(None) == ("chikun", "liteord"), "liteverse.io only when the operator ticks it (its terms)"
    assert kc.ltc_sources(["liteverse", "nope"]) == ("liteverse",) and kc.ltc_sources([]) == ()


def test_the_importer_reads_each_ticked_site_at_its_own_pace(tmp_path):
    clock = Clock()
    fake = Sites(clock)
    store, imp = _importer(tmp_path, clock, fake, switched=lambda: kc.ltc_sources(None))
    _run(imp, clock)
    assert {c[0] for c in fake.calls} == {CHIKUN}, "liteverse.io not ticked: never asked; LiteOrd has nothing to list"
    lines = {r["slug"]: r for r in store.catalog("ltc")}
    assert set(lines) == {"chikun:punks", "chikun:sadfrog", "chikun:2048", "chikun:litebars-10-percent"}
    assert all(not r["imported"] and r["skipped"] == kc.ChikunAdapter.NO_PIECES for r in lines.values())
    assert lines["chikun:punks"]["cover"].startswith("ebc07ae3") and lines["chikun:punks"]["source"] == "chikun"
    store.want("ltc", "chikun:punks")                         # a listed-only line opened: said, never asked
    clock.t += 120
    _run(imp, clock)
    assert store.wants("ltc") == {} and len(fake.calls) == 1
    # liteverse.io ticked: its listing, then a collection a visitor opened, at one request a second to each host.
    imp.switched = lambda: ("chikun", "liteverse", "liteord")
    store.want("ltc", "liteverse:12")                         # not listed yet: kept until its line comes
    _run(imp, clock)
    got = store.collection("ltc", "liteverse:12")
    assert got is not None and got["count"] == 5 and got["source"] == "liteverse" and got["sig"] == "100|100"
    assert got["fp"] == kc.fingerprint("ltc", "liteverse:12", [p["inscription_id"] for p in
                                                                fx("liteverse_inscriptions_12.json")])
    assert store.collection("ltc", "liteverse:27") is None, "on demand: only the one opened"
    assert store.in_collection("ltc", got["cover"])["slug"] == "liteverse:12"
    for host in (CHIKUN, LITEVERSE, LITEORD):
        times = [c[2] for c in fake.calls if c[0] == host]
        assert all(b - a >= kc.PACE for a, b in zip(times, times[1:])), host
    status = {r.get("source"): r for r in imp.status()["chains"]}
    assert status["liteverse"]["collections"] == 1 and status["liteverse"]["pieces"] == 5
    assert status["chikun"]["listed"] == 4 and status["chikun"]["pieces_published"] is False
    assert imp.status()["ltc_sources"] == ["chikun", "liteverse", "liteord"]


def test_a_liteord_parent_opened_is_read_and_one_it_does_not_know_is_kept_as_skipped(tmp_path):
    clock = Clock()
    fake = Sites(clock)
    store, imp = _importer(tmp_path, clock, fake)
    assert store.open_line("ltc", "liteord:" + PARENT, "Children of f14af2c5")
    store.want("ltc", "liteord:" + PARENT)
    unknown = "liteord:" + _id(9)
    assert store.open_line("ltc", unknown, "Children of ?")
    store.want("ltc", unknown, now=clock.t + 1)
    _run(imp, clock)
    got = store.collection("ltc", "liteord:" + PARENT)
    assert got["count"] == 5 and got["source"] == "liteord"
    assert [i["position"] for i in store.items("ltc", "liteord:" + PARENT)] == [1, 2, 3, 101, 102]
    assert store.listed("ltc", unknown)["skipped"] == "LiteOrd does not know this inscription (yet)"
    assert store.wants("ltc") == {} and LITEORD not in imp._backoff, "a 404 is an answer, not a failure"
    assert not store.open_line("doge", "liteord:" + PARENT, "x") and not store.open_line("ltc", "liteord:nope", "x")


def test_opened_liteord_lines_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(kc, "MAX_OPENED", 3)
    store = kc.KnownCollections(tmp_path / "kc.sqlite")
    for k in range(3):
        assert store.open_line("ltc", f"liteord:{_id(k)}", f"c{k}")
    store.want("ltc", f"liteord:{_id(0)}")
    assert store.open_line("ltc", f"liteord:{_id(3)}", "c3"), "room made: the oldest neither held nor wanted goes"
    kept = {r["slug"] for r in store.catalog("ltc")}
    assert kept == {f"liteord:{_id(k)}" for k in (0, 2, 3)}


def test_a_site_that_fails_is_backed_off_alone(tmp_path):
    clock = Clock()
    fake = Sites(clock)
    fake.fail[CHIKUN] = urllib.error.HTTPError(CHIKUN, 429, "Too Many Requests", {}, io.BytesIO(b""))
    store, imp = _importer(tmp_path, clock, fake, switched=lambda: ("chikun", "liteverse"))
    _run(imp, clock, 6)
    assert CHIKUN in imp._backoff and LITEVERSE not in imp._backoff
    assert {r["slug"].split(":")[0] for r in store.catalog("ltc")} == {"liteverse"}, "the other site read meanwhile"
    assert "HTTPError" in {r.get("source"): r for r in imp.status()["chains"]}["chikun"]["error"]


# --------------------------------------------------------------------------- two sites, the same slug

def test_the_same_slug_from_two_sites_is_two_collections_that_never_flap(tmp_path):
    clock = Clock()
    fake = Sites(clock)
    store, imp = _importer(tmp_path, clock, fake, switched=lambda: ("chikun", "liteverse"))
    a_ids, b_ids = [_id(k) for k in range(4)], [_id(k) for k in range(2, 6)]
    # chikun.market's key "12" and liteverse.io's collection 12: the same key, "12", from two sites.
    fa = store.put("ltc", "chikun:12", "Litecoin Punks", 4, _pieces("Punk", a_ids), "chikun")
    fb = store.put("ltc", "liteverse:12", "Litecoin Punks", 4, _pieces("Punk", b_ids), "liteverse")
    assert fa != fb and store.counts()["ltc"] == {"collections": 2, "pieces": 8}
    _run(imp, clock)                                          # liteverse.io's line differs from the held one: read again
    lv = store.collection("ltc", "liteverse:12")
    assert lv["fp"] == kc.fingerprint("ltc", "liteverse:12", [p["inscription_id"] for p in
                                                               fx("liteverse_inscriptions_12.json")])
    assert store.collection("ltc", "chikun:12")["fp"] == fa, "the other site's list untouched"
    asked = len(fake.calls)
    clock.t += kc.REFRESH + 1                                 # a day on: both listings read again, nothing flaps
    _run(imp, clock)
    assert store.collection("ltc", "liteverse:12")["fp"] == lv["fp"] and store.collection("ltc", "chikun:12")["fp"] == fa
    assert [c[1] for c in fake.calls[asked:]] == ["/v1/collections", "/collections"], "only the listings: no piece again"
    assert [i["id"] for i in store.items("ltc", "chikun:12")] == a_ids


# --------------------------------------------------------------------------- the mesh

def test_litecoin_lists_are_shared_and_agreed_over_the_mesh_per_site(tmp_path):
    a, b = Arcade(tmp_path, "a" * 64, mode="all"), Arcade(tmp_path, "b" * 64, mode="all")
    ids = [p["inscription_id"] for p in fx("liteverse_inscriptions_12.json")]
    fp = a.store.put("ltc", "liteverse:12", "Litecoin Punks", 100, _pieces("Punk", ids), "liteverse", sig="100|100")
    a.store.put("ltc", "liteord:" + PARENT, "Children", None, _pieces("Child", [_id(k) for k in range(3)]), "liteord")
    b.store.put("ltc", "chikun:punks", "Litecoin Punks", 4, _pieces("Punk", ids[:4]), "chikun")
    to_b, to_a = _link(a, b)

    async def go():
        await a.share.link_up(to_b)
        await b.share.link_up(to_a)
        await _settle()
        for _ in range(4):
            await b.share.rebalance()
            await a.share.rebalance()
            await _settle()
    asyncio.run(go())
    got = b.store.collection("ltc", "liteverse:12")
    assert got is not None and got["fp"] == fp and got["source"] == "mesh:" + "a" * 12
    assert b.share.agreements("ltc")["liteverse:12"] == 2 and a.share.agreement("ltc", "liteverse:12") == 2
    assert a.store.collection("ltc", "chikun:punks")["fp"] == b.store.collection("ltc", "chikun:punks")["fp"], \
        "the other site's list came the other way, under its own slug"
    assert a.store.digest("ltc") == b.store.digest("ltc")
    assert {r["slug"] for r in b.store.index("ltc")} == {"liteverse:12", "liteord:" + PARENT, "chikun:punks"}


def test_a_list_read_from_its_litecoin_site_today_stands_against_two_arcades(tmp_path):
    """As a list read from Nintondo today does: the site's own word for a day, then the agreement."""
    a, b, c = (Arcade(tmp_path, x * 64) for x in "abc")
    ids = [_id(k) for k in range(4)]
    for x in (a, c):
        x.store.put("ltc", "liteverse:12", "Punks", 5, _pieces("Punk", ids + [_id(900)]), "mesh:" + "d" * 12)
    mine = b.store.put("ltc", "liteverse:12", "Punks", 4, _pieces("Punk", ids), "liteverse")
    pairs = [(a, b), (c, b)]
    links = [_link(x, y) for x, y in pairs]

    async def go():
        for (x, y), (to_y, to_x) in zip(pairs, links):
            await x.share.link_up(to_y)
            await y.share.link_up(to_x)
        await _settle()
        await b.share.rebalance()
        await _settle()
    asyncio.run(go())
    assert b.store.collection("ltc", "liteverse:12")["fp"] == mine


def test_a_litecoin_row_of_an_unknown_site_or_a_plain_slug_is_left_out():
    fp = "ab" * 32
    assert cs._row(["liteverse:12", fp, "Punks", 100, 100, ""], "ltc")["slug"] == "liteverse:12"
    assert cs._row(["newsite:12", fp, "Punks", 100, 100, ""], "ltc") is None, "a newer arcade's other site"
    assert cs._row(["punks", fp, "Punks", 100, 100, ""], "ltc") is None
    assert cs._row(["chikun:punks", fp, "Punks", 100, 100, ""], "doge") is None
    assert cs._row(["bone", fp, "Bone", 3, 3, ""], "doge")["slug"] == "bone"


# --------------------------------------------------------------------------- Explore and Admin

def test_explore_lists_litecoin_collections_with_their_site_and_both_of_a_same_name(walking):
    app, state = walking
    store = state.known_collections()
    ids = [p["inscription_id"] for p in fx("liteverse_inscriptions_12.json")]
    store.put("ltc", "liteverse:12", "Litecoin Punks", 100, _pieces("Punk", ids), "liteverse", sig="100|100")
    with store._db() as conn:                                 # chikun.market's line, listed only, as the importer keeps it
        conn.execute("INSERT INTO kc_listing (chain, slug, name, supply, sig, done, skipped, cover) VALUES "
                     "('ltc', 'chikun:punks', 'Litecoin Punks', 100, '100', 1, ?, ?)",
                     (kc.ChikunAdapter.NO_PIECES, "ebc07ae334fbd2753a32550dd28341af90c2219885b401a529307ed8f093fd17i0"))
    said = app.get("/explore/api/collections?net=ltc&q=punks", headers=EDGE).json()["collections"]
    by = {c["slug"]: c for c in said if c["kind"] == "known"}
    assert set(by) == {"liteverse:12", "chikun:punks"}, "two sites, two collections: shown side by side, not merged"
    lv, ck = by["liteverse:12"], by["chikun:punks"]
    assert (lv["listed_by"], lv["source"], lv["imported"], lv["count"], lv["agreed"]) == \
        ("liteverse.io", "liteverse", True, 5, 1)
    assert lv["link"] == "https://liteverse.io/market/collection?id=12"
    assert (ck["listed_by"], ck["imported"], ck["cover"][:8]) == ("chikun.market", False, "ebc07ae3")
    assert lv["also"] == [{"slug": "chikun:punks", "listed_by": "chikun.market"}] and \
        ck["also"] == [{"slug": "liteverse:12", "listed_by": "liteverse.io"}]
    page = app.get("/explore/api/collection?net=ltc&slug=liteverse:12&offset=0", headers=EDGE).json()
    assert [i["number"] for i in page["items"]] == [1, 2, 3, 4, 5] and page["items"][0]["inscription_number"] == 1000
    listed_only = app.get("/explore/api/collection?net=ltc&slug=chikun:punks", headers=EDGE).json()
    assert listed_only["skipped"] == kc.ChikunAdapter.NO_PIECES and store.wants("ltc") == {}
    seen = app.get(f"/explore/api/inscription?net=ltc&id={ids[0]}", headers=EDGE).json()
    if "collection" in seen:                                  # with a Litecoin ord index to read the number
        assert seen["collection"]["slug"] == "liteverse:12"
    assert store.in_collection("ltc", ids[0])["slug"] == "liteverse:12"
    for bad in ("punks", "othersite:punks", "chikun:../x"):
        assert app.get(f"/explore/api/collection?net=ltc&slug={bad}", headers=EDGE).status_code == 400, bad
    text = app.get("/explore", headers=EDGE).text
    assert "listed by" in text and "also listed by" in text and "Litecoin ordinals" in text


def test_explore_opens_a_parents_children_from_liteord(walking):
    app, state = walking
    store = state.known_collections()
    said = app.get(f"/explore/api/collections?net=ltc&q={PARENT}", headers=EDGE).json()["collections"]
    row = next(c for c in said if c["kind"] == "known")
    assert row["slug"] == "liteord:" + PARENT and row["listed_by"] == "LiteOrd" and not row["imported"]
    assert store.listed("ltc", row["slug"]) is None, "a search keeps nothing"
    got = app.get(f"/explore/api/collection?net=ltc&slug={row['slug']}", headers=EDGE).json()
    assert got["loading"] is True and got["importing"] is False, "the import is off here: linked arcades are asked"
    assert row["slug"] in store.wants("ltc") and store.listed("ltc", row["slug"])["name"].startswith("Children of f14af2c5")
    doge = app.get("/explore/api/collections?net=doge&q=" + PARENT, headers=EDGE).json()["collections"]
    assert all(c["kind"] != "known" for c in doge), "Litecoin's alone"


def test_admin_ticks_the_litecoin_sites(client):
    app, state = client
    got = app.get("/admin/api/known-collections", headers=LOCAL).json()
    assert got["ltc_sources"] == ["chikun", "liteord"], "liteverse.io not ticked until the operator does"
    assert [c["label"] for c in got["chains"] if c["net"] == "ltc"] == ["chikun.market", "liteverse.io", "LiteOrd"]
    assert [c["source"] for c in got["ltc_choices"]] == ["chikun", "liteverse", "liteord"]
    assert app.post("/admin/api/known-collections", headers=LOCAL, json={"ltc_sources": ["liteverse"]}).status_code == 403
    for bad in ("liteverse", ["nope"], [1], [{}], [["liteord"]]):
        assert app.post("/admin/api/known-collections", headers={**LOCAL, **WRITE},
                        json={"ltc_sources": bad}).status_code == 400, bad
    said = app.post("/admin/api/known-collections", headers={**LOCAL, **WRITE},
                    json={"ltc_sources": ["liteord", "liteverse"]}).json()
    assert said["ltc_sources"] == ["liteverse", "liteord"] and state.setting(kc.LTC_SETTING) == ["liteverse", "liteord"]
    got = app.get("/admin/api/known-collections", headers=LOCAL).json()
    assert got["ltc_sources"] == ["liteverse", "liteord"]
    assert {c["source"]: c["switched"] for c in got["chains"] if c["net"] == "ltc"} == \
        {"chikun": False, "liteverse": True, "liteord": True}
    assert 'id="kcltc"' in app.get("/admin", headers=LOCAL).text
