"""DigiDollar addresses in the wallet (arcade/ddaddress.py, /lite/api/ddaddress): the same address the DigiDollar
module pays, an @name's read from the key that signed its claim and kept only when that key IS the name's address."""
import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).parent))
from test_web import app_state  # noqa: F401,E402
from test_auth_web import _sign_in  # noqa: E402

from arcade import ddaddress as ddlib  # noqa: E402
from arcade import taproot  # noqa: E402
from arcade.script import b58check_encode, hash160  # noqa: E402
from arcade.web.app import create_app  # noqa: E402

PILOT = Path(__file__).parent.parent / "examples" / "packages" / "digidollar"
KEY = bytes.fromhex("02" + "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")      # the generator G
OTHER = bytes.fromhex("03" + "c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5")    # 2G
OUTSIDE = {"host": "arcade.example.com", "cf-connecting-ip": "198.51.100.9"}


def _pilot():
    sys.path.insert(0, str(PILOT))
    try:
        return importlib.import_module("module")
    finally:
        sys.path.remove(str(PILOT))


def test_the_wallets_digidollar_address_is_the_one_the_module_pays():
    pilot = _pilot()
    for net in ("dgbtest", "dgb"):
        mine = ddlib.address(net, KEY)
        assert mine == pilot.dd_address(net, taproot.xonly(KEY)) and mine[:2] == ("TD" if net == "dgbtest" else "DD")
        assert ddlib.network_of(mine) == net and pilot.dd_key(net, mine) == taproot.tweak(taproot.xonly(KEY))[0]
    assert ddlib.network_of("ndjhPWKYKFcTCN7oyWBNaJMT9JTALmNFUs") is None and ddlib.network_of("@apple") is None


def test_a_key_is_read_from_a_transaction_only_when_it_is_the_names_own():
    tx = {"vin": [{"scriptSig": {"asm": "3044[ALL] " + OTHER.hex()}},
                  {"scriptSig": {"asm": "3045[ALL] " + KEY.hex()}},
                  {"txinwitness": ["00"]}]}
    assert ddlib.key_in(tx, hash160(KEY)) == KEY, "the input the name's address signed"
    assert ddlib.key_in(tx, hash160(b"someone else")) is None, "never another input's key"
    assert ddlib.key_in({"vin": [{"scriptSig": {"asm": "zz"}}]}, hash160(KEY)) is None


class _Index:
    def __init__(self, claims):
        self.claims = claims

    def claim_of(self, tag):
        return self.claims.get(tag)

    def address_of(self, tag):
        return (self.claims.get(tag) or {}).get("address")


@pytest.fixture
def app(app_state):
    app_state.set_setting("public_hosts", ["arcade.example.com"])
    yield TestClient(create_app(app_state))
    app_state.set_setting("public_hosts", [])


def test_the_window_says_whose_digidollar_address_to_pay(app, app_state, monkeypatch):
    version = app_state.messaging.params.pubkeyhash_version
    apple = b58check_encode(version, hash160(KEY))
    monkeypatch.setattr(type(app_state), "token_index",
                        lambda self, chain: _Index({"apple": {"address": apple, "claimed_txid": "aa" * 32}}))
    # @apple has an account here: the key it signed up with, checked against its address
    app_state.set_setting("address:" + "11" * 32, apple)
    app_state.set_setting("coinkey:" + "11" * 32, KEY.hex())
    got = app.get("/lite/api/ddaddress", params={"to": "@apple", "chain": "dgbtest"}, headers=OUTSIDE)
    assert got.status_code == 200 and got.json() == {"address": ddlib.address("dgbtest", KEY), "tag": "apple"}
    # a key that is not the name's is never used, whatever the arcade was told
    app_state.set_setting("coinkey:" + "11" * 32, OTHER.hex())
    monkeypatch.setattr(type(app_state), "token_index",
                        lambda self, chain: _Index({"pear": {"address": apple, "claimed_txid": "bb" * 32}}))
    refused = app.get("/lite/api/ddaddress", params={"to": "@pear", "chain": "dgbtest"}, headers=OUTSIDE)
    assert refused.status_code == 400 and "TD" in refused.json()["detail"]
    assert app.get("/lite/api/ddaddress", params={"to": "@nobody", "chain": "dgbtest"}).status_code == 400
    typed = ddlib.address("dgbtest", OTHER)
    assert app.get("/lite/api/ddaddress", params={"to": typed, "chain": "dgbtest"}).json()["address"] == typed
    assert app.get("/lite/api/ddaddress", params={"to": typed, "chain": "dgb"}).status_code == 400, "other chain"
    assert app.get("/lite/api/ddaddress", params={"to": "@apple", "chain": "dgb"}).status_code == 400, \
        "an @name's mainnet key is not on the name chain"
    # your own, from the key your wallet told this arcade
    assert app.get("/lite/api/ddaddress", params={"to": "me", "chain": "dgbtest"}).status_code == 400, "sign in"
    signer = SigningKey.generate()
    assert _sign_in(app, signer, headers=OUTSIDE).status_code == 200
    app_state.set_setting("coinkey:" + signer.verify_key.encode().hex(), OTHER.hex())
    me = app.get("/lite/api/ddaddress", params={"to": "me", "chain": "dgbtest"}, headers=OUTSIDE).json()
    assert me["address"] == ddlib.address("dgbtest", OTHER)
