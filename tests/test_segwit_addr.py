"""bech32 and segwit addresses, against the published vectors.

The point of deriving segwit addresses the Electrum way is that the coins show
up in Electrum-LTC and in Litecoin Core -- a scheme of our own would not. So,
like `test_seed.py`, this holds the encoder to numbers written down by somebody
else: the BIP173 bitcoin vectors (bc1/tb1) and BIP350's bech32m strings, which
fix the checksum and the 8-to-5-bit packing. The Litecoin prefixes ("ltc",
"tltc") are that same algorithm with a different human-readable part, so once
the bitcoin vectors pass the prefixes are right by construction; the final
"does litecoind accept it" check is a node-level one and lives with node 2's
regtest litecoind, as the LITECOIN Params note records.
"""

import pytest

from arcade import script
from arcade.config import LITECOIN, LITECOIN_TESTNET


# --- the checksum constant, anchored to BIP173 and BIP350 --------------------

def test_bech32_and_bech32m_constants_match_the_published_strings():
    # BIP173 valid bech32:
    hrp, data, const = script.bech32_decode("A12UEL5L")
    assert hrp == "a" and const == script.BECH32_CONST
    # BIP350 valid bech32m:
    hrp, data, const = script.bech32_decode("A1LQFN3A")
    assert hrp == "a" and const == script.BECH32M_CONST


def test_a_mistyped_checksum_is_refused():
    assert script.bech32_decode("A12UEL5M") == (None, None, None)       # last char wrong
    assert script.bech32_decode("a12uEL5L") == (None, None, None)       # mixed case
    assert script.bech32_decode("1nwldj5") == (None, None, None)        # empty hrp


# --- the segwit P2WPKH / P2WSH vectors from BIP173 ---------------------------

#: BIP173's worked example: witness v0, this 20-byte program, mainnet.
P2WPKH_PROGRAM = bytes.fromhex("751e76e8199196d454941c45d1b3a323f1433bd6")
BC1_P2WPKH = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"

#: BIP173's testnet P2WSH example: witness v0, this 32-byte program.
P2WSH_PROGRAM = bytes.fromhex(
    "1863143c14c5166804bd19203356da136c985678cd4d27a1b8c6329604903262")
TB1_P2WSH = "tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7"


def test_p2wpkh_encode_matches_bip173():
    assert script.segwit_encode("bc", 0, P2WPKH_PROGRAM) == BC1_P2WPKH


def test_p2wsh_decode_matches_bip173():
    witver, program = script.segwit_decode("tb", TB1_P2WSH)
    assert witver == 0
    assert program == P2WSH_PROGRAM


@pytest.mark.parametrize("hrp,program", [
    ("bc", P2WPKH_PROGRAM), ("tb", P2WSH_PROGRAM),
    ("ltc", P2WPKH_PROGRAM), ("tltc", P2WPKH_PROGRAM),
])
def test_segwit_round_trips(hrp, program):
    addr = script.segwit_encode(hrp, 0, program)
    assert addr.startswith(hrp + "1q")          # witver 0 -> the symbol 'q'
    assert script.segwit_decode(hrp, addr) == (0, program)


def test_the_two_specs_do_not_cross():
    # A v0 (bech32) address must not read under the bech32m constant, nor a
    # v1 (bech32m) address under bech32 -- that separation is why Taproot is
    # never read as P2WPKH.
    v0 = script.segwit_encode("tltc", 0, P2WPKH_PROGRAM)
    v1 = script.segwit_encode("tltc", 1, P2WPKH_PROGRAM)
    assert script.segwit_decode("tltc", v0) == (0, P2WPKH_PROGRAM)
    assert script.segwit_decode("tltc", v1) == (1, P2WPKH_PROGRAM)
    # swapping the body between them breaks the checksum:
    assert script.bech32_decode(v0)[2] == script.BECH32_CONST
    assert script.bech32_decode(v1)[2] == script.BECH32M_CONST


# --- the three address kinds for Litecoin and its testnet --------------------

KEYHASH = bytes.fromhex("751e76e8199196d454941c45d1b3a323f1433bd6")


def test_native_segwit_prefix_per_chain():
    assert script.p2wpkh_address(LITECOIN, KEYHASH).startswith("ltc1q")
    assert script.p2wpkh_address(LITECOIN_TESTNET, KEYHASH).startswith("tltc1q")


def test_native_segwit_decodes_back_to_the_key_hash():
    addr = script.p2wpkh_address(LITECOIN_TESTNET, KEYHASH)
    assert script.segwit_decode("tltc", addr) == (0, KEYHASH)


def test_p2wpkh_script_bytes():
    assert script.p2wpkh_script(KEYHASH) == bytes([0x00, 0x14]) + KEYHASH


def test_nested_p2sh_segwit_is_the_scripthash_of_the_witness_program():
    # The redeemScript is the witness program; the address is its P2SH, in the
    # chain's own script-hash version (Litecoin 50 -> "M", LTC testnet 58).
    redeem = script.p2sh_p2wpkh_redeem(KEYHASH)
    assert redeem == bytes([0x00, 0x14]) + KEYHASH
    for params in (LITECOIN, LITECOIN_TESTNET):
        addr = script.p2sh_p2wpkh_address(params, KEYHASH)
        version, payload = script.b58check_decode(addr)
        assert version == params.scripthash_version
        assert payload == script.hash160(redeem)


def test_litecoin_mainnet_nested_address_starts_with_M():
    # scripthash_version 50 encodes to a leading "M" on Litecoin mainnet.
    assert script.p2sh_p2wpkh_address(LITECOIN, KEYHASH)[0] == "M"
