"""What one account may take from a node that is shared with everybody else.

docs/multi-user.md §6 asks for two halves and they are both tested here,
because a limit nobody was told about is not a limit, it is a mystery: there
is a number on each action an account can take, and that number is on the
account's own page before it is reached rather than in a refusal after it.

So the tests are about the seams more than the arithmetic -- the counting is a
`COUNT(*)` over rows that delete themselves. They ask: does the page say the
numbers before anybody needs them, does a refusal name the action and roughly
when it lifts, does a build that failed for a reason unrelated to the
allowance cost an account nothing, is one account's hour anybody else's
business, and does the operator's page move the NEXT action rather than the
next restart?

What is deliberately NOT here: inscription bytes per day. An account cannot
inscribe yet -- there is no route that builds one -- so §6's "one active
inscription run at a time" has no account-side shape either. The bytes that
ARE counted are the ones an account can push onto the chain today, which is a
post, a reaction or a sealed message; the ceiling is the same ceiling and
takes the extra kinds when inscribing reaches accounts.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                           # noqa: F401,E402
from test_me_page import _seat                                   # noqa: E402
from test_funding import _pubkey                                 # noqa: E402
from test_account_mainnet import MAIN, TEST                      # noqa: E402

from arcade import accounts as accountslib                       # noqa: E402
from arcade.script import b58check_encode, hash160                # noqa: E402

COIN = 100_000_000

#: Somebody else's wallets, made with this repository's own encoder, so the
#: sends in here are refused for the reason this file is about and not for
#: being shaped wrong.
THEIRS_MAIN = b58check_encode(56, hash160(_pubkey(int("33" * 32, 16))))
THEIRS_TEST = b58check_encode(111, hash160(_pubkey(int("44" * 32, 16))))

SECRET = int("51" * 32, 16)


def _open(app):
    """A signed-in account with an address on each chain, as signup leaves it."""
    pubkey = _seat(app)
    app.post("/account/address", json={"address": TEST,
                                       "coin_pubkey": _pubkey(SECRET).hex()})
    app.post("/account/address", json={"address": MAIN, "chain": "main",
                                       "coin_pubkey": _pubkey(SECRET).hex()})
    return pubkey


def _coin(app, state, address, chain, value=50 * COIN):
    """A coin for that address, written straight into the index.

    The point of §4's index is that a build is a read of rows this node built,
    not a call into a wallet it does not have -- so these tests need the rows
    and no daemon. Nothing here is ever signed or broadcast: an offer that
    nobody brings back is exactly what an allowance is for.
    """
    index = state.token_index(state.ledger if chain == "main"
                              else state.messaging)
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) "
            "VALUES (?,?,?,?,?)", ("cd" * 32, 0, address, value, 1))
        db.conn.commit()


def _send(app, chain="main"):
    return app.post("/account/send", json={
        "to": THEIRS_MAIN if chain == "main" else THEIRS_TEST,
        "amount": "1", "chain": chain})


def _quota(app):
    return app.get("/account").json()["quota"]


def _row(app, kind):
    return next(one for one in _quota(app)["actions"] if one["kind"] == kind)


# --- visible, not silent ------------------------------------------------------

def test_the_numbers_are_on_the_page_before_anybody_runs_into_them(client):
    """§6's whole point: a person who finds a wall they were never shown
    concludes the node is broken, and the operator gets that message."""
    app, _ = client
    _open(app)
    quota = _quota(app)
    assert {one["kind"] for one in quota["actions"]} == set(
        accountslib.PER_HOUR)
    assert all(one["used"] == 0 for one in quota["actions"])
    assert quota["bytes"] == {"limit": accountslib.BYTES_PER_DAY, "used": 0}
    assert quota["hour"] == accountslib.HOUR and quota["day"] == accountslib.DAY
    # Words, not keys: a page that says "react: 60 of 60" is a form, not an
    # explanation.
    assert _row(app, "react")["label"] == accountslib.LABELS["react"]


def test_the_operators_page_shows_the_same_numbers_it_writes(client):
    app, state = client
    body = app.get("/").text
    assert "What one account may do here" in body
    assert f'value="{accountslib.PER_HOUR["post"]}"' in body, \
        "the form shows what is in force, not what the code defaults to"
    state.set_setting("quota:post", 7)
    assert 'value="7"' in app.get("/").text


def test_the_account_page_shows_its_own_room(client):
    app, _ = client
    _seat(app)
    assert 'id="room"' in app.get("/me").text


# --- counted where the work is done -------------------------------------------

def test_an_action_that_ran_out_is_refused_saying_which_and_when(client):
    """Not "too many requests": the number, the action, and roughly when the
    page can be used again."""
    app, state = client
    _open(app)
    _coin(app, state, MAIN, "main")
    state.set_setting("quota:send", 2)
    assert _row(app, "send")["limit"] == 2, \
        "the page and the route cannot be holding different numbers"
    for _ in range(2):
        assert _send(app).status_code == 200
    refused = _send(app)
    assert refused.status_code == 400
    said = refused.json()["detail"]
    assert "2 sends" in said and "minute" in said, said
    assert _row(app, "send")["used"] == 2, "a refusal counts nothing"


def test_a_build_that_failed_for_a_different_reason_costs_nothing(client):
    """No coins is not the same story as no allowance, and charging an
    allowance for a transaction that was never built would punish somebody
    for a thing the node could not do."""
    app, _ = client
    _open(app)
    broke = _send(app)
    assert broke.status_code == 400 and "offer" not in broke.json()
    assert "allowance" not in str(broke.json())
    assert _row(app, "send")["used"] == 0


def test_one_accounts_hour_is_not_another_accounts(client):
    app, state = client
    _coin(app, state, MAIN, "main")
    _open(app)
    state.set_setting("quota:send", 1)
    assert _send(app).status_code == 200
    assert _row(app, "send")["used"] == 1

    # A second account on the same node, who has done nothing at all.
    _open(app)
    assert _row(app, "send")["used"] == 0
    assert _send(app).status_code == 200, \
        "the second account's turn is its own, not the first one's leftover"


def test_zero_closes_a_door_and_says_who_closed_it(client):
    """An operator who sets zero made a decision. An account should be told
    that is what happened rather than told it ran out of something."""
    app, state = client
    _open(app)
    _coin(app, state, MAIN, "main")
    state.set_setting("quota:send", 0)
    refused = _send(app)
    assert refused.status_code == 400
    assert "operator closed" in refused.json()["detail"]


def test_the_bytes_a_day_are_counted_and_a_big_one_is_refused_honestly(client):
    """The bytes are the chain this node has to store and carry forever, so
    they are counted whatever carried them -- and the refusal says how much
    of the day is left, because "too big" is not an answer."""
    app, state = client
    _open(app)
    _coin(app, state, TEST, "test")
    state.set_setting("quota:bytes", 400)
    big = app.post("/account/write", json={"to": THEIRS_TEST,
                                          "sealed": "aa" * 450})
    assert big.status_code == 400, big.text
    assert "400 bytes a day" in big.json()["detail"], big.json()

    small = app.post("/account/write", json={"to": THEIRS_TEST,
                                            "sealed": "bb" * 100})
    assert small.status_code == 200, small.text
    assert small.json()["bytes"] == 100, "the bytes charged are the bytes sent"
    assert _quota(app)["bytes"] == {"limit": 400, "used": 100}


def test_the_pile_of_unsigned_offers_is_bounded(client):
    """Building is free to ask for and costs a read of the index plus a thing
    held in memory naming coins, so an account cannot have an unlimited
    number of asks outstanding. This is §6's "one active run at a time"."""
    app, state = client
    _open(app)
    _coin(app, state, MAIN, "main")
    for _ in range(accountslib.OFFERS_WAITING):
        assert _send(app).status_code == 200
    over = _send(app)
    assert over.status_code == 400
    assert "other offers" in over.json()["detail"], over.json()
    assert _row(app, "send")["used"] == accountslib.OFFERS_WAITING


# --- the operator's page -----------------------------------------------------

FIELDS = {"post": "30", "react": "60", "message": "30", "send": "30",
          "listings": "30", "claims": "5", "inscribe": "10", "issue": "10",
          "payload": "1000"}


def test_the_operators_page_moves_the_next_action(client):
    """Not the next restart: a door an operator can only close by editing
    code and restart is a door they will not close in time."""
    app, state = client
    saved = app.post("/settings/quotas", follow_redirects=False,
                     data={"csrf_token": state.csrf_token,
                           **FIELDS | {"post": "7"}})
    assert saved.status_code == 303, saved.text
    said = accountslib.limits(state.settings())
    assert said["hour"]["post"] == 7 and said["bytes"] == 1000
    _open(app)
    assert _row(app, "post")["limit"] == 7, \
        "an account's page shows the numbers the operator just wrote"


def test_a_number_that_is_not_a_number_changes_nothing(client):
    app, state = client
    state.set_setting("quota:post", 9)
    bad = app.post("/settings/quotas", follow_redirects=False,
                   data={"csrf_token": state.csrf_token,
                         **FIELDS | {"payload": "a megabyte"}})
    assert bad.status_code == 303
    assert state.notice_kind == "err", state.notice
    assert "megabyte" in state.notice
    assert state.setting("quota:post") == 9
    assert state.setting("quota:send", None) is None, \
        "the six numbers that were fine were not saved on their own"


def test_the_settings_page_is_not_the_public_ones(client):
    """The numbers are the operator's. On a public instance the form is not
    even drawn -- the front page is the splash -- and the route is refused."""
    app, state = client
    state.public = True
    try:
        answer = app.post("/settings/quotas",
                          data={"csrf_token": state.csrf_token, **FIELDS},
                          headers={"host": "127.0.0.1:8420"})
        assert answer.status_code == 404, answer.status_code
    finally:
        state.public = False


# --- the register itself -----------------------------------------------------

@pytest.fixture
def register(tmp_path):
    return accountslib.Accounts(tmp_path / "accounts.sqlite", seats=3)


def test_the_window_rolls_and_the_rows_go_away(register):
    now = 1_800_000_000
    caps = accountslib.limits({"quota:send": 2})
    who = "ab" * 32
    for _ in range(2):
        register.charge(who, "send", caps=caps, now=now)
    assert register.used(who, "send", now=now) == 2
    with pytest.raises(accountslib.AccountError):
        register.charge(who, "send", caps=caps, now=now + 60)
    assert register.used(who, "send", now=now) == 2, "a refusal counted"

    # An hour on, the same two rows are outside the window: the allowance is
    # rolling rather than a page that resets at midnight somewhere.
    assert register.used(who, "send", now=now + accountslib.HOUR + 1) == 0
    register.charge(who, "send", caps=caps, now=now + accountslib.HOUR + 1)

    # And a table of them is not a history anybody can read afterwards: the
    # rows are gone once a day of them says nothing that is still counted.
    register.sweep(now + accountslib.DAY + 10)
    assert register.conn.execute(
        "SELECT COUNT(*) FROM deed WHERE at <= ?",
        (now,)).fetchone()[0] == 0
    assert register.used(who, "send", now=now + accountslib.DAY + 10) == 0


def test_a_number_nobody_typed_is_clamped(register):
    """The page cannot write these; a settings.json opened in an editor can.
    An allowance that goes wrong goes wrong by being small."""
    assert accountslib.limits({"quota:post": -5})["hour"]["post"] == 0
    assert accountslib.limits(
        {"quota:post": "9" * 12})["hour"]["post"] == accountslib.CEILING
    assert accountslib.limits(
        {"quota:post": None})["hour"]["post"] == accountslib.PER_HOUR["post"]
    assert accountslib.limits(
        {"quota:bytes": "lots"})["bytes"] == accountslib.BYTES_PER_DAY
    with pytest.raises(accountslib.AccountError):
        register.charge("ab" * 32, "post",
                        caps=accountslib.limits({"quota:post": -5}))
