"""A key that already has an account signs in; it cannot sign up again under another name (SadFrog's side, 2026-10-25).
/signup takes a public key and proves nothing about holding it, so a second sign-up was a session as the key's owner."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from nacl.signing import SigningKey                              # noqa: E402

import signup_proof as SP                                         # noqa: E402

BLOB = {"sealed": "c2VhbGVk", "salt": "00", "nonce": "00", "iterations": 600000}


def test_a_second_sign_up_with_a_key_that_has_an_account_is_refused(client, monkeypatch):
    page, state = client

    class Index:
        def address_of(self, wanted):
            return None
    monkeypatch.setattr(type(state), "token_index", lambda self, chain: Index())
    key = SigningKey.generate()
    pubkey = key.verify_key.encode().hex()
    coin = SP.coin_key()
    made = SP.signup(page, state, "owner", key=key, coin=coin, blob=BLOB)
    assert made.status_code == 200, made.text
    page.cookies.clear()
    again = SP.signup(page, state, "somebody", key=key, blob=BLOB)
    assert again.status_code == 400 and "sign in instead" in again.json()["detail"]
    assert "arcade_session" not in page.cookies, "no second session through sign-up"
    assert state.setting(f"address:{pubkey}") == SP.address_of(state, coin), "the owner's address is untouched"
    assert state.vault().get("somebody") is None
    fresh = SP.signup(page, state, "newcomer", blob=BLOB)
    assert fresh.status_code == 200, "a new key still signs up"


def test_a_wallet_sealed_with_too_few_rounds_is_not_filed(client, monkeypatch):
    """It is served to whoever asks for its name (2026-11-08, security review): a page that was tampered with could
    file one far cheaper to guess offline."""
    page, state = client

    class Index:
        def address_of(self, wanted):
            return None
    monkeypatch.setattr(type(state), "token_index", lambda self, chain: Index())
    weak = SP.signup(page, state, "cheap", blob={**BLOB, "iterations": 1000})
    assert weak.status_code == 400 and "sealed" in weak.json()["detail"]
    assert state.vault().get("cheap") is None


def test_a_sign_up_proves_the_key_and_the_address(client, monkeypatch):
    """2026-11-08, security review: /signup opened a session for any public key posted to it. Now the account key
    signs a challenge, as a sign-in does, and the coin key proves the address; either missing or wrong, nothing is
    filed and no session opens."""
    page, state = client

    class Index:
        def address_of(self, wanted):
            return None
    monkeypatch.setattr(type(state), "token_index", lambda self, chain: Index())
    victim = SigningKey.generate()
    for said in (SP.signup(page, state, "a", key=victim, sign_key=False, blob=BLOB),
                 SP.signup(page, state, "b", key=victim, sign_address=False, blob=BLOB),
                 SP.signup(page, state, "c", key=victim, blob=BLOB, address=SP.address_of(state, SP.coin_key()))):
        assert said.status_code in (400, 403), said.text
        assert "arcade_session" not in page.cookies
    # A signature by another key over the victim's challenge is not the victim's.
    challenge = page.get("/auth/challenge").json()
    from arcade import accounts
    forged = SigningKey.generate().sign(accounts.login_message(challenge["origin"], challenge["nonce"])).signature
    coin = SP.coin_key()
    import arcade.wallet_imports as W
    pubkey = victim.verify_key.encode().hex()
    address = SP.address_of(state, coin)
    said = page.post("/signup", json={
        "tag": "d", "pubkey": pubkey, "address": address, "coin_pubkey": coin[1].hex(), "blob": BLOB,
        "address_signature": SP.coin_sign(coin[0], W.proof_digest(pubkey, state.messaging.network, address,
                                                                   coin[1].hex())).hex(),
        "nonce": challenge["nonce"], "signature": forged.hex()})
    assert said.status_code in (400, 403), said.text
    assert "arcade_session" not in page.cookies
    assert all(state.vault().get(t) is None for t in "abcd"), "nothing filed"
    assert state.accounts().account(pubkey) is None, "the victim's key has no seat here"
