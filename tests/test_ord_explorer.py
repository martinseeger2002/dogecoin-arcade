"""Admin -> Ords -> the explorer (2026-10-21: "The Explorer on the bottom should be picked by the coin type at
the top you should be able to search by number as well as TXID utxo:index ... Below the search should display ord 0 of
the chain that is selected with arrows to navigate by ord number. Below that should be PRC and DRC token info and the
ability to search by token ticker ... a list rich list of the token holders paginated to 20 holders").

The index answers an inscription by number (rid = number + 1), ticks by how they start, and a token's holders a page
at a time, sorted by SQL on bal_rich; a read-only arcade asks a walking one over the mesh, and one from before the
explorer is answered as well as it can be, or said plainly."""
import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_doginal_index import _story_chain, _walker, small   # noqa: F401,E402 -- small: 4-block chunks
from test_web import app_state, client                         # noqa: F401,E402

from arcade import doginal_index as dx                          # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}
EDGE = {"host": "node.dogecoinarcade.com", "cf-ray": "abc-LHR"}


def _walked(tmp_path):
    chain = _story_chain()
    index = _walker(tmp_path, chain)
    index.walker.step(chain, dx.BASE + 11, budget=60)
    return chain, index


def _token(index, tick, holders, supply="100000", dec=0):
    """A token with these balances ({owner: units}), written straight into the index."""
    index.store.x("INSERT INTO tok VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (tick, tick.upper(), "ab" * 32 + "i0", 7, supply, "1000", dec, str(sum(map(int, holders.values()))),
                   "Ddeployer", 5))
    index.store.many("INSERT INTO bal VALUES (?,?,?,?)", [(o, tick, u, "0") for o, u in holders.items()])


# --------------------------------------------------------------------------- the index

def test_an_inscription_by_number_is_its_row_and_says_the_last_one(tmp_path):
    _chain, index = _walked(tmp_path)
    last = index.last_number()
    assert last == index.store.q("SELECT MAX(number) FROM insc")[0][0] and last >= 5
    first = index.inscription(0)
    x = first["inscription"]
    assert first["last"] == last and x["number"] == 0 and x["content_type"] == "image/png"
    assert x["id"] == index.where(x["id"])["id"] and x["output"] == index.where(x["id"])["output"]
    assert x["owner"] and x["owner"].startswith("D"), "who holds the coin, read from the Core"
    burned = [index.inscription(n)["inscription"] for n in range(last + 1)]
    assert any(b["burned"] and b["owner"] is None for b in burned), "a burn sits nowhere and nobody holds it"
    assert index.inscription(1)["inscription"]["protocol"] == "drc-20"
    by_id = index.inscription(iid=burned[3]["id"])["inscription"]
    assert by_id["number"] == 3, "by id too"
    assert index.inscription(last + 1) == {"inscription": None, "last": last}
    assert index.inscription(-4)["inscription"] is None
    with pytest.raises(ValueError):
        index.inscription("seven")
    assert index.answer({"what": "number", "number": 0})["number"] == first


def test_ticks_by_how_they_start_capped_and_counted(tmp_path):
    _chain, index = _walked(tmp_path)
    for tick in ("woog", "wolf", "wow!", "bark"):
        _token(index, tick, {"Da": "1"})
    got = index.ticks("wo")
    assert got["count"] == 4 and [t["tick"] for t in got["first"]] == ["WOLF", "woof", "WOOG", "WOW!"]
    assert index.ticks("WOO")["count"] == 2, "any case"
    assert index.ticks("", first=2) == {"count": 5, "first": index.ticks("", first=2)["first"]}
    assert len(index.ticks("", first=2)["first"]) == 2 and index.ticks("zz") == {"count": 0, "first": []}
    woof = [t for t in index.ticks("woof")["first"]][0]
    assert woof["minted"] == "100" and woof["minted_pct"] == 10.0 and woof["number"] == 1


def test_the_rich_list_is_sorted_as_numbers_twenty_a_page(tmp_path):
    """Balances are text (they pass 2^63): "900" < "25" as text, so the order is by length, then text."""
    _chain, index = _walked(tmp_path)
    holders = {f"D{k:03d}": str(k * 7 + 1) for k in range(45)}
    holders.update({"Dbig": "1000", "Dnine": "900", "Dtwentyfive": "25", "Dhuge": "123456789012345678901234567890",
                    "Dnone": "0"})
    _token(index, "rich", holders, supply="200000000000000000000000000000")
    first = index.rich_list("RICH", 0)
    real = sorted(((int(u), o) for o, u in holders.items() if u != "0"), reverse=True)
    assert first["holders"] == 49 and first["pages"] == 3 and first["per"] == 20 and len(first["rows"]) == 20
    assert first["rows"][0]["owner"] == "Dhuge" and first["rows"][0]["pct"] == 61.7284
    assert [r["owner"] for r in first["rows"][:3]] == ["Dhuge", "Dbig", "Dnine"]
    pages = first["rows"] + index.rich_list("rich", 1)["rows"] + index.rich_list("rich", 2)["rows"]
    assert [int(r["balance"]) for r in pages] == [u for u, _o in real], "every holder once, biggest first"
    assert [r["rank"] for r in pages] == list(range(1, 50))
    assert "Dnone" not in {r["owner"] for r in pages}, "nothing held, not on the list"
    assert index.rich_list("rich", 3)["rows"] == [] and index.rich_list("nope") is None
    assert "top" not in first and first["tick"] == "RICH" and first["deploy"] == "ab" * 32 + "i0"
    with pytest.raises(ValueError):
        index.rich_list("rich", "x")
    # token() keeps its shape for its callers, its top now from the same sort
    assert [h["owner"] for h in index.token("rich", top=3)["top"]] == ["Dhuge", "Dbig", "Dnine"]
    assert index.token("rich", top=0)["holders"] == 49 and index.token("rich", top=0)["top"] == []
    plan = " ".join(str(r) for r in index.store.q(
        f"EXPLAIN QUERY PLAN SELECT owner FROM bal WHERE tick=? AND overall != '0' {dx.RICH_ORDER} LIMIT 20", ("rich",)))
    assert "bal_rich" in plan and "TEMP B-TREE" not in plan, "read off the index, never sorted"


def test_decimals_shown_and_the_lookup_made_on_an_index_from_before(tmp_path):
    _chain, index = _walked(tmp_path)
    _token(index, "deci", {"Da": "1500", "Db": "20"}, supply="10000", dec=2)
    rows = index.rich_list("deci")["rows"]
    assert [(r["balance"], r["pct"]) for r in rows] == [("15", 15.0), ("0.2", 0.2)]
    assert index.balances("Da") == [{"tick": "DECI", "units": "1500", "transferable_units": "0", "decimals": 2}]
    # An index of this layout from before 2026-10-21 has no bal_rich: opening it makes one in place.
    index.store.x("DROP INDEX bal_rich")
    index.store.close()
    again = dx.DoginalIndex(tmp_path / "walk.sqlite", None, lambda: "walk")
    assert again.store.q("SELECT name FROM sqlite_master WHERE name='bal_rich'")
    assert again.rich_list("deci")["rows"][0]["owner"] == "Da"


# --------------------------------------------------------------------------- read only: a walker answers over the mesh

def _linked(walker, reader):
    from arcade.mesh.doginalshare import DxShare

    class Link:
        def __init__(self, peer_id):
            self.peer_id, self.far = peer_id, None

        async def send(self, header, blob=b""):
            await self.far.share.on_frame(self.far, header, blob)

    class Node:
        def __init__(self):
            self.links = {}
    w_share, r_share = DxShare(walker), DxShare(reader)
    to_walker, to_reader = Link("walker-node"), Link("reader-node")
    to_walker.far, to_reader.far = to_reader, to_walker
    to_reader.share, to_walker.share = w_share, r_share
    w_share.node, r_share.node = Node(), Node()
    r_share.node.links = {"walker-node": to_walker}
    w_share.node.links = {"reader-node": to_reader}

    def ask(*questions):
        async def go():
            r_share.loop = w_share.loop = asyncio.get_running_loop()
            await r_share.on_frame(to_walker, {"t": "dx", **walker.hello()}, b"")
            out = []
            for q in questions:
                try:
                    out.append(await asyncio.to_thread(reader.lookup, q))
                except ValueError as exc:
                    out.append(exc)
            return out
        return asyncio.run(go())
    return ask


def test_a_read_only_arcade_asks_the_new_questions_of_a_walker(tmp_path):
    chain, walker = _walked(tmp_path)
    _token(walker, "rich", {f"D{k:02d}": str(100 + k) for k in range(30)})
    reader = dx.DoginalIndex(tmp_path / "read.sqlite", lambda: chain, lambda: "read")
    number, ticks, page = _linked(walker, reader)(
        {"what": "number", "number": 0}, {"what": "ticks", "prefix": "ri"}, {"what": "holders", "tick": "rich", "page": 1})
    assert number["number"] == walker.inscription(0) and number["from"] == "arcade walker-node"
    assert ticks["ticks"]["first"][0]["tick"] == "RICH"
    assert [r["rank"] for r in page["holders"]["rows"]] == list(range(21, 31)) and page["holders"]["pages"] == 2
    assert reader.store.q("SELECT COUNT(*) FROM tok")[0][0] == 0, "and the reader keeps nothing"


def test_a_walker_from_before_the_explorer_answers_what_it_can(tmp_path, monkeypatch):
    """An older walker says "no such question" to number, ticks and holders. The rich list's first page comes from its
    "token" answer (its biggest holders); the rest is said plainly, never a bare error."""
    chain, walker = _walked(tmp_path)
    _token(walker, "rich", {f"D{k:02d}": str(100 + k) for k in range(30)})
    newer = walker.answer

    def older(question):
        if question.get("what") in dx.NEWER_QUESTIONS:
            raise ValueError("no such question")
        return newer(question)
    monkeypatch.setattr(walker, "answer", older)
    reader = dx.DoginalIndex(tmp_path / "read.sqlite", lambda: chain, lambda: "read")
    number, first, second = _linked(walker, reader)(
        {"what": "number", "number": 0}, {"what": "holders", "tick": "rich", "page": 0},
        {"what": "holders", "tick": "rich", "page": 1})
    assert isinstance(number, ValueError) and "older version" in str(number) and "walker-node" in str(number)
    assert isinstance(second, ValueError) and "older version" in str(second)
    rows = first["holders"]["rows"]
    assert len(rows) == 20 and rows[0] == {"rank": 1, "owner": "D29", "balance": "129", "transferable": "0", "pct": None}
    assert first["holders"]["holders"] == 30 and "first page" in first["older"]


# --------------------------------------------------------------------------- the routes

@pytest.fixture
def walking(client):
    app, state = client
    state.set_setting("ord_index:main", "walk")
    index = state.ord_indexes()["main"]
    store = index.store
    store.x("INSERT OR IGNORE INTO ctypes (ct, ctype) VALUES (1, 'text/plain')")
    store.many("INSERT INTO insc (rid, tx, n, number, height, txpos, ct, proto, size, pieces, burned) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               [(k + 1, bytes([k + 1]) * 32, 0, k, 186_920 + k, 0, 1, None, 5, 1, None) for k in range(3)])
    store.many("INSERT INTO at VALUES (?,?,?,?,?)", [(k + 1, bytes([k + 1]) * 32, 0, 0, 186_920 + k) for k in range(3)])
    _token(index, "pepi", {f"P{k:02d}": str(10 * k + 5) for k in range(25)} | {"Pbig": "1000"})
    yield app, state
    for table in ("insc", "at", "ctypes", "tok", "bal"):
        store.x(f"DELETE FROM {table}")
    state.set_setting("ord_index:main", None)


def test_the_explorer_routes(walking):
    app, state = walking
    got = app.get("/admin/api/ords/inscription?net=main", headers=LOCAL).json()
    x = got["number"]["inscription"]
    assert x["number"] == 0 and x["id"] == "01" * 32 + "i0" and got["number"]["last"] == 2 and got["from"] == "this arcade"
    assert x["output"] == "01" * 32 + ":0" and "owner" in x
    assert app.get("/admin/api/ords/inscription?net=main&number=2", headers=LOCAL).json()["number"]["inscription"][
        "number"] == 2
    assert app.get("/admin/api/ords/inscription?net=main&number=3", headers=LOCAL).json()["number"]["inscription"] is None
    assert app.get(f"/admin/api/ords/inscription?net=main&id={'02' * 32}i0", headers=LOCAL).json()[
        "number"]["inscription"]["number"] == 1
    for bad in ("number=-1", "number=x", "number=1.5", "id=nonsense"):
        assert app.get(f"/admin/api/ords/inscription?net=main&{bad}", headers=LOCAL).status_code == 400, bad
    assert app.get("/admin/api/ords/inscription?net=nochain", headers=LOCAL).status_code == 404
    # the search box takes a number now, and a tick of digits as tick:
    said = app.get("/admin/api/ords/lookup?net=main&q=1", headers=LOCAL).json()
    assert said["number"]["inscription"]["number"] == 1
    assert app.get("/admin/api/ords/lookup?net=main&q=%232", headers=LOCAL).json()["number"]["inscription"]["number"] == 2
    assert app.get("/admin/api/ords/lookup?net=main&q=tick:pepi", headers=LOCAL).json()["token"]["holders"] == 26
    # ticks and holders
    ticks = app.get("/admin/api/ords/ticks?net=main&q=pe", headers=LOCAL).json()
    assert ticks["ticks"]["count"] == 1 and ticks["ticks"]["first"][0]["tick"] == "PEPI"
    assert app.get("/admin/api/ords/ticks?net=main&q=" + "x" * 9, headers=LOCAL).status_code == 400
    rich = app.get("/admin/api/ords/holders?net=main&tick=pepi", headers=LOCAL).json()["holders"]
    assert rich["pages"] == 2 and len(rich["rows"]) == 20 and rich["rows"][0]["owner"] == "Pbig"
    assert rich["rows"][1]["balance"] == "245", "245 before 1000's neighbours as numbers, not text"
    second = app.get("/admin/api/ords/holders?net=main&tick=PEPI&page=1", headers=LOCAL).json()["holders"]
    assert len(second["rows"]) == 6 and second["rows"][-1]["balance"] == "5"
    for bad in ("tick=&page=0", "tick=pepi&page=-1", "tick=pepi&page=one", "tick=" + "y" * 9):
        assert app.get(f"/admin/api/ords/holders?net=main&{bad}", headers=LOCAL).status_code == 400, bad
    assert app.get("/admin/api/ords/holders?net=main&tick=none", headers=LOCAL).json()["holders"] is None


def test_the_explorer_is_the_operators_only(walking):
    app, state = walking
    for path in ("/admin/api/ords/inscription?net=main&number=0", "/admin/api/ords/ticks?net=main&q=p",
                 "/admin/api/ords/holders?net=main&tick=pepi"):
        assert app.get(path, headers=EDGE).status_code != 200, path


def test_the_explorer_on_the_page_follows_the_chain_at_the_top(walking):
    app, state = walking
    page = app.get("/admin", headers=LOCAL).text
    assert 'id="ordlooknet"' not in page, "no chain choice of its own"
    for part in ('id="ordexplorer"', 'id="ordprev"', 'id="ordnext"', 'id="ordnum"', 'id="ordtickpick"',
                 'class="pick-q"', "window.arcadePick", 'sandbox'):
        assert part in page, part
    assert page.count("window.arcadePick = window.arcadePick") == 1, "the Exchange's one search tool, included once"


def test_an_inscription_other_explorers_miss_carries_a_note(tmp_path):
    """2026-10-21: 83d1cbea...i0 (PEP block 292,159) is a whole inscription by ord-pepecoin's rules that another
    explorer does not show; it stays numbered, with a note (doginal_index.SEEN_ELSEWHERE) the explorer shows."""
    from arcade import doginal_index as dx
    iid = "83d1cbea653d4c8e8f729dc1af8fea4802b27abdafde436524ff8882768d0a31i0"
    assert "Other explorers do not show this one" in dx.SEEN_ELSEWHERE["main"][iid]
    assert "pepeblocks" not in dx.SEEN_ELSEWHERE["main"][iid].lower(), "no outside index named"
