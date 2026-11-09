"""The .mesh web (arcade/mesh_web.py, web/meshweb.py; 2026-10-26): a .mesh name's page is the newest "p":"mesh"
inscription made to the address holding the name now ("Latest by the holder")."""
import hashlib
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from arcade import doginal_index as dx  # noqa: E402
from arcade import mesh_web as M  # noqa: E402
from arcade.script import b58check_decode  # noqa: E402
from test_doginal_index import FUND, SIG, Chain, _envelope, _tx_to  # noqa: E402
from address_claim import file_address, keyed  # noqa: E402

TEXT = b"text/plain;charset=utf-8"
#: A's address as Pepecoin mainnet says it, from a key: filing it as an account's is proved (2026-11-09).
A_ADDRESS, _ = keyed("main", 0xaa)
A = b"\x76\xa9\x14" + b58check_decode(A_ADDRESS)[1] + b"\x88\xac"
B = b"\x76\xa9\x14" + b"\xbb" * 20 + b"\x88\xac"


@pytest.fixture(autouse=True)
def small(monkeypatch):
    monkeypatch.setattr(dx, "CHUNK", 4)
    monkeypatch.setattr(dx, "SETTLE", 0)
    monkeypatch.setattr(dx, "WALK_SETTLE", 0)


def _j(obj):
    return json.dumps(obj, separators=(",", ":")).encode()


def _ins(fund_vout, body, to):
    return _tx_to([(FUND, fund_vout, _envelope(TEXT, parts=((0, body),)))], [(100_000, to)])


# --------------------------------------------------------------------------- the format

def test_a_page_is_json_p_mesh_with_its_name_and_pages():
    body = M.make("Me-Seeks.mesh", {"/": "# Hello\n=> about.mesh More", "about": "## About"})
    page = M.parse(body)
    assert page == {"name": "me-seeks.mesh", "pages": {"/": "# Hello\n=> about.mesh More", "/about": "## About"}}
    assert M.title_of(page["pages"]["/"]) == "Hello"
    assert M.links_of("x\n=> a.mesh/b Label here\n```\n=> not a link\n```\n=> /c") == [("a.mesh/b", "Label here"),
                                                                                     ("/c", "/c")]
    assert M.split_address("mesh://Me-Seeks.mesh/About/") == ("me-seeks.mesh", "/about")
    assert M.split_address("me-seeks.mesh") == ("me-seeks.mesh", "/")
    for bad in ({"p": "mesh", "name": "x.mesh", "pages": {"/": {"html": "<b>hi</b>"}}},  # .mesh: MeshText only
                {"p": "mesh", "name": "x.mesh", "pages": {"/": "a" * 3100}},           # .mesh: one LoRa message
                {"p": "mesh", "name": "123.pepemap", "pages": {"/": "hi"}},            # a block map is no name
                {"p": "dns", "name": "x.mesh", "pages": {"/": "hi"}},
                {"p": "mesh", "name": "x.mesh", "pages": {"no slash": "hi"}},
                {"p": "mesh", "name": "x.mesh"}):
        assert M.parse(_j(bad)) is None, bad
    with pytest.raises(M.MeshError, match="at most"):
        M.make("x.mesh", {"/": "a" * 4000})
    with pytest.raises(M.MeshError, match="front page"):
        M.make("x.mesh", {"/about": "hi"})


# --------------------------------------------------------------------------- the walk resolves it

def _story():
    chain = Chain()
    claim = _ins(0, _j({"p": "dns", "op": "reg", "name": "me-seeks.mesh"}), A)
    page_a = _ins(1, M.make("me-seeks.mesh", {"/": "# From A"}), A)
    chain.add([claim, page_a])
    page_b = _ins(2, M.make("me-seeks.mesh", {"/": "# From B", "/about": "B's about"}), B)   # newer, B holds nothing
    other = _ins(3, M.make("other.mesh", {"/": "# Unclaimed"}), A)                             # its name never claimed
    chain.add([page_b, other])
    return chain, claim, page_a, page_b


def _index(tmp_path, chain):
    # walked as the test chains are (from dx.BASE); the resolver reads no chain-specific rule
    index = dx.DoginalIndex(tmp_path / "w.sqlite", lambda: chain, lambda: "walk")
    index.mesh_reader = lambda iid: ("text/plain", _body_of(chain, iid))
    index.step()
    return index


def _body_of(chain, iid):
    from arcade import ord_rules, rawchain
    tx = rawchain.read_tx(chain.txs[iid[:-2]])
    return ord_rules.read([tx.vin[0]["script"]])[2]


def test_the_newest_page_by_the_names_holder_counts_and_follows_a_sale(tmp_path):
    chain, claim, page_a, page_b = _story()
    index = _index(tmp_path, chain)
    got = index.mesh("me-seeks.mesh")
    assert got["claim"] == f"{claim[1]}i0" and got["holder"]
    assert got["page"]["id"] == f"{page_a[1]}i0", "B's newer page does not count: B does not hold the name"
    assert got["page"]["pages"] == {"/": "# From A"}
    assert index.mesh("other.mesh")["claim"] is None and index.mesh("other.mesh")["page"] is None
    assert [d["name"] for d in index.mesh_directory()] == ["me-seeks.mesh"]
    # A sells the name to B: B's page counts now, A's no longer
    sale = _tx_to([(claim[1], 0, SIG)], [(100_000, B)])
    chain.add([sale])
    chain.add([])
    index.step()
    got = index.mesh("me-seeks.mesh")
    assert got["page"]["id"] == f"{page_b[1]}i0" and got["page"]["pages"]["/about"] == "B's about"
    assert index.answer({"what": "mesh", "name": "Me-Seeks.mesh"})["mesh"]["page"]["id"] == f"{page_b[1]}i0"
    assert index.answer({"what": "mesh_directory"})["mesh_directory"][0]["title"] == "From B"


# --------------------------------------------------------------------------- the routes

from test_web import app_state, client  # noqa: E402,F401


def test_the_routes_serve_pages_the_directory_and_a_preview(client, tmp_path, monkeypatch):
    app, state = client
    chain, claim, page_a, page_b = _story()
    index = _index(tmp_path, chain)
    monkeypatch.setattr(state, "ord_indexes", lambda: {"main": index})
    page = app.get("/r/mesh/me-seeks.mesh").json()
    assert page["body"] == "# From A" and page["page"] == f"{page_a[1]}i0" and page["title"] == "From A"
    assert page["paths"] == ["/"]
    same = app.get("/r/mesh/me-seeks.mesh", params={"have": page["page"]}).json()
    assert same["unchanged"] and "body" not in same, "the T-Deck's copy is the newest: nothing sent again"
    assert app.get("/r/mesh/me-seeks.mesh/about").status_code == 404
    assert "no page yet" in app.get("/r/mesh/other.mesh").json()["detail"] or \
        "Nobody has claimed" in app.get("/r/mesh/other.mesh").json()["detail"]
    assert app.get("/r/mesh/not-a-name").status_code == 400
    assert [n["name"] for n in app.get("/r/mesh").json()["names"]] == ["me-seeks.mesh"]
    outside = {"host": "app.example.com", "cf-ray": "x"}
    shown = app.get("/meshweb/me-seeks.mesh", headers=outside)
    assert shown.status_code == 200 and "<h1>From A</h1>" in shown.text
    home = app.get("/meshweb", headers=outside)
    assert home.status_code == 200 and 'href="/meshweb/me-seeks.mesh"' in home.text
    assert app.get("/r/mesh/me-seeks.mesh", headers=outside).status_code == 200, "the page API's tree is open"
    d = app.get("/r/mesh/directory").json()
    assert d["page"].startswith("directory-") and "=> me-seeks.mesh me-seeks.mesh - From A" in d["body"]
    assert app.get("/r/mesh/directory", params={"have": d["page"]}).json()["unchanged"]


def test_only_the_names_holder_gets_a_draft_to_publish(client, tmp_path, monkeypatch):
    """Creator -> Names -> Publish a page: the site and its draft, only for the account whose Pepecoin mainnet address
    holds the name."""
    import base64
    from arcade.script import b58check_encode
    from test_doginal_wallet import _seat
    app, state = client
    _seat(app)
    chain, claim, page_a, page_b = _story()
    index = _index(tmp_path, chain)
    # the test chain's owners as Pepecoin mainnet addresses (version 56), as the real walk of that chain says them
    index.walker.owner_of = lambda spk: b58check_encode(56, spk[3:23]) if len(spk) == 25 else None
    index.__dict__.pop("_owners", None)
    monkeypatch.setattr(state, "ord_indexes", lambda: {"main": index})
    a_address = A_ADDRESS
    assert index.mesh("me-seeks.mesh")["holder"] == a_address
    stranger = app.get("/account/mesh/site", params={"name": "me-seeks.mesh"}).json()
    assert not stranger["mine"] and "not by your address on main" in stranger["why"]
    assert app.post("/account/mesh/draft", json={"name": "me-seeks.mesh", "pages": {"/": "# Mine?"}}).status_code == 403
    said = file_address(app, a_address, chain="main")
    assert said.status_code == 200, said.text
    site = app.get("/account/mesh/site", params={"name": "me-seeks.mesh"}).json()
    assert site["mine"] and site["page"]["pages"] == {"/": "# From A"}, "the holder's editor opens on the page now"
    draft = app.post("/account/mesh/draft", json={"name": "me-seeks.mesh", "pages": {"/": "# New front", "about": "Hi"}})
    assert draft.status_code == 200 and draft.json()["chain"] == "main"
    assert M.parse(base64.b64decode(draft.json()["content"]))["pages"] == {"/": "# New front", "/about": "Hi"}
    too_big = app.post("/account/mesh/draft", json={"name": "me-seeks.mesh", "pages": {"/": "x" * 4000}})
    assert too_big.status_code == 400 and "at most" in too_big.json()["detail"]
    assert app.post("/account/mesh/draft", json={"name": "other.mesh", "pages": {"/": "# No"}}).status_code == 403


# --------------------------------------------------------------------------- sites on any name, pictures (2026-10-26)

IID = "ab" * 32 + "i0"


def test_any_name_has_a_site_of_text_html_or_inscriptions_and_no_lora_cap():
    big = "<!doctype html><title>Big</title>" + "x" * 20_000
    body = M.make("Shop.Pepe", {"/": "# Hi\n![a frog](" + IID.upper() + ")", "/big": {"html": big}, "/game": {"id": IID}})
    page = M.parse(body)
    assert page["name"] == "shop.pepe" and page["pages"]["/big"] == {"html": big} and page["pages"]["/game"] == {"id": IID}
    assert M.title_of(page["pages"]["/big"]) == "Big" and M.title_of(page["pages"]["/game"]) == ""
    assert M.pictures_of(page["pages"]["/"]) == [(IID, "a frog")] and M.title_of(page["pages"]["/"]) == "Hi"
    with pytest.raises(M.MeshError, match="MeshText"):
        M.make("x.mesh", {"/": {"html": "<b>no</b>"}})
    with pytest.raises(M.MeshError, match="record is at most"):
        M.make("x.pepe", {"/": "y" * (M.RECORD_MAX + 1)})
    assert len(M.make("x.mesh", {"/": "# Hi\n![pic](" + IID + ")"})) < M.SITE_MAX   # a picture costs its line only


def test_a_mesh_picture_draws_from_the_top_in_bands_of_one_lora_part():
    io = pytest.importorskip("io")
    Image = pytest.importorskip("PIL.Image")
    from arcade import mesh_picture as MP
    photo = Image.radial_gradient("L").resize((640, 480)).convert("RGB")
    raw = io.BytesIO()
    photo.save(raw, "PNG")
    body = MP.make(raw.getvalue())
    w, h, colours, rows = MP.read(body)
    assert (w, h) == (267, 200) and len(rows) == 200 and 1 <= len(colours) <= 16 and len(body) <= MP.PICTURE_MAX
    at, used = 6 + 2 * len(colours), 0
    while at < len(body):                                 # every band fits one LoRa part
        n, length = body[at], int.from_bytes(body[at + 1:at + 3], "big")
        assert length <= MP.BAND_BYTES or n == 1
        at += 3 + length
        used += n
    assert used == h
    sprite = Image.new("RGBA", (24, 24), (0, 0, 0, 0))
    sprite.putpixel((3, 4), (255, 0, 0, 255))
    raw = io.BytesIO()
    sprite.save(raw, "PNG")
    w, h, colours, rows = MP.read(MP.make(raw.getvalue()))
    assert (w, h) == (24, 24), "small pictures are never made bigger"
    assert colours[rows[4][3]] == MP.rgb565(255, 0, 0) and colours[rows[0][0]] == MP.rgb565(*MP.BACKGROUND)
    with pytest.raises(MP.PictureError):
        MP.make(b"not a picture")


def test_the_browser_finds_a_site_on_any_chain(client, tmp_path, monkeypatch):
    app, state = client
    chain = Chain()
    claim = _ins(0, _j({"p": "dns", "op": "reg", "name": "shop.pepe"}), A)
    site = _ins(1, M.make("shop.pepe", {"/": "# Shop\n=> /deals Deals", "/deals": {"html": "<title>Deals</title><b>50%</b>"},
                                       "/frog": {"id": IID}}), A)
    chain.add([claim, site])
    index = _index(tmp_path, chain)
    mesh_chain, _c, page_a, _b = _story()
    mesh_index = _index(tmp_path / "m", mesh_chain) if (tmp_path / "m").mkdir() is None else None
    monkeypatch.setattr(state, "ord_indexes", lambda: {"doge": index, "main": mesh_index})
    listed = app.get("/r/sites")
    assert listed.headers["access-control-allow-origin"] == "*", "an inscribed page (a search engine) reads the list"
    every = listed.json()["names"]
    assert {(n["chain"], n["name"]) for n in every} == {("doge", "shop.pepe"), ("main", "me-seeks.mesh")}
    assert [n["name"] for n in app.get("/r/mesh").json()["names"]] == ["me-seeks.mesh"], "the T-Deck: .mesh only"
    front = app.get("/r/site/shop.pepe").json()
    assert front["chain"] == "doge" and front["kind"] == "text" and front["body"].startswith("# Shop")
    deals = app.get("/r/site/shop.pepe/deals").json()
    assert deals["kind"] == "html" and deals["url"] == "/site/doge/shop.pepe/deals" and deals["title"] == "Shop"
    served = app.get(deals["url"])
    assert served.status_code == 200 and "<b>50%</b>" in served.text
    assert "sandbox" in served.headers["content-security-policy"], "a site's HTML gets nothing of the arcade's"
    frog = app.get("/r/site/shop.pepe/frog").json()
    assert frog["kind"] == "id" and frog["inscription"] == IID
    assert app.get(frog["url"], follow_redirects=False).headers["location"] == f"/content/{IID}"
    assert app.get("/r/site/me-seeks.mesh").json()["chain"] == "main"
    assert app.get("/r/site/nobody.pepe").status_code == 404 and app.get("/r/site/not-a-name").status_code == 400
    typo = app.get("/r/site/Mee-seeks.mesh").json()                    # 2026-10-26, the operator on the PWA
    assert typo["suggest"] == ["me-seeks.mesh"] and "Did you mean me-seeks.mesh?" in typo["detail"]
    page = app.get("/browser/shop.pepe", headers={"host": "app.example.com", "cf-ray": "x"})
    assert page.status_code == 200 and "<title>Browser</title>" in page.text, "the Browser is a public page"
    from arcade.web import door
    assert door.public_path("/site/doge/shop.pepe/deals") and door.public_path("/browser")


def test_one_chains_index_failing_does_not_hide_a_name_on_another(client, tmp_path, monkeypatch):
    """2026-10-27, Strawberry: Pepecoin testnet's lookup said "no linked arcade walks this chain yet" and the website
    wizard showed that for ribbit.pepe, claimed on Pepecoin mainnet."""
    from test_doginal_wallet import _seat
    app, state = client
    _seat(app)
    chain = Chain()
    chain.add([_ins(0, _j({"p": "dns", "op": "reg", "name": "shop.pepe"}), A)])
    index = _index(tmp_path, chain)

    class Unwalked:
        def lookup(self, question):
            raise ValueError("no linked arcade walks this chain yet")
    monkeypatch.setitem(dx.STARTS, "regtest", 0)               # the test arcade's second chain reads ords too
    monkeypatch.setattr(state, "ord_indexes", lambda: {"main": index, "regtest": Unwalked()})
    site = app.get("/account/mesh/site", params={"name": "shop.pepe"}).json()
    assert site["chain"] == "main" and site["claim"], site["why"]
    monkeypatch.setattr(state, "ord_indexes", lambda: {"regtest": Unwalked()})
    alone = app.get("/account/mesh/site", params={"name": "shop.pepe"}).json()
    assert not alone["claim"] and "could not say just now" in alone["why"] and "Nobody has claimed" not in alone["why"]


def test_a_picture_for_the_tdeck_comes_from_what_content_serves(client, monkeypatch):
    io = pytest.importorskip("io")
    Image = pytest.importorskip("PIL.Image")
    app, state = client
    raw = io.BytesIO()
    Image.new("RGB", (40, 30), (0, 200, 0)).save(raw, "PNG")
    from fastapi.responses import Response
    import arcade.web.app as webapp  # noqa: F401
    seen = []

    def fake(key, *a, **k):
        seen.append(key)
        return Response(raw.getvalue(), media_type="image/png") if key == IID else Response(b"hi", media_type="text/plain")
    route = next(r for r in app.app.routes if getattr(r, "path", "") == "/r/mesh/picture/{iid}")
    cells = dict(zip(route.endpoint.__code__.co_freevars, route.endpoint.__closure__))
    monkeypatch.setattr(cells["content"], "cell_contents", fake)
    got = app.get(f"/r/mesh/picture/{IID}")
    assert got.status_code == 200
    from arcade import mesh_picture as MP
    assert MP.read(got.content)[:2] == (40, 30)
    app.get(f"/r/mesh/picture/{IID}")
    assert seen == [IID], "made once, then kept"
    assert app.get("/r/mesh/picture/" + "cd" * 32 + "i0").status_code == 404
    assert app.get("/r/mesh/picture/nope").status_code == 400


def test_a_page_counts_where_it_landed_not_at_its_first_reveal_output(tmp_path):
    """2026-10-26: me-seeks.mesh's /show record took three transactions; the inscription landed on the last reveal, and
    output 0 of the first only carried it on (to the next piece's lock). It counts by who holds it now."""
    chain = Chain()
    claim = _ins(0, _j({"p": "dns", "op": "reg", "name": "me-seeks.mesh"}), A)
    lock = b"\xa9\x14" + b"\xcc" * 20 + b"\x87"                  # the next piece's lock, as a chunked reveal pays
    first = _ins(1, M.make("me-seeks.mesh", {"/": "# Landed"}), lock)
    chain.add([claim, first])
    last = _tx_to([(first[1], 0, SIG)], [(100_000, A)])           # the last reveal: the ord comes to rest at A
    chain.add([last])
    chain.add([])
    index = _index(tmp_path, chain)
    got = index.mesh("me-seeks.mesh")
    assert got["page"] and got["page"]["id"] == f"{first[1]}i0" and got["page"]["pages"] == {"/": "# Landed"}
