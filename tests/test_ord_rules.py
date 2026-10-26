"""The official indexers' rules (arcade/ord_rules.py, 2026-10-20): inscriptions as wonky-ord / ord-dogecoin
number them, DRC-20 balances as wonky-ord keeps them, and every reading of a "p":"dns" name kept side by side.
Blocks are made up byte for byte (test_doginal_index's Chain) and walked as the real chain is."""
import json

import pytest

from arcade import doginal_index as dx
from arcade import ord_rules, rawchain

from test_doginal_index import FUND, SIG, Chain, _envelope, _push, _tx  # noqa: E402


@pytest.fixture(autouse=True)
def small(monkeypatch):
    monkeypatch.setattr(dx, "CHUNK", 4)
    monkeypatch.setattr(dx, "SETTLE", 0)
    monkeypatch.setattr(dx, "WALK_SETTLE", 0)


def _walk(tmp_path, chain):
    index = dx.DoginalIndex(tmp_path / "w.sqlite", lambda: chain, lambda: "walk")
    index.step()
    return index


def _script(raw, n=0):
    return rawchain.read_tx(raw).vout[n][1]


def _json(obj):
    return json.dumps(obj, separators=(",", ":")).encode()


TEXT = b"text/plain;charset=utf-8"


# --------------------------------------------------------------------------- reading an envelope

def test_pushes_are_read_as_the_indexers_read_them_quirk_and_all():
    assert ord_rules.decode_push_datas(b"\x03ord\x51\x00") == [b"ord", b"\x01", b""]
    assert ord_rules.decode_push_datas(b"\x03ord\xac") is None, "anything but a push: not an inscription"
    # OP_PUSHDATA2 of 300 bytes: the indexers take (0x01 << 8) + 0x4d = 333 as its length, run past the end -> None.
    assert ord_rules.decode_push_datas(b"\x4d\x2c\x01" + b"x" * 300) is None
    assert ord_rules.parse([b"\x03ord\x51\x0atext/plain\x00\x02hi"]) == ("complete", b"text/plain", b"hi")
    assert ord_rules.parse([b"\x03ord\x00\x0atext/plain\x00\x02hi"])[0] == "none", "a piece count of 0"
    assert ord_rules.parse([b"\x03ord\x52\x0atext/plain\x00\x02hi"])[0] == "partial", "countdown must start at n-1"


def test_only_input_0_and_no_new_number_on_an_inscribed_first_koinu(tmp_path, monkeypatch):
    monkeypatch.setitem(dx.STARTS, "lky", dx.BASE)            # the fake chain starts where a Dogecoin one does
    chain = Chain()
    first = _tx([(FUND, 0, _envelope())], [100_000], tag=b"a")
    second_input = _tx([(FUND, 1, SIG), (FUND, 2, _envelope())], [100_000], tag=b"b")   # envelope in input 1
    chain.add([first, second_input])
    # Spends the inscribed coin as input 0 and carries an envelope too: the first koinu is inscribed, so no new one.
    again = _tx([(first[1], 0, _envelope(b"text/html"))], [100_000], tag=b"c")
    chain.add([again])
    # Luckycoin's Doginals kind (wonky-ord's rule): no new number on an inscribed first koinu. Dogecoin numbers it
    # since rules 5 (tests/test_ord_dialects.py); Litecoin reads ord's own envelopes since rules 6 (test_ord_lite.py).
    index = dx.DoginalIndex(tmp_path / "lky.sqlite", lambda: chain, lambda: "walk", net="lky")
    index.walker.step(chain, dx.BASE + 1, budget=60)
    ids = [dx.iid_of(*r) for r in index.store.q("SELECT tx, n FROM insc ORDER BY number")]
    assert ids == [f"{first[1]}i0"], ids
    assert index.where(f"{first[1]}i0")["output"] == f"{again[1]}:0", "it moved; nothing new was born on it"


def test_numbers_follow_completion_order(tmp_path):
    chain = Chain()
    multi = _tx([(FUND, 0, _envelope(parts=((1, b"one"),), total=2))], [100_000], tag=b"m")
    single = _tx([(FUND, 1, _envelope())], [100_000], tag=b"s")
    chain.add([multi, single])
    from test_doginal_index import _more
    rest = _tx([(multi[1], 0, _more(((0, b"two"),)))], [100_000], tag=b"r")
    chain.add([rest])
    index = _walk(tmp_path, chain)
    assert index.where(f"{single[1]}i0")["number"] == 0, "complete first"
    assert index.where(f"{multi[1]}i0")["number"] == 1, "numbered when its last piece arrives"


# --------------------------------------------------------------------------- DRC-20

def _drc(chain_txs, chain, op, fund_vout, tag, to_value=100_000):
    t = _tx([(FUND, fund_vout, _envelope(TEXT, parts=((0, _json(op)),)))], [to_value], tag=tag)
    chain_txs.append(t)
    return t


def test_deploy_mint_and_a_transfer_note_move_balances_as_wonky_ord_does(tmp_path):
    chain = Chain()
    txs: list = []
    deploy = _drc(txs, chain, {"p": "drc-20", "op": "deploy", "tick": "Wow!", "max": "100", "lim": "60"}, 0, b"d")
    mint1 = _drc(txs, chain, {"p": "drc-20", "op": "mint", "tick": "wow!", "amt": "60"}, 1, b"m1")
    mint2 = _drc(txs, chain, {"p": "drc-20", "op": "mint", "tick": "WOW!", "amt": "60"}, 2, b"m2")   # cut to 40
    big = _drc(txs, chain, {"p": "drc-20", "op": "mint", "tick": "wow!", "amt": "61"}, 3, b"m3")     # over the limit
    chain.add(txs)
    index = _walk(tmp_path, chain)
    tok = index.token("WOW!")
    assert tok["minted"] == "100" and tok["supply"] == "100" and tok["limit"] == "60", tok
    w = index.walker
    alice, bob = w.owner_of(_script(mint1[0])), w.owner_of(_script(mint2[0]))
    bal = {b["owner"]: b["balance"] for b in tok["top"]}
    assert bal == {alice: "60", bob: "40"}, "the second mint cut to what was left; the third refused"

    # Alice sets 25 aside on a note, then sends the note: the receiver gets 25.
    txs = []
    note_op = {"p": "drc-20", "op": "transfer", "tick": "wow!", "amt": "25"}
    # The note must be inscribed TO alice: an output paying alice's script.
    note = _tx([(FUND, 9, _envelope(TEXT, parts=((0, _json(note_op)),)))], [100_000], tag=b"d")
    assert w.owner_of(_script(note[0])) != alice
    alice_script = _script(mint1[0])
    note_raw = bytearray(note[0])
    note_raw[-4 - len(alice_script):-4] = alice_script
    note = (bytes(note_raw), rawchain.read_tx(bytes(note_raw)).txid)
    chain.add([note])
    index.step()
    assert {b["owner"]: (b["balance"], b["transferable"]) for b in index.token("wow!")["top"]}[alice] == ("60", "25")
    send = _tx([(note[1], 0, SIG)], [100_000], tag=b"to-carol")
    chain.add([send])
    index.step()
    carol = w.owner_of(_script(send[0]))
    bal = {b["owner"]: b["balance"] for b in index.token("wow!")["top"]}
    assert bal[alice] == "35" and bal[carol] == "25", bal
    # Its second move is nothing.
    again = _tx([(send[1], 0, SIG)], [100_000], tag=b"to-dave")
    chain.add([again])
    index.step()
    assert w.owner_of(_script(again[0])) not in {b["owner"] for b in index.token("wow!")["top"]}


def test_a_note_sent_into_the_fee_goes_back_to_its_sender_and_a_reorg_undoes_balances(tmp_path):
    chain = Chain()
    chain.add([_tx([(FUND, 0, _envelope(TEXT, parts=((0, _json({"p": "drc-20", "op": "deploy", "tick": "abcd",
                                                                   "max": "10"})),)))], [100_000], tag=b"d")])
    mint = _tx([(FUND, 1, _envelope(TEXT, parts=((0, _json({"p": "drc-20", "op": "mint", "tick": "abcd",
                                                             "amt": "10"})),)))], [100_000], tag=b"m")
    chain.add([mint])
    index = _walk(tmp_path, chain)
    w = index.walker
    owner = w.owner_of(_script(mint[0]))
    note_op = _json({"p": "drc-20", "op": "transfer", "tick": "abcd", "amt": "4"})
    note = _tx([(mint[1], 0, SIG), (FUND, 2, _envelope(TEXT, parts=((0, note_op),)))], [100_000, 100_000], tag=b"n")
    # input 0 carries the mint's inscription at the first koinu: no note is born there -- the official rule.
    chain.add([note])
    index.step()
    assert index.store.q("SELECT COUNT(*) FROM xfer")[0][0] == 0
    before = {b["owner"]: b["balance"] for b in index.token("abcd")["top"]}
    assert before == {owner: "10"}
    # A reorg back over the last block puts everything as it was.
    chain.replace_from(dx.BASE + 2)
    chain.add([])
    index.step()
    assert {b["owner"]: b["balance"] for b in index.token("abcd")["top"]} == {owner: "10"}


def test_numbers_and_ticks_are_read_strictly():
    assert ord_rules.token_op("drc-20", TEXT, _json({"p": "drc-20", "op": "mint", "tick": "wow", "amt": "1"})) is None, \
        "a tick is exactly 4 bytes"
    assert ord_rules.token_op("drc-20", TEXT, _json({"p": "drc-20", "op": "mint", "tick": "wows", "amt": 1})) is None, \
        "amounts are strings"
    short = b'{"p":"drc-20","op":"deploy","tick":"wo"}'[:39]
    assert len(short) == 39 and ord_rules.token_op("drc-20", TEXT, short) is None, "under 40 characters: not DRC-20"
    assert ord_rules.token_op("drc-20", TEXT, b'{"p":"drc-20","op":"mint","tick":"wow!","amt":"1"}')["amt"] == "1"
    assert ord_rules.token_op("prc-20", TEXT, _json({"p": "drc-20", "op": "mint", "tick": "wows",
                                                      "amt": "1000"})) is None, "Pepecoin reads prc-20"
    for bad in ("1e3", "+1", "-1", ".5", "5.", "1_000", " 1"):
        with pytest.raises(ord_rules.TokenError):
            ord_rules._num(bad)
    assert ord_rules._num("2.50") == ord_rules._num("2.5")


# --------------------------------------------------------------------------- names

def test_every_reading_of_a_name_is_kept_and_their_disagreements_show(tmp_path):
    chain = Chain()
    text_first = _tx([(FUND, 0, _envelope(TEXT, parts=((0, b"jon.doge"),)))], [100_000], tag=b"t")
    json_later = _tx([(FUND, 1, _envelope(TEXT, parts=((0, _json({"p": "dns", "op": "reg", "name": "Jon.doge"})),)))],
                     [100_000], tag=b"j")
    other = _tx([(FUND, 2, _envelope(TEXT, parts=((0, _json({"p": "dns", "op": "reg", "name": "wow.dogecoin"})),)))],
                [100_000], tag=b"o")
    chain.add([text_first, json_later, other])
    index = _walk(tmp_path, chain)
    jon = index.name("jon.doge")["readings"]
    assert jon["spec"]["winner"]["inscription"] == f"{text_first[1]}i0", "the spec counts the plain-text mint first"
    assert jon["spec"]["later_claims"] == 1
    assert jon["json"]["winner"]["inscription"] == f"{json_later[1]}i0", "JSON only: the later JSON claim wins"
    assert jon["doge2023"]["winner"]["inscription"] == f"{json_later[1]}i0"
    wow = index.name("wow.dogecoin")["readings"]
    assert wow["doge2023"]["winner"] is None, "the 2023 guide counts .doge only"
    seen = index.name_disagreements()
    assert seen["different_first_claim"] == 1 and seen["examples"][0]["name"] == "jon.doge"
    # the shape /account/ord/name reads (Creator -> Names): a name's answer under "name"
    said = index.answer({"what": "name", "name": "jon.doge"})
    assert said["name"]["readings"]["spec"]["winner"]["inscription"] == f"{text_first[1]}i0"


def test_name_rules():
    n = ord_rules.normalise
    assert n("spec", "text", "Jon.Doge") == "jon.doge"
    assert n("spec", "text", "a.b.doge") is None, "exactly one dot"
    assert n("spec", "text", "jоn.doge") is None, "Latin and Cyrillic letters in one label"
    assert n("spec", "text", "self_total.doge") is None, "no underscore"
    assert n("spec", "text", "Ｊｏｎ.doge") == "jon.doge", "fullwidth folds to ASCII"
    assert n("json", "text", "jon.doge") is None and n("json", "json", "Jon.doge") == "jon.doge"
    assert n("doge2023", "json", "jon.dogecoin") is None and n("doge2023", "json", "JON.doge extra") == "jon.doge"
    assert ord_rules.name_claim(TEXT, b"hello world") is None, "no dot: not a claim"
    assert ord_rules.name_claim(TEXT, b"jon.doge and more") == ("text", "jon.doge")


def test_block_maps_are_not_names():
    """2026-10-26: "fix the swamped indexer" -- Pepecoin's 1.64 million "<block>.pepemap" claims were counted as
    names under the spec reading, beside some 2,750 real ones."""
    n = ord_rules.normalise
    for claim in ("295434.pepemap", "3.pepmap", "12.kekmap", "7.frogmap", "9.pepemaps", "4.pepemep", "1.bitmap",
                  "5.dogemap", "5.dogemaps"):
        assert n("spec", "text", claim) is None, claim
    for name in ("market.pepe", "genesis.pep", "frog.ribbit", "me-seeks.mesh", "pepmap.pepe", "maps.kek"):
        assert n("spec", "text", name) == name, name
    assert n("spec", "text", "-meseeks.mesh") is None and n("spec", "text", "meseeks-.mesh") is None, \
        "a hyphen inside a label, never at its ends"


def test_an_index_from_before_loses_its_block_maps_once_and_keeps_its_names(tmp_path):
    """The names an index already kept: block maps out on its next open, the real names and JSON claims kept."""
    import sqlite3
    from arcade.doginal_index import Store
    path = tmp_path / "ord-main.sqlite"
    s = Store(path, net="main")
    rows = [(1, 10, 100, "text", "1.pepemap", "1.pepemap", None, None),
            (2, 11, 100, "text", "market.pepe", "market.pepe", None, None),
            (3, 12, 100, "json", "2.pepmap", "2.pepmap", "2.pepmap", None),         # JSON-only still counts it
            (4, 13, 100, "text", "7.kekmap", "7.kekmap", None, None)]
    s._db.executemany("INSERT INTO names VALUES (?,?,?,?,?,?,?,?)", rows)
    s._db.execute("DELETE FROM meta WHERE k='names_maps'")                           # as an index walked before
    s._db.close()
    s = Store(path, net="main")
    got = {r[0]: (r[1], r[2]) for r in sqlite3.connect(path).execute("SELECT rid, spec, json FROM names")}
    assert got == {2: ("market.pepe", None), 3: (None, "2.pepmap")}
    assert s.meta("names_maps") == 1
    s._db.execute("INSERT INTO names VALUES (5, 14, 101, 'text', '8.pepemap', '8.pepemap', NULL, NULL)")
    Store(path, net="main")
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM names WHERE rid=5").fetchone()[0] == 1, \
        "once per index: what the walk writes from now on is the rule's own business"


def test_every_inscription_keeps_its_type_its_protocol_and_its_size(tmp_path):
    """2026-10-20: "keep track of which ord are what MIME type? So we can go back and scan by mime type later?
    In case there is another protocol we want to index" -- the type as text, a JSON body's "p", and the size."""
    chain = Chain()
    png = _tx([(FUND, 0, _envelope(b"image/png", parts=((0, b"\x89PNG..."),)))], [100_000], tag=b"p")
    mint = _tx([(FUND, 1, _envelope(TEXT, parts=((0, _json({"p": "drc-20", "op": "mint", "tick": "abcd",
                                                             "amt": "1"})),)))], [100_000], tag=b"m")
    other = _tx([(FUND, 2, _envelope(TEXT, parts=((0, _json({"p": "some-new-thing", "v": 1})),)))], [100_000],
                tag=b"o")
    chain.add([png, mint, other])
    index = _walk(tmp_path, chain)
    w = index.where(f"{png[1]}i0")
    assert (w["content_type"], w["protocol"], w["size"]) == ("image/png", None, 7)
    assert index.where(f"{mint[1]}i0")["protocol"] == "drc-20"
    assert index.kinds(proto="some-new-thing") == {"count": 1, "first": [{"number": 2, "id": f"{other[1]}i0"}]}
    assert index.kinds(ctype="text/")["count"] == 2 and index.kinds(ctype="image/")["count"] == 1
    summary = index.kinds()
    assert ("image/png", 1) in [tuple(r) for r in summary["types"]]
    assert {r[0] for r in summary["protocols"]} == {"drc-20", "some-new-thing"}
    assert ord_rules.protocol_of(b'  {"p":"dns","op":"reg","name":"a.doge"}') == "dns"
    assert ord_rules.protocol_of(b"jon.doge") is None and ord_rules.protocol_of(b"{not json") is None
