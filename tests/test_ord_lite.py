"""Litecoin's ord index read as ord-litecoin reads it (ord_rules.ORD_LITE; doginal_index.Walker._ord_block), 2026-10-26:
to match ord.chikun.market and litescribe.io (stock ynohtna92/ord-litecoin 0.20). Witness envelopes alone; numbered
where they land; ord's curses, negative before the jubilee and vindicated after it; the pointer; unbound; what goes to the
fee lands in the coinbase, numbered after the rest of the block. The chain is made up, byte for byte."""
import hashlib

import pytest

from test_doginal_index import FUND, SIG, Chain, _dump, _push, _tx_to, small   # noqa: F401
from test_bells_index import _wtx

from arcade import doginal_index as dx, ord_notes, ord_rules

P2PKH = b"\x76\xa9\x14" + hashlib.sha256(b"a").digest()[:20] + b"\x88\xac"
P2TR = b"\x51\x20" + hashlib.sha256(b"t").digest()


def _tap(*envelopes) -> list[bytes]:
    """A script-path witness whose tapscript holds these envelopes, each a list of pushes after "ord"."""
    script = _push(b"\x02" * 32) + b"\xac"
    for pushes in envelopes:
        script += b"\x00\x63" + _push(b"ord") + b"".join(_push(p) for p in pushes) + b"\x68"
    return [b"\x30" * 64, script, b"\xc0" + b"\x11" * 32]


def _plain(text: bytes = b"hi") -> list[bytes]:
    return [b"\x01", b"text/plain", b"", text]


@pytest.fixture
def lite(monkeypatch):
    monkeypatch.setitem(dx.STARTS, "ltc", dx.BASE)
    monkeypatch.setitem(ord_rules.ORD_LITE, "ltc", dx.BASE + 100)          # the jubilee far ahead: curses count


def _walk(tmp_path, chain, name="walk"):
    index = dx.DoginalIndex(tmp_path / f"{name}.sqlite", lambda: chain, lambda: "walk", net="ltc")
    index.walker.step(chain, dx.BASE + len(chain.blocks) - 1, budget=60)
    return index


def _numbers(index) -> dict[str, int]:
    return {dx.iid_of(t, n): num for t, n, num in index.store.q("SELECT tx, n, number FROM insc")}


def _story():
    """Block 0 funds; block 1: a plain one, two in one input (the second cursed), one in input 1 (cursed), one with a
    pointer to output 1 (cursed), one from a worthless input (unbound), and one past the outputs (to the fee: lands in
    the coinbase, numbered last); block 2: a reinscription on the plain one's coin; then empty blocks."""
    chain = Chain()
    fund = _tx_to([(FUND, 0, SIG)], [(100_000, P2PKH)] * 8 + [(0, P2PKH)])
    chain.add([fund])                                                             # block 0
    f = fund[1]
    plain = _wtx([(f, 0, b"", _tap(_plain(b"one")))], [(100_000, P2TR)])
    pair = _wtx([(f, 1, b"", _tap(_plain(b"a"), _plain(b"b")))], [(100_000, P2TR)])
    second_in = _wtx([(f, 2, b"", []), (f, 3, b"", _tap(_plain(b"in1")))], [(150_000, P2TR), (50_000, P2PKH)])
    pointed = _wtx([(f, 4, b"", _tap([b"\x02", (60_000).to_bytes(3, "little")] + _plain(b"p")))],
                   [(50_000, P2PKH), (50_000, P2TR)])
    unbound = _wtx([(f, 8, b"", _tap(_plain(b"u"))), (f, 5, b"", [])], [(100_000, P2TR)])
    to_fee = _wtx([(f, 6, b"", []), (f, 7, b"", _tap(_plain(b"fee")))], [(50_000, P2PKH)])
    chain.add([plain, pair, second_in, pointed, unbound, to_fee])                 # block 1
    again = _wtx([(plain[1], 0, b"", _tap(_plain(b"two")))], [(100_000, P2TR)])
    chain.add([again])                                                            # block 2
    for _ in range(5):
        chain.add([])                                                             # blocks 3-7
    ids = {k: v[1] for k, v in dict(plain=plain, pair=pair, second_in=second_in, pointed=pointed, unbound=unbound,
                                       to_fee=to_fee, again=again).items()}
    return chain, ids


def test_numbered_where_they_land_cursed_ones_below_zero(tmp_path, lite):
    chain, t = _story()
    index = _walk(tmp_path, chain)
    got = _numbers(index)
    assert got[f"{t['plain']}i0"] == 0 and got[f"{t['pair']}i0"] == 1
    assert got[f"{t['pair']}i1"] == -1, "the second envelope in an input: NotAtOffsetZero"
    assert got[f"{t['second_in']}i0"] == -2, "in input 1: NotInFirstInput"
    assert got[f"{t['pointed']}i0"] == -3, "a pointer: cursed"
    assert got[f"{t['unbound']}i0"] == 2, "unbound, not cursed: blessed"
    assert got[f"{t['to_fee']}i0"] == -4, "in input 1 again, and numbered last: it lands in the coinbase"
    assert got[f"{t['again']}i0"] == -5, "a reinscription on a blessed inscription's sat"
    assert index.last_number() == 2


def test_where_they_land_the_pointer_unbound_and_the_coinbase(tmp_path, lite):
    chain, t = _story()
    index = _walk(tmp_path, chain)
    assert index.where(f"{t['pointed']}i0")["output"] == f"{t['pointed']}:1", "the pointer, 60,000 in: output 1"
    assert index.where(f"{t['second_in']}i0")["output"] == f"{t['second_in']}:0", "input 1 starts at 100,000"
    assert index.where(f"{t['unbound']}i0")["output"] is None, "unbound: no place"
    coinbase = chain.blocks[1][1]
    coinbase_txid = dx.rawchain.read_block(coinbase, chain.blocks[1][0]).txs[0].txid
    assert index.where(f"{t['to_fee']}i0")["output"] == f"{coinbase_txid}:0", "the fee's sat is the miner's"
    charms = index.inscription(-1)["inscription"]["charms"]
    assert charms == ["cursed", "reinscription"], "the second on the same sat as the first: a reinscription too"
    assert index.inscription(-2)["inscription"]["charms"] == ["cursed"]
    assert index.inscription(2)["inscription"]["charms"] == ["unbound"]
    assert "reinscription" in index.inscription(-5)["inscription"]["charms"]


def test_after_the_jubilee_cursed_ones_are_vindicated_and_numbered_on(tmp_path, monkeypatch):
    monkeypatch.setitem(dx.STARTS, "ltc", dx.BASE)
    monkeypatch.setitem(ord_rules.ORD_LITE, "ltc", dx.BASE)                       # jubilant from the start
    chain, t = _story()
    index = _walk(tmp_path, chain)
    got = _numbers(index)
    assert all(n >= 0 for n in got.values()) and sorted(got.values()) == list(range(8))
    assert got[f"{t['to_fee']}i0"] == 6, "numbered last in its block"
    assert index.inscription(got[f"{t['pair']}i1"])["inscription"]["charms"] == ["reinscription", "vindicated"]


def test_a_reinscription_on_a_cursed_ones_sat_is_not_cursed(tmp_path, lite):
    chain = Chain()
    fund = _tx_to([(FUND, 0, SIG)], [(100_000, P2PKH)] * 2)
    chain.add([fund])
    first = _wtx([(fund[1], 0, b"", _tap([b"\x02", b"\x00"] + _plain(b"x")))], [(100_000, P2TR)])  # pointer 0: cursed
    chain.add([first])
    again = _wtx([(first[1], 0, b"", _tap(_plain(b"y")))], [(100_000, P2TR)])
    chain.add([again])
    for _ in range(5):
        chain.add([])
    got = _numbers(_walk(tmp_path, chain))
    assert got == {f"{first[1]}i0": -1, f"{again[1]}i0": 0}


def test_an_index_stitched_from_notes_is_the_one_walked(tmp_path, lite):
    chain, _t = _story()
    walked = _walk(tmp_path, chain)
    kept = {c: walked.store.notes_of(c) for c in range(2)}
    assert all(k and k[2] == "" for k in kept.values())
    stitched = dx.DoginalIndex(tmp_path / "stitch.sqlite", lambda: chain, lambda: "walk", net="ltc")
    stitched.walker.step(chain, dx.BASE + 7, budget=60,
                         notes=lambda c: (ord_notes.decode(ord_notes.unpack(kept[c][0])), False) if c in kept else False)
    assert _dump(stitched) == _dump(walked)
    assert stitched.store.q("SELECT * FROM charm ORDER BY rid") == walked.store.q("SELECT * FROM charm ORDER BY rid")


def test_the_chunk_hash_counts_every_envelope_and_no_doginals_kind(lite):
    chain, t = _story()
    block = dx.rawchain.read_block(chain.blocks[1][1], chain.blocks[1][0])
    found = dx.births(block, True, "dogecoin", ord_lite=True)
    assert len(found) == 7 and (t["pair"], 1, 0, b"text/plain".hex()) in found
    assert dx.ord_rules.subsidy(0) == 50 * 10 ** 8 and dx.ord_rules.subsidy(840_000) == 25 * 10 ** 8
    assert dx.ord_rules.subsidy(2_608_704) == 625_000_000
