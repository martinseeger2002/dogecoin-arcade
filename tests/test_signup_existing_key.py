"""A key that already has an account signs in; it cannot sign up again under another name (SadFrog's side, 2026-10-25).
/signup takes a public key and proves nothing about holding it, so a second sign-up was a session as the key's owner."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import script                                         # noqa: E402

BLOB = {"sealed": "c2VhbGVk", "salt": "00", "nonce": "00", "iterations": 600000}


def test_a_second_sign_up_with_a_key_that_has_an_account_is_refused(client, monkeypatch):
    page, state = client

    class Index:
        def address_of(self, wanted):
            return None
    monkeypatch.setattr(type(state), "token_index", lambda self, chain: Index())
    key = "11" * 32
    theirs = script.b58check_encode(113, bytes(range(20)))
    other = script.b58check_encode(113, bytes(range(20, 40)))
    made = page.post("/signup", json={"tag": "owner", "pubkey": key, "address": theirs, "blob": BLOB})
    assert made.status_code == 200, made.text
    page.cookies.clear()
    again = page.post("/signup", json={"tag": "somebody", "pubkey": key, "address": other, "blob": BLOB})
    assert again.status_code == 400 and "sign in instead" in again.json()["detail"]
    assert "arcade_session" not in page.cookies, "no session for a key nobody proved"
    assert state.setting(f"address:{key}") == theirs, "the owner's address is untouched"
    assert state.vault().get("somebody") is None
    fresh = page.post("/signup", json={"tag": "newcomer", "pubkey": "22" * 32, "address": other, "blob": BLOB})
    assert fresh.status_code == 200, "a new key still signs up"
