"""The Wallet's Doginals one page at a time (arcade/doginal_pages.py, 2026-11-06, the operator: "build it", #91's "load a big
wallet's Doginals one page at a time"; "Check it against apples big wallet"). The arcade builds an address's list once:
its coins read once, the guard's answer for each small coin, the numbers kept for good, the collections looked up in
one go. The browser asks for one page."""

import pathlib
import sqlite3
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_doginal_wallet import ME, _doge_account                 # noqa: E402
from test_web import app_state, client                            # noqa: F401,E402

from arcade import doginal_pages as P                             # noqa: E402
from arcade import doginals                                       # noqa: E402


class Guard:
    """The inscription guard's answers: one inscription on each small coin of `inscribed`, counted."""

    def __init__(self, inscribed, unknown=(), down=()):
        self.inscribed, self.unknown, self.down, self.asked = inscribed, set(unknown), set(down), []

    def held(self, txid):
        self.asked.append(txid)
        if txid in self.down:
            raise doginals.Unknown("down")
        if txid in self.unknown:
            return {0: [{"id": None, "unknown": True, "wait": True, "h": 6_000_000}]}
        return {0: [{"id": txid + "i0", "offset": 0, "ctype": self.inscribed[txid]}]} if txid in self.inscribed else {}

    def ctype_of(self, iid, read=True):
        return None


def _wallet(n_ins=7000, n_tokens=4400):
    """A wallet the size of @apple's: about 7,100 coins, 7,000 of them inscribed, 4,400 of those DRC-20 operations."""
    coins, inscribed, numbers = [], {}, {}
    for k in range(n_ins):
        t = f"{k:064x}"
        coins.append({"txid": t, "vout": 0, "value": 100_000})
        token = k < n_tokens
        inscribed[t] = "text/plain" if token or k % 7 == 0 else "image/png"
        numbers[t + "i0"] = {"number": (k * 7919) % 1_000_003 + 10, "height": 4_600_000 + k,
                             "content_type": inscribed[t], "size": 400, "protocol": "drc-20" if token else None}
    for k in range(100):                                            # plain coins, a few too big to be postage
        coins.append({"txid": f"{k + 10**6:064x}", "vout": 0, "value": 100_000 if k < 90 else 5_000_000_000})
    return coins, inscribed, numbers


def _source(coins, guard, numbers, colls=None, asked=None):
    asked = asked if asked is not None else []

    def ask(ids):
        asked.append(len(ids))
        assert len(ids) <= P.NUMBERS_AT_ONCE
        return {i: numbers[i] for i in ids if i in numbers}
    colls = colls or {}
    return P.Source(coins=lambda: list(coins), guard=guard, numbers=ask,
                    collections=lambda ids: {i: colls[i] for i in ids if i in colls}, token_protocol="drc-20"), asked


def test_a_big_wallet_is_built_once_in_number_order_without_its_token_operations(tmp_path):
    coins, inscribed, numbers = _wallet()
    some = sorted(numbers)[5000:5012]
    colls = {i: {"name": "DogePunks", "item": f"DogePunk #{n}", "position": n, "slug": "dogepunks", "source": "mesh"}
             for n, i in enumerate(some, 1)}
    guard = Guard(inscribed)
    source, asked = _source(coins, guard, numbers, colls)
    pages = P.Pages(P.Numbers(tmp_path / "n.sqlite"), unknown=doginals.Unknown)
    started = time.time()
    doc = pages.build("doge", source)
    took = time.time() - started
    assert doc["done"] and doc["of"] == 7090, "only the small coins' transactions are looked at"
    assert sorted(guard.asked) == sorted({c["txid"] for c in coins if c["value"] <= P.POSTAGE})
    assert sum(asked) == 7000 and max(asked) == P.NUMBERS_AT_ONCE, "numbers a question's worth at a time"
    page = P.view(doc, size=30)
    assert page["counts"] == {"all": 2600, "art": 2600 - sum(1 for k in range(4400, 7000) if k % 7 == 0),
                              "text": sum(1 for k in range(4400, 7000) if k % 7 == 0), "tokens": 4400}
    nums = sorted(numbers[t + "i0"]["number"] for t in inscribed if numbers[t + "i0"]["protocol"] is None)
    assert [g["number"] for g in page["items"]] == nums[:30], "earliest number first"
    assert page["pages"] == 87 and page["total"] == 2600 and len(page["items"]) == 30
    assert page["collections"] == [{"key": "dogepunks", "name": "DogePunks", "n": 12}] and page["loose"] == 2588
    last = P.view(doc, page=86, order="new")
    assert [g["number"] for g in last["items"]] == nums[:20][::-1], "newest first, the last page the earliest"
    punks = P.view(doc, coll="dogepunks")
    assert punks["total"] == 12 and all(g["collection"]["slug"] == "dogepunks" for g in punks["items"])
    text = P.view(doc, kind="text", size=96)
    assert text["total"] == page["counts"]["text"] and all(g["text"] for g in text["items"])
    # Go to one by number: the page holding it, marked.
    target = nums[1234]
    went = P.view(doc, go=f"#{target:,}")
    assert went["page"] == 1234 // 30 and went["focus"] in {g["id"] for g in went["items"]}
    assert P.view(doc, go="#5")["missed"] is True
    assert took < 20, f"a wallet of 7,000 built in {took:.1f}s"
    # Built again (a minute later): every number from this arcade's own copy, none asked of the index.
    asked.clear()
    again = pages.build("doge", source)
    assert asked == [] and [g["id"] for g in again["items"]] == [g["id"] for g in doc["items"]]


def test_waiting_unread_and_failing_coins_are_counted_not_shown(tmp_path):
    coins = [{"txid": t * 64, "vout": 0, "value": 100_000} for t in "abcd"]
    guard = Guard({"a" * 64: "image/png"}, unknown={"b" * 64}, down={"c" * 64})
    source, _ = _source(coins, guard, {"a" * 64 + "i0": {"number": 3}})
    doc = P.Pages(P.Numbers(tmp_path / "n.sqlite"), unknown=doginals.Unknown).build("doge", source)
    page = P.view(doc)
    assert [g["id"] for g in page["items"]] == ["a" * 64 + "i0"]
    assert (page["waiting"], page["unread"], page["failed"]) == (1, 1, 1)
    assert page["unnumbered"] == 0 and page["counts"]["all"] == 1


def test_one_the_index_does_not_hold_comes_last_and_is_asked_again(tmp_path):
    coins = [{"txid": t * 64, "vout": 0, "value": 100_000} for t in "ab"]
    numbers = {"b" * 64 + "i0": {"number": 9}}
    source, asked = _source(coins, Guard({"a" * 64: "image/png", "b" * 64: "image/png"}), numbers)
    pages = P.Pages(P.Numbers(tmp_path / "n.sqlite"), unknown=doginals.Unknown)
    doc = pages.build("doge", source)
    assert [g["number"] for g in doc["items"]] == [9, None] and P.view(doc)["unnumbered"] == 1
    numbers["a" * 64 + "i0"] = {"number": 4}
    asked.clear()
    doc = pages.build("doge", source)
    assert asked == [1], "only the one it lacked" and [g["number"] for g in doc["items"]] == [4, 9]


def test_a_list_is_built_again_when_the_coins_change_and_stays_shown_meanwhile(tmp_path):
    coins = [{"txid": t * 64, "vout": 0, "value": 100_000} for t in "ab"]
    numbers = {"a" * 64 + "i0": {"number": 1}, "b" * 64 + "i0": {"number": 2}, "c" * 64 + "i0": {"number": 3}}
    gate = threading.Event()
    gate.set()

    class Slow(Guard):
        def held(self, txid):
            gate.wait(5)
            return super().held(txid)
    guard = Slow({t * 64: "image/png" for t in "abc"})
    source, _ = _source(coins, guard, numbers)
    source.coins = lambda: list(coins)
    pages = P.Pages(P.Numbers(tmp_path / "n.sqlite"), unknown=doginals.Unknown)
    key = ("doge", ME, "me")
    first = pages.get(key, source, wait=5)
    assert first["done"] and [g["number"] for g in first["items"]] == [1, 2]
    assert pages.get(key, source, wait=5)["building"] is False, "the same coins: not built again"
    # A coin arrives while the guard is slow: the old list is served until the new one is whole.
    gate.clear()
    coins.append({"txid": "c" * 64, "vout": 0, "value": 100_000})
    during = pages.get(key, source, wait=0.2)
    assert during["building"] and [g["number"] for g in during["items"]] == [1, 2], "never a blank page"
    gate.set()
    for _ in range(100):
        now = pages.get(key, source, wait=1)
        if not now["building"]:
            break
        time.sleep(0.05)
    assert [g["number"] for g in now["items"]] == [1, 2, 3]
    # A send: the coin is gone, and the list follows at once.
    coins.pop(0)
    gone = pages.get(key, source, wait=5)
    assert [g["number"] for g in gone["items"]] == [2, 3]


def test_the_page_route_answers_one_page_of_the_accounts_own_doginals(client, monkeypatch, tmp_path):
    from arcade import funding, modules
    from arcade.web import door
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    many = [f"{k + 0x900:064x}" for k in range(2000)]
    with state.token_index(state.doge).open() as db:
        for t in many:
            db.conn.execute("INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) VALUES (?,?,?,?,?)",
                            (t, 0, ME, 100_000, 2))
        db.conn.commit()
    answers = {t: {"txid": t, "inscriptions": [{"vout": 0, "inscription_id": t + "i0", "sat_offset": 0}]} for t in many}
    answers.update({"11" * 32: {"txid": "11" * 32, "inscriptions": []}, "22" * 32: {"txid": "22" * 32,
                                                                                     "inscriptions": []}})
    monkeypatch.setattr(funding, "GUARD", doginals.Doginals(tmp_path / "dg2.sqlite", fetch=lambda t: answers[t]))
    funding.GUARD.ctype_of = lambda iid: "image/png"
    asked = []

    class Index:
        def lookup(self, q):
            asked.append(len(q["ids"]))
            return {"numbers": {i: {"number": 10_000 - (int(i[:64], 16) - 0x900), "content_type": "image/png"}
                                for i in q["ids"]}}
    monkeypatch.setattr(type(state), "ord_indexes", lambda self: {"doge": Index()})
    got = None
    for _ in range(200):
        got = app.get("/account/doginals/page?chain=doge&page=0&size=48")
        assert got.status_code == 200, got.text
        if got.json()["done"]:
            break
        time.sleep(0.05)
    body = got.json()
    assert body["done"] and body["counts"]["all"] == 2000 and body["pages"] == 42 and len(body["items"]) == 48
    numbers = [g["number"] for g in body["items"]]
    assert numbers == sorted(numbers), "in number order"
    assert {"id", "txid", "vout", "value", "number", "collection", "text"} <= set(body["items"][0])
    assert sum(asked) == 2000
    # Paging and filtering read the kept list: the index is not asked again.
    nxt = app.get("/account/doginals/page?chain=doge&page=1&size=48").json()
    assert nxt["page"] == 1 and nxt["items"][0]["number"] > numbers[-1] and sum(asked) == 2000
    assert app.get(f"/account/doginals/page?chain=doge&address={'1' * 34}").status_code == 403
    assert "/account/doginals/page" in door.PUBLIC_PAGES
    assert any("/account/doginals/page" in m.routes for m in modules._MODULES)


def test_both_collection_stores_answer_many_ids_in_one_go(tmp_path):
    from arcade.collection_registry import SCHEMA, Registry
    from arcade.known_collections import KnownCollections
    store = KnownCollections(tmp_path / "kc.sqlite")
    ids = [f"{k:064x}i0" for k in range(1200)]
    store.put("doge", "big", "Big", 1200, [{"id": i, "name": f"Big #{n}"} for n, i in enumerate(ids, 1)], "mesh:x")
    store.put("doge", "small", "Small", 2, [{"id": ids[3], "name": "Small #1"}, {"id": ids[4]}], "mesh:y")
    many = store.in_collections("doge", ids + ["ff" * 32 + "i0"])
    assert len(many) == 1200 and all(many[i] == store.in_collection("doge", i) for i in ids[:10])
    assert many[ids[3]]["slug"] == "small", "the smaller collection, as one at a time"
    reg = Registry(tmp_path / "reg.sqlite")
    with sqlite3.connect(reg.path) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO ext_root (txid, number, publisher, network, status, name, chain, supply) "
                   "VALUES ('root1', 5, 'D', 'doge', 'active', 'Anti Doginal Dogs', 'doge', 2)")
        db.executemany("INSERT INTO ext_item (chain, id, root, number) VALUES ('doge', ?, 'root1', ?)",
                       [(ids[0], 1), (ids[700], 2)])
    got = reg.items_of("doge", ids)
    assert got == {ids[0]: {"root": "root1", "number": 1, "name": "Anti Doginal Dogs"},
                   ids[700]: {"root": "root1", "number": 2, "name": "Anti Doginal Dogs"}}


def test_a_list_still_being_read_is_already_in_number_order_and_known_answers_are_read_in_one_go(tmp_path):
    """2026-11-06: "My wallet is still not showing my lowest number inscriptions first" and "the wallet takes
    too long to load the initial load". On .25's slow node a big wallet's first list was shown for minutes in id
    order, the numbers coming only at the end; every saved guard answer was read one connection at a time; and an
    inscription the index had not numbered had its whole content fetched from the chain at every step."""
    coins, inscribed, numbers = _wallet(n_ins=3000, n_tokens=0)

    class Kept(Guard):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.many, self.reads = 0, []

        def kept_many(self, txids):
            self.many += 1
            return {t: {0: [{"id": t + "i0", "offset": 0, "ctype": self.inscribed[t]}]}
                    for t in list(txids)[:2000] if t in self.inscribed}

        def ctype_of(self, iid, read=True):
            self.reads.append(read)
            return None
    guard = Kept(inscribed)
    for t in list(inscribed)[2500:]:                               # no type anywhere: the index has none either
        inscribed[t] = None
    for t in list(inscribed)[2500:]:
        numbers.pop(t + "i0")
    source, asked = _source(coins, guard, numbers)
    pages = P.Pages(P.Numbers(tmp_path / "n.sqlite"), unknown=doginals.Unknown)
    seen = []
    doc = pages.build("doge", source, publish=lambda d: seen.append(d))
    assert guard.many == 1 and len(guard.asked) == len({c["txid"] for c in coins if c["value"] <= P.POSTAGE}) - 2000, \
        "the saved answers in one read, only the rest asked one by one"
    steps = [d for d in seen if d["items"]]
    assert steps and steps[0]["checked"] == 2000, "the saved answers shown at once"
    for d in steps:
        got = [g["number"] for g in d["items"] if g["number"] is not None]
        assert got == sorted(got) and len(got) >= len(d["items"]) - 2 * P.NUMBERS_AT_ONCE - 500, \
            "a list still being read is in number order, at most the last question's worth without a number"
    assert all(n <= P.NUMBERS_AT_ONCE for n in asked) and all(n == P.NUMBERS_AT_ONCE for n in asked[:-1])
    assert guard.reads and not any(guard.reads), "a type is never read from the chain while a list is built"
    assert len(guard.reads) == 500, "and each one looked for once"
    assert doc["done"] and doc["unnumbered"] == 500
    nums = [g["number"] for g in doc["items"]][:2500]
    assert nums == sorted(nums)


def test_a_numbers_answer_fits_in_one_mesh_frame():
    """A question's answer comes back over a mesh link in one frame (64 KB). 500 ids made 94 KB: never delivered, the
    Wallet on .25 waited out every question and read its own short copy instead (2026-11-06)."""
    import json
    from arcade.mesh.link import MAX_FRAME
    worst = {f"{k:064x}i{k % 1000}": {"number": 180_000_000 + k, "height": 6_445_529, "size": 3_999_999,
                                      "content_type": "application/vnd.something-long+json;charset=utf-8",
                                      "protocol": "drc-20"} for k in range(P.NUMBERS_AT_ONCE)}
    frame = json.dumps({"t": "dx", "op": "a", "id": 10**9, "numbers": worst, "walked_to": 6_445_529,
                        "from": "arcade 8c95b1358339"})
    assert len(frame.encode()) < MAX_FRAME * 0.9
