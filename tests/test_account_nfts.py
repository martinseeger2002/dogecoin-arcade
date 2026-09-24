"""What an account holds, and handing one on.

The operator: "Then add NFT's to Doge arcade Web."

The pieces themselves were always public -- `/nfts`, a collection's page,
the Exchange -- and an account could look at all of it. What it could not
do was see what IT holds, or send one. Both of those are questions about
an address, and an account's address is one the node watches but holds no
key for, so the answer comes out of the same index and the signing happens
in the browser like everything else.

A transfer is the one send that cannot be undone by sending it back, so
what is tested hardest here is the refusing: somebody else's piece, a
recipient on the wrong chain, and a piece that is not there at all.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_me_page import _seat, public, EDGE            # noqa: F401,E402

MAIN = "PiNoqXu5dWsaEhiuoWCHwsZFB1e2CJBNmD"
TEST = "mqxyzWHvgSMmDYPg9aWpcmXWnkouLUDbWg"
SOMEBODY = "mfvqxFCtMc4sgdgQvBEGnMEVYpiQMqBjE9"


def _inscribed(state, owner, *, number=1, txid=None):
    """One inscription in the index, held by `owner`."""
    chain = state.messaging
    index = state.token_index(chain)
    txid = txid or ("11" * 32)
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO inscription"
            "(txid, number, creator, owner, block_height, position, "
            " content_type, content_len, sha256, json, chunks) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,1)",
            (txid, number, owner, owner, 100, 0, "image/png", 1234,
             "00" * 32, "{}"))
        db.conn.commit()
    return txid


def test_an_account_sees_what_it_holds(client):
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    txid = _inscribed(state, TEST, number=7)
    said = app.get("/account/nfts").json()
    pieces = [p for chain in said["chains"] for p in chain["pieces"]]
    assert [p["number"] for p in pieces] == [7]
    assert pieces[0]["txid"] == txid
    assert pieces[0]["content_type"] == "image/png"


def test_it_does_not_see_somebody_elses(client):
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    _inscribed(state, SOMEBODY, number=9, txid="22" * 32)
    said = app.get("/account/nfts").json()
    pieces = [p for chain in said["chains"] for p in chain["pieces"]]
    assert pieces == []


def test_a_piece_that_is_not_yours_cannot_be_sent(client):
    """Ownership is the chain's answer, not a hidden field's."""
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    other = _inscribed(state, SOMEBODY, number=9, txid="33" * 32)
    answer = app.post("/account/nft/send",
                      json={"piece": other, "to": SOMEBODY})
    assert answer.status_code == 400
    assert "not this account's to send" in answer.json()["detail"]


def test_a_piece_nobody_has_heard_of_is_refused(client):
    app, _ = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    answer = app.post("/account/nft/send",
                      json={"piece": "44" * 32, "to": SOMEBODY})
    assert answer.status_code == 400
    assert "no such inscription" in answer.json()["detail"]


def test_it_will_not_send_to_an_address_on_the_other_chain(client):
    """A mainnet address in the testnet field is one version byte away
    from a piece going somewhere nobody can reach."""
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    mine = _inscribed(state, TEST, number=11, txid="55" * 32)
    answer = app.post("/account/nft/send", json={"piece": mine, "to": MAIN})
    assert answer.status_code == 400
    assert "not a testnet one" in answer.json()["detail"]


def test_it_will_not_send_a_piece_to_itself(client):
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    mine = _inscribed(state, TEST, number=12, txid="66" * 32)
    answer = app.post("/account/nft/send", json={"piece": mine, "to": TEST})
    assert answer.status_code == 400
    assert "own address" in answer.json()["detail"]


def test_the_page_is_there_and_in_the_account_menu(public):
    """The account menu, which is what an account sees: on the machine
    itself the operator gets their own tabs instead."""
    app, _ = public
    _seat(app)
    body = app.get("/me/nfts", headers=EDGE).text
    # The same title and tab bar as the wallet's own NFTs tab.
    assert "<h1>Wallet</h1>" in body
    assert 'href="/me/nfts"' in body, "and is reachable from the menu"
    assert 'href="/me/wallet"' in body and 'href="/me/wallet/tokens"' in body
    # It says what an account cannot do rather than leaving a missing
    # button to be discovered. One piece is no longer that sentence, and
    # neither is a whole collection -- so the page names the thing that is
    # left, which is an item too big for one transaction.
    assert "does not fit in one transaction" in body
    assert "Run a collection" in body


def test_the_public_nfts_page_offers_the_account_s_inscribe(public):
    """The bug the operator met on test.dogecoinarcade.com on 2026-09-24, said as an
    assertion. `/nfts` carried the NODE's inscribe -- a link into the collection
    wizard and two forms that post to `/inscriptions/create` -- and the door
    shuts all three on a public instance, so an account that pressed "Inscribe"
    or "Inscribe a collection" was told "Not here".

    What it gets now is what the account's own page has, drawn from the same
    file: one piece and a whole collection, each offered unsigned by this node
    and signed in the tab. Nothing here spends this node's wallet, so there is
    nothing here for the door to refuse.
    """
    app, _ = public
    _seat(app)
    app.post("/account/address", json={"address": TEST}, headers=EDGE)
    body = app.get("/nfts", headers=EDGE).text
    assert 'id="inscribe-it"' in body and 'id="run-start"' in body
    assert "/account/inscribe" in body and "/account/run/start" in body
    for refused in ('action="/inscriptions/create"',
                    'href="/inscriptions/collection"',
                    'href="/wallet/nfts"'):
        assert refused not in body, f"{refused} is a route the door shuts"


def test_the_public_nfts_page_says_who_can_inscribe(public):
    """The same page to somebody with no seat: no controls, and the sentence
    that says where the controls come from -- not a form that would refuse on
    the next press.
    """
    app, _ = public
    body = app.get("/nfts", headers=EDGE).text
    assert 'id="inscribe-it"' not in body
    assert "/account/inscribe" not in body
    for refused in ('action="/inscriptions/create"',
                    'href="/inscriptions/collection"'):
        assert refused not in body, f"{refused} is a route the door shuts"
    assert "Sign in and open your wallet" in body


def test_the_run_list_is_asked_the_way_the_route_is_built(client):
    """A wrong method is a refused question that nobody hears.

    `/account/run` is registered as a post and `door.py` lets only a post
    through it. Asked as a GET -- which is what this page's `runs()` did, in
    `my_nfts.html` and then in the file it moved into -- a public instance
    answers with the door's "Not here" page, the JSON parse throws on the HTML,
    and the catch files it as a line in the trouble row that nobody reads on a
    page that worked. So the sentence printed above the button -- that a closed
    tab loses a run nothing, and the run is there at the piece it stopped on --
    was false on every reload: the next piece was never offered again. Nothing
    in the suite reloaded a page with a run in progress, so nothing knew.
    """
    import test_public

    app, _ = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    assert app.get("/account/run").status_code != 200, "this route is a post"
    assert "runs" in app.post("/account/run").json()
    shared = pathlib.Path("arcade/web/templates"
                          "/_account_inscribe.html").read_text()
    asked = [one for one in test_public._asked(shared)
             if one[0] == "/account/run"]
    assert asked == [("/account/run", "POST")], asked


def test_the_inscribe_panels_are_one_file(client):
    """Two pages offer them, so there is one copy of the code that spends.

    Not a style rule: the last time this pair of controls was written twice,
    one of the two went on answering 403 for a month.
    """
    where = pathlib.Path("arcade/web/templates")
    shared = (where / "_account_inscribe.html").read_text()
    for name in ("my_nfts.html", "inscriptions.html"):
        page = (where / name).read_text()
        assert '{% include "_account_inscribe.html" %}' in page, name
        assert "/account/inscribe" not in page, \
            f"{name} has its own copy of the inscribe offer"
    for route in ("/account/inscribe", "/account/run/start",
                  "/account/run/piece", "/account/run/stop"):
        assert route in shared, route


def test_a_stranger_is_sent_to_sign_up(client):
    app, _ = client
    answer = app.get("/me/nfts", follow_redirects=False)
    assert answer.status_code == 303
    assert answer.headers["location"] == "/join"


def test_the_page_never_renders_a_piece_as_markup(client):
    """A content type, a collection name and a price all come off the
    chain, which means somebody else wrote them."""
    page = pathlib.Path("arcade/web/templates/my_nfts.html").read_text()
    uses = [line.strip() for line in page.splitlines()
            if "innerHTML" in line and not line.strip().startswith("//")]
    assert uses == ['$("chains").innerHTML = "";'], uses


def test_only_the_holder_can_price_a_piece(client):
    """An ask is not an escrow: the piece stays where it is and the price
    is honoured only while its owner holds it, so the engine refuses one
    from anybody else and this refuses it before that."""
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    other = _inscribed(state, SOMEBODY, number=21, txid="77" * 32)
    answer = app.post("/account/nft/sell",
                      json={"piece": other, "amount": "10"})
    assert answer.status_code == 400
    assert "only whoever holds a piece can price it" in answer.json()["detail"]


def test_a_price_of_nothing_is_refused(client):
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    mine = _inscribed(state, TEST, number=22, txid="88" * 32)
    answer = app.post("/account/nft/sell", json={"piece": mine, "amount": ""})
    assert answer.status_code == 400


def test_taking_the_price_off_is_the_same_handshake(client):
    """Same route, nothing in the take leg. It gets as far as funding,
    which on a node with no coins for this address is where it stops --
    and that is the right place for it to stop."""
    app, state = client
    _seat(app)
    app.post("/account/address", json={"address": TEST})
    mine = _inscribed(state, TEST, number=23, txid="99" * 32)
    answer = app.post("/account/nft/sell",
                      json={"piece": mine, "unlist": True})
    assert answer.status_code == 400
    said = answer.json()["detail"]
    assert "only whoever holds" not in said
    assert "no such inscription" not in said
