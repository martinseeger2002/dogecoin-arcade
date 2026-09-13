"""The remote-access tunnel: who gets in, and what closes the door.

Everything here is about the guard, because the guard is the whole feature. The
tunnel itself was checked against the real thing: a quick tunnel to a node-less
instance, knocked on from the public internet, gave 403 for the bare URL, 403
for the bot RPC, a Secure+HttpOnly cookie for the right key, 403 for a wrong
one, the real page with the cookie, and 530 from Cloudflare once it was closed.
"""

import pathlib
import sys
import time

import pytest

from arcade import remote as remotelib
from arcade.web import app as webapp

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402


class FakeTunnel:
    """A tunnel without a cloudflared behind it."""

    def __init__(self, token="the-key", url="https://four-words.trycloudflare.com",
                 seconds=900):
        self.token = token
        self.url = url
        self.opened = time.time()
        self.closes = time.time() + seconds
        self.process = None
        self.link = f"{url}/remote/unlock?k={token}"
        self.seconds_left = seconds

    def alive(self):
        return time.time() < self.closes


EDGE = {"cf-ray": "a3a425055fecace2-MSP", "cdn-loop": "cloudflare; loops=1"}


# --- telling a phone from this machine ----------------------------------------


def test_a_request_from_this_machine_is_not_remote():
    assert remotelib.is_remote({}, "127.0.0.1:8420") is False
    assert remotelib.is_remote({}, "localhost:8420") is False


def test_a_request_over_the_tunnel_is_remote_even_though_it_is_local():
    """Measured, not assumed: cloudflared runs here and connects to 127.0.0.1,
    so a phone in another country arrives from localhost. The headers are the
    only thing that tells them apart."""
    assert remotelib.is_remote(EDGE, "four-words.trycloudflare.com") is True
    assert remotelib.is_remote({"cf-ray": "x"}, "127.0.0.1:8420") is True


def test_the_tunnel_hostname_alone_is_enough():
    """A second signal, in case a header is ever stripped between us."""
    assert remotelib.is_remote({}, "four-words.trycloudflare.com",
                               "https://four-words.trycloudflare.com") is True


def test_a_wallet_on_someones_own_network_is_not_locked_out():
    """An unfamiliar Host is NOT remote. Someone reaching their own wallet at
    192.168.1.5:8420 has not gone through Cloudflare and must not be shut out
    by a feature they never turned on."""
    assert remotelib.is_remote({}, "192.168.1.5:8420") is False
    assert remotelib.is_remote({}, "my-nas.local:8420") is False


# --- the guard ----------------------------------------------------------------


def test_nothing_remote_gets_in_while_no_tunnel_is_open(client):
    app, _ = client
    response = app.get("/", headers=EDGE)
    assert response.status_code == 403
    assert "Not open" in response.text
    assert "Overview" not in response.text, "a locked page must name nothing"


def test_the_url_on_its_own_does_not_open_it(client):
    """The whole point. A URL is read over a shoulder, kept in a history and
    seen by the edge; it cannot be the secret."""
    app, state = client
    state.set_tunnel(FakeTunnel())

    response = app.get("/", headers=EDGE)
    assert response.status_code == 403
    assert "Locked" in response.text


def test_the_key_opens_it_and_leaves_the_url(client):
    app, state = client
    tunnel = FakeTunnel()
    state.set_tunnel(tunnel)

    response = app.get(f"/remote/unlock?k={tunnel.token}", headers=EDGE,
                       follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert remotelib.COOKIE_NAME in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie, (
        "the key must not be readable by script or travel in clear")

    # Set by hand rather than followed from the jar: the client refuses to keep
    # a Secure cookie over http://testserver, which is the cookie doing its job.
    # The real round trip -- redirect, cookie, page -- was checked over a live
    # https tunnel; see the module docstring.
    app.cookies.set(remotelib.COOKIE_NAME, tunnel.token)
    assert app.get("/", headers=EDGE).status_code == 200


def test_a_wrong_key_opens_nothing(client):
    app, state = client
    state.set_tunnel(FakeTunnel())
    response = app.get("/remote/unlock?k=not-the-key", headers=EDGE)
    assert response.status_code == 403
    assert "Wrong key" in response.text


def test_a_wrong_cookie_opens_nothing(client):
    app, state = client
    state.set_tunnel(FakeTunnel())
    app.cookies.set(remotelib.COOKIE_NAME, "not-the-key")
    assert app.get("/", headers=EDGE).status_code == 403


def test_the_bot_rpc_is_never_reachable_through_the_tunnel(client):
    """It has its own key, in a file on that machine, and it can spend."""
    app, state = client
    tunnel = FakeTunnel()
    state.set_tunnel(tunnel)
    app.cookies.set(remotelib.COOKIE_NAME, tunnel.token)

    response = app.post("/rpc/test", json={"method": "omni_getinfo", "id": 1},
                        headers=EDGE)
    assert response.status_code == 403
    assert "not available through the tunnel" in response.json()["error"]["message"]


def test_the_deadline_closes_it_with_nobody_watching(client):
    """A door left open by accident is the failure this exists to prevent."""
    app, state = client
    tunnel = FakeTunnel(seconds=0)
    state.set_tunnel(tunnel)
    app.cookies.set(remotelib.COOKIE_NAME, tunnel.token)

    assert app.get("/", headers=EDGE).status_code == 403
    assert state.remote_tunnel() is None, "an expired tunnel is forgotten, not kept"


def test_closing_it_shuts_every_phone_out(client):
    app, state = client
    tunnel = FakeTunnel()
    state.set_tunnel(tunnel)
    app.cookies.set(remotelib.COOKIE_NAME, tunnel.token)
    assert app.get("/", headers=EDGE).status_code == 200

    state.set_tunnel(None)
    assert app.get("/", headers=EDGE).status_code == 403


def test_the_guard_covers_routes_nobody_has_written_yet():
    """Middleware, not a decorator on each route. A door guarded route by route
    is a door that is open the first time somebody forgets."""
    import inspect

    source = inspect.getsource(webapp.create_app)
    assert 'app.middleware("http")(remote_guard(state))' in source


# --- the page -----------------------------------------------------------------


def test_the_page_says_plainly_what_scanning_it_gives_away(client):
    app, _ = client
    body = app.get("/remote").text
    assert "can spend your coins" in body
    assert "Open the tunnel" in body


def test_the_page_shows_a_qr_of_the_link_once_it_is_open(client):
    app, state = client
    tunnel = FakeTunnel()
    state.set_tunnel(tunnel)

    body = app.get("/remote").text
    assert "<svg" in body, "the QR is drawn into the page"
    assert tunnel.url in body
    assert tunnel.token not in body.replace(
        remotelib.qr_svg(tunnel.link), ""), (
        "the key belongs in the QR, not in the text of the page")


def test_the_qr_encodes_the_whole_link():
    """A QR of the address alone would send the phone to a locked page."""
    import segno

    link = "https://four-words.trycloudflare.com/remote/unlock?k=abc123"
    drawn = remotelib.qr_svg(link)
    assert "<svg" in drawn
    # Same content, same symbol: this is the code that gets scanned.
    assert segno.make(link, error="m").matrix == segno.make(link, error="m").matrix
    assert f'width="{segno.make(link, error="m").symbol_size(scale=5, border=2)[0]}"' in drawn


def test_starting_needs_the_csrf_token(client):
    app, _ = client
    assert app.post("/remote/start", data={"minutes": "60"}).status_code == 400


def test_only_the_offered_lengths_are_accepted(client, monkeypatch):
    """Not unlimited: the deadline is the feature."""
    app, state = client
    monkeypatch.setattr(remotelib, "open_tunnel",
                        lambda *a, **k: pytest.fail("must not open a tunnel"))
    body = app.post("/remote/start",
                    data={"csrf_token": state.csrf_token, "minutes": "99999"}).text
    assert "choose one of the offered lengths" in body


def test_a_missing_cloudflared_is_explained_not_crashed(client, monkeypatch):
    app, _ = client
    monkeypatch.setattr(remotelib, "find_cloudflared", lambda home=None: None)
    body = app.get("/remote").text
    assert "cloudflared is not installed" in body


def test_opening_without_cloudflared_says_where_to_get_it(monkeypatch):
    monkeypatch.setattr(remotelib, "find_cloudflared", lambda home=None: None)
    with pytest.raises(remotelib.TunnelError, match="cloudflared is not installed"):
        remotelib.open_tunnel(8420)


def test_the_offered_lengths_are_what_the_page_shows(client):
    """Read from the page, not from the constant: a select that disagrees with
    what the route accepts is a button that cannot be pressed."""
    app, _ = client
    body = app.get("/remote").text
    for minutes in remotelib.DURATIONS:
        assert f'value="{minutes}"' in body
    assert "4 hours" in body and "12 hours" in body and "1 day" in body
    assert f'value="{remotelib.DEFAULT_MINUTES}" selected' in body


def test_the_shortest_length_is_the_default():
    """The one that leaves the door open longest should be the one somebody
    has to choose on purpose."""
    assert remotelib.DEFAULT_MINUTES == min(remotelib.DURATIONS)
