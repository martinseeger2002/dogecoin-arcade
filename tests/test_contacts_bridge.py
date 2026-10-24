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
    assert "{list: list, add: add, remove: remove}" in r.text, "add and remove ride the same channel (2026-10-21)"


def test_the_door_is_read_only_asks_once_and_gives_a_guest_nothing():
    door = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates/_page_doors.js.html").read_text()
    body = door[door.index("function contactsDoor"):door.index("function contactsChange")]
    assert "op is list, add or remove" in body
    assert "viewer !== 'account'" in body and "contacts: []" in body, "a guest gets an empty list"
    assert "arcadeContactsAllowed(here)" in body and "arcadeContactsOn(here, g)" in body, \
        "an app's Contacts switch decides (2026-10-23: a box on the card)"
    assert "return 'off'" in body and "why: yes" in body, "off says why, and asks nothing"
    assert "arcadeContactsAsk(here" in body, "a page with no card is asked, not handed over"
    base = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates/base.html").read_text()
    assert "var ARCADE_CONTACTS = 'arcade.contacts.allow:'" in base, "the old answers' key: a yes stays a yes"
    assert "{address: c.address, tag: c.tag || ''}" in body, "names and addresses, nothing else"
    assert "contacts: 'viewer'" in door, "registered, or the message is dropped"


def test_add_and_remove_write_the_address_books_own_way_and_only_after_a_yes():
    """The browser test (test_contacts_add_browser.py) drives it; this pins the shape where no browser runs."""
    door = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates/_page_doors.js.html").read_text()
    body = door[door.index("function contactsChange"):door.index("window.addEventListener('message'")]
    for write in ("mail.addToBook(", "mail.removeFromBook(", "mail.updateBookEntry("):
        assert write in body, f"{write} is the Address book's own write"
    assert body.count("arcadeAsk(") == 2, "one card for add, one for remove"
    assert body.index("status: 'absent'") < body.index("arcadeAsk(") and \
        body.index("status: 'already'") < body.index("arcadeAsk("), "nothing to change asks nothing"
    assert "if (!yes) throw new Error('declined')" in body
    assert "throw new Error('busy:" in body
    assert "'sign in first:" in body and "!ARCADE_SIGNED" in body
    assert "arcade.contacts.allow" not in body, "a yes to an add is not permission to list()"
