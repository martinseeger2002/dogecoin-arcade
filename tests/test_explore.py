"""The Explore tab (2026-10-25, SadFrog: "an explore tab that has tabs for every coin the node has ordinals/omni on and
if both seperate drop downs to view which protocol ... a user should be able to look up known collections, inscription
id's, inscription numbers, file types"). The Admin -> Ords explorer's questions, open to anybody and read only, minus
the ones that count a whole chain: a content type is paged by number instead ("typed")."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_ord_explorer import EDGE, LOCAL, _walked, walking                       # noqa: F401,E402
from test_doginal_index import small                                            # noqa: F401,E402
from test_web import app_state, client                                          # noqa: F401,E402

from arcade import doginal_index as dx, ord_chains                              # noqa: E402
from arcade.web import door                                                     # noqa: E402


# --------------------------------------------------------------------------- the coins and their protocols

def test_the_coins_in_sadfrogs_order_each_with_its_protocols():
    assert ord_chains.EXPLORE_ORDER[:4] == ("doge", "main", "bel", "ltc"), "Doginals, Pepinals, Bellinals, LTC ords"
    assert [p["label"] for p in ord_chains.explore_protocols("doge")] == ["Doginals", "DRC-20"]
    assert [p["label"] for p in ord_chains.explore_protocols("main")] == ["Pepinals", "PRC-20"]
    assert [p["kind"] for p in ord_chains.explore_protocols("ltc")] == ["ord", "token", "omni"], "Litecoin has Omni"
    assert [p["label"] for p in ord_chains.explore_protocols("bel")] == ["Bellinals", "BEL-20"]
    assert ord_chains.explore_protocols("nope") == []
    order = ord_chains.EXPLORE_ORDER
    assert order.index("wjk") < order.index("dgb") < order.index("ltctest"), "DigiByte with the rest, before the testnets"


def test_explore_is_open_to_anybody_for_reading_only():
    for path in ("/explore", "/explore/api/inscription", "/explore/api/typed", "/explore/api/collections"):
        assert door.public_path(path), path
    assert not door.public_path("/explore/api/typed", "POST")


# --------------------------------------------------------------------------- one content type, a page at a time

def test_a_type_is_paged_by_number_without_counting(tmp_path):
    _chain, index = _walked(tmp_path)
    everything = index.store.q("SELECT i.number, c.ctype FROM insc i JOIN ctypes c ON c.ct = i.ct ORDER BY i.rid")
    kind = max({t for _, t in everything}, key=lambda t: sum(1 for _, u in everything if u == t))
    these = [n for n, t in everything if t == kind]
    assert len(these) >= 2, everything
    got = index.typed(kind, limit=1)
    assert [r["number"] for r in got["rows"]] == these[:1] and got["next"] == these[0]
    assert got["rows"][0]["content_type"] == kind and got["rows"][0]["id"].endswith("i0")
    rest = index.typed(kind, after=got["next"], limit=60)
    assert [r["number"] for r in rest["rows"]] == these[1:] and rest["next"] is None, "to the end"
    head = kind.split("/")[0] + "/"
    assert [r["number"] for r in index.typed(head)["rows"]] == [n for n, t in everything if t.startswith(head)][:24]
    assert index.typed("model/gltf") == {"rows": [], "next": None}
    assert index.typed("%") == {"rows": [], "next": None}, "a wildcard is not a type"
    assert index.answer({"what": "typed", "ctype": kind, "after": None})["typed"]["rows"][0]["number"] == these[0]


def test_a_rare_type_reads_a_stretch_and_says_where_to_go_on(tmp_path, monkeypatch):
    _chain, index = _walked(tmp_path)
    kind, last = index.store.q("SELECT c.ctype, MAX(i.number) FROM insc i JOIN ctypes c ON c.ct = i.ct GROUP BY i.ct "
                               "ORDER BY COUNT(*) DESC LIMIT 1")[0]
    monkeypatch.setattr(dx.DoginalIndex, "TYPED_WINDOW", 1)
    monkeypatch.setattr(dx.DoginalIndex, "TYPED_SCAN", 1)
    got = index.typed(kind, after=-1)
    assert len(got["rows"]) <= 1 and got["next"] is not None, "one row read, then where to go on from"
    seen, after = [], -1
    for _ in range(100):
        page = index.typed(kind, after=after)
        seen += [r["number"] for r in page["rows"]]
        if page["next"] is None:
            break
        after = page["next"]
    assert seen and seen[-1] == last and seen == sorted(set(seen)), "every one, once, in order"


# --------------------------------------------------------------------------- the routes

def test_the_page_has_a_tab_per_coin_and_a_protocol_menu(walking):
    app, _state = walking
    page = app.get("/explore", headers=EDGE)
    assert page.status_code == 200, page.text
    text = page.text
    assert 'data-net="main"' in text and 'id="xpproto"' in text and "window.arcadePick" in text
    assert '"Pepinals"' in text and '"PRC-20"' in text
    assert 'href="/explore"' in app.get("/", headers=LOCAL).text or 'href="/explore"' in app.get("/me", headers=LOCAL).text


def test_anybody_reads_an_inscription_a_type_and_a_token(walking):
    app, _state = walking
    got = app.get("/explore/api/inscription?net=main&number=1", headers=EDGE)
    assert got.status_code == 200 and got.json()["number"]["inscription"]["number"] == 1
    assert app.get(f"/explore/api/inscription?net=main&id={'03' * 32}i0", headers=EDGE).json()[
        "number"]["inscription"]["number"] == 2
    typed = app.get("/explore/api/typed?net=main&type=text/plain", headers=EDGE).json()["typed"]
    assert [r["number"] for r in typed["rows"]] == [0, 1, 2] and typed["next"] is None
    assert app.get("/explore/api/typed?net=main&type=text/&after=0", headers=EDGE).json()["typed"]["rows"][0][
        "number"] == 1
    assert app.get("/explore/api/holders?net=main&tick=pepi", headers=EDGE).json()["holders"]["rows"][0]["owner"] == "Pbig"
    assert app.get("/explore/api/ticks?net=main&q=pe", headers=EDGE).json()["ticks"]["count"] == 1
    assert app.get("/explore/api/lookup?net=main&q=%232", headers=EDGE).json()["number"]["inscription"]["number"] == 2
    for bad in ("typed?net=main&type=", "typed?net=main&type=%25", "typed?net=main&type=text/plain&after=x",
                "lookup?net=main&q=kinds", "lookup?net=main&q=type:image/png", "lookup?net=main&q=p:prc-20",
                "inscription?net=main&number=x"):
        assert app.get("/explore/api/" + bad, headers=EDGE).status_code == 400, bad
    assert app.get("/explore/api/inscription?net=nochain", headers=EDGE).status_code == 404


def test_known_collections_by_name_and_a_page_of_pieces(walking, monkeypatch):
    app, state = walking
    root = "ab" * 32
    coll = {"txid": root, "number": 9, "name": "Doginal Frogs", "chain": "doge", "supply": 3, "description": "frogs",
            "cover": "cd" * 32 + "i0", "count": 3, "traits": [], "status": "active"}

    class Reg:
        def active(self):
            return [coll, {**coll, "txid": "ef" * 32, "name": "Only Frogs", "chain": "main"}]

        def root(self, txid):
            return coll if txid == root else None

        def items(self, txid, offset=0, limit=60):
            return [{"id": f"{k:02x}" * 32 + "i0", "number": k, "traits": {}} for k in range(3)][offset:offset + limit]
    monkeypatch.setattr(state, "collection_registry", lambda: Reg())
    said = app.get("/explore/api/collections?net=doge&q=frog", headers=EDGE).json()["collections"]
    assert [c["name"] for c in said] == ["Doginal Frogs"], "this chain's only"
    assert app.get("/explore/api/collections?net=doge&q=dogs", headers=EDGE).json()["collections"] == []
    page = app.get(f"/explore/api/collection?root={root}&offset=1", headers=EDGE).json()
    assert page["collection"]["name"] == "Doginal Frogs" and [i["number"] for i in page["items"]] == [1, 2]
    assert app.get("/explore/api/collection?root=" + "00" * 32, headers=EDGE).status_code == 404
    assert app.get("/explore/api/collection?root=nonsense", headers=EDGE).status_code == 400


def test_a_visitor_asking_too_fast_waits(walking):
    app, _state = walking
    who = {**EDGE, "cf-connecting-ip": "203.0.113.9"}
    codes = [app.get("/explore/api/inscription?net=main&number=0", headers=who).status_code for _ in range(125)]
    assert codes[:120] == [200] * 120 and codes[-1] == 429
    other = {**EDGE, "cf-connecting-ip": "203.0.113.10"}
    assert app.get("/explore/api/inscription?net=main&number=0", headers=other).status_code == 200, "each their own"


# --------------------------------------------------------------------------- the arcade's own tokens and NFTs

def test_the_arcades_own_assets_on_a_chain_by_name_and_category(walking, monkeypatch):
    """2026-10-25, SadFrog: "a section to explor pepecoin test omni assets like the ashvale game assets"."""
    app, state = walking
    from types import SimpleNamespace
    props = [{"property_id": 26, "name": "ASHVALE GOLD", "category": "", "subcategory": "", "total_display": "152,936",
              "holder_count": 9, "divisible": False, "test_ecosystem": True, "managed": True, "issuer": "nIssuer",
              "creation_block": 900, "creation_txid": "ab" * 32, "data": '{"about":"The coin of the valley","icon":"'
              + "cd" * 32 + '"}'},
             {"property_id": 36, "name": "ASHVALE BRONZE ARROWS", "category": "ammo", "subcategory": "arrow",
              "total_display": "823", "holder_count": 3, "divisible": False, "test_ecosystem": True, "managed": True,
              "issuer": "nIssuer", "creation_block": 901, "creation_txid": "ef" * 32, "data": '{"about":"Arrows"}'}]

    class Ledger:
        def properties(self):
            return props

        def property(self, pid):
            return next((p for p in props if p["property_id"] == pid), None)

        def holders(self, pid):
            return [{"address": f"n{k}", "display": str(100 - k)} for k in range(60)]

        def inscription(self, key):
            return {"held": True, "content_type": "image/png"}
    chain = SimpleNamespace(network="main")
    monkeypatch.setattr(type(state), "token_chains", property(lambda self: [chain]))
    monkeypatch.setattr(state, "token_index", lambda c: Ledger())
    page = app.get("/explore?net=main", headers=EDGE).text
    assert '"Arcade assets (Omni)"' in page and 'id="xpassets"' in page
    got = app.get("/explore/api/assets?net=main", headers=EDGE).json()
    assert [a["name"] for a in got["assets"]] == ["ASHVALE GOLD", "ASHVALE BRONZE ARROWS"] and got["count"] == 2
    assert got["assets"][0]["icon"] == "cd" * 32 and got["assets"][0]["about"] == "The coin of the valley"
    assert ["ammo", 1] in got["categories"]
    assert [a["id"] for a in app.get("/explore/api/assets?net=main&category=ammo", headers=EDGE).json()["assets"]] == [36]
    assert [a["id"] for a in app.get("/explore/api/assets?net=main&q=gold", headers=EDGE).json()["assets"]] == [26]
    one = app.get("/explore/api/asset?net=main&id=36", headers=EDGE).json()
    assert one["asset"]["page"] == "/tokens/36?chain=main" and one["asset"]["about"] == "Arrows"
    assert len(one["holders"]) == 50 and one["holders"][0] == {"address": "n0", "held": "100"}
    assert app.get("/explore/api/asset?net=main&id=99", headers=EDGE).status_code == 404
    assert app.get("/explore/api/asset?net=main&id=x", headers=EDGE).status_code == 400
    assert app.get("/explore/api/assets?net=doge", headers=EDGE).status_code == 404, "no ledger of that chain here"


def test_a_linked_arcades_answer_gets_its_holder_from_our_own_core(tmp_path, monkeypatch):
    from test_ord_explorer import _walked
    _chain, index = _walked(tmp_path)
    monkeypatch.setattr(index, "_owner_at", lambda txid, vout: f"D-holder-of-{txid[:4]}:{vout}")
    said = index._own_owners({"from": "arcade x", "number": {"inscription": {"id": "x", "output": "ab" * 32 + ":1",
                                                                              "owner": None, "burned": False}}})
    assert said["number"]["inscription"]["owner"] == "D-holder-of-abab:1"
    kept = index._own_owners({"where": {"output": "cd" * 32 + ":0", "owner": "Dalready", "burned": False}})
    assert kept["where"]["owner"] == "Dalready", "an answer that names its holder is left as it is"
    burned = index._own_owners({"where": {"output": None, "burned": True}})
    assert "owner" not in burned["where"]


def test_negative_numbers_are_asked_and_odd_types_said_plainly(walking):
    app, _state = walking
    assert app.get("/explore/api/inscription?net=main&number=-1", headers=EDGE).status_code == 200
    assert app.get("/explore/api/lookup?net=main&q=-3", headers=EDGE).status_code == 200
    assert app.get("/explore/api/inscription?net=main&number=--1", headers=EDGE).status_code == 400


# --------------------------------------------------------------------------- the collection search (2026-10-26)
#
# SadFrog: "can you make the collection search more in depth with sorting and filters and lazy loading one at a time on
# demand." Every known collection is listed, held or not; a page at a time; one not held is fetched when opened.

def _line(store, chain, slug, name, supply, owners=None, height=None, first=None, last=None):
    """A line of Nintondo's listing, as the importer keeps it."""
    with store._db() as conn:
        conn.execute("INSERT INTO kc_listing (chain, slug, name, supply, sig, owners, height, first, last, size) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)", (chain, slug, name, supply, f"{supply}|{first}|{last}|0", owners,
                                                      height, first, last, 0))


def _ids(k0, n):
    return [{"id": f"{k0 + k:064x}i0", "name": f"P #{k + 1}"} for k in range(n)]


@pytest.fixture
def searched(walking, monkeypatch):
    app, state = walking
    store = state.known_collections()
    _line(store, "doge", "frogs", "Doge Frogs", 3, owners=50, height=5_000_000, first=10, last=12)
    _line(store, "doge", "apes", "Apes", 12_000, owners=900, height=4_000_000)
    _line(store, "doge", "bone", "Bone", 57_951, owners=3_000, height=4_500_000)
    _line(store, "doge", "zebra-club", "Zebra Club", 500, owners=10, height=5_100_000)
    _line(store, "doge", "toads", "Toads", 4, owners=5, height=4_800_000)
    store.put("doge", "frogs", "Doge Frogs", 3, _ids(1, 3), "nintondo", sig="3|10|12|0")
    store.put("doge", "toads", "Toads", 4, _ids(10, 2), "nintondo", sig="x")            # held, two of four
    store.put("doge", "mesh-only", "Mesh Only", 2, _ids(20, 2), "mesh:" + "a" * 12, sig="2|7|8|99")

    class Reg:
        def active(self):
            return [{"txid": "ab" * 32, "number": 9, "name": "Registry Frogs", "chain": "doge", "supply": 2000,
                     "description": "", "cover": "", "count": 2000, "traits": [], "status": "active"}]
    monkeypatch.setattr(state, "collection_registry", lambda: Reg())
    return app, state, store


def _ask(app, **args):
    import urllib.parse
    got = app.get("/explore/api/collections?" + urllib.parse.urlencode({"net": "doge", **args}),
                  headers={**EDGE, "cf-connecting-ip": "198.51.100.7"})
    assert got.status_code == 200, (args, got.text)
    return got.json()


def _slugs(j):
    return [c.get("slug") or c["name"] for c in j["collections"]]


def test_the_search_lists_every_known_collection_held_or_not(searched):
    app, _state, _store = searched
    j = _ask(app)
    assert j["total"] == 7 and j["offset"] == 0 and j["limit"] == 30
    assert _slugs(j) == ["bone", "apes", "Registry Frogs", "zebra-club", "toads", "frogs", "mesh-only"], \
        "biggest first by default"
    by = {c.get("slug") or c["name"]: c for c in j["collections"]}
    assert by["bone"]["imported"] is False and by["bone"]["count"] == 0 and by["bone"]["owners"] == 3000 \
        and by["bone"]["agreed"] == 0 and by["bone"]["cover"] == "" and by["bone"]["kind"] == "known"
    assert by["frogs"]["imported"] is True and by["frogs"]["count"] == 3 and by["frogs"]["agreed"] == 1 \
        and by["frogs"]["cover"] == f"{1:064x}i0" and (by["frogs"]["first"], by["frogs"]["last"]) == (10, 12)
    assert by["mesh-only"]["imported"] and by["mesh-only"]["source"].startswith("mesh:") \
        and (by["mesh-only"]["first"], by["mesh-only"]["last"], by["mesh-only"]["size"]) == (7, 8, 99), \
        "a linked arcade's list, its numbers read from its line"
    assert by["Registry Frogs"]["kind"] == "registry"
    page = app.get("/explore", headers=EDGE).text
    for control in ("xpcollq", "xpcollsort", "xpcollsrc", "xpcollcomplete", "xpcollsize", "xpcollagreed",
                    "xpcollsend", "xpcollsmore", "xpcollgridend", "xpcollmore"):
        assert f'id="{control}"' in page, control
    assert "IntersectionObserver" in page and "not fetched yet" in page and "Fetching from Nintondo" in page


def test_the_search_sorts_filters_and_pages(searched):
    app, _state, _store = searched
    assert _slugs(_ask(app, sort="name")) == ["apes", "bone", "frogs", "mesh-only", "Registry Frogs", "toads",
                                              "zebra-club"]
    assert _slugs(_ask(app, sort="holders"))[:5] == ["bone", "apes", "frogs", "zebra-club", "toads"]
    assert _slugs(_ask(app, sort="newest"))[:5] == ["zebra-club", "frogs", "toads", "bone", "apes"]
    assert _slugs(_ask(app, sort="agreed"))[:3] == ["toads", "frogs", "mesh-only"], "held here first, then by size"
    assert _slugs(_ask(app, source="registry")) == ["Registry Frogs"]
    assert "Registry Frogs" not in _slugs(_ask(app, source="known"))
    assert _slugs(_ask(app, q="FROG")) == ["Registry Frogs", "frogs"], "any case"
    assert _slugs(_ask(app, q="zebra-cl")) == ["zebra-club"], "by slug too"
    assert _slugs(_ask(app, complete="1")) == ["Registry Frogs", "frogs", "mesh-only"], "held whole only"
    assert _slugs(_ask(app, max="999")) == ["zebra-club", "toads", "frogs", "mesh-only"]
    assert _slugs(_ask(app, min="1000", max="10000")) == ["Registry Frogs"]
    assert _slugs(_ask(app, min="10001")) == ["bone", "apes"]
    assert _slugs(_ask(app, agreed="1")) == ["toads", "frogs", "mesh-only"]
    assert _ask(app, agreed="2")["collections"] == []
    first, rest = _ask(app, sort="name", limit="3"), _ask(app, sort="name", limit="3", offset="3")
    assert _slugs(first) == ["apes", "bone", "frogs"] and _slugs(rest) == ["mesh-only", "Registry Frogs", "toads"]
    assert first["total"] == rest["total"] == 7 and _ask(app, offset="7")["collections"] == []
    assert _ask(app, q="nothing like it") == {"collections": [], "total": 0, "offset": 0, "limit": 30}


def test_the_search_refuses_nonsense(searched):
    app, _state, _store = searched
    for bad in ("sort=biggest", "source=nintondo", "complete=yes", "min=-1", "max=lots", "min=5&max=4", "agreed=x",
                "agreed=1000", "offset=-1", "offset=x", "limit=0", "limit=61", "limit=x", "q=" + "a" * 81,
                "q=%01", "net=../x", "net="):
        got = app.get("/explore/api/collections?" + ("net=doge&" if "net=" not in bad else "") + bad, headers=EDGE)
        assert got.status_code == 400, bad
    assert app.get("/explore/api/collections?net=doge&limit=60", headers=EDGE).status_code == 200


def test_a_collection_not_held_is_fetched_when_opened_then_paged(searched):
    app, state, store = searched
    url = "/explore/api/collection?net=doge&slug=zebra-club&offset=0"
    j = app.get(url, headers=EDGE).json()
    assert j["loading"] is True and j["items"] == [] and j["have"] == 0 and j["of"] == 500
    assert j["collection"]["name"] == "Zebra Club" and j["collection"]["imported"] is False
    assert "zebra-club" in store.wants("doge"), "queued"
    assert state.known_importer().wake.is_set(), "and the importer woken"
    with store._db() as conn:                                  # the importer part way through
        conn.executemany("INSERT INTO kc_pending (chain, slug, id, name, position, number) VALUES (?,?,?,?,?,?)",
                         [("doge", "zebra-club", f"{900 + k:064x}i0", "", None, None) for k in range(120)])
    assert app.get(url, headers=EDGE).json()["have"] == 120, "how many pieces have come so far"
    store.put("doge", "zebra-club", "Zebra Club", 500, _ids(1000, 50), "nintondo")
    assert store.wants("doge") == {}
    held = app.get(url, headers=EDGE).json()
    assert held["loading"] is False and len(held["items"]) == 48 and held["collection"]["imported"] is True \
        and held["collection"]["owners"] == 10
    more = app.get(url.replace("offset=0", "offset=48"), headers=EDGE).json()
    assert [i["name"] for i in more["items"]] == ["P #49", "P #50"]
    _line(store, "doge", "giant", "Giant", 250_000)
    big = app.get("/explore/api/collection?net=doge&slug=giant", headers=EDGE).json()
    assert big["loading"] is False and "more than" in big["skipped"] and "giant" not in store.wants("doge"), \
        "too big to keep: said so, not queued"
    assert app.get("/explore/api/collection?net=doge&slug=nope", headers=EDGE).status_code == 404
    assert app.get("/explore/api/collection?net=doge&slug=zebra-club&offset=x", headers=EDGE).status_code == 400
