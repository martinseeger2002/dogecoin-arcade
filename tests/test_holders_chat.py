"""Holders' chats (2026-09-25): the node's part is the public list of who
holds an asset, and the switch on the asset's page. The group itself is kept by
the creator's browser (messaging.js startHoldersChat / tendHolderChats)."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_me_page import EDGE, public                           # noqa: F401,E402


def test_the_holders_list_answers_even_with_nothing_indexed(client):
    app, _ = client
    assert app.get("/r/holders?token=5").json() == {"holders": []}
    assert app.get("/r/holders?creator=nX&collection=Y").json() == {"holders": []}


def test_a_holders_call_that_names_nobody_says_so(client):
    """An empty list is the answer "nobody holds this", so it cannot also be the
    answer to a call that never said what it was asking about (2026-10-05). It
    read as a broken route to an outside caller, and it is the reading the
    creator's browser ACTS on: holders that come back empty move the holders'
    chat to a new key without them. `ids` is what the token routes beside this
    one use, so the guess itself is taken as `token` rather than refused."""
    app, _ = client
    assert app.get("/r/holders?ids=5").json() == {"holders": []}
    for wrong in ("/r/holders", "/r/holders?ids=5,6", "/r/holders?ids=notanid",
                  "/r/holders?creator=nX"):
        said = app.get(wrong)
        assert said.status_code == 404, wrong
        assert said.json()["error"], wrong


def test_a_node_that_cannot_list_holders_says_so(client, monkeypatch):
    """The exception used to fall through to the same empty list. A node that is
    mid-reindex is not a token that everybody sold, and the difference is what
    the browser does with it."""
    app, _ = client
    from arcade import ledger

    def no_index(self, property_id):
        raise RuntimeError("the index is not ready")

    monkeypatch.setattr(ledger.LedgerIndex, "holders", no_index)
    said = app.get("/r/holders?token=5")
    assert said.status_code == 503
    assert said.json() == {"error": "this node could not list holders"}


def test_the_messages_page_keeps_the_chats_in_step(public):
    app, _ = public
    from test_me_page import _seat
    _seat(app)
    page = app.get("/me/messages", headers=EDGE).text
    assert "mail.tendHolderChats(keys, me)" in page


def test_the_collection_page_itself_carries_the_switch(public):
    """/collections/<creator>/<set> as well as the market page (filming, 2026-09-26).

    The switch is drawn on the PUBLIC copy (2026-09-28: it is drawn from the
    public door, not from a state flag a test can wave), so this asks as an
    edge request and the set is indexed where the pages look -- mainnet.
    """
    app, state = public
    (state.home / "tokens-chain").write_text("main\n")
    state._token_chain = None
    from test_collection_web import index_with_a_collection
    index_with_a_collection(state.home)
    page = app.get("/collections/nMe/Doge%20Punks", headers=EDGE).text
    assert "Doge Punks" in page
    assert 'id="hc-box"' in page, "the holders' chat switch is on the collection page"
