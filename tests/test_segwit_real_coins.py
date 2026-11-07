"""A choice of address type on real coins (2026-11-07, SadFrog: "users should also be able to have the most updated or
choice of address type as opposed to only legacy"): Legacy, P2SH-nested and native SegWit on real DigiByte and Litecoin,
Legacy and native SegWit on Bellscoin (its P2SH byte is Dogecoin's "D"), the same menu in Lite's Send and Receive."""
import json
import pathlib
import re

import pytest

from arcade import script, wallet_imports
from arcade.config import NETWORKS, unpayable_versions

ROOT = pathlib.Path(__file__).resolve().parent.parent
WALLET_JS = (ROOT / "arcade" / "web" / "templates" / "wallet_js.js").read_text()
KEY = bytes.fromhex("0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")


def _kinds():
    """ADDRESS_KINDS as data: each chain's rows, read from wallet_js.js."""
    block = re.search(r"export const ADDRESS_KINDS = \{(.*?)\n\};", WALLET_JS, re.S).group(1)
    out = {}
    for name, rows in re.findall(r"\n  (\w+): \[(.*?)\n  \],", block, re.S):
        out[name] = [json.loads(re.sub(r"(\w+):", r'"\1":', row)) for row in re.findall(r"\{[^}]*\}", rows)]
    return out


def test_real_digibyte_litecoin_and_bellscoin_offer_segwit_with_legacy_first():
    kinds = _kinds()
    for network in ("ltc", "dgb", "dgbtest", "bel", "ltctest"):
        assert kinds[network][0]["id"] == "legacy", f"{network}: Legacy stays the default"
        assert any(k["id"] == "p2wpkh" for k in kinds[network]), network
    assert [k["id"] for k in kinds["bel"]] == ["legacy", "p2wpkh"], "no nested kind where P2SH reads as a D address"


def test_each_kind_matches_its_chain():
    for network, rows in _kinds().items():
        params = NETWORKS[network]
        coin = 1 if not params.real_coins else 3
        for k in rows:
            purpose = {"legacy": 44, "p2sh-p2wpkh": 49, "p2wpkh": 84}[k["id"]]
            assert k["path"] == f"m/{purpose}'/{coin}'/0'/0/0", (network, k)
            if k["id"] == "p2wpkh":
                assert k["hrp"] == params.bech32_hrp, network
            if k["id"] == "p2sh-p2wpkh":
                assert k["scripthash"] == params.scripthash_version, network
                assert k["scripthash"] not in unpayable_versions(params), network


def test_imported_segwit_wallets_on_real_coins():
    kh = script.hash160(KEY)
    assert wallet_imports.address_of(NETWORKS["dgb"], KEY, "p2wpkh") == script.p2wpkh_address(NETWORKS["dgb"], kh)
    assert wallet_imports.address_of(NETWORKS["ltc"], KEY, "p2sh-p2wpkh").startswith("M")
    assert wallet_imports.address_of(NETWORKS["dgb"], KEY, "p2sh-p2wpkh").startswith("S")
    assert wallet_imports.address_of(NETWORKS["bel"], KEY, "p2wpkh").startswith("bel1q")
    with pytest.raises(wallet_imports.ImportRefused):
        wallet_imports.address_of(NETWORKS["bel"], KEY, "p2sh-p2wpkh")
    with pytest.raises(wallet_imports.ImportRefused):
        wallet_imports.address_of(NETWORKS["main"], KEY, "p2wpkh")        # Pepecoin has no SegWit


def test_no_route_keeps_segwit_to_test_chains():
    app = (ROOT / "arcade" / "web" / "app.py").read_text()
    assert "segwit is on test chains only" not in app


def test_lite_send_and_receive_have_the_address_type_menu():
    page = (ROOT / "arcade" / "lite" / "templates" / "_wallet.html").read_text()
    assert len(re.findall(r'id="w-kind-(?:send|receive)" data-kind', page)) == 2
    assert "wallet.addressKinds" in page and "/account/segwit/register" in page
    assert "offerSend(to, amount, c.network, from ? from.address" in page, "a send spends that kind's own coins"
