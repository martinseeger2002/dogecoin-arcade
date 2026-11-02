"""The terms of use a public node may ask for (arcade/web/terms.py): off by default, asked only of the public, and
never in the way of the node's own machine or network, of Admin, or of anything that is not a browser opening a page."""

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade.web import door, terms                               # noqa: E402

NAME = "node.example.com"
#: What a browser sends when it opens a page at a public name.
PAGE = {"host": NAME, "accept": "text/html,*/*", "sec-fetch-dest": "document", "sec-fetch-site": "none"}


@pytest.fixture
def public(client):
    """A node with a public name, and the switch on."""
    app, state = client
    state.set_setting("public_hosts", [NAME])
    state.set_setting(terms.GATE_SETTING, True)
    app.cookies.clear()
    yield app, state
    app.cookies.clear()
    state.set_setting("public_hosts", [])
    state.set_setting(terms.GATE_SETTING, None)
    state.set_setting(terms.TEXT_SETTING, None)


def _asked(answer):
    return answer.status_code == 303 and answer.headers["location"].startswith("/terms?")


def _accept(app, back="/feed", state=None, host=NAME):
    return app.post("/terms/accept", headers={"host": host}, follow_redirects=False,
                    data={"version": terms.version(terms.text(state)), "back": back})


def test_off_is_the_default_and_nobody_is_asked(client):
    app, state = client
    state.set_setting("public_hosts", [NAME])
    try:
        assert not terms.on(state)
        assert not _asked(app.get("/feed", headers=PAGE, follow_redirects=False))
        assert app.get("/terms", headers=PAGE).status_code == 404
        assert "terms of use" not in app.get("/feed", headers=PAGE).text
    finally:
        state.set_setting("public_hosts", [])


def test_a_visitor_is_sent_to_the_terms_and_back_to_the_page_they_asked_for(public):
    app, state = public
    first = app.get("/feed?tab=new", headers=PAGE, follow_redirects=False)
    assert _asked(first)
    assert first.headers["cache-control"] == "no-store"
    page = app.get(first.headers["location"], headers=PAGE)
    assert page.status_code == 200
    assert "Terms of use" in page.text and "Accept" in page.text and "Leave" in page.text
    assert 'value="/feed?tab=new"' in page.text
    assert page.headers["cache-control"] == "no-store"
    answer = _accept(app, "/feed?tab=new", state)
    assert answer.status_code == 303 and answer.headers["location"] == "/feed?tab=new"
    cookie = answer.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert not _asked(app.get("/feed", headers=PAGE, follow_redirects=False))
    assert app.get("/feed", headers=PAGE).status_code == 200


def test_the_yes_is_a_secure_cookie_over_https(public):
    app, state = public
    answer = app.post("/terms/accept", headers={"host": NAME, "x-forwarded-proto": "https"}, follow_redirects=False,
                      data={"version": terms.version(terms.text(state)), "back": "/feed"})
    assert "secure" in answer.headers["set-cookie"].lower()


def test_leave_exits_and_takes_back_an_earlier_yes(public):
    app, state = public
    assert app.get("/terms", headers=PAGE).text.count('href="/terms/left"') == 1
    _accept(app, state=state)
    left = app.get("/terms/left", headers=PAGE)
    assert left.status_code == 200 and "You have left" in left.text
    assert "<form" not in left.text
    assert _asked(app.get("/feed", headers=PAGE, follow_redirects=False))


def test_changed_words_ask_again(public):
    app, state = public
    _accept(app, state=state)
    assert not _asked(app.get("/feed", headers=PAGE, follow_redirects=False))
    state.set_setting(terms.TEXT_SETTING, terms.DEFAULT + "\n\nOne more line.")
    assert _asked(app.get("/feed", headers=PAGE, follow_redirects=False))


def test_a_yes_counts_only_for_the_words_on_the_screen(public):
    """The text changed while the visitor read it: they are shown the new words, and hold no yes."""
    app, state = public
    old = terms.version(terms.text(state))
    state.set_setting(terms.TEXT_SETTING, "New words.")
    answer = app.post("/terms/accept", headers={"host": NAME}, data={"version": old, "back": "/feed"},
                      follow_redirects=False)
    assert _asked(answer) and "set-cookie" not in answer.headers


def test_another_site_cannot_say_yes(public):
    app, state = public
    answer = app.post("/terms/accept", headers={"host": NAME, "sec-fetch-site": "cross-site"},
                      data={"version": terms.version(terms.text(state))}, follow_redirects=False)
    assert answer.status_code == 403 and "set-cookie" not in answer.headers


@pytest.mark.parametrize("where", ["https://evil.example/", "//evil.example/", "/\\evil.example", "/\tx", "evil",
                                   "/terms", "/terms/left", ""])
def test_accept_returns_only_to_this_site(where):
    assert terms.safe_back(where) == "/"


def test_accept_keeps_a_whole_address():
    assert terms.safe_back("/play/game?from=https%3A%2F%2Fa.example&hop=1#open=/me") \
        == "/play/game?from=https%3A%2F%2Fa.example&hop=1#open=/me"
    assert terms.safe_back("/inscriptions/1/full?ring=abc") == "/inscriptions/1/full?ring=abc"


def test_this_machine_is_never_asked(public):
    app, _ = public
    assert not _asked(app.get("/feed", headers={**PAGE, "host": "127.0.0.1:8420"}, follow_redirects=False))
    assert not _asked(app.get("/admin", headers={**PAGE, "host": "127.0.0.1:8420"}, follow_redirects=False))


@pytest.mark.parametrize("peer,host,edge,asked", [
    ("10.9.8.20", "10.9.8.25:8421", {}, False),      # the node's own network, by its address
    ("10.0.0.4", "arcade.lan:8421", {}, False),
    ("100.64.0.9", "100.64.0.1:8421", {}, False),          # a carrier-grade or Tailscale address
    ("::ffff:10.9.8.20", "10.9.8.25:8421", {}, False),
    ("127.0.0.1", "127.0.0.1:8421", {}, False),
    ("10.9.8.20", NAME, {}, True),                      # the public name, even from inside
    ("127.0.0.1", NAME, {}, True),                         # a tunnel's connection comes from this machine
    ("127.0.0.1", "unlisted.example.com", {"cf-ray": "x"}, True),   # a tunnel's name nobody listed
    ("10.9.8.2", "10.9.8.25:8421", {"x-forwarded-for": "8.8.8.8"}, True),     # a proxy on the LAN
    ("8.8.8.8", "203.0.113.5:8421", {}, True),             # the open internet, by address
])
def test_who_is_a_public_visitor(peer, host, edge, asked):
    assert door.public_visitor(edge, host, (NAME,), peer=peer) is asked


def test_the_lan_gets_in_with_the_switch_on(public):
    """The whole app, as a LAN machine reaches it: Starlette's test client has no way to name its peer."""
    app, _ = public

    async def from_the_lan(scope, receive, send):
        if scope["type"] == "http":
            scope["client"] = ("10.9.8.20", 50000)
        await app.app(scope, receive, send)

    lan = TestClient(from_the_lan)
    assert not _asked(lan.get("/feed", headers={**PAGE, "host": "10.9.8.25:8421"}, follow_redirects=False))
    assert _asked(lan.get("/feed", headers=PAGE, follow_redirects=False))


def test_admin_is_not_sent_to_the_terms(public):
    """The panel keeps its own password, and an outsider there is refused as before, not offered a yes."""
    app, _ = public
    for path in ("/admin", "/admin/login", "/admin/api/state", "/admin/terms/preview"):
        assert not _asked(app.get(path, headers=PAGE, follow_redirects=False)), path
    assert app.get("/admin/api/state", headers=PAGE).status_code in (401, 403, 404)
    assert app.get("/admin/terms/preview", headers=PAGE).status_code in (401, 403, 404)


@pytest.mark.parametrize("headers", [
    {"accept": "*/*"},                                     # curl, git, another arcade, a link preview
    {"accept": "application/json"},
    {"accept": "*/*", "sec-fetch-dest": "empty"},          # a page's own fetch
    {"accept": "text/html", "sec-fetch-dest": "iframe"},   # a frame: the page around it was asked
    {"accept": "text/html", "sec-fetch-dest": "manifest"},
])
def test_only_a_browser_opening_a_page_is_stopped(public, headers):
    app, _ = public
    for path in ("/account", "/events", "/feed", "/signin/somebody"):
        assert not _asked(app.get(path, headers={"host": NAME, **headers}, follow_redirects=False)), path


def test_a_sandboxed_frame_is_never_stopped_on_a_browser_that_does_not_say_it_is_one(public):
    """A browser with no Sec-Fetch-Dest: the frame has no cookie and could never accept."""
    app, _ = public
    plain = {"host": NAME, "accept": "text/html,*/*"}
    assert _asked(app.get("/feed", headers=plain, follow_redirects=False))
    for path in ("/content/0", "/r/whatever", "/site/name/index.html"):
        assert not _asked(app.get(path, headers=plain, follow_redirects=False)), path


def test_the_pages_name_is_not_asked(public):
    app, state = public
    state.set_setting("pages_host", "pages.example.com")
    try:
        assert not _asked(app.get("/content/0", headers={**PAGE, "host": "pages.example.com"},
                                  follow_redirects=False))
        assert app.get("/terms", headers={**PAGE, "host": "pages.example.com"}).status_code == 404
    finally:
        state.set_setting("pages_host", None)


def test_the_routes_are_on_the_door(public):
    assert door.public_path("/terms") and door.public_path("/terms/left")
    assert door.public_path("/terms/accept", "POST")
    app, state = public
    state.public = True
    try:
        assert app.get("/terms", headers=PAGE).status_code == 200
    finally:
        state.public = False


def test_the_footer_links_the_terms_for_a_visitor_only(public):
    app, state = public
    _accept(app, state=state)
    assert 'href="/terms"' in app.get("/feed", headers=PAGE).text
    assert 'href="/terms"' not in app.get("/feed", headers={"host": "127.0.0.1:8420"}).text


def test_the_page_escapes_the_operators_words(public):
    app, state = public
    state.set_setting(terms.TEXT_SETTING, "# <b>Mine</b>\n\n<script>alert(1)</script>")
    page = app.get("/terms", headers=PAGE).text
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)" in page
    assert "<h2>&lt;b&gt;Mine&lt;/b&gt;</h2>" in page


def test_admin_saves_the_switch_and_the_words(client):
    app, state = client
    write = {"host": "127.0.0.1:8420", "x-arcade-admin": "1"}
    try:
        shown = app.get("/admin/api/state", headers=write).json()["terms"]
        assert shown["on"] is False and shown["own"] is False and "perpetual testing" in shown["text"]

        assert app.post("/admin/api/settings", headers=write,
                        json={"terms_gate": True, "terms_text": "My own\r\nwords."}).status_code == 200
        shown = app.get("/admin/api/state", headers=write).json()["terms"]
        assert shown["on"] is True and shown["own"] is True and shown["text"] == "My own\nwords."
        assert shown["version"] == terms.version("My own\nwords.")
        assert terms.on(state) and terms.text(state) == "My own\nwords."

        preview = app.get("/admin/terms/preview", headers=write)
        assert preview.status_code == 200 and "My own" in preview.text and "disabled" in preview.text

        # Empty, or the template word for word, follows the template again.
        app.post("/admin/api/settings", headers=write, json={"terms_text": ""})
        assert app.get("/admin/api/state", headers=write).json()["terms"]["own"] is False
        app.post("/admin/api/settings", headers=write, json={"terms_text": terms.DEFAULT})
        assert app.get("/admin/api/state", headers=write).json()["terms"]["own"] is False

        too_long = app.post("/admin/api/settings", headers=write, json={"terms_text": "x" * (terms.TEXT_MAX + 1)})
        assert too_long.status_code == 400
    finally:
        state.set_setting(terms.GATE_SETTING, None)
        state.set_setting(terms.TEXT_SETTING, None)


def test_a_stranger_cannot_change_the_terms(public):
    app, state = public
    answer = app.post("/admin/api/settings", headers={"host": NAME, "x-arcade-admin": "1"},
                      json={"terms_gate": False})
    assert answer.status_code in (401, 403, 404)
    assert terms.on(state)


def test_admin_says_the_template_is_not_legal_advice():
    admin = pathlib.Path("arcade/web/templates/admin.html").read_text()
    assert "This text is a template, not legal advice." in admin


@pytest.mark.parametrize("words", [
    "perpetual testing", "as is", "no warranty", "cross-check", "verify them on the chain", "at your own risk",
    "claim no responsibility", "freedom of expression and peer-to-peer speech", "their own speech",
    "financial, investment, tax or legal advice", "irreversible", "laws that apply to you", "18 years old",
    "not endorsed", "no guarantee", "refuse service", "change these terms", "not liable", "defend and cover",
    "Built with care", "describes effort, not a promise or a warranty",
])
def test_the_template_covers_what_it_must(words):
    assert words in terms.DEFAULT


def test_the_template_names_nobody():
    """It ships in a public repository, for every node and every face."""
    lower = terms.DEFAULT.lower()
    for word in ("dogecoin", "pepecoin", "litecoin", "sadfrog"):
        assert word not in lower
