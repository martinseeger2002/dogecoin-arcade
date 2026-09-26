"""The seat register: who may use a node, and how they prove it.

Every test here is about a promise made on the splash page or in
docs/multi-user.md, because those are the sentences somebody will believe.
"""

import time

import pytest
from nacl.signing import SigningKey

from arcade import seed
from arcade.accounts import (
    Accounts, AccountError, SeatsFull, is_pubkey, login_message,
)


@pytest.fixture
def register(tmp_path):
    return Accounts(tmp_path / "accounts.sqlite", seats=3, idle_days=90)


def _key():
    return SigningKey.generate()


def _sign_in(register, key, *, join=True, origin="https://node", ip="1.2.3.4",
             now=None):
    challenge = register.challenge(origin, now=now)
    signature = key.sign(login_message(origin, challenge["nonce"])).signature
    return register.login(key.verify_key.encode().hex(), challenge["nonce"],
                          signature.hex(), origin=origin, ip=ip, join=join,
                          now=now)


# --- seats --------------------------------------------------------------------

def test_a_node_seats_exactly_as_many_as_it_says(register):
    keys = [_key() for _ in range(3)]
    for key in keys:
        _sign_in(register, key)
    assert register.taken() == 3
    assert register.free() == 0 and register.full()

    with pytest.raises(SeatsFull) as refused:
        _sign_in(register, _key())
    # The refusal has to say where else to go, because there is somewhere
    # else to go -- that is the whole argument for a hard cap.
    assert "chain" in str(refused.value) and "yourself" in str(refused.value)


def test_signing_in_again_is_not_a_second_seat(register):
    key = _key()
    _sign_in(register, key)
    _sign_in(register, key)
    _sign_in(register, key)
    assert register.taken() == 1


def test_a_seat_comes_back_after_ninety_days_of_silence(register):
    old, recent = _key(), _key()
    long_ago = int(time.time()) - 91 * 86400
    _sign_in(register, old, now=long_ago)
    _sign_in(register, recent)
    assert register.taken() == 1, "the silent one no longer holds a seat"

    # And nothing of theirs was touched: the row, the name and the join date
    # are all still there, because the seat was the only thing they lost.
    account = register.account(old.verify_key.encode().hex())
    assert account is not None and account.created == long_ago
    assert account.seated is False


def test_coming_back_after_a_sweep_restores_the_same_account(register):
    key = _key()
    long_ago = int(time.time()) - 200 * 86400
    _sign_in(register, key, now=long_ago)
    assert register.taken() == 0

    _sign_in(register, key)
    account = register.account(key.verify_key.encode().hex())
    assert account.seated
    assert account.created == long_ago, "the same account, not a new one"


def test_using_the_node_keeps_the_seat(register):
    key = _key()
    nearly = int(time.time()) - 89 * 86400
    _sign_in(register, key, now=nearly)
    register.touch(key.verify_key.encode().hex())
    assert register.taken() == 1


def test_a_seat_given_back_ends_that_accounts_sessions(register):
    key = _key()
    token = _sign_in(register, key)
    assert register.session(token) is not None
    register.release(key.verify_key.encode().hex())
    assert register.session(token) is None
    assert register.free() == 3


# --- proving who you are ------------------------------------------------------

def test_a_signature_over_the_nonce_opens_a_session(register):
    key = _key()
    token = _sign_in(register, key)
    account = register.session(token)
    assert account.pubkey == key.verify_key.encode().hex()


def test_the_token_is_never_stored(register, tmp_path):
    """A backup of this file must not be a way into somebody's account."""
    token = _sign_in(register, _key())
    raw = (tmp_path / "accounts.sqlite").read_bytes()
    assert token.encode() not in raw
    for suffix in ("-wal", "-shm"):
        extra = tmp_path / ("accounts.sqlite" + suffix)
        if extra.exists():
            assert token.encode() not in extra.read_bytes()


def test_a_nonce_works_once(register):
    key = _key()
    origin = "https://node"
    challenge = register.challenge(origin)
    signature = key.sign(login_message(origin, challenge["nonce"])).signature
    pubkey = key.verify_key.encode().hex()
    register.login(pubkey, challenge["nonce"], signature.hex(), origin=origin,
                   join=True)
    with pytest.raises(AccountError) as again:
        register.login(pubkey, challenge["nonce"], signature.hex(),
                       origin=origin, join=True)
    assert "not one this node is waiting for" in str(again.value)


def test_a_signature_for_another_node_does_not_open_this_one(register):
    """The origin is inside the signed bytes, so a signature is not portable."""
    key = _key()
    challenge = register.challenge("https://node")
    elsewhere = key.sign(login_message("https://somebody-else",
                                       challenge["nonce"])).signature
    with pytest.raises(AccountError) as refused:
        register.login(key.verify_key.encode().hex(), challenge["nonce"],
                       elsewhere.hex(), origin="https://node", join=True)
    assert "does not match the key" in str(refused.value)


def test_a_wrong_signature_spends_the_nonce_anyway(register):
    key = _key()
    challenge = register.challenge("https://node")
    with pytest.raises(AccountError):
        register.login(key.verify_key.encode().hex(), challenge["nonce"],
                       ("00" * 64), origin="https://node", join=True)
    good = key.sign(login_message("https://node", challenge["nonce"])).signature
    with pytest.raises(AccountError) as second:
        register.login(key.verify_key.encode().hex(), challenge["nonce"],
                       good.hex(), origin="https://node", join=True)
    assert "waiting for" in str(second.value)


def test_an_expired_challenge_is_refused(register):
    key = _key()
    now = int(time.time())
    challenge = register.challenge("https://node", now=now)
    signature = key.sign(login_message("https://node", challenge["nonce"])).signature
    with pytest.raises(AccountError) as refused:
        register.login(key.verify_key.encode().hex(), challenge["nonce"],
                       signature.hex(), origin="https://node", join=True,
                       now=now + 121)
    assert "expired" in str(refused.value)


def test_an_unknown_key_is_told_to_sign_up_rather_than_seated(register):
    key = _key()
    with pytest.raises(AccountError) as refused:
        _sign_in(register, key, join=False)
    assert "seat" in str(refused.value)
    assert register.taken() == 0


def test_attempts_are_rate_limited_per_address(register):
    key = _key()
    for _ in range(20):
        with pytest.raises(AccountError):
            register.login(key.verify_key.encode().hex(), "nope", "00",
                           origin="https://node", ip="9.9.9.9", join=True)
    with pytest.raises(AccountError) as stopped:
        _sign_in(register, key, ip="9.9.9.9")
    assert "too many" in str(stopped.value)
    # A different visitor is not punished for it.
    assert _sign_in(register, _key(), ip="8.8.8.8")


def test_no_name_is_kept_here(register):
    """A @tag is chain state. A copy in this table would be a name that is
    wrong rather than a name that is missing, which is strictly worse."""
    key = _key()
    _sign_in(register, key)
    account = register.account(key.verify_key.encode().hex())
    assert not hasattr(account, "tag")
    columns = {row[1] for row in
               register.conn.execute("PRAGMA table_info(account)")}
    assert "tag" not in columns


def test_nonsense_is_never_stored(register):
    assert is_pubkey("ab" * 32)
    assert not is_pubkey("ab" * 31)
    assert not is_pubkey("zz" * 32)
    with pytest.raises(AccountError):
        register.join("not a key")
    assert register.taken() == 0


def test_a_session_expires(register):
    key = _key()
    now = int(time.time())
    token = _sign_in(register, key, now=now)
    assert register.session(token, now=now + 29 * 86400) is not None
    # Reading it moved it along, which is what using the wallet should do.
    assert register.session(token, now=now + 55 * 86400) is not None
    assert register.session(token, now=now + 120 * 86400) is None


def test_logging_out_ends_it(register):
    key = _key()
    token = _sign_in(register, key)
    register.logout(token)
    assert register.session(token) is None


# --- the same key on both sides -----------------------------------------------

def test_the_phrase_is_the_account(register):
    """A seat follows the words, which is what makes it portable.

    The same twenty-four words produce the same account id here and in the
    browser (arcade/web/templates/signin.js derives the same path), so
    somebody who runs out of seats on one node signs into another and is
    the same person.
    """
    phrase = seed.generate()
    key = SigningKey(seed.login_key(phrase))
    assert key.verify_key.encode().hex() == seed.login_pubkey(phrase)
    token = _sign_in(register, key)
    assert register.session(token).pubkey == seed.login_pubkey(phrase)


def test_the_login_key_is_not_the_messaging_key():
    phrase = seed.generate()
    assert seed.login_key(phrase) != seed.messaging_key(phrase)


def test_the_arcade_tree_never_touches_the_coin_path():
    """m/44'/1'/0' is the testnet account of every BIP44 wallet, and this
    module's derivation is not real BIP32 -- so a key derived here at that
    path would be a different 32 bytes wearing a name that says it spends."""
    assert seed.ARCADE_PURPOSE != 44
    assert seed.MESSAGING_BRANCH[0] == seed.LOGIN_BRANCH[0] == seed.ARCADE_PURPOSE
    assert seed.MESSAGING_BRANCH != seed.LOGIN_BRANCH


def test_one_register_survives_many_threads_at_once(register):
    """The live node shares one register across every request thread. Unguarded,
    two threads stepping statements on the one connection logged "bad parameter
    or other API misuse", and a COUNT(*) that came back None -- a 500 on the
    account page (2026-09-25)."""
    import threading
    who = "ab" * 32
    caps = {"hour": {"send": 10_000}, "bytes": 10_000_000}
    errors = []

    def busy(n):
        try:
            for i in range(200):
                if i % 5 == 0:
                    register.charge(who, "send", 10, caps=caps)
                assert isinstance(register.used(who, "send"), int)
                register.room(who, caps=caps)
        except Exception as exc:                       # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=busy, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert register.used(who, "send") == 8 * 40
