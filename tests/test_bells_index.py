"""Bellscoin's ord index read as Nintondo reads it (2026-10-25, SadFrog: "index bel-20 tokens, keep Taproot births in
input 0, read envelopes in every input like Nintondo, and show bel1q/bel1p owners").

Checked against nintondo.io the same day: a NOOK mint in input 1 of 6d110444...e809 (block 41,937) is #14,234,881 there
and was missing here, 344 more in 52e3fa95...56f0 (block 42,881): every number after them was 345 short. Bells' tokens
are "p":"bel-20" (the index read "drc-20", so it held none of its 415 ticks). Taproot envelopes in input 0 -- every
real one sampled -- were left out of the walk (only the chunk hash counted them). SegWit and Taproot holders were named
"script:<hash>". The chain here is made up, byte for byte, as in test_doginal_index."""
import hashlib

import pytest

from test_doginal_index import (FUND, SIG, Chain, _coinbase, _dump, _envelope, _json, _push, _tx_to,   # noqa: F401
                                _varint, small)

from arcade import doginal_index as dx, ord_notes, ord_rules, rawchain
from arcade.config import NETWORKS
from arcade.script import b58check_encode, segwit_encode

BEL = NETWORKS["bel"]


def _h(tag: bytes) -> bytes:
    return hashlib.sha256(tag).digest()


P2PKH_A = b"\x76\xa9\x14" + _h(b"a")[:20] + b"\x88\xac"
P2WPKH_B = b"\x00\x14" + _h(b"b")[:20]
P2TR_C = b"\x51\x20" + _h(b"c")
P2WSH_D = b"\x00\x20" + _h(b"d")
A = b58check_encode(BEL.pubkeyhash_version, _h(b"a")[:20])
B = segwit_encode("bel", 0, _h(b"b")[:20])
C = segwit_encode("bel", 1, _h(b"c"))
D = segwit_encode("bel", 0, _h(b"d"))


def _wtx(vin, outs):
    """A segwit transaction: vin [(txid, vout, script, witness items)], outs [(koinu, script)]."""
    body = _varint(len(vin))
    for txid, n, script, _w in vin:
        body += bytes.fromhex(txid)[::-1] + n.to_bytes(4, "little") + _varint(len(script)) + script + b"\xff\xff\xff\xff"
    body += _varint(len(outs))
    for v, spk in outs:
        body += v.to_bytes(8, "little") + _varint(len(spk)) + spk
    wit = b"".join(_varint(len(w)) + b"".join(_varint(len(x)) + x for x in w) for _t, _n, _s, w in vin)
    raw = (2).to_bytes(4, "little") + b"\x00\x01" + body + wit + (0).to_bytes(4, "little")
    return raw, rawchain.read_tx(raw).txid


def _tapscript(ctype: bytes, body: bytes) -> list[bytes]:
    script = (_push(b"\x02" * 32) + b"\xac" + b"\x00\x63" + _push(b"ord") + _push(b"\x01") + _push(ctype)
              + b"\x00" + _push(body) + b"\x68")
    return [b"\x30" * 64, script, b"\xc0" + b"\x11" * 32]


def _mint(amt: str) -> bytes:
    return _json({"p": "bel-20", "op": "mint", "tick": "nook", "amt": amt})


def _bells_chain():
    """Block 0 funds six coins; block 1 deploys nook and mints it twice in one transaction (inputs 0 and 1, the second
    landing on a bel1q output); block 2 mints it from a Taproot envelope in input 0 to a bel1p output, and sends a coin
    to a P2WSH; then empty blocks to fill two chunks."""
    chain = Chain()
    fund = _tx_to([(FUND, 0, SIG)], [(100_000, P2PKH_A)] * 6)
    chain.add([fund])                                                            # block 0
    deploy = _tx_to([(fund[1], 0, _envelope(b"text/plain", parts=((0, _json(
        {"p": "bel-20", "op": "deploy", "tick": "nook", "max": "1000", "lim": "100"})),)))], [(100_000, P2PKH_A)])
    two = _tx_to([(fund[1], 1, _envelope(b"text/plain", parts=((0, _mint("100")),))),
                  (fund[1], 2, _envelope(b"text/plain", parts=((0, _mint("60")),)))],
                 [(100_000, P2PKH_A), (100_000, P2WPKH_B)])
    chain.add([deploy, two])                                                     # block 1
    tap = _wtx([(fund[1], 3, b"", _tapscript(b"text/plain", _mint("30")))], [(100_000, P2TR_C)])
    wsh = _tx_to([(fund[1], 4, SIG)], [(100_000, P2WSH_D)])
    chain.add([tap, wsh])                                                        # block 2
    for _ in range(5):
        chain.add([])                                                            # blocks 3-7
    return chain, {"deploy": deploy[1], "two": two[1], "tap": tap[1], "wsh": wsh[1]}


@pytest.fixture
def bells(monkeypatch):
    monkeypatch.setitem(dx.STARTS, "bel", dx.BASE)


def _walked(tmp_path, chain, name="walk"):
    index = dx.DoginalIndex(tmp_path / f"{name}.sqlite", lambda: chain, lambda: "walk", net="bel")
    index.walker.step(chain, dx.BASE + len(chain.blocks) - 1, budget=60)
    return index


# --------------------------------------------------------------------------- what is born

def test_bells_reads_an_envelope_in_every_input_and_the_others_input_0_only(bells):
    chain, ids = _bells_chain()
    two = rawchain.read_tx(chain.txs[ids["two"]])
    bel = [(b["n"], b["vin"], b["kind"]) for b in dx.births_of(two, True, "dogecoin", every_input=True)]
    assert bel == [(0, 0, "p2sh"), (1, 1, "p2sh")], "i0 and i1, as Nintondo numbers them"
    assert [(b["n"], b["vin"]) for b in dx.births_of(two, False, "dogecoin")] == [(0, 0)], "Dogecoin: input 0 only"
    assert dx.births_of(two, True, "dogecoin")[0]["kind"] == "p2sh"
    tap = rawchain.read_tx(chain.txs[ids["tap"]])
    assert [(b["n"], b["vin"], b["kind"]) for b in dx.births_of(tap, True, "dogecoin", every_input=True)] == \
        [(0, 0, "taproot")]
    assert ord_rules.every_input("bel") and not ord_rules.every_input("doge") and not ord_rules.every_input("ltc")


def test_a_later_inputs_envelope_counts_only_when_complete_in_its_transaction(bells):
    chain, _ids = _bells_chain()
    fund = next(t for t in chain.txs if t != FUND)
    half = _envelope(b"image/png", parts=((1, b"P" * 50),), total=2)
    tx = rawchain.read_tx(_tx_to([(fund, 0, SIG), (fund, 1, half)], [(100_000, P2PKH_A)])[0])
    assert dx.births_of(tx, True, "dogecoin", every_input=True) == [], "a first piece of two, in input 1: none"


# --------------------------------------------------------------------------- the walk

def test_the_walk_numbers_each_input_and_the_taproot_one_like_nintondo(tmp_path, bells):
    chain, ids = _bells_chain()
    index = _walked(tmp_path, chain)
    got = {index.inscription(n)["inscription"]["id"]: n for n in range(index.last_number() + 1)}
    assert got == {f"{ids['deploy']}i0": 0, f"{ids['two']}i0": 1, f"{ids['two']}i1": 2, f"{ids['tap']}i0": 3}
    tap = index.inscription(3)["inscription"]
    assert tap["content_type"] == "text/plain" and tap["size"] == len(_mint("30")) and tap["protocol"] == "bel-20"
    assert tap["output"] == f"{ids['tap']}:0"
    assert index.inscription(2)["inscription"]["output"] == f"{ids['two']}:1", "input 1's first koinu: output 1"


def test_bel_20_is_indexed_from_every_kind_of_birth(tmp_path, bells):
    chain, _ids = _bells_chain()
    index = _walked(tmp_path, chain)
    nook = index.token("nook")
    assert nook is not None and nook["minted"] == "190" and nook["holders"] == 3
    balances = {o: b for o, t, b, _x in index.store.q("SELECT * FROM bal")}
    unit = 10 ** 18
    assert balances == {A: str(100 * unit), B: str(60 * unit), C: str(30 * unit)}, "named by their addresses"
    assert index.walker.token_protocol == "bel-20"


def test_segwit_and_taproot_holders_are_named_by_their_addresses(tmp_path, bells):
    chain, _ids = _bells_chain()
    index = _walked(tmp_path, chain)
    w = index.walker
    assert (w.owner_of(P2PKH_A), w.owner_of(P2WPKH_B), w.owner_of(P2TR_C), w.owner_of(P2WSH_D)) == (A, B, C, D)
    assert B.startswith("bel1q") and C.startswith("bel1p")
    assert index.inscription(3)["inscription"]["owner"] == C
    assert w.owner_of(b"\x51") == "script:" + hashlib.new("ripemd160", hashlib.sha256(b"\x51").digest()).hexdigest()
    ltc = dx.Walker(index.store, True, False, params=NETWORKS["ltc"])
    assert ltc.owner_of(P2WPKH_B).startswith("ltc1q") and ltc.owner_of(P2TR_C).startswith("ltc1p")
    doge = dx.Walker(index.store, False, False, params=NETWORKS["doge"])
    assert doge.owner_of(P2WPKH_B).startswith("script:"), "no SegWit on Dogecoin: as before"


def test_an_index_stitched_from_bells_notes_is_the_one_walked(tmp_path, bells):
    """The notes of a Taproot chain carry SegWit owners and later inputs' envelopes (format 3), so a walk stitched
    only from them ends the same as one that read every block, holders and tokens included."""
    chain, _ids = _bells_chain()
    walked = _walked(tmp_path, chain)
    kept = {c: walked.store.notes_of(c) for c in range(2)}
    assert all(k and k[2] == "" for k in kept.values())
    assert ord_notes.unpack(kept[0][0])[0] == 3, "format 3 on a Taproot chain"
    stitched = dx.DoginalIndex(tmp_path / "stitch.sqlite", lambda: chain, lambda: "walk", net="bel")
    got = stitched.walker.step(chain, dx.BASE + 7, budget=60,
                               notes=lambda c: (ord_notes.decode(ord_notes.unpack(kept[c][0])), False)
                               if c in kept else False)
    assert got == 8
    assert _dump(stitched) == _dump(walked)


def test_other_chains_notes_stay_format_2(tmp_path):
    from test_doginal_index import _story_chain
    chain = _story_chain()
    index = dx.DoginalIndex(tmp_path / "doge.sqlite", lambda: chain, lambda: "walk")
    index.walker.step(chain, dx.BASE + 11, budget=60)
    assert ord_notes.unpack(index.store.notes_of(0)[0])[0] == 2


def test_bells_and_litecoin_indexes_are_cut_again_for_the_new_reading():
    assert ord_rules.rules_for("bel") == 7
    assert ord_rules.rules_for("ltc") == ord_rules.rules_for("ltctest") == 5, "their holders are named anew"
    assert ord_rules.rules_for("doge") == 5 and ord_rules.rules_for("main") == ord_rules.RULES == 4, "the rest as before"
