"""DogeFeedX reads and writes through this arcade alone (2026-11-03: "this is meant not to be reliant on any
external API's or external servers or services. Everything needs to work through arcade").

Its look stays DogeFeed's; its data is the arcade's: the feed, collections, the ord index, mintpads, messages,
profiles. These tests keep any other server's address out of its code and pages, check every endpoint answers from
the arcade, and check Home's Post and the reactions go through the arcade's own routes.
"""
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
CODE = [ROOT / "arcade" / "web" / "dogefeedx" / name for name in ("apps.py", "wallet_assets.py", "network.py",
                                                                    "__init__.py")]
PAGES = sorted((ROOT / "arcade" / "web" / "templates" / "dogefeedx").glob("*")) + [
    ROOT / "arcade" / "web" / "templates" / "desktop.html",
    ROOT / "arcade" / "web" / "templates" / "deskdogefeedx_css.css",
]
#: Hosts DogeFeedX used to ask, and any absolute http(s) URL a page might load or fetch.
BANNED = ("dogefeed.app", "dogesoft.io", "doggy.market", "fonts.googleapis.com", "fonts.gstatic.com",
          "dogecoinnames")


def test_no_other_server_is_named_in_dogefeedx_code_or_pages():
    for path in CODE + PAGES:
        if not path.is_file():
            continue
        text = path.read_text()
        for host in BANNED:
            assert host not in text, (path.name, host)
        if path.suffix == ".py":
            assert "import httpx" not in text and "urllib.request" not in text, path.name
    for path in PAGES:
        if not path.is_file():
            continue
        # Nothing fetched, loaded or linked from an absolute address (an <a> to the SVG namespace is not a load).
        for m in re.finditer(r"""(?:src|href|fetch\(|url\()\s*=?\s*["']?(https?://[^"' )]+)""", path.read_text()):
            assert m.group(1).startswith("http://www.w3.org/"), (path.name, m.group(1))


def test_the_public_read_module_is_gone():
    assert not (ROOT / "arcade" / "web" / "dogefeedx" / "public_read.py").exists()


def test_every_endpoint_answers_from_the_arcade(client):
    app, _state = client
    for path in ("/dogefeedx/api/feed", "/dogefeedx/api/sidebar", "/dogefeedx/api/leaderboard",
                 "/dogefeedx/api/market/doginals", "/dogefeedx/api/collections", "/dogefeedx/api/tokens",
                 "/dogefeedx/api/memes", "/dogefeedx/api/launchpad", "/dogefeedx/api/arcade",
                 "/dogefeedx/api/price", "/dogefeedx/api/me", "/dogefeedx/api/search?q=doge",
                 "/dogefeedx/api/name?name=much.doge", "/dogefeedx/api/names?q=much",
                 "/dogefeedx/api/market/drc20", "/dogefeedx/api/users/someone/posts",
                 "/dogefeedx/api/posts/" + "a" * 64):
        r = app.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
        assert r.json().get("source") == "arcade", (path, r.json())


def test_home_posts_and_reacts_through_the_arcades_own_checks(client, monkeypatch):
    """The operator's Post and reactions are /feed/post and /feed/{txid}/{doing} answered in JSON: the same CSRF
    check and the same "a name first" rule, refused in words (a test node has no node to post with, as in
    test_feed_web.test_you_need_a_name_before_you_can_post)."""
    app, state = client
    posted, acted = [], []
    monkeypatch.setattr(state, "post", lambda text: posted.append(text) or "f" * 64, raising=False)
    monkeypatch.setattr(state, "send_feed_act", lambda *a: acted.append(a) or "e" * 64, raising=False)
    token = state.csrf_token
    assert app.post("/dogefeedx/api/post", json={"text": "x", "csrf_token": "nope"}).status_code == 400
    assert app.post("/dogefeedx/api/react", json={"txid": "a" * 64, "doing": "like",
                                                  "csrf_token": "nope"}).status_code == 400
    for path, body in (("/dogefeedx/api/post", {"text": "much wow"}),
                       ("/dogefeedx/api/react", {"txid": "a" * 64, "doing": "like"})):
        r = app.post(path, json=dict(body, csrf_token=token))
        assert r.status_code == 400 and r.json()["ok"] is False
        assert "claim a @tag first" in r.json()["error"] or "waiting for the Pepecoin test node" in r.json()["error"]
    assert posted == [] and acted == []
    bad = app.post("/dogefeedx/api/react", json={"txid": "nothex", "doing": "delete", "csrf_token": token})
    assert bad.status_code == 400 and acted == []


def test_home_messages_and_settings_use_the_arcades_own_modules():
    pages = ROOT / "arcade" / "web" / "templates" / "dogefeedx"
    act = (pages / "_arcade_act.html").read_text()
    assert "wallet.post(" in act and "wallet.react(" in act
    assert "/dogefeedx/api/post" in act and "/dogefeedx/api/react" in act
    for name in ("feed.html", "post.html"):
        assert '{% include "dogefeedx/_arcade_act.html" %}' in (pages / name).read_text(), name
    messages = (pages / "messages.html").read_text()
    assert "/messaging.js" in messages and "/node-mail.js" in messages and "mail.write(" in messages
    assert "dogefeedx.messages" not in messages                    # no stand-in letters kept in the browser
    settings = (pages / "settings.html").read_text()
    assert "mail.publishProfile(" in settings
    feed = (pages / "feed.html").read_text()
    assert "dogefeedx.localBarks" not in feed


# --------------------------------------------------------------------- an account on a public arcade (2026-11-03)
NODE_WATCHED = "DNodeWatchAddress111111111111111X"


def test_the_door_opens_dogefeedx_reads_and_keeps_the_nodes_own_closed():
    """2026-11-03: the DogeFeedX windows for the user end on the public website, as the arcade already is."""
    from arcade.web import door
    desktop = (ROOT / "arcade" / "web" / "templates" / "desktop.html").read_text()
    dock = re.findall(r"url: '(/dogefeedx/[a-z]+)'", desktop)
    assert len(set(dock)) >= 16
    for path in dock + ["/dogefeedx/post", "/dogefeedx/explore", "/dogefeedx/search", "/dogefeedx/inscription",
                        "/dogefeedx/collections", "/dogefeedx/api/feed", "/dogefeedx/api/posts/" + "a" * 64,
                        "/dogefeedx/api/users/someone/posts", "/dogefeedx/api/wallet/holdings",
                        "/dogefeedx/api/me", "/dogefeedx/aqua.css"]:
        assert door.public_path(path), path
    for path in ("/dogefeedx/api/status", "/dogefeedx/api/peers", "/dogefeedx/api/balance",
                 "/dogefeedx/api/address/inscriptions", "/dogefeedx/api/address/tokens", "/dogefeedx/api/lookup",
                 "/dogefeedx/network", "/dogefeedx/network/api/status", "/dogefeedx/terminal"):
        assert not door.public_path(path), path
    for path in ("/dogefeedx/api/post", "/dogefeedx/api/react", "/dogefeedx/network/api/sync/doge"):
        assert not door.public_path(path, "POST"), path


def test_an_account_reads_its_own_wallet_never_the_nodes(client, monkeypatch):
    from arcade.web.dogefeedx import wallet_assets
    app, state = client
    monkeypatch.setattr(wallet_assets, "node_watch_addresses", lambda: [NODE_WATCHED])
    # The operator, on its own machine: the node's watch list.
    assert app.get("/dogefeedx/api/wallet/addresses").json()["addresses"] == [NODE_WATCHED]
    state.public = True
    try:
        assert app.get("/dogefeedx/feed").status_code == 200
        said = app.get("/dogefeedx/api/wallet/addresses").json()
        assert NODE_WATCHED not in said["addresses"] and said["source"] == "account"
        held = app.get("/dogefeedx/api/wallet/holdings").json()
        assert NODE_WATCHED not in json.dumps(held) and "Sign in" in held["empty"]
        other = app.get(f"/dogefeedx/api/wallet/holdings?address={NODE_WATCHED}").json()
        assert other["collections"] == [] and other["addresses"] == [] and other["balance_doge"] is None
        assert app.get("/dogefeedx/api/status").status_code in (403, 404)
        assert app.post("/dogefeedx/api/post", json={"text": "x"}).status_code in (403, 404)
        me = app.get("/dogefeedx/api/me").json()
        assert me["operator"] is False and me["address"] == ""
    finally:
        state.public = False


def test_a_signed_in_account_gets_its_own_address_only(client, monkeypatch):
    from test_account_feed import _sign_in
    from arcade.web.dogefeedx import wallet_assets
    app, state = client
    monkeypatch.setattr(wallet_assets, "node_watch_addresses", lambda: [NODE_WATCHED])
    state.public = True
    try:
        _sign_in(app)
        said = app.get("/dogefeedx/api/wallet/addresses").json()
        assert NODE_WATCHED not in said["addresses"] and len(said["addresses"]) <= 1
        held = app.get("/dogefeedx/api/wallet/holdings").json()
        assert NODE_WATCHED not in json.dumps(held)
        if not said["addresses"]:                                  # a test arcade runs no Dogecoin
            assert "no Dogecoin address" in held["empty"]
        assert app.get("/dogefeedx/api/me").json()["operator"] is False
    finally:
        state.public = False
