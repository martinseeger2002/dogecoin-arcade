"""DigiAssets on DigiByte are read by the inscription guard (2026-11-09, security review): an output a DigiAsset
transaction pays an asset to is kept out of ordinary sends, module spends and Gather, on dgb and dgbtest.

The transactions here are built byte for byte from DigiAsset Core's encoding (DigiAsset::decodeAssetTxHeader,
DigiByteTransaction::decodeAssetTransfer, BitIO::getFixedPrecision): an OP_RETURN pushing "DA", a version, an opcode,
then the body."""
import hashlib
import pathlib
import sqlite3
import struct
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import digiassets as DA, doginals, funding, modhost  # noqa: E402
from arcade.config import NETWORKS                               # noqa: E402

A, B, C = (bytes.fromhex("76a914" + h * 20 + "88ac") for h in ("11", "22", "33"))
CHANGE = bytes.fromhex("76a914" + "44" * 20 + "88ac")
SHA256 = hashlib.sha256(b"metadata").digest()


def _number(n: int) -> bytes:
    """A fixed-precision number as DigiAsset Core writes it: 1 byte under 32, else 2 to 4 bytes of a mantissa and a
    power of ten (3 bits of length, then the mantissa, then 4 bits of exponent)."""
    if n < 32:
        return bytes([n])
    exponent = 0
    while n % 10 == 0 and exponent < 15:
        n, exponent = n // 10, exponent + 1
    for size in (2, 3, 4):
        if n < 1 << (size * 8 - 7):
            word = ((size - 1) << (size * 8 - 3)) | (n << 4) | exponent
            return word.to_bytes(size, "big")
    raise ValueError(n)


def _pay(output: int, amount: int, skip=False, ranged=False, percent=False) -> bytes:
    """One transfer instruction: skip, range, percent, then the output (13 bits with range, else 5), then the amount."""
    flags = (skip << 2) | (ranged << 1) | percent
    head = ((flags << 13) | output).to_bytes(2, "big") if ranged else bytes([(flags << 5) | output])
    return head + (bytes([amount]) if percent else _number(amount))


def _op_return(data: bytes) -> bytes:
    return bytes([0x6A, len(data)]) + data if len(data) <= 75 else bytes([0x6A, 0x4C, len(data)]) + data


def _issuance(*pays: bytes, version=3, opcode=1, amount=100, flags=0b000_0_00_00) -> bytes:
    body = (b"\x00" * 20 if version < 3 and opcode < 3 else b"") + (SHA256 if opcode in (1, 3, 4) else b"")
    return _op_return(b"DA" + bytes([version, opcode]) + body + _number(amount) + b"".join(pays) + bytes([flags]))


def _transfer(*pays: bytes, version=3, opcode=DA.TRANSFER) -> bytes:
    return _op_return(b"DA" + bytes([version, opcode]) + b"".join(pays))


def test_fixed_precision_numbers_read_as_digiasset_core_writes_them():
    # 1000 is 1 x 10^3 in two bytes: length 001, mantissa 000000001, exponent 0011.
    assert _number(1000) == bytes.fromhex("2013") and _number(7) == b"\x07"
    for n in (0, 7, 31, 32, 100, 1000, 511, 12_345, 9_999_999, 10 ** 12):
        bits = DA._Bits(_number(n))
        assert bits.number() == n and bits.left() == 0, n
    # Seven bytes: the length is "11" alone, then a 54-bit mantissa (BitIO::getFixedPrecision).
    seven = ((0b11 << 54) | 123_456_789_012).to_bytes(7, "big")
    bits = DA._Bits(seven)
    assert bits.number() == 123_456_789_012 and bits.left() == 0


def test_an_issuance_marks_the_outputs_it_pays_and_the_last_for_whats_left():
    # 100 issued: 40 to output 1, the 60 left over to the last output (the change). Output 0 is DGB only.
    scripts = [A, B, _issuance(_pay(1, 40)), CHANGE]
    assert DA.asset_outputs(scripts) == {1: "issuance", 3: "issuance"}
    # Version 2, opcode 1: a 20-byte hash comes first; opcode 5 has no metadata hash at all.
    assert DA.asset_outputs([A, _issuance(_pay(0, 1000), version=2), B]) == {0: "issuance", 2: "issuance"}
    assert DA.asset_outputs([A, _issuance(_pay(0, 3), opcode=5, amount=3), B]) == {0: "issuance", 2: "issuance"}
    # The DigiDollar record and an ordinary OP_RETURN are not DigiAssets.
    assert DA.asset_outputs([A, bytes([0x6A, 2]) + b"DD", B]) == {}
    assert DA.asset_outputs([A, _op_return(b"hello"), B]) == {} and DA.asset_outputs([A, B]) == {}


def test_a_transfer_marks_named_ranged_and_leftover_outputs_and_a_burn_marks_none_it_burns():
    scripts = [A, B, C, _transfer(_pay(0, 5, skip=True), _pay(2, 128, percent=True)), CHANGE]
    assert DA.asset_outputs(scripts) == {0: "transfer", 2: "transfer", 4: "transfer"}, "output 1 is DGB only"
    ranged = [A, B, C, CHANGE, _transfer(_pay(2, 7, ranged=True), version=2)]
    assert DA.asset_outputs(ranged) == {0: "transfer", 1: "transfer", 2: "transfer"}, \
        "a range pays outputs 0 to 2; the last output is the OP_RETURN, which holds nothing"
    burn = [A, _transfer(_pay(31, 10), opcode=DA.BURN), CHANGE]
    assert DA.asset_outputs(burn) == {2: "burn"}, "output 31 without range burns: only the leftover is paid"
    # An instruction naming an output past the end pays nothing there (DigiAsset Core refuses it; all goes last).
    assert DA.asset_outputs([A, _transfer(_pay(9, 1)), B]) == {2: "transfer"}


@pytest.mark.parametrize("scripts", [
    [A, _transfer(_pay(0, 5) + b"\x00\x20"), B],                     # a 2-byte amount cut after its first byte
    [A, _transfer(_pay(0, 5), version=9), B],                        # an unknown version
    [A, _transfer(_pay(0, 5), opcode=0x10), B],                      # a transfer opcode not read here
    [A, _issuance(_pay(0, 5), opcode=3), B],                         # an issuance with rules
    [A, _op_return(b"DA\x03\x01" + SHA256[:10]), B],                 # an issuance cut inside its hash
    [A, _transfer(_pay(0, 5)), _op_return(b"more"), B],               # two OP_RETURNs
    [A, bytes([0x6A, 0x30]) + b"DA\x03\x15", B],                     # a push longer than the script
])
def test_a_digiasset_transaction_not_read_exactly_marks_every_output_it_made(scripts):
    assert DA.asset_outputs(scripts) == {0: "unreadable", len(scripts) - 1: "unreadable"}


def test_the_reader_reads_the_nodes_decoded_transaction():
    tx = {"vout": [{"n": 1, "value": 0, "scriptPubKey": {"hex": _transfer(_pay(0, 1)).hex()}},
                   {"n": 0, "value": 0.00000600, "scriptPubKey": {"hex": A.hex()}},
                   {"n": 2, "value": 5.0, "scriptPubKey": {"hex": CHANGE.hex()}}]}
    assert DA.asset_outputs_of(tx) == {0: "transfer", 2: "transfer"}


# --- the guard ------------------------------------------------------------------------------------------------------

DA_TX, PLAIN_TX, BAD_TX = "da" * 32, "ab" * 32, "bd" * 32


class _Node:
    """A DigiByte node's getrawtransaction: a transfer paying output 0 (600 satoshis) with 3 DGB change last, a plain
    payment, and a DigiAsset transaction cut short."""

    def __init__(self):
        def out(n, value, script):
            return {"n": n, "value": value, "scriptPubKey": {"hex": script.hex()}}
        self.txs = {
            DA_TX: {"txid": DA_TX, "vout": [out(0, 0.000006, A), out(1, 0, _transfer(_pay(0, 1))), out(2, 2.0, B),
                                            out(3, 3.0, CHANGE)]},
            PLAIN_TX: {"txid": PLAIN_TX, "vout": [out(0, 0.000006, A), out(1, 2.0, CHANGE)]},
            BAD_TX: {"txid": BAD_TX, "vout": [out(0, 0.000006, A), out(1, 0, _transfer(_pay(0, 5) + b"\x00\x20")),
                                              out(2, 2.0, CHANGE)]}}
        self.asked = []

    def call(self, method, txid, *_):
        assert method == "getrawtransaction"
        self.asked.append(txid)
        return self.txs[txid]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _coins():
    return [{"txid": t, "vout": n, "value": v} for t, n, v in (
        (DA_TX, 0, 600), (DA_TX, 2, 2 * 10 ** 8), (DA_TX, 3, 3 * 10 ** 8), (PLAIN_TX, 0, 600),
        (PLAIN_TX, 1, 2 * 10 ** 8), (BAD_TX, 0, 600), (BAD_TX, 2, 2 * 10 ** 8))]


def test_the_guard_keeps_coins_a_digiasset_may_sit_on_out_of_a_send_whatever_their_value(tmp_path):
    node = _Node()
    guard = doginals.Doginals(tmp_path / "dgbtest.sqlite", rpc_factory=lambda: node, digiassets=True, ords=False)
    free = [(c["txid"], c["vout"]) for c in guard.spendable(_coins())]
    assert free == [(DA_TX, 2), (PLAIN_TX, 0), (PLAIN_TX, 1)], \
        "the asset's output and the 3 DGB change it may hold stay; a cut-short DigiAsset transaction keeps all"
    assert guard.held(DA_TX)[3] == [{"id": None, "offset": None, "digiasset": "transfer"}]
    free_, held, unchecked = guard.sort_known(_coins())
    assert len(held) == 4 and not unchecked, "a DigiAsset is shown as held"
    with pytest.raises(doginals.Unknown):
        guard.content(DA_TX + "i0")                         # it reads no inscriptions there
    node.asked.clear()
    guard.spendable(_coins())
    assert node.asked == [], "read once: an answer is kept"


def test_an_answer_kept_before_the_guard_read_digiassets_is_not_taken_as_one_that_did(tmp_path):
    with sqlite3.connect(tmp_path / "dgb.sqlite") as db:
        db.execute("CREATE TABLE tx2 (txid TEXT PRIMARY KEY, held TEXT NOT NULL, at REAL NOT NULL)")
        db.execute("INSERT INTO tx2 VALUES (?, '{}', 0)", (DA_TX,))
    guard = doginals.Doginals(tmp_path / "dgb.sqlite", rpc_factory=_Node, digiassets=True, ords=False)
    assert 0 in guard.held(DA_TX), "read again for DigiAssets"
    plain = doginals.Doginals(tmp_path / "dgb.sqlite", rpc_factory=_Node)
    assert plain.held(DA_TX) == {}, "a guard that reads no DigiAssets keeps its own answers"


def test_a_broadcast_that_may_carry_a_digiasset_is_never_vouched_clean(tmp_path):
    guard = doginals.Doginals(tmp_path / "dgb.sqlite", rpc_factory=_Node, digiassets=True, ords=False)
    guard.held(PLAIN_TX)                                   # its coins are known clean

    def raw(*scripts):
        tx = struct.pack("<I", 2) + b"\x01" + bytes.fromhex(PLAIN_TX)[::-1] + struct.pack("<I", 1) + b"\x00" \
            + struct.pack("<I", 0xFFFFFFFF) + bytes([len(scripts)])
        for s in scripts:
            tx += struct.pack("<q", 1000) + bytes([len(s)]) + s
        return (tx + struct.pack("<I", 0)).hex()
    assert not guard.vouch_broadcast(raw(A, _transfer(_pay(0, 1))), "e1" * 32)
    assert guard.kept("e1" * 32) is None
    assert guard.vouch_broadcast(raw(A, CHANGE), "e2" * 32) and guard.kept("e2" * 32) == {}
    assert DA.maybe_in_raw(bytes.fromhex(raw(A, _issuance(_pay(0, 1))))) and not DA.maybe_in_raw(bytes.fromhex(raw(A)))


def test_a_modules_coin_read_says_when_digiassets_were_read(tmp_path, monkeypatch):
    node = _Node()
    guard = doginals.Doginals(tmp_path / "g.sqlite", rpc_factory=lambda: node, digiassets=True, ords=False)
    said = {"success": True, "height": 900, "unspents": [
        {"txid": t, "vout": n, "amount": v / 1e8, "scriptPubKey": "76a9"} for t, n, v in
        ((DA_TX, 0, 600), (PLAIN_TX, 0, 600), (PLAIN_TX, 1, 2 * 10 ** 8))]}
    monkeypatch.setattr(funding, "GUARD_BY_NET", {"dgbtest": guard})
    kept = modhost.without_inscriptions("dgbtest", "scantxoutset", said)
    assert [(u["txid"], u["vout"]) for u in kept["unspents"]] == [(PLAIN_TX, 0), (PLAIN_TX, 1)]
    assert kept["digiassets"] == "read"
    monkeypatch.setattr(funding, "GUARD_BY_NET", {"dgbtest": doginals.Doginals(tmp_path / "o.sqlite",
                                                                               fetch=lambda t: {"txid": t})})
    assert "digiassets" not in modhost.without_inscriptions("dgbtest", "scantxoutset", said)


def test_digibyte_and_its_testnet_are_guarded_for_digiassets(app_state, monkeypatch):
    from arcade.web.state import ChainContext
    monkeypatch.setattr(funding, "GUARD", None)
    monkeypatch.setattr(funding, "GUARDS", [])
    monkeypatch.setattr(funding, "GUARD_BY_NET", {})
    more = {n: ChainContext(network=n, role="ledger", label=NETWORKS[n].label,
                            conf=pathlib.Path(f"/nonexistent/{NETWORKS[n].node_conf}")) for n in ("dgb", "dgbtest")}
    monkeypatch.setattr(app_state, "more", more, raising=False)
    TestClient(__import__("arcade.web.app", fromlist=["create_app"]).create_app(app_state))
    main, test = funding.guard_on("dgb"), funding.guard_on("dgbtest")
    assert main.digiassets and main.ords, "mainnet: its ord index and DigiAssets"
    assert test.digiassets and not test.ords, "the testnet has no ord index: DigiAssets alone"
    from arcade.script import b58check_encode
    assert funding.guard_for(b58check_encode(126, b"\x01" * 20)) is test
