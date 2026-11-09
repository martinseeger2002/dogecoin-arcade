"""No account takes another's address (2026-11-09, security review).

/account/address and /account/segwit/register take an address on the browser's word (the node has no key to derive
it from). Account A could file account B's address as its own, and then what this node keeps by address -- a
mintpad's announced listings, whose payment woke whom (`_owners`), a segwit watch -- was A's on B's address. Both
routes now refuse an address another account holds on that chain.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_funding import _pubkey                                 # noqa: E402
from test_web import app_state                                   # noqa: F401,E402

from arcade.config import NETWORKS                               # noqa: E402
from arcade.script import b58check_encode, hash160               # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}


def _sign_in(client, headers) -> str:
    from nacl.signing import SigningKey

    from arcade import accounts
    key = SigningKey.generate()
    challenge = client.get("/auth/challenge", headers=headers).json()
    signature = key.sign(accounts.login_message(challenge["origin"], challenge["nonce"])).signature
    answer = client.post("/auth/login", headers=headers, json={
        "pubkey": key.verify_key.encode().hex(), "nonce": challenge["nonce"], "signature": signature.hex(),
        "join": True})
    assert answer.status_code == 200, answer.text
    return key.verify_key.encode().hex()


def test_an_account_cannot_file_another_accounts_address_as_its_own(app_state):
    """/account/address takes an address on the browser's word. Account A filing account B's address used to be
    taken, and then what this node keeps by address was A's on B's address (the cross-account sweep found A's mintpad
    "announced" list written under B's address, 2026-11-09). Now it is refused, on every chain, and B's address stays
    B's."""
    from fastapi.testclient import TestClient

    from arcade.web.app import create_app
    app = create_app(app_state)
    a, b = TestClient(app), TestClient(app)
    a_pub, b_pub = _sign_in(a, LOCAL), _sign_in(b, LOCAL)
    test = NETWORKS["test"]
    b_addr = b58check_encode(test.pubkeyhash_version, hash160(_pubkey(int("b0" * 32, 16))))
    a_addr = b58check_encode(test.pubkeyhash_version, hash160(_pubkey(int("a0" * 32, 16))))
    assert b.post("/account/address", json={"address": b_addr}).status_code == 200
    assert a.post("/account/address", json={"address": a_addr}).status_code == 200
    r = a.post("/account/address", json={"address": b_addr})
    assert r.status_code == 409 and "another account" in r.json()["detail"], r.text
    assert app_state.setting(f"address:{a_pub}") == a_addr and app_state.setting(f"address:{b_pub}") == b_addr
    assert b.post("/account/address", json={"address": b_addr}).status_code == 200, "its own, again: still fine"


def test_an_account_cannot_register_another_accounts_segwit_address(app_state, monkeypatch):
    """The same for /account/segwit/register (native and nested), on Litecoin testnet: an address another account
    registered is refused, and its own registration stands."""
    from fastapi.testclient import TestClient

    from arcade import script
    from arcade.web.app import create_app
    from test_litecoin_testnet import _with_ltc_test
    _with_ltc_test(app_state, monkeypatch, ready=True)
    app = create_app(app_state)
    a, b = TestClient(app), TestClient(app)
    _sign_in(a, LOCAL)
    b_pub = _sign_in(b, LOCAL)
    p = NETWORKS["ltctest"]
    native = script.p2wpkh_address(p, hash160(_pubkey(int("84" * 32, 16))))
    nested_key = _pubkey(int("49" * 32, 16))
    nested = script.p2sh_p2wpkh_address(p, hash160(nested_key))
    assert b.post("/account/segwit/register", json={"chain": "ltctest", "address": native}).status_code == 200
    assert b.post("/account/segwit/register", json={"chain": "ltctest", "address": nested,
                                                    "pubkey": nested_key.hex()}).status_code == 200
    r = a.post("/account/segwit/register", json={"chain": "ltctest", "address": native})
    assert r.status_code == 409 and "another account" in r.json()["detail"], r.text
    r = a.post("/account/segwit/register", json={"chain": "ltctest", "address": nested, "pubkey": nested_key.hex()})
    assert r.status_code == 409, r.text
    assert app_state.setting(f"segwit_watch:{b_pub}:ltctest") == native
