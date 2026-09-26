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
    assert app.get("/r/holders").json() == {"holders": []}


def test_the_messages_page_keeps_the_chats_in_step(public):
    app, _ = public
    from test_me_page import _seat
    _seat(app)
    page = app.get("/me/messages", headers=EDGE).text
    assert "mail.tendHolderChats(keys, me)" in page
