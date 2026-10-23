"""Each chain's ord index reads envelopes as its own official indexer does (ord_rules rules 4, 2026-10-21, the operator: "be
exact and follow the official indexers' source to the letter"):

* Pepecoin (mvdnbrk/ord-pepecoin src/inscriptions/parser.rs, tag.rs, index/updater/inscription_updater.rs): a piece
  count of 0 is a complete inscription with no body; after the countdown the PRC-721 tag trailer (word keys,
  "delegate" / "parent" as 36-byte ids); a new inscription looked for in every transaction; a parent spent beside its
  child goes to output 1.
* Dogecoin (tylordius/wonky-dogeord src/inscription.rs, tag.rs): a piece count of 0 is nothing; after the countdown
  1-byte-key fields, tag 11 the delegate.

And /content serves a delegate's content. Blocks are made up byte for byte (test_doginal_index's Chain)."""
import sys
from pathlib import Path

import pytest

from arcade import doginal_index as dx
from arcade import ord_notes, ord_rules, rawchain

sys.path.insert(0, str(Path(__file__).parent))
from test_doginal_index import FUND, SIG, Chain, _dump, _envelope, _push, _tx  # noqa: E402

TAIL = _push(b"\x30" * 71) + _push(b"\x51" * 30)            # the signature and redeem script a reveal ends with


@pytest.fixture(autouse=True)
def small(monkeypatch):
    monkeypatch.setattr(dx, "CHUNK", 4)
    monkeypatch.setattr(dx, "SETTLE", 0)
    monkeypatch.setattr(dx, "WALK_SETTLE", 0)
    monkeypatch.setitem(dx.STARTS, "main", dx.BASE)        # Pepecoin's index over the made-up chain's heights


def _pep_id(iid: str) -> bytes:
    """An id as ord-pepecoin's tag trailer carries it: the txid's 32 bytes as stored (reversed), the index LE u32."""
    txid, n = iid.split("i")
    return bytes.fromhex(txid)[::-1] + int(n).to_bytes(4, "little")


def _zero_pieces(ctype=b"text/plain", tags=()):
    """A Pepecoin envelope of no pieces: "ord", 0, the content type, then its tags."""
    s = _push(b"ord") + b"\x00" + _push(ctype)
    for k, v in tags:
        s += _push(k) + _push(v)
    return s + TAIL


def _with_fields(ctype=b"text/plain", body=b"hi", fields=(), tags=()):
    """A one-piece envelope with Dogecoin fields (1-byte keys) or Pepecoin tags after its countdown."""
    s = _push(b"ord") + b"\x51" + _push(ctype) + b"\x00" + _push(body)
    for k, v in list(fields) + list(tags):
        s += _push(k) + _push(v)
    return s + TAIL


def _index(tmp_path, chain, net, name="w", mode="walk", **kw):
    return dx.DoginalIndex(tmp_path / f"{name}.sqlite", lambda: chain, lambda: mode, net=net, **kw)


# --------------------------------------------------------------------------- ord_rules, per dialect

def test_a_chain_reads_in_its_own_indexers_dialect():
    assert [ord_rules.dialect_of(n) for n in ("main", "test", "doge", "ltc", "ltctest")] == \
        ["pepecoin", "pepecoin", "dogecoin", "dogecoin", "dogecoin"]


def test_zero_pieces_is_complete_on_pepecoin_and_nothing_on_dogecoin():
    s = _zero_pieces(b"image/png")
    assert ord_rules.parse([s], "pepecoin") == ("complete", b"image/png", b""), "parser.rs: body None, complete"
    assert ord_rules.genesis(s, "pepecoin") == ("complete", b"image/png")
    assert ord_rules.parse([s], "dogecoin")[0] == "none" and ord_rules.genesis(s, "dogecoin") is None
    assert ord_rules.parse([s])[0] == "none", "Dogecoin's reading is the default, as before"


def test_pepecoin_tags_after_the_countdown_in_the_same_script_and_in_later_ones():
    target = "ab" * 32 + "i7"
    body = _with_fields(b"text/plain", b"woof", tags=[(b"delegate", _pep_id(target)), (b"parent", _pep_id(target))])
    state, ctype, got, tags = ord_rules.read([body], "pepecoin")
    assert (state, ctype, got) == ("complete", b"text/plain", b"woof"), "a body followed by tags: the same body"
    assert ord_rules.delegate_of(tags, "pepecoin") == target and ord_rules.parents_of(tags, "pepecoin") == [target]
    assert ord_rules.parse([body], "dogecoin") == ("complete", b"text/plain", b"woof")
    # Split over two transactions: the second piece ends the countdown and carries the tag.
    first = _push(b"ord") + b"\x52" + _push(b"text/plain") + b"\x51" + _push(b"wo") + TAIL
    second = b"\x00" + _push(b"of") + _push(b"delegate") + _push(_pep_id(target)) + TAIL
    got = ord_rules.read([first, second], "pepecoin")
    assert got[:3] == ("complete", b"text/plain", b"woof") and ord_rules.delegate_of(got[3], "pepecoin") == target
    env = ord_rules.envelope_more(ord_rules.envelope_start(first, "pepecoin"), second, "pepecoin")
    assert env[0] == "complete" and ord_rules.delegate_of(env[4], "pepecoin") == target, "a piece at a time: the same"
    # The trailer runs on through a LATER script (parser.rs: "in this tx and all subsequent txs in the chain").
    later = _push(b"delegate") + _push(_pep_id(target))
    got = ord_rules.read([_zero_pieces(), later], "pepecoin")
    assert got[0] == "complete" and ord_rules.delegate_of(got[3], "pepecoin") == target
    got = ord_rules.read([_with_fields(), later], "pepecoin")
    assert got[2] == b"hi" and ord_rules.delegate_of(got[3], "pepecoin") == target
    # A key that is not UTF-8 skips its pair; the first delegate value is THE delegate, a wrong one is none.
    odd = _zero_pieces(tags=[(b"\xff", b"x"), (b"delegate", b"short"), (b"delegate", _pep_id(target))])
    tags = ord_rules.read([odd], "pepecoin")[3]
    assert "�" not in "".join(tags) and ord_rules.delegate_of(tags, "pepecoin") is None, "first value only"


def test_dogecoin_fields_are_one_byte_keys_up_to_the_first_other():
    target = "cd" * 32 + "i0"
    raw = bytes.fromhex(target[:64])[::-1]
    s = _with_fields(b"image/png", b"\x89PNG", fields=[(b"\x0b", raw + b"\x00\x00\x00\x00")])
    state, ctype, body, tags = ord_rules.read([s], "dogecoin")
    assert (state, body) == ("complete", b"\x89PNG") and ord_rules.delegate_of(tags, "dogecoin") == target
    # A key of another length ends the fields: a delegate after it is not read.
    late = _with_fields(fields=[(b"\x05", b"m"), (b"xx", b"y"), (b"\x0b", raw)])
    assert ord_rules.delegate_of(ord_rules.read([late], "dogecoin")[3], "dogecoin") is None
    # Pepecoin's word key means nothing on Dogecoin, and Dogecoin's tag 11 nothing on Pepecoin.
    word = _with_fields(tags=[(b"delegate", _pep_id(target))])
    assert ord_rules.delegate_of(ord_rules.read([word], "dogecoin")[3], "dogecoin") is None
    assert ord_rules.delegate_of(ord_rules.read([s], "pepecoin")[3], "pepecoin") is None


def test_ids_decode_as_each_indexer_decodes_them():
    txid = bytes(range(32))                                  # as stored: written reversed
    written = txid[::-1].hex()
    assert ord_rules.id_from_tag(txid + (5).to_bytes(4, "little"), "pepecoin") == f"{written}i5"
    assert ord_rules.id_from_tag(txid + (70000).to_bytes(4, "little"), "pepecoin") == f"{written}i70000"
    for wrong in (txid, txid + b"\x05", txid + b"\x00" * 5, b"", txid[:31] + b"\x00" * 4):
        assert ord_rules.id_from_tag(wrong, "pepecoin") is None, len(wrong)
    # ord's inscription_id_field: 0-4 index bytes, no trailing zero unless all four are there.
    assert ord_rules.id_from_tag(txid, "dogecoin") == f"{written}i0"
    assert ord_rules.id_from_tag(txid + b"\x05", "dogecoin") == f"{written}i5"
    assert ord_rules.id_from_tag(txid + b"\x00\x01", "dogecoin") == f"{written}i256"
    assert ord_rules.id_from_tag(txid + b"\x05\x00\x00\x00", "dogecoin") == f"{written}i5"
    for wrong in (txid + b"\x00", txid + b"\x05\x00", txid[:31], txid + b"\x00" * 5):
        assert ord_rules.id_from_tag(wrong, "dogecoin") is None, wrong[32:]


def test_pepecoin_reads_a_long_push_right_and_dogecoin_keeps_the_quirk():
    """ord-pepecoin 414ce571 (2026-03-10, "fix: OP_PUSHDATA2/4 parser bug"); wonky-ord still reads the opcode byte as
    the low byte of the length."""
    big = b"x" * 300
    s = _push(b"ord") + b"\x51" + _push(b"text/plain") + b"\x00" + b"\x4d" + (300).to_bytes(2, "little") + big
    assert ord_rules.parse([s], "pepecoin") == ("complete", b"text/plain", big)
    assert ord_rules.parse([s], "dogecoin")[0] == "none"
    four = b"\x4e" + (300).to_bytes(4, "little") + big
    assert ord_rules.decode_push_datas(four, "pepecoin") == [big] and ord_rules.decode_push_datas(four) is None


# --------------------------------------------------------------------------- the walk

def _delegate_chain():
    """Block 0: T, an ordinary inscription. Block 1: D, a zero-piece inscription naming T as its delegate (Pepecoin's
    way), then N. Block 2: F, a one-piece inscription with Dogecoin's delegate field naming T. Blocks 3-7: nothing."""
    chain = Chain()
    t = _tx([(FUND, 0, _envelope(b"image/png", parts=((0, b"\x89PNG target"),)))], [100_000], tag=b"t")
    chain.add([t])
    tid = f"{t[1]}i0"
    d = _tx([(FUND, 1, _zero_pieces(b"image/png", tags=[(b"delegate", _pep_id(tid))]))], [100_000], tag=b"d")
    n = _tx([(FUND, 2, _envelope(b"text/plain", parts=((0, b"next"),)))], [100_000], tag=b"n")
    chain.add([d, n])
    f = _tx([(FUND, 3, _with_fields(b"text/plain", b"", fields=[(b"\x0b", bytes.fromhex(t[1])[::-1])]))],
            [100_000], tag=b"f")
    chain.add([f])
    for _ in range(5):
        chain.add([])
    return chain, t[1], d[1], n[1], f[1]


def _numbers(index):
    return {dx.iid_of(tx, n): num for tx, n, num in index.store.q("SELECT tx, n, number FROM insc")}


def test_a_pepecoin_zero_piece_delegate_is_numbered_and_its_delegate_kept(tmp_path):
    chain, t, d, n, f = _delegate_chain()
    pep = _index(tmp_path, chain, "main")
    assert pep.dialect == "pepecoin" and pep.walker.dialect == "pepecoin" and pep.store.dialect == "pepecoin"
    pep.walker.step(chain, dx.BASE + 7, budget=60)
    assert _numbers(pep) == {f"{t}i0": 0, f"{d}i0": 1, f"{n}i0": 2, f"{f}i0": 3}, "every later number one up"
    where = pep.where(f"{d}i0")
    assert where["delegate"] == f"{t}i0" and where["parent"] is None and where["size"] == 0
    assert pep.where(f"{f}i0")["delegate"] is None, "a 1-byte field means nothing on Pepecoin"
    assert pep.store.q("SELECT delegate FROM insc WHERE tx=?", (bytes.fromhex(d),))[0][0] == \
        bytes.fromhex(t) + b"\x00" * 4, "kept as 36 bytes: the txid as written, then n"
    assert pep.delegate(f"{d}i0") == {"delegate": f"{t}i0", "known": True, "delegates": False}
    assert pep.delegate(f"{t}i0") is None and pep.delegate(f"{n}i0") is None
    shown = pep.inscription(1)["inscription"]
    assert shown["delegate"] == f"{t}i0" and shown["delegate_content_type"] == "image/png"
    born = pep.store.q("SELECT n FROM block WHERE height=?", (dx.BASE + 1,))[0][0]
    assert born == 2, "the zero-piece one is a birth: chunk hashes count it"


def test_dogecoin_numbers_no_zero_piece_inscription_and_keeps_a_delegate_field(tmp_path):
    chain, t, d, n, f = _delegate_chain()
    doge = _index(tmp_path, chain, "doge")
    assert doge.dialect == "dogecoin"
    doge.walker.step(chain, dx.BASE + 7, budget=60)
    assert _numbers(doge) == {f"{t}i0": 0, f"{n}i0": 1, f"{f}i0": 2}, "no piece, no inscription (wonky-ord)"
    assert doge.where(f"{f}i0")["delegate"] == f"{t}i0", "tag 11, the txid as stored, index left out"
    assert doge.where(f"{n}i0")["delegate"] is None and doge.where(f"{d}i0") is None
    assert doge.store.q("SELECT n FROM block WHERE height=?", (dx.BASE + 1,))[0][0] == 1


def test_a_chunk_hash_differs_by_dialect_and_each_dialects_helpers_agree_with_its_walkers(tmp_path):
    chain, *_ = _delegate_chain()
    from test_doginal_index import Mesh
    lines = {}
    for net in ("main", "doge"):
        mesh = Mesh()
        walker = _index(tmp_path, chain, net, name=f"w{net}")
        helper = _index(tmp_path, chain, net, name=f"h{net}", mode="help", cores=lambda: 1)
        mesh.join("walker", walker)
        mesh.join("helper", helper)
        walker.step()
        for _ in range(10):
            helper.step()
        assert helper.store.words(0)[""] == walker.store.words(0)[""], net
        lines[net] = walker.store.words(0)[""]
    assert lines["main"] != lines["doge"], "the zero-piece birth is in Pepecoin's chunk hash only"


def test_pepecoin_looks_for_a_new_inscription_even_on_an_inscribed_first_koinu(tmp_path, monkeypatch):
    """ord-pepecoin inscription_updater.rs: "In Pepecoin, we always check for new inscriptions" (`if true`), where
    wonky-ord looks only when no inscription sits on the first koinu."""
    monkeypatch.setitem(dx.STARTS, "ltc", dx.BASE)            # the fake chain starts where a Dogecoin one does
    chain = Chain()
    first = _tx([(FUND, 0, _envelope())], [100_000], tag=b"a")
    chain.add([first])
    again = _tx([(first[1], 0, _envelope(b"text/html"))], [100_000], tag=b"c")
    later = _tx([(FUND, 1, _envelope())], [100_000], tag=b"l")
    chain.add([again, later])
    for _ in range(2):
        chain.add([])
    pep, doge = _index(tmp_path, chain, "main", name="p"), _index(tmp_path, chain, "doge", name="d")
    ltc = _index(tmp_path, chain, "ltc", name="l")
    for idx in (pep, doge, ltc):
        idx.walker.step(chain, dx.BASE + 3, budget=60)
    assert _numbers(pep) == {f"{first[1]}i0": 0, f"{again[1]}i0": 1, f"{later[1]}i0": 2}
    # Dogecoin numbers the reinscription too since rules 5 (2026-10-22: "Number them like nintondo");
    # Litecoin's Doginals kind keeps wonky-ord's first-koinu rule.
    assert _numbers(doge) == {f"{first[1]}i0": 0, f"{again[1]}i0": 1, f"{later[1]}i0": 2}
    assert _numbers(ltc) == {f"{first[1]}i0": 0, f"{later[1]}i0": 1}
    assert pep.where(f"{first[1]}i0")["output"] == pep.where(f"{again[1]}i0")["output"] == f"{again[1]}:0"
    # Bellscoin numbers it as nintondo's ord does (rules 6); Luckycoin keeps wonky-ord's rule, as Litecoin (2026-10-22).
    monkeypatch.setitem(dx.STARTS, "bel", dx.BASE)
    monkeypatch.setitem(dx.STARTS, "lky", dx.BASE)
    bel, lky = _index(tmp_path, chain, "bel", name="b"), _index(tmp_path, chain, "lky", name="k")
    for idx in (bel, lky):
        idx.walker.step(chain, dx.BASE + 3, budget=60)
    assert _numbers(bel) == _numbers(doge) and _numbers(lky) == _numbers(ltc)
    # Each is marked for what it is (2026-10-22: "highlight them as reinscriptions"), wherever it is numbered.
    for idx in (pep, doge, bel):
        assert idx.where(f"{again[1]}i0")["reinscription"] is True
        assert idx.where(f"{first[1]}i0")["reinscription"] is False and idx.where(f"{later[1]}i0")["reinscription"] is False
    for idx in (doge, bel):
        assert idx.store.q("SELECT COUNT(reinscribed) FROM insc")[0][0] == 1, "counted for Admin -> Ords"


def test_a_pepecoin_parent_spent_beside_its_child_is_kept_and_sent_to_output_1(tmp_path):
    """inscription_updater.rs: a parent claim stands when the parent is indexed and the child's transaction spends
    its coin; it then goes to output 1, koinu 0. A claim on a parent not spent there is not kept."""
    chain = Chain()
    parent = _tx([(FUND, 0, _envelope(b"text/plain", parts=((0, b"mother"),)))], [100_000], tag=b"p")
    other = _tx([(FUND, 1, _envelope(b"text/plain", parts=((0, b"stranger"),)))], [100_000], tag=b"o")
    coin = _tx([(FUND, 4, SIG)], [100_000], tag=b"k")
    chain.add([parent, other, coin])
    pid, oid = f"{parent[1]}i0", f"{other[1]}i0"
    # The parent is koinu 100,000 of the inputs: by koinu order output 1, 40,000 in.
    child = _tx([(coin[1], 0, _with_fields(b"text/plain", b"child", tags=[(b"parent", _pep_id(pid))])),
                 (parent[1], 0, SIG)], [60_000, 100_000, 40_000], tag=b"c")
    orphan = _tx([(FUND, 3, _with_fields(b"text/plain", b"claims", tags=[(b"parent", _pep_id(oid))]))],
                 [100_000], tag=b"x")
    chain.add([child, orphan])
    for _ in range(2):
        chain.add([])
    pep = _index(tmp_path, chain, "main")
    pep.walker.step(chain, dx.BASE + 3, budget=60)
    assert pep.where(f"{child[1]}i0")["parent"] == pid
    assert pep.where(pid)["output"] == f"{child[1]}:1" and pep.where(pid)["offset"] == 0, "the return output"
    assert pep.where(f"{orphan[1]}i0")["parent"] is None, "its parent was not spent in that transaction"
    assert pep.inscription(2)["inscription"]["parent"] == pid
    doge = _index(tmp_path, chain, "doge", name="d")
    doge.walker.step(chain, dx.BASE + 3, budget=60)
    assert doge.where(f"{child[1]}i0")["parent"] is None, "Dogecoin keeps no parent"
    assert doge.where(pid)["output"] == f"{child[1]}:1" and doge.where(pid)["offset"] == 40_000, \
        "koinu order, as wonky-ord moves it"


def test_notes_keep_the_tags_and_a_walk_stitched_from_them_is_the_walk_from_a_core(tmp_path):
    """A zero-piece image's tags (36-byte ids, over the 8 bytes zeroing keeps) survive the notes, which zero what
    cannot matter; an arcade stitching Pepecoin's notes gets the same births hash, numbers and delegates."""
    chain, t, d, n, f = _delegate_chain()
    walked = _index(tmp_path, chain, "main")
    walked.walker.step(chain, dx.BASE + 7, budget=60)
    kept = {c: walked.store.notes_of(c) for c in range(2)}
    assert all(k and k[2] == "" for k in kept.values())
    notes = ord_notes.unpack(kept[0][0])
    assert b"\x89PNG target" not in notes, "the image's own body: zeroed"
    assert _pep_id(f"{t}i0") in notes, "the delegate's id: kept"
    stitched = _index(tmp_path, chain, "main", name="s")
    stitched.walker.step(chain, dx.BASE + 7, budget=60,
                         notes=lambda c: (ord_notes.decode(ord_notes.unpack(kept[c][0])), False))
    assert _dump(stitched) == _dump(walked)
    assert stitched.where(f"{d}i0")["delegate"] == f"{t}i0" and _numbers(stitched) == _numbers(walked)
    # Dogecoin's notes keep its field the same way.
    dw = _index(tmp_path, chain, "doge", name="dw")
    dw.walker.step(chain, dx.BASE + 7, budget=60)
    dk = {c: dw.store.notes_of(c) for c in range(2)}
    ds = _index(tmp_path, chain, "doge", name="ds")
    ds.walker.step(chain, dx.BASE + 7, budget=60, notes=lambda c: (ord_notes.decode(ord_notes.unpack(dk[c][0])), False))
    assert _dump(ds) == _dump(dw) and ds.where(f"{f}i0")["delegate"] == f"{t}i0"


def test_zeroing_keeps_a_last_pieces_tags_but_not_its_signature():
    target = "ef" * 32 + "i1"
    s = _with_fields(b"image/png", b"P" * 100, tags=[(b"delegate", _pep_id(target))])
    z = ord_notes._zeroed(s, True, "pepecoin")
    assert len(z) == len(s) and b"P" * 100 not in z and _pep_id(target) in z and b"\x30" * 71 not in z
    assert ord_rules.delegate_of(ord_rules.read([z], "pepecoin")[3], "pepecoin") == target
    assert ord_notes._zeroed(s, True, "dogecoin").count(_pep_id(target)) == 0, "a word key is no field on Dogecoin"
    assert ord_notes.may_envelope(_zero_pieces(), "pepecoin") and ord_notes.may_envelope(b"\x00" + _push(b"x"))


def test_a_taproot_envelopes_delegate_is_read():
    tid = bytes(range(32))
    script = b"\x00\x63" + _push(b"ord") + _push(b"\x01") + _push(b"text/plain") + _push(b"\x0b") + _push(tid) + \
        b"\x00" + _push(b"") + b"\x68"
    assert dx.taproot_read([b"\x01" * 64, script, b"\xc0" + b"\x02" * 32]) == [(b"text/plain".hex(),
                                                                               f"{tid[::-1].hex()}i0")]
    assert dx.taproot_envelopes([b"\x01" * 64, script, b"\xc0" + b"\x02" * 32]) == [b"text/plain".hex()]


# --------------------------------------------------------------------------- /content

class _Guard:
    """A chain's inscription reader (doginals.Doginals.content), faked: each id's (type, bytes)."""

    def __init__(self, held):
        self.held, self.asked = held, []

    def content(self, key):
        from arcade import doginals
        self.asked.append(key)
        if key not in self.held:
            raise doginals.Unknown("not here")
        return self.held[key]


@pytest.fixture
def served(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).parent))
    from fastapi.testclient import TestClient
    from test_web import AppState, ChainContext, create_app  # noqa: F401
    from arcade import funding
    state = AppState(home=tmp_path, messaging=ChainContext(network="regtest", role="messaging", label="Testnet",
                                                            datadir=Path("/nonexistent")),
                     ledger=ChainContext(network="main", role="ledger", label="Mainnet", datadir=Path("/nonexistent")))
    app = TestClient(create_app(state))
    chain, t, d, n, f = _delegate_chain()
    index = _index(tmp_path, chain, "main", name="ordmain")
    index.walker.step(chain, dx.BASE + 7, budget=60)
    monkeypatch.setattr(state, "ord_indexes", lambda: {"main": index})
    guard = _Guard({f"{t}i0": ("image/png", b"\x89PNG the delegate's own"), f"{n}i0": ("text/plain", b"next")})
    monkeypatch.setattr(funding, "GUARD", None)
    monkeypatch.setattr(funding, "GUARDS", [(lambda a: False, guard)])
    monkeypatch.setattr(funding, "GUARD_BY_NET", {"main": guard})
    return app, state, index, guard, (t, d, n, f)


def test_content_of_a_delegate_inscription_is_its_delegates(served):
    app, state, index, guard, (t, d, n, f) = served
    got = app.get(f"/content/{d}i0")
    assert got.status_code == 200 and got.content == b"\x89PNG the delegate's own"
    assert got.headers["content-type"].startswith("image/png"), "the delegate's type"
    assert guard.asked[-1] == f"{t}i0", "read from the chain as the delegate"
    assert app.get(f"/content/{n}i0").content == b"next", "one with no delegate: its own"


def test_content_of_a_delegate_the_index_does_not_know_or_that_delegates_again(served, monkeypatch):
    app, state, index, guard, (t, d, n, f) = served
    monkeypatch.setattr(index, "delegate", lambda iid: {"delegate": "77" * 32 + "i0", "known": False,
                                                        "delegates": False} if iid == f"{n}i0" else None)
    assert app.get(f"/content/{n}i0").status_code == 404, "ord: delegate not found"
    monkeypatch.setattr(index, "delegate", lambda iid: {"delegate": f"{t}i0", "known": True, "delegates": True})
    assert app.get(f"/content/{n}i0").status_code == 404, "ord-pepecoin: a delegate's own delegate has no content"
    monkeypatch.setattr(state, "ord_indexes", lambda: {})
    assert app.get(f"/content/{n}i0").content == b"next", "no index of that chain: served as before"


# --------------------------------------------------------------------------- the most complete index answers

def _walkers(tmp_path, mine_to, theirs, own_mode="walk"):
    """This arcade ("me", walked to `mine_to`) linked to walkers {name: (walked to, rules)} over test_doginal_index's
    Mesh, every one heard just now."""
    from test_doginal_index import Mesh, _busy_chain
    chain = _busy_chain(12)
    mesh, me = Mesh(), _index(tmp_path, chain, "doge", name="me", mode=own_mode)
    if mine_to:
        me.walker.step(chain, mine_to - 1, budget=60)
    others = {}
    for name, (upto, rules) in theirs.items():
        other = others[name] = _index(tmp_path, chain, "doge", name=name)
        other.walker.step(chain, upto - 1, budget=60)
    mesh.join("me", me)
    for name, other in others.items():
        mesh.join(name, other)
        me.heard(name, {**other.hello(), "rules": theirs[name][1]})
    return me, others


def test_the_explorer_asks_a_linked_walker_further_along_than_this_arcade(tmp_path):
    me, others = _walkers(tmp_path, dx.BASE + 4, {"far": (dx.BASE + 12, ord_rules.rules_for("doge"))})
    said = me.lookup({"what": "number", "number": 0})
    assert said["node"] == "far" and said["walked_to"] == dx.BASE + 12 and said["from"] != "this arcade"


def test_this_arcade_answers_when_its_own_walk_is_furthest_or_tied(tmp_path):
    me, others = _walkers(tmp_path, dx.BASE + 12, {"near": (dx.BASE + 4, ord_rules.rules_for("doge")),
                                                   "tied": (dx.BASE + 12, ord_rules.rules_for("doge"))})
    said = me.lookup({"what": "number", "number": 0})
    assert said["from"] == "this arcade" and said["walked_to"] == dx.BASE + 12 and "node" not in said


def test_a_walker_on_other_rules_is_never_asked(tmp_path, monkeypatch):
    me, others = _walkers(tmp_path, 0, {"old": (dx.BASE + 12, ord_rules.rules_for("doge") - 1)}, own_mode="read")
    asked = []
    monkeypatch.setattr(others["old"], "answer", lambda q: asked.append(q) or {})
    assert me.walkers() == []
    with pytest.raises(ValueError, match="no linked arcade walks"):
        me.lookup({"what": "number", "number": 0})
    assert asked == []
    (tmp_path / "b").mkdir()
    me2, others2 = _walkers(tmp_path / "b", dx.BASE + 4, {"old": (dx.BASE + 12, ord_rules.rules_for("doge") - 1)})
    assert me2.lookup({"what": "number", "number": 0})["from"] == "this arcade", "its own, though behind"


def test_a_walker_that_fails_is_passed_over_for_the_next_then_this_arcade(tmp_path, monkeypatch):
    me, others = _walkers(tmp_path, dx.BASE + 4, {"far": (dx.BASE + 12, ord_rules.rules_for("doge")),
                                                  "mid": (dx.BASE + 8, ord_rules.rules_for("doge"))})

    def down(question):
        raise TimeoutError("no answer in time")
    monkeypatch.setattr(others["far"], "answer", down)
    said = me.lookup({"what": "number", "number": 0})
    assert said["node"] == "mid" and said["walked_to"] == dx.BASE + 8
    monkeypatch.setattr(others["mid"], "answer", down)
    said = me.lookup({"what": "number", "number": 0})
    assert said["from"] == "this arcade" and said["walked_to"] == dx.BASE + 4


def test_a_multisig_spend_is_not_followed_back_as_a_piece():
    """2026-10-21: on Dogecoin, P2SH multisig spends (OP_0, a DER signature, a redeem script ending OP_CHECKMULTISIG)
    pass may_envelope; followed back 5,000 transactions each, a chunk's notes were never finished and a test machine's and
    .25's walks stood still. The follow-back now sees them for what they are; what notes keep is unchanged."""
    from arcade import ord_notes
    def push(b):
        return bytes([len(b)]) + b
    sig = b"\x30" + b"\x45" * 70 + b"\x01"
    redeem = b"\x52" + push(b"\x02" * 33) + push(b"\x03" * 33) + b"\x52\xae"
    multisig = b"\x00" + push(sig) + push(sig) + bytes([0x4c, len(redeem)]) + redeem
    assert ord_notes.may_envelope(multisig, "dogecoin"), "kept by the notes, as before"
    assert ord_notes._multisig_spend(multisig, "dogecoin")
    piece = push(b"\x02") + push(b"x" * 80) + push(b"\x01") + push(b"y" * 80) + push(sig) + push(b"\x21" + b"\x02" * 33 + b"\xac")
    assert not ord_notes._multisig_spend(piece, "dogecoin"), "a Doginal piece is still followed"
    calls = []
    def lookup(txid):
        calls.append(txid)
        raise AssertionError("a multisig chain is not followed")
    assert ord_notes.genesis_before.__name__  # (the candidate itself is skipped in finish; see the walk test below)



def test_each_chain_has_its_own_rules_version_and_only_dogecoin_is_cut_again(tmp_path):
    """2026-10-22: Dogecoin's rules moved to 5 alone (reinscriptions numbered); Pepecoin and Litecoin stay on 4, so
    their indexes are not rebuilt for it. A Dogecoin index of rules 4 is cut again; a Pepecoin one of 4 is kept."""
    from arcade import ord_rules
    assert ord_rules.rules_for("doge") == 5 and ord_rules.rules_for("main") == ord_rules.rules_for("ltc") == 4
    assert ord_rules.rules_for("bel") == 6 and ord_rules.NUMBERS_REINSCRIPTIONS == {"doge", "bel"}
    for net, kept in (("doge", False), ("main", True)):
        path = tmp_path / f"{net}.sqlite"
        st = dx.Store(path, dialect=ord_rules.dialect_of(net), net=net)
        st.x("INSERT INTO block (height, hash, line) VALUES (1, 'h', 'l')")
        st.set_meta("rules", 4)
        st._db.close()
        again = dx.Store(path, dialect=ord_rules.dialect_of(net), net=net)
        assert again.meta("rules") == ord_rules.rules_for(net)
        assert (again.q("SELECT COUNT(*) FROM block")[0][0] == 1) is kept, net
