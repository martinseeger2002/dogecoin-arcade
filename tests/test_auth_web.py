"""The door: the splash, the challenge, the cookie.

The route tests exist because the interesting failures of a login are not
in the arithmetic -- `tests/test_accounts.py` covers that -- but in what
the browser is asked to do and what it is told when it cannot.
"""

import pathlib
import sys

import pytest
from nacl.signing import SigningKey

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade import accounts, seed                                # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}
LAN = {"host": "192.168.1.5:8420"}
TUNNEL = {"host": "wallet.example.com", "x-forwarded-proto": "https"}


def _sign_in(app, key, headers=LOCAL, join=True):
    challenge = app.get("/auth/challenge", headers=headers).json()
    signature = key.sign(accounts.login_message(
        challenge["origin"], challenge["nonce"])).signature
    return app.post("/auth/login", headers=headers, json={
        "pubkey": key.verify_key.encode().hex(),
        "nonce": challenge["nonce"],
        "signature": signature.hex(),
        "join": join,
    })


def test_the_splash_says_how_many_seats_are_left(client):
    app, state = client
    body = app.get("/join", headers=LOCAL).text
    assert f"{accounts.SEATS}" in body
    assert "seats open" in body


def test_the_splash_points_at_running_your_own(client):
    """A full node is supposed to produce another node, not a queue. The
    instructions themselves live on /clone, which serves the program and a
    copy of the index; the splash points there rather than repeating
    them."""
    app, _ = client
    body = app.get("/join", headers=LOCAL).text
    assert 'href="/clone"' in body

    whole = app.get("/clone", headers=LOCAL).text
    assert "source.tar.gz" in whole and "installer/install.py" in whole


def test_a_full_node_says_so_and_still_explains_the_way_out(client):
    app, state = client
    register = state.accounts()
    register.seats = 1
    _sign_in(app, SigningKey.generate())
    body = app.get("/join", headers=LOCAL).text
    assert "No open seats" in body
    assert 'href="/clone"' in body, "and where else to go, on the same page"


def test_the_number_on_the_page_is_counted_not_typed(client):
    app, state = client
    state.accounts().seats = 4
    _sign_in(app, SigningKey.generate())
    body = app.get("/join", headers=LOCAL).text
    assert "<strong>3</strong> of 4 seats open" in body


def test_over_plain_http_on_the_network_the_page_says_why_not(client):
    """`crypto.subtle` does not exist outside a secure context, so the door
    is shut and explained rather than shown and broken."""
    app, _ = client
    body = app.get("/join", headers=LAN).text
    assert "https" in body and "cannot generate a key" in body
    assert 'id="forms" hidden' in body, "and nothing is offered that cannot work"


@pytest.mark.parametrize("headers", [LOCAL, TUNNEL])
def test_where_the_browser_can_do_it_the_door_is_open(client, headers):
    app, _ = client
    body = app.get("/join", headers=headers).text
    assert 'id="forms" hidden' not in body


def test_the_page_a_newcomer_lands_on_offers_no_box_for_a_phrase(client):
    """The rule the whole sign-in design rests on, on the page people reach.

    A wallet's seed lives in the browser storage of the origin that made it,
    so a clone on another domain cannot read it -- the only way it gets the
    key is by asking for the words. The defence is to make the legitimate
    occasions almost none, so that being asked is itself the answer. This
    page offers a name and a password, or a file and a password, and it has
    nowhere to type a seed.
    """
    app, _ = client
    body = app.get("/join", headers=LOCAL).text
    assert 'id="backup-file"' in body and 'id="open-file"' in body
    assert "<textarea" not in body, "no box for a seed on the front door"
    # Worded since the Restore page (2026-09-26): Restore is the one page that
    # asks for the words, and anything else asking for them is stealing them.
    assert "Anything else asking for them is stealing them" in " ".join(body.split())


def test_the_rule_is_said_where_the_words_are_shown(client):
    """Not in a help page: at the one moment somebody is about to hold a
    wallet in their hands and could still be told what it costs."""
    app, _ = client
    body = app.get("/join", headers=LOCAL).text
    written = " ".join(body.split("id=\"written\"", 1)[1].split())
    assert "the only time they are shown" in written
    assert "this site unlocks with your password" in written
    assert "never one you followed a link to" in written


def test_the_page_that_sets_the_password_says_what_has_to_hold_it_up(client):
    """The encrypted wallet is handed to anybody who asks for the name, so the
    password carries the whole weight of a stranger's guessing at leisure,
    with no rate limit that means anything. That exposure was accepted as the
    price of a transfer that works when the old device is gone, so the page
    has to say it outright rather than leave the strength meter to be the only
    place anybody meets it.
    """
    app, _ = client
    said = " ".join(app.get("/join", headers=LOCAL).text.split())
    assert "hands your encrypted wallet to anybody who asks for your name" in said
    assert "nothing counts the tries" in said


def test_the_phrase_page_says_the_rule_and_asks_first(client):
    """Typing the words stays possible and stops being ordinary.

    `/join/keys` is the older page and the only place in the tree with a
    phrase box, so this is where the softener has to live. It is behind a
    thing a person says out loud, with the rule next to it and the file path
    pointed at, rather than a textarea sitting in the middle of the doors.
    """
    app, _ = client
    body = app.get("/join/keys", headers=LOCAL).text
    assert "I have lost my backup file" in body
    assert 'id="lost"' in body
    assert 'id="words-in" hidden' in body, "shut until they say they must"
    assert 'href="/join"' in body, "and the file is the way it points at first"
    said = " ".join(body.split())
    assert "Anything else asking for your twelve words" in said
    assert "twenty-four" not in said, "twelve is what the page actually shows"


def test_a_wallet_that_arrived_with_a_file_is_not_this_nodes_copy(client):
    """What the file path lands on, said by the page rather than discovered.

    `/auth/login` seats a key and writes no vault row -- the only route that
    writes one is the signup that made the wallet here. So an account that
    came with a file has a seat, no name in this node's table, and no copy
    this node could ever send back. That is the price of the path, and the
    Backup page is where it has to be written.
    """
    app, state = client
    key = SigningKey.generate()
    assert _sign_in(app, key).status_code == 200
    assert state.vault().by_pubkey(key.verify_key.encode().hex()) is None
    body = app.get("/me/backup", headers=LOCAL).text
    assert "no copy to send back" in body


def test_signing_a_challenge_sets_a_session_cookie(client):
    app, state = client
    key = SigningKey.generate()
    answer = _sign_in(app, key)
    assert answer.status_code == 200
    assert answer.json()["pubkey"] == key.verify_key.encode().hex()
    who = app.get("/auth/who", headers=LOCAL).json()
    assert who["pubkey"] == key.verify_key.encode().hex()


def test_the_cookie_is_httponly_and_not_secure_on_localhost(client):
    """Marking it Secure over plain http means the browser drops it, and the
    symptom is a login that appears to work and then does not."""
    app, _ = client
    answer = _sign_in(app, SigningKey.generate())
    cookie = answer.headers["set-cookie"]
    # Lax, not Strict: a game opened from a link on another site must arrive signed in
    # (2026-10-08); Lax still keeps the cookie off cross-site POSTs and fetches
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")
    assert "Secure" not in cookie


def test_over_https_the_cookie_is_secure(client):
    app, _ = client
    answer = _sign_in(app, SigningKey.generate(), headers=TUNNEL)
    assert "Secure" in answer.headers["set-cookie"]


def test_a_signature_for_another_origin_is_refused(client):
    """The origin is in the signed bytes, so a session opened on one node
    cannot be opened with the same signature on another."""
    app, _ = client
    key = SigningKey.generate()
    challenge = app.get("/auth/challenge", headers=LOCAL).json()
    elsewhere = key.sign(accounts.login_message(
        "https://somewhere-else", challenge["nonce"])).signature
    answer = app.post("/auth/login", headers=LOCAL, json={
        "pubkey": key.verify_key.encode().hex(),
        "nonce": challenge["nonce"], "signature": elsewhere.hex(), "join": True})
    assert answer.status_code == 403
    assert "signature" in answer.json()["detail"]


def test_rubbish_is_refused_without_a_traceback(client):
    app, _ = client
    for body in ({}, {"pubkey": "x"}, {"pubkey": "ab" * 32, "nonce": "no"}):
        answer = app.post("/auth/login", headers=LOCAL, json=body)
        assert answer.status_code == 403
        assert "Traceback" not in answer.text


def test_a_full_node_answers_409_rather_than_a_page(client):
    app, state = client
    state.accounts().seats = 1
    _sign_in(app, SigningKey.generate())
    answer = _sign_in(app, SigningKey.generate())
    assert answer.status_code == 409
    assert answer.json()["free"] == 0


def test_signing_out_forgets_the_session(client):
    app, _ = client
    _sign_in(app, SigningKey.generate())
    app.post("/auth/logout", headers=LOCAL)
    assert app.get("/auth/who", headers=LOCAL).json()["pubkey"] is None


def test_who_tells_a_stranger_nothing_but_the_seat_count(client):
    app, state = client
    key = SigningKey.generate()
    _sign_in(app, key)
    app.cookies.clear()
    said = app.get("/auth/who", headers=LOCAL).json()
    assert said["pubkey"] is None
    assert key.verify_key.encode().hex() not in str(said)


def test_the_word_list_is_served_from_the_node(client):
    """A wallet whose restore depends on somebody else's website is a wallet
    that stops restoring."""
    app, _ = client
    answer = app.get("/bip39-english.txt")
    assert answer.status_code == 200
    words = answer.text.split()
    assert len(words) == 2048 and words[0] == "abandon"


def test_the_browser_half_is_served_and_derives_the_same_path(client):
    app, _ = client
    body = app.get("/signin.js").text
    assert "no-store" in app.get("/signin.js").headers["cache-control"]
    assert f"ARCADE_PURPOSE = {seed.ARCADE_PURPOSE}" in body
    assert f"LOGIN_BRANCH = [ARCADE_PURPOSE, {seed.LOGIN_BRANCH[1]}, " \
           f"{seed.LOGIN_BRANCH[2]}]" in body


def test_the_overview_shows_the_seat_count_and_links_to_the_splash(client):
    app, _ = client
    body = app.get("/").text
    assert "seats open" in body and 'href="/join"' in body


def test_the_splash_renders_with_no_node(client):
    """Signing up must not need a synced chain: it is a browser key and a row."""
    app, _ = client
    answer = app.get("/join", headers=LOCAL)
    assert answer.status_code == 200
    for marker in ("Traceback", "NameError", "KeyError"):
        assert marker not in answer.text


def test_an_operator_can_close_signups(client, tmp_path):
    """`seats: 0` in settings.json means closed, and must not fall back to
    the default -- an `or` here would quietly reopen the node."""
    app, state = client
    state.set_setting("seats", 0)
    state._accounts = None
    body = app.get("/join", headers=LOCAL).text
    assert "No open seats" in body
    assert _sign_in(app, SigningKey.generate()).status_code == 409
