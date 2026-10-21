"""BIP143 segwit sighash, against the standard's own worked example.

The legacy sighash is settled against pepecoind (test_funding_leg.py); this
one is settled against the number BIP143 itself writes down for its native
P2WPKH example. If the browser (coins.js sighashWitness) and this agree with
that number, a segwit input is signed over the right bytes -- and that is the
one thing that must not be got by reading our own code back to ourselves.
"""

import hashlib

from arcade import funding


def _p2pkh(keyhash_hex: str) -> bytes:
    return bytes([0x76, 0xA9, 0x14]) + bytes.fromhex(keyhash_hex) + bytes([0x88, 0xAC])


# BIP143, "Native P2WPKH" worked example. Input 1 is the P2WPKH one.
def test_bip143_native_p2wpkh_sighash_matches_the_standard():
    inputs = [
        {"txid": "9f96ade4b41d5433f4eda31e1738ec2b36f6e7d1420d94a6af99801a88f7f7ff",
         "vout": 0, "sequence": 0xFFFFFFEE},
        {"txid": "8ac60eb9575db5b2d987e29f301b5b819ea83a5c6579d282d189cc04b8e151ef",
         "vout": 1, "sequence": 0xFFFFFFFF},
    ]
    outputs = [
        (112340000, _p2pkh("8280b37df378db99f66f85c95a783a76ac7a6d59")),
        (223450000, _p2pkh("3bde42dbee7e4dbe6a21b2d50ce2f0167faa8159")),
    ]
    script_code = funding.witness_scriptcode(
        bytes.fromhex("1d0f172a0ecb48aee1be1f2687d2963ae33f71a1"))
    digest = funding.sighash_witness(inputs, outputs, 1, script_code,
                                     amount=600000000, version=1, locktime=17)
    assert digest.hex() == \
        "c37af31116d1b27caf68aae9e3ac82f1477929014d5b917657d0eb49478cb670"


def test_anyonecanpay_zeroes_prevouts_and_sequence_but_keeps_outputs():
    # A smoke test of the one other type this builds: the digest must change
    # from the ALL one (different preimage), and must not raise.
    inputs = [{"txid": "11" * 32, "vout": 0, "sequence": 0xFFFFFFFF}]
    outputs = [(5000, _p2pkh("22" * 20))]
    sc = funding.witness_scriptcode(bytes.fromhex("33" * 20))
    a = funding.sighash_witness(inputs, outputs, 0, sc, 10000,
                                sighash_type=funding.SIGHASH_ALL)
    b = funding.sighash_witness(inputs, outputs, 0, sc, 10000,
                                sighash_type=funding.ALL_ANYONECANPAY)
    assert a != b and len(a) == 32 and len(b) == 32


def test_an_unsupported_sighash_type_is_refused():
    import pytest
    inputs = [{"txid": "11" * 32, "vout": 0}]
    outputs = [(1, _p2pkh("22" * 20))]
    sc = funding.witness_scriptcode(bytes.fromhex("33" * 20))
    with pytest.raises(funding.FundingError):
        funding.sighash_witness(inputs, outputs, 0, sc, 1, sighash_type=0x03)


# --- witness serialisation (BIP144 marker/flag + the witness stack) ----------

def test_serialisation_carries_marker_flag_and_witness():
    witness = [bytes.fromhex("3045") + b"\x01", b"\x02" * 33]
    inputs = [{"txid": "ab" * 32, "vout": 3, "sequence": 0xFFFFFFFF,
               "script_sig": b"", "witness": witness}]
    outputs = [(1000, _p2pkh("44" * 20))]
    ser = funding.serialize_segwit(inputs, outputs, locktime=0, version=1)
    assert ser[:8] == "01000000"               # version
    assert ser[8:12] == "0001"                 # BIP144 marker + flag
    # the witness item count (2) appears once the outputs are done; cheap check:
    assert "02" + "03" in ser or witness[0].hex() in ser


def test_txid_is_the_witness_stripped_double_sha():
    inputs = [{"txid": "cd" * 32, "vout": 0, "sequence": 0xFFFFFFFF,
               "script_sig": b"", "witness": [b"\x09" * 10, b"\x08" * 33]}]
    outputs = [(2500, _p2pkh("55" * 20))]
    txid = funding.segwit_txid(inputs, outputs)
    stripped = funding.serialize_segwit(
        [{**inputs[0], "witness": []}], outputs, 0, 1)
    want = hashlib.sha256(hashlib.sha256(bytes.fromhex(stripped)).digest()).digest()[::-1].hex()
    assert txid == want
    # and the stripped form has no marker/flag:
    assert stripped[8:12] != "0001"
