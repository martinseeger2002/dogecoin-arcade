"""A first claim of an address is proved (2026-11-09, security review).

/account/address, /account/segwit/register and the other chains' addresses a sign-up files took the first claim of
any address of the right shape as the signed-in account's own. A 409 stopped an address another account held, but an
address nobody here held yet could still be filed by anybody, though somebody else holds its key. Now its own key
signs that it is this account's (wallet_imports.check_proof, the proof /signup asks of its first address), and a
claim without that proof is refused. The address the account already has needs nothing new."""
import pathlib
import sys

from nacl.signing import SigningKey

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_me_page import _seat                                   # noqa: E402
from test_litecoin_testnet import _with_ltc_test                  # noqa: E402
from address_claim import file_address, keyed, proof, register_segwit   # noqa: E402
from signup_proof import signup                                  # noqa: E402

MINE, MY_COIN = keyed("main", 0xC1A1)
THEIRS, THEIR_COIN = keyed("main", 0xC1A2)


def test_a_first_claim_without_its_keys_proof_is_refused(client):
    app, state = client
    pubkey = _seat(app)
    for missing in ({}, {"coin_pubkey": MY_COIN[1].hex()}, {"coin_pubkey": MY_COIN[1].hex(), "address_signature": "zz"}):
        r = app.post("/account/address", json={"address": MINE, "chain": "main", **missing})
        assert r.status_code == 400 and "not proved yours" in r.json()["detail"], (missing, r.text)
    assert not state.setting(f"address:main:{pubkey}")


def test_a_proof_by_another_key_or_for_another_account_is_refused(client):
    app, state = client
    pubkey = _seat(app)
    # Somebody else's key signing for my address: the address is not that key's.
    signed_by_them = proof(pubkey, "main", MINE, THEIR_COIN)
    r = app.post("/account/address", json={"address": MINE, "chain": "main", **signed_by_them})
    assert r.status_code == 400, r.text
    # My address's key, but its signature carried over from another account (or another chain): signed nothing here.
    elsewhere = proof("ab" * 32, "main", MINE, MY_COIN)
    r = app.post("/account/address", json={"address": MINE, "chain": "main", **elsewhere})
    assert r.status_code == 400, r.text
    wrong_chain = proof(pubkey, "doge", MINE, MY_COIN)
    r = app.post("/account/address", json={"address": MINE, "chain": "main", **wrong_chain})
    assert r.status_code == 400, r.text
    assert not state.setting(f"address:main:{pubkey}")


def test_a_proper_proof_files_the_address_and_saying_it_again_needs_none(client):
    app, state = client
    pubkey = _seat(app)
    r = file_address(app, MINE, chain="main")
    assert r.status_code == 200, r.text
    assert state.setting(f"address:main:{pubkey}") == MINE
    assert state.setting(f"coinkey:main:{pubkey}") == MY_COIN[1].hex()
    again = app.post("/account/address", json={"address": MINE, "chain": "main"})
    assert again.status_code == 200, "the account's own address, again: nothing new to prove"
    # Moving to another address is a first claim of that one.
    r = app.post("/account/address", json={"address": THEIRS, "chain": "main"})
    assert r.status_code == 400 and state.setting(f"address:main:{pubkey}") == MINE


def test_a_segwit_address_is_registered_only_with_its_keys_proof(client, monkeypatch):
    app, state = client
    _with_ltc_test(state, monkeypatch, ready=True)
    pubkey = _seat(app)
    native, native_coin = keyed("ltctest", 0x84C1, kind="p2wpkh")
    nested, nested_coin = keyed("ltctest", 0x49C1, kind="p2sh-p2wpkh")
    _, other_coin = keyed("ltctest", 0x49C2, kind="p2sh-p2wpkh")
    for address, coin in ((native, native_coin), (nested, nested_coin)):
        bare = app.post("/account/segwit/register", json={"chain": "ltctest", "address": address,
                                                          "pubkey": coin[1].hex()})
        assert bare.status_code == 400 and "not proved yours" in bare.json()["detail"], bare.text
        theirs = register_segwit(app, address, other_coin, chain="ltctest")
        assert theirs.status_code == 400, theirs.text
        assert register_segwit(app, address, coin, chain="ltctest").status_code == 200
        assert app.post("/account/segwit/register", json={"chain": "ltctest", "address": address}).status_code == 200
    assert state.setting(f"segwit_watch:{pubkey}:ltctest") == native
    assert state.setting(f"segwit_watch:p2sh-p2wpkh:{pubkey}:ltctest") == nested


def test_a_sign_up_files_another_chains_address_only_with_its_proof(client):
    app, state = client
    key = SigningKey.generate()
    pubkey = key.verify_key.encode().hex()
    proved = proof(pubkey, "main", MINE, MY_COIN)
    said = signup(app, state, "provedmain", key=key, address_main=MINE, coin_pubkey_main=MY_COIN[1].hex(),
                  address_signature_main=proved["address_signature"])
    assert said.status_code == 200, said.text
    assert state.setting(f"address:main:{pubkey}") == MINE

    other = SigningKey.generate()
    said = signup(app, state, "unprovedmain", key=other, address_main=THEIRS, coin_pubkey_main=THEIR_COIN[1].hex())
    assert said.status_code == 200, "the sign-up itself goes ahead"
    assert not state.setting(f"address:main:{other.verify_key.encode().hex()}"), \
        "an address its key did not sign over is left for /account/address, which asks the same"
