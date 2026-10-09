"""Real coins are an account's own from the start; backing up is a notice.

docs/multi-user.md §1b used to put mainnet behind the mnemonic
confirmation. 2026-10-08, after an account of his own had real coins
it could not move: "don't require them to enter their 12 words in order to
enable main chain transactions, but allow them to back them up at their
leisure. If they don't back them up, that's on them give them a notice."

So: no send is refused for the words, the pages say when they were never
typed back, and the check that takes the notice away still happens in the
browser -- the node is told the answer and never the words (D-155, D-159).
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


def test_a_new_account_is_told_it_has_not_backed_up(client):
    """The notice needs to know before the person sends anything."""
    app, _ = client
    _open_both(app)
    assert all(row["backed_up"] is False
               for row in app.get("/account").json()["chains"])
    assert "locked" not in _main_chain_row(app), "nothing is locked any more"


def test_a_real_send_is_not_refused_for_the_words(client):
    """A real send goes as far as its signatures: these are junk, so it
    fails -- and not because the words were never typed back."""
    app, state = client
    _open_both(app)
    _fund(app, state, MAIN)
    offered = app.post("/account/send",
                       json={"to": THEIRS_MAIN,
                             "amount": "1", "chain": "main"})
    assert offered.status_code == 200, offered.text
    done = app.post("/account/sign", json={
        "offer": offered.json()["offer"], "signatures": [], "pubkey": "02" + "aa" * 31})
    assert WORDS not in str(done.json()), done.text


def test_typing_the_words_back_takes_the_notice_away(client):
    """Right words mark it backed up; wrong ones change nothing; and it is
    this account's flag and nobody else's."""
    app, state = client
    _open_both(app)
    refused = app.post("/account/mainnet", json={"words_match": False})
    assert refused.status_code == 400
    assert _main_chain_row(app)["backed_up"] is False, "nothing was changed"
    assert "Show my twelve words" in app.get("/me/backup").text
    assert "not backed up your twelve words" in app.get("/me/backup").text

    taken = app.post("/account/mainnet", json={"words_match": True})
    assert taken.status_code == 200, taken.text
    assert taken.json()["backed_up"] is True
    assert _main_chain_row(app)["backed_up"] is True
    page = app.get("/me/backup").text
    assert "not backed up your twelve words" not in page and "Backed up" in page

    # A second account on the same node has not typed anything.
    _seat(app)
    assert _main_chain_row(app)["backed_up"] is False, \
        "one account's words are not another account's backup"


def test_the_words_page_asks_for_the_password_itself(client):
    """Shown only after the password is typed on the Backup page, never from
    the wallet already open in the tab (2026-10-08: "Make sure you have to
    enter the password a second time to view the 12 words")."""
    app, _ = client
    _open_both(app)
    page = app.get("/me/backup").text
    script = page.split('$("show-words").onclick', 1)[1].split("};\n", 1)[0]
    assert 'wallet.open(sealed, password)' in script
    assert '$("words-pw").value' in script
    assert "opened(" not in script and "sessionStorage" not in script, \
        "an unlocked tab must not be a way to the words"
    assert "fetch(\"/account" not in script, "the words are never sent"


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
