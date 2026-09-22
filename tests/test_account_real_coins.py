"""Real coins stay out of reach until somebody proves they can come back.

docs/multi-user.md §1b: mainnet is opt-in per account, behind the mnemonic
confirmation, "so nobody touches real coins before proving they wrote the
words down". An account gets a wallet on both chains the minute it exists
(D-162), so without this gate the first thing a careless person ever does
with real money is move it out of a browser they can never return to.

What the gate is and is not, since the difference is the part worth
testing: the node cannot check the words without knowing them, which every
other decision here exists to prevent (D-155, D-159). So the browser checks
them by deriving the account's own mainnet address out of the words as
typed, and the node is told the answer. These tests are about what the node
then does with it -- refuses a real send, refuses it in a way that says
what to do, refuses it without eating the offer or the account's neighbour's
turn, and never once mentions it while the coins are the free kind.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_me_page import _seat                                   # noqa: E402
from test_funding import _pubkey                                 # noqa: E402
from test_account_mainnet import MAIN, TEST                      # noqa: E402

from arcade.script import b58check_encode, hash160               # noqa: E402

COIN = 100_00000000

#: Somebody else's wallet on each chain, made with this repository's own
#: encoder. Paying your own address is refused for another reason, and this
#: file is not about that reason.
THEIRS_MAIN = b58check_encode(56, hash160(_pubkey(int("11" * 32, 16))))
THEIRS_TEST = b58check_encode(111, hash160(_pubkey(int("22" * 32, 16))))

#: The sentence the page and the refusal are supposed to share. Checked as
#: a fragment, because what matters is that a person is told to go and look
#: at their words rather than being told "forbidden".
WORDS = "twelve words"


def _main_chain_row(app):
    return next(row for row in app.get("/account").json()["chains"]
                if row["mainnet"])


def _fund(app, state, address, value=50 * COIN):
    """Give the mainnet wallet a coin, without needing a mainnet node.

    The point of the UTXO index (§4) is that funding an account is a read
    of rows this node built, not a call into a wallet it does not have --
    so the rows are the only thing a mainnet offer needs. What is NOT here
    is any way for the offer to be broadcast: the chain has no node behind
    it in this file, on purpose.
    """
    index = state.token_index(state.ledger)
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO utxo(txid,vout,address,value,height) "
            "VALUES (?,?,?,?,?)", ("ab" * 32, 0, address, value, 1))
        db.conn.commit()


def _open_both(app):
    """A signed-in account with a wallet on each chain, as signup leaves it."""
    pubkey = _seat(app)
    app.post("/account/address", json={"address": TEST})
    app.post("/account/address", json={"address": MAIN, "chain": "main"})
    return pubkey


def test_a_new_account_cannot_see_mainnet_as_unlocked(client):
    """The page has to know before it offers a send, not after somebody
    signs and reads why it failed."""
    app, _ = client
    _open_both(app)
    row = _main_chain_row(app)
    assert row["locked"] is True
    assert next(one for one in app.get("/account").json()["chains"]
                if not one["mainnet"])["locked"] is False, \
        "the free coins stay exactly as available as they were"


def test_a_real_send_is_refused_in_words_somebody_can_act_on(client):
    """Not "403 forbidden": the answer to "why not" has to be the thing to
    do next, because the person reading it has not written anything down."""
    app, state = client
    _open_both(app)
    _fund(app, state, MAIN)
    offered = app.post("/account/send",
                       json={"to": THEIRS_MAIN,
                             "amount": "1", "chain": "main"})
    assert offered.status_code == 200, offered.text
    done = app.post("/account/sign", json={
        "offer": offered.json()["offer"], "signatures": [], "pubkey": "02" + "aa" * 31})
    assert done.status_code == 400
    assert WORDS in done.json()["detail"], done.json()["detail"]


def test_saying_no_costs_the_offer_nothing(client):
    """A refusal that ate the offer would send the browser away to build the
    whole transaction again after a trip to another page -- and an offer is
    single-use by design, so the second attempt is not free.
    """
    app, state = client
    _open_both(app)
    _fund(app, state, MAIN)
    offered = app.post("/account/send",
                       json={"to": THEIRS_MAIN,
                             "amount": "1", "chain": "main"}).json()
    body = {"offer": offered["offer"], "signatures": [],
            "pubkey": "02" + "aa" * 31}
    first = app.post("/account/sign", json=body)
    assert first.status_code == 400 and WORDS in first.json()["detail"]
    again = app.post("/account/sign", json=body)
    assert WORDS in again.json()["detail"], \
        "the second refusal found the offer, so the first did not consume it"


def test_the_words_are_the_answer_and_not_a_passport(client):
    """Confirming switches it on; refusing to confirm switches on nothing;
    and it is this account's flag and nobody else's."""
    app, state = client
    _open_both(app)
    refused = app.post("/account/mainnet", json={"words_match": False})
    assert refused.status_code == 400
    assert _main_chain_row(app)["locked"] is True, "nothing was changed"

    taken = app.post("/account/mainnet", json={"words_match": True})
    assert taken.status_code == 200, taken.text
    assert taken.json()["mainnet"] is True
    assert _main_chain_row(app)["locked"] is False

    # A second account on the same node has not typed anything.
    _seat(app)
    assert _main_chain_row(app)["locked"] is True, \
        "one account's words are not another account's permission"


def test_no_sentence_about_words_on_a_chain_where_coins_are_free(client):
    """The gate is about money. Testnet sends must not be slowed by it, and
    a testnet failure must not be explained by it."""
    app, _ = client
    _open_both(app)
    broke = app.post("/account/send",
                     json={"to": THEIRS_TEST, "amount": "1",
                           "chain": "regtest"})
    assert broke.status_code == 400, "no coins, which is the true reason"
    assert WORDS not in str(broke.json()), broke.text


def test_the_words_never_reach_the_node(app_state):
    """Not a preference about logging -- the request that switches this on
    carries no words to log, so there is nothing to leak by an access log, a
    crash, or an operator with a debugger. Checked against the route's own
    source because that is where the promise lives.
    """
    source = pathlib.Path("arcade/web/app.py").read_text()
    route = source.split('"/account/mainnet"', 1)[1].split('"/account/', 1)[0]
    assert "words_match" in route
    for word in ("phrase", "mnemonic", "seed", "words\""):
        assert word not in route, f"{word} would mean the words came here"
