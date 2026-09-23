"""The page an account actually uses.

The operator's pages drive the NODE's wallet and always have. This is the
same application seen by somebody whose keys are in their own browser, so
it is a different page rather than the same page with a different balance
in it -- almost nothing on the operator's version would be true.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

LOCAL = {"host": "127.0.0.1:8420"}


def _seat(app):
    from nacl.signing import SigningKey

    from arcade import accounts

    key = SigningKey.generate()
    challenge = app.get("/auth/challenge", headers=LOCAL).json()
    signature = key.sign(accounts.login_message(
        challenge["origin"], challenge["nonce"])).signature
    answer = app.post("/auth/login", headers=LOCAL, json={
        "pubkey": key.verify_key.encode().hex(), "nonce": challenge["nonce"],
        "signature": signature.hex(), "join": True})
    assert answer.status_code == 200, answer.text
    return key.verify_key.encode().hex()


def test_a_stranger_is_sent_to_sign_up(client):
    app, _ = client
    answer = app.get("/me", follow_redirects=False)
    assert answer.status_code == 303
    assert answer.headers["location"] == "/join"


def test_an_account_gets_its_own_page(client):
    app, _ = client
    _seat(app)
    body = app.get("/me").text
    assert "Your arcade" in body
    assert "Your keys are in this browser" in body
    # It now mirrors the parts of Overview that genuinely apply to an
    # account -- its own name, contacts and chain status -- but not the
    # settings that configure automation running on the NODE's own key,
    # which an account has no equivalent of and is not offered.
    assert "Known contacts" in body
    assert "Accept offers that meet my asking price" not in body
    assert "Install updates automatically" not in body
    assert 'id="scan"' not in body


def test_it_asks_for_the_password_before_anything(client):
    """The key is opened for one tab and forgotten when it closes."""
    app, _ = client
    _seat(app)
    body = app.get("/me").text
    assert 'id="unlock"' in body
    assert 'id="doing" hidden' in body, "nothing is offered until it is open"
    assert "never leaves this browser" in body


def test_the_overview_page_renders_nothing_off_the_chain_as_markup(client):
    """Overview no longer shows a message body at all -- that moved to the
    Messages page, which has its own version of this test. What is left
    here (a wallet card, a contact code) is either drawn by hand or is this
    account's own words, never somebody else's."""
    app, _ = client
    _seat(app)
    page = pathlib.Path("arcade/web/templates/me.html").read_text()
    # Every innerHTML here empties an element before it is refilled by
    # hand. None of them is ever given content that came off the chain.
    uses = [line.strip() for line in page.splitlines()
            if "innerHTML" in line and not line.strip().startswith("//")]
    assert uses == ['$("wallets").innerHTML = "";'], uses


def test_the_feed_is_the_same_posts(client):
    """Nothing here is private: every post was public when it was mined.
    An account's feed differs only in what it says about who is looking."""
    app, state = client
    _seat(app)
    with state.store() as store:
        store.add_group_post(state.messaging.network, "", "aa" * 32, 5, 50,
                             "nSomebody", "", "a public post", mine=False)
    said = app.get("/account/feed").json()
    assert any(p["text"] == "a public post" for p in said["posts"])
    assert said["mine"] == "", "and no name for an account that has none"


def test_the_feed_says_who_is_looking(client):
    app, state = client
    pubkey = _seat(app)
    state.vault().put("reader", pubkey, "nAddr", '{"sealed":"00"}')
    assert app.get("/account/feed").json()["mine"] == "reader"


def test_everything_on_the_page_is_reachable_by_an_account(client):
    """A page that offers a button the door refuses is a page that lies."""
    import re

    from arcade.web import door

    app, _ = client
    _seat(app)
    body = app.get("/me").text
    for path in set(re.findall(r'fetch\("(/[^"?]+)', body)):
        assert door.public_path(path) or door.public_path(path, "POST"), path


# --- being able to find it ----------------------------------------------------

EDGE = {"host": "node.dogecoinarcade.com", "cf-ray": "abc-LHR"}


@pytest.fixture
def public(client):
    app, state = client
    state.set_setting("public_hosts", ["node.dogecoinarcade.com"])
    yield app, state
    state.set_setting("public_hosts", [])


def test_signing_up_does_not_end_on_the_page_that_asks_you_to_sign_up(public):
    """Somebody presses "take me in" and goes to `/`. Without this they
    land back on the splash, signed in, being asked to sign up."""
    app, _ = public
    assert app.get("/", headers=EDGE).status_code == 200, "a stranger: splash"
    _seat(app)
    answer = app.get("/", headers=EDGE, follow_redirects=False)
    assert answer.status_code == 303
    assert answer.headers["location"] == "/me"


def test_an_account_gets_its_own_tabs(public):
    """Four things that work, rather than eleven with seven missing."""
    app, _ = public
    _seat(app)
    body = app.get("/me", headers=EDGE).text
    assert 'href="/me"' in body and "Your arcade" in body
    assert 'href="/feed"' in body
    assert 'href="/clone"' in body
    for theirs in ("/wallet", "/messages", "/contacts", "/backup",
                   "/approvals"):
        assert f'href="{theirs}"' not in body, theirs


def test_a_stranger_still_gets_the_public_tabs(public):
    app, _ = public
    body = app.get("/feed", headers=EDGE).text
    assert "Your arcade" not in body
    assert 'href="/wallet"' not in body


def test_the_operator_still_gets_everything(client):
    """On the machine itself, nothing has changed."""
    app, _ = client
    body = app.get("/").text
    for theirs in ("/wallet", "/messages", "/contacts", "/backup"):
        assert f'href="{theirs}"' in body, theirs
    assert "Your arcade" not in body


def test_every_account_tab_is_one_the_door_allows(client):
    from arcade.web import app as webapp
    from arcade.web import door

    for path, label, _chain, built in webapp.ACCOUNT_NAV:
        assert built
        assert door.public_path(path), f"{label} is in the menu and refused"


def test_signing_in_opens_the_wallet(client):
    """The password IS the login. Asking for it again on the next page is
    asking the same question twice."""
    page = pathlib.Path("arcade/web/templates/wallet_js.js").read_text()
    signup = page[page.index("async function _signUp"):page.index("async function _signIn")]
    signin = page[page.index("async function _signIn"):]
    assert "remember(wallet.phrase)" in signup, "signing up opens it"
    # Signing in goes to `seatWith` rather than remembering for itself, and so
    # does opening a backup file: the words come from the node's blob one way
    # and from a saved file the other, and the two used to carry their own copy
    # of the line that keeps the tab open. One place does it now, so the check
    # is that each way in arrives there and that it is the place that remembers.
    assert "await seatWith(wallet)" in \
        signin.split("async function _openFile")[0], "signing in goes there"
    seat = page[page.index("async function seatWith"):]
    assert "remember(wallet.phrase)" in seat.split("export")[0], \
        "and so does signing in"


def test_a_new_tab_says_why_it_is_asking(client):
    """A session cookie lasts thirty days and a tab does not, so this is
    the one time a password is wanted twice -- and the page says which of
    the two situations it is in rather than looking like a second login."""
    app, _ = client
    _seat(app)
    body = app.get("/me").text
    assert "Welcome back" in body
    assert "new tab" in body
    assert "Signing in opens it" in body


# --- the messages page --------------------------------------------------------

def test_messages_needs_an_account(client):
    app, _ = client
    answer = app.get("/me/messages", follow_redirects=False)
    assert answer.status_code == 303
    assert answer.headers["location"] == "/join"


def test_the_messages_page_is_there_and_is_in_the_menu(public):
    """The account menu, which is what an account sees: on the machine
    itself the operator gets their own eleven tabs instead."""
    app, _ = public
    _seat(app)
    body = app.get("/me/messages", headers=EDGE).text
    assert "Conversations" in body, "the same messenger as the wallet's own"
    assert "cannot read one" in body, "and says what the node can and cannot do"
    assert 'href="/me/messages"' in body, "and is reachable from the menu"


def test_a_message_is_drawn_as_words_there_too(client):
    """The same rule as the inbox on /me: somebody else's words are never
    rendered as markup."""
    page = pathlib.Path("arcade/web/templates/my_messages.html").read_text()
    assert "body.textContent = mail.text(letter);" in page
    # Each of these empties an element this file then fills by hand. A
    # message's own words never go near one.
    uses = [line.strip() for line in page.splitlines()
            if "innerHTML" in line and not line.strip().startswith("//")]
    assert uses == ['$("threads").innerHTML = "";',
                    '$("bubbles").innerHTML = "";'], uses


def test_it_says_who_a_message_is_going_to_before_it_is_written(client):
    """From the chain, not from anything typed: somebody writing to @gx1
    should see the address their words are sealed to."""
    app, _ = client
    _seat(app)
    body = app.get("/me/messages").text
    # The conversation says who it is with, from the chain, above what is
    # being typed -- and the reply checks that the key behind that name is
    # still the key this conversation has been with before it seals
    # anything to it.
    assert 'id="convo-name"' in body and 'id="convo-sub"' in body
    assert "mail.lookUp" in body
    assert "is not who this conversation is with any more" in body
