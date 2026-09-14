"""Fees on the size the miner counts: sigops, for a multisig transaction."""

import pytest

from arcade import fees
from arcade.txbuild import build_raw_tx, multisig_script, op_return_script, p2pkh_script

ADDR = "mfWxJ45yp2SFn7UciZyNpvDKrzbhyfKrY8"
KEY = bytes.fromhex("02" + "11" * 32)


def test_sigops_are_counted_as_a_block_counts_them():
    assert fees.legacy_sigops(p2pkh_script(ADDR)) == 1
    assert fees.legacy_sigops(multisig_script([KEY, KEY, KEY], 1)) == 20, "twenty, keys or no keys"
    assert fees.legacy_sigops(multisig_script([KEY, KEY], 1)) == 20
    assert fees.legacy_sigops(op_return_script(b"x" * 70)) == 0
    # Pushed data is not code: a signature that happens to contain 0xac is a signature.
    assert fees.legacy_sigops(bytes([0x4C, 2, 0xAC, 0xAE])) == 0
    assert fees.legacy_sigops(bytes([0x4D, 2, 0, 0xAC, 0xAE, 0xAC])) == 1
    assert fees.legacy_sigops(bytes([0x4B]) + b"\xac" * 0x4B) == 0


def test_a_multisig_piece_weighs_its_sigops_not_its_bytes():
    outputs = [(1_000_000, multisig_script([KEY, KEY, KEY], 1)) for _ in range(98)]
    outputs += [(1_000_000, p2pkh_script(ADDR)), (5_000_000, p2pkh_script(ADDR))]
    raw = build_raw_tx([("aa" * 32, 0)], outputs)
    decoded = {"vin": [{"scriptSig": {"hex": ""}}],
               "vout": [{"scriptPubKey": {"hex": s.hex()}} for _, s in outputs]}
    assert fees.sigops_of(decoded) == 98 * 20 + 2 == 1962, "what a test machine saw in its template"
    size = len(raw) // 2 + fees.SCRIPTSIG_BYTES
    assert 11_000 < size < 12_000
    assert fees.virtual_size(size, 1962) == 39_240
    assert fees.fee_for(size, 1962) == 39_240_000, "0.3924 PEP, not 0.11"
    # A plain send is priced by its bytes, as before.
    assert fees.virtual_size(226, 2) == 226 and fees.fee_for(226, 2) == 226_000


class FakeNode:
    """Funds by raw size at 0.01/kB unless told a feeRate, like the wallet."""

    def __init__(self):
        self.calls = []

    def call(self, method, *args):
        self.calls.append((method, *args))
        if method == "fundrawtransaction":
            raw, opts = args[0], (args[1] if len(args) > 1 else {})
            rate = opts.get("feeRate", 0.01)
            signed = len(raw) // 2 + 107
            return {"hex": raw, "fee": round(rate * signed / 1000, 8), "changepos": 1}
        if method == "decoderawtransaction":
            raw = args[0]
            n_ms = raw.count(multisig_script([KEY, KEY, KEY], 1).hex())
            return {"vin": [{"scriptSig": {"hex": ""}}],
                    "vout": [{"scriptPubKey": {"hex": multisig_script([KEY, KEY, KEY], 1).hex()}}] * n_ms
                    + [{"scriptPubKey": {"hex": p2pkh_script(ADDR).hex()}}]}
        raise AssertionError(method)


def test_fund_pays_for_the_virtual_size_and_only_when_it_must():
    node = FakeNode()
    plain = build_raw_tx([("aa" * 32, 0)], [(5_000_000, p2pkh_script(ADDR))])
    funded = fees.fund(node, plain, {"changeAddress": ADDR})
    assert [c[0] for c in node.calls] == ["fundrawtransaction", "decoderawtransaction"]
    assert node.calls[0][2] == {"changeAddress": ADDR}, "no feeRate: the wallet's own price"
    assert funded["sigops"] == 1 and funded["vsize"] == len(plain) // 2 + 107

    node.calls.clear()
    piece = build_raw_tx([("aa" * 32, 0)],
                         [(1_000_000, multisig_script([KEY, KEY, KEY], 1))] * 98
                         + [(5_000_000, p2pkh_script(ADDR))])
    funded = fees.fund(node, piece, {"changeAddress": ADDR})
    refunds = [c for c in node.calls if c[0] == "fundrawtransaction"]
    assert len(refunds) == 2 and "feeRate" in refunds[1][2]
    paid = int(round(funded["fee"] * fees.COIN))
    assert funded["vsize"] == (98 * 20 + 1) * 20 == 39_220
    assert 39_220_000 <= paid <= 39_220_000 * 1.02, paid
    assert funded["changepos"] == 1, "what fundrawtransaction said comes through"
