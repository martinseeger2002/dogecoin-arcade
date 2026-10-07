"""arcade.contacts (2026-10-06, for ASHVALE's Atlas): a page asks the viewer for the
signed-in player's address book, read-only. The script is served like storage.js;
the viewer's door answers [] to a guest and asks the person once per game."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                                 # noqa: F401,E402


def test_the_page_script_is_served_like_the_other_page_scripts(client):
    app, state = client
    r = app.get("/r/contacts.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers.get("access-control-allow-origin") == "*"
    assert "arcade.contacts" in r.text and "op: 'list'" in r.text


def test_the_door_is_read_only_asks_once_and_gives_a_guest_nothing():
    door = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates/_page_doors.js.html").read_text()
    body = door[door.index("function contactsDoor"):door.index("window.addEventListener('message'")]
    assert "contacts are read-only" in body
    assert "viewer !== 'account'" in body and "contacts: []" in body, "a guest gets an empty list"
    assert "arcadeAsk('Let this game see your contacts?'" in body, "asked, not handed over"
    assert "'arcade.contacts.allow:' + here" in body, "remembered per game"
    assert "{address: c.address, tag: c.tag || ''}" in body, "names and addresses, nothing else"
    assert "contacts: 'viewer'" in door, "registered, or the message is dropped"
