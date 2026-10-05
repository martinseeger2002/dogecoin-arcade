"""The creator allowance (2026-10-04): what an account's page asks it to send says
whose game the page is and whose the asset is, so the reader's tab can let a game's
own asset go back to the game without a card -- a piece the page's creator
inscribed, or a token it issued, sent to that creator -- and ask for anything else.
An account's page may now ask to send an inscription, as the operator's could."""

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import node, _seated, _settled, _inscribed, _signed  # noqa: F401,E402


def _ticket(client, page: str) -> str:
    html = client.get(f"/inscriptions/{page}/view").text
    found = re.search(r"/content/" + page + r"\?v=([A-Za-z0-9_\-]+)", html)
    assert found, "the viewer puts a ticket on the frame"
    return found.group(1)


def test_a_page_asks_to_send_its_creators_piece_back_and_says_whose_it_is(node):
    app, state, rpc = node
    maker = _seated(app, state, rpc, 160)
    player = _seated(app, state, rpc, 161)
    mc, msecret, mpub, maddr = maker
    pc, _psecret, _ppub, paddr = player
    game = _inscribed(mc, state, rpc, msecret, mpub, "<p>a game</p>")
    sword = _inscribed(mc, state, rpc, msecret, mpub, "a sword of this game")
    stranger_piece = _inscribed(pc, state, rpc, *player[1:3], "the player's own drawing")
    sent = mc.post("/account/nft/send", json={"piece": sword, "to": paddr})
    assert sent.status_code == 200, sent.text
    assert _signed(mc, msecret, mpub, sent.json()).status_code == 200
    _settled(state, rpc)

    ticket = _ticket(pc, game)
    # The reader is asked only where accounts sign for themselves -- a public
    # arcade -- and a framed page's fetch arrives there from the pages host.
    state.set_setting("pages_host", "pages.example")
    was, state.public = state.public, True
    try:
        _asks(pc, game, ticket, sword, stranger_piece, maddr, paddr)
    finally:
        state.public = was
        state.set_setting("pages_host", None)


def _asks(pc, game, ticket, sword, stranger_piece, maddr, paddr):
    ref = {"host": "pages.example", "cf-ray": "abc",
           "referer": f"https://pages.example/content/{game}?v={ticket}"}
    back = pc.post("/r/send", headers=ref, json={"kind": "inscription", "inscription": sword,
                                                 "to": maddr, "label": "a game", "silent": True})
    assert back.status_code == 202, back.text
    mine = pc.post("/r/send", headers=ref, json={"kind": "inscription",
                                                 "inscription": stranger_piece, "to": maddr})
    assert mine.status_code == 202, mine.text
    notyours = pc.post("/r/send", headers=ref, json={"kind": "inscription", "inscription": game,
                                                     "to": maddr})
    assert notyours.status_code == 400, "a piece the reader does not hold is refused at once"

    asks = {r["inscription"]: r for r in pc.get("/account/pagesends").json()["requests"]}
    a = asks[sword]
    assert (a["page"], a["creator"], a["to_address"], a["made_by"]) == (game, maddr, maddr, maddr), a
    assert a["kind"] == "inscription" and a["name"].startswith("#")
    assert a["silent"] is True, "a page that never raises a card says so"
    b = asks[stranger_piece]
    assert b["made_by"] == paddr != b["creator"], "the player's own piece is not the game's"
    assert b["silent"] is False

    # Without a ticket for that page the node does not know whose game it is.
    bare = pc.post("/r/send", json={"kind": "inscription", "inscription": sword, "to": maddr},
                   headers={"host": "pages.example", "cf-ray": "abc"})
    assert bare.status_code == 403, bare.text
