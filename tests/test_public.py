"""A public arcade: what a stranger may reach, and what they may not.

The whole file is one question asked many ways -- can somebody who is not
The operator get at the operator's wallet -- because that is the question
going live on a real domain asks, and the answer has to be no by
construction rather than by everybody remembering.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402

from arcade.web import door                                      # noqa: E402

LOCAL = {"host": "127.0.0.1:8420"}


@pytest.fixture
def public(client):
    app, state = client
    state.public = True
    yield app, state
    state.public = False


#: Every route that spends, signs, reveals the operator, or holds a key.
#: Named one by one rather than generated, because the list is the claim.
CUSTODIAL = [
    "/wallet", "/wallet/tokens", "/wallet/nfts", "/wallet/receive",
    "/backup", "/messages", "/inbox", "/compose", "/contacts",
    "/approvals", "/approvals/waiting", "/scan", "/publish", "/publish-key",
    "/tokens/create", "/tokens/send", "/tokens/chain",
    "/inscriptions/create", "/inscriptions/collection",
    "/settings/updates", "/setup-identity", "/r/wallet", "/fund",
]


@pytest.mark.parametrize("path", CUSTODIAL)
def test_a_stranger_reaches_nothing_custodial(public, path):
    app, _ = public
    answer = app.get(path, headers=LOCAL)
    assert answer.status_code == 404, f"{path} answered {answer.status_code}"
    assert "Not here" in answer.text


PUBLIC = ["/", "/join", "/feed", "/guide", "/collections", "/tokens",
          "/nfts", "/inscriptions", "/exchange", "/favicon.ico",
          "/icon-32.png", "/signin.js", "/bip39-english.txt", "/events"]


@pytest.mark.parametrize("path", PUBLIC)
def test_the_public_surface_is_served(public, path):
    app, _ = public
    answer = app.get(path, headers=LOCAL)
    assert answer.status_code == 200, f"{path} answered {answer.status_code}"


def test_the_front_page_is_the_splash_not_the_wallet(public):
    """`/` is allowlisted because a public arcade needs a front page. So the
    route itself has to be the safe one, or the allowlist hands out the
    Overview's balances and unread counts."""
    app, _ = public
    body = app.get("/", headers=LOCAL).text
    assert "seats free" in body or "No seats free" in body
    assert "Spendable" not in body
    assert "Known contacts" not in body


def test_every_form_is_refused(public):
    """Every form in this application spends the NODE's wallet, and none of
    it is per-account yet. Signing in is the exception, and it is the only
    one."""
    app, state = public
    for path in ("/feed/post", "/wallet/send", "/tokens/send", "/contacts/save",
                 "/inscriptions/collection/start", "/exchange/offer",
                 "/settings/updates", "/messages/start"):
        answer = app.post(path, data={"csrf_token": state.csrf_token},
                          headers=LOCAL)
        assert answer.status_code == 404, f"{path} answered {answer.status_code}"


def test_the_bot_rpc_is_not_served_publicly(public):
    """It has its own key, in a file beside the node, and it can spend."""
    app, _ = public
    answer = app.post("/rpc/main", json={"method": "getblockcount"},
                      headers=LOCAL)
    assert answer.status_code == 403
    assert "not served publicly" in answer.text


def test_signing_in_is_the_one_post_that_works(public):
    app, _ = public
    assert app.get("/auth/challenge", headers=LOCAL).status_code == 200
    # A bad login is a 403 from the register, not a 404 from the door: the
    # route was reached.
    assert app.post("/auth/login", json={}, headers=LOCAL).status_code == 403


def test_a_session_does_not_open_the_wallet(public):
    """The refusal is about the ROUTE, not the visitor. There is no
    combination of cookies that turns a public instance into a wallet."""
    from nacl.signing import SigningKey

    from arcade import accounts

    app, state = public
    key = SigningKey.generate()
    challenge = app.get("/auth/challenge", headers=LOCAL).json()
    signature = key.sign(accounts.login_message(
        challenge["origin"], challenge["nonce"])).signature
    opened = app.post("/auth/login", headers=LOCAL, json={
        "pubkey": key.verify_key.encode().hex(), "nonce": challenge["nonce"],
        "signature": signature.hex(), "join": True})
    assert opened.status_code == 200, "the seat was taken"
    assert app.get("/auth/who", headers=LOCAL).json()["pubkey"]

    for path in CUSTODIAL:
        assert app.get(path, headers=LOCAL).status_code == 404, path


def test_a_wallet_serves_everything_as_before(client):
    """The default is unchanged: one person's machine, everything theirs."""
    app, _ = client
    for path in ("/wallet", "/messages", "/contacts", "/backup", "/approvals"):
        assert app.get(path).status_code == 200, path


# --- the allowlist itself -----------------------------------------------------

def test_a_route_nobody_named_is_refused():
    """An allowlist, so a route added tomorrow is refused until somebody
    names it. That is a visible one-line mistake; the other way round the
    mistake is somebody's coins."""
    assert not door.public_path("/something/new/tomorrow")
    assert not door.public_path("/wallet/drain", "POST")


def test_the_public_trees_never_learn_to_spend():
    """Each prefix is a promise about a whole subtree. `/tokens/` is public
    and `/tokens/create` is not, so the denials are checked FIRST and do not
    depend on the order two prefixes happen to be listed in."""
    assert door.public_path("/tokens/5")
    assert not door.public_path("/tokens/create")
    assert not door.public_path("/tokens/send")
    assert door.public_path("/r/tag/robin")
    assert not door.public_path("/r/send")
    assert not door.public_path("/r/wallet")


def test_the_pages_door_serves_only_pages():
    assert door.pages_path("/content/abc")
    assert door.pages_path("/r/blockheight")
    assert not door.pages_path("/wallet")
    assert not door.pages_path("/")


# --- what the audit found -----------------------------------------------------

def test_the_wizard_s_preview_is_not_public():
    """It reads a BUILD FOLDER named in the query string -- a path on the
    operator's own disk. It serves only what the build lists, but a stranger
    naming directories and being told which ones look like builds is a
    stranger reading somebody else's filesystem. It was public for one
    afternoon on the grounds that a preview is read-only; read-only of the
    wrong machine."""
    for path in ("/inscriptions/collection/preview",
                 "/inscriptions/collection/preview/piece",
                 "/inscriptions/collection/preview/set",
                 "/inscriptions/collection",
                 "/tokens/launchpad/preview"):
        assert not door.public_path(path), path


def test_the_preview_is_refused_through_the_door(public, tmp_path):
    app, _ = public
    answer = app.get("/inscriptions/collection/preview",
                     params={"folder": str(tmp_path)}, headers=LOCAL)
    assert answer.status_code == 404


def test_every_public_page_links_only_where_a_stranger_may_go(public):
    """A public page pointing at a route a public instance refuses is a site
    broken for exactly the people it is for."""
    import re

    app, _ = public
    bad = []
    for page in ("/", "/join", "/feed", "/docs", "/clone", "/collections"):
        body = app.get(page, headers=LOCAL).text
        for href in set(re.findall(r'href="(/[^"#?]*)', body)):
            probe = href.rstrip("/") or "/"
            if not door.public_path(probe):
                bad.append(f"{page} -> {href}")
    assert bad == [], bad


# --- one machine, two things at once -------------------------------------------

EDGE = {"host": "node.dogecoinarcade.com", "cf-ray": "abc123-LHR"}


@pytest.fixture
def named(client):
    """A wallet that also answers to a public name, which is the shape of
    the machine this is actually run on."""
    app, state = client
    state.set_setting("public_hosts", ["node.dogecoinarcade.com"])
    yield app, state
    state.set_setting("public_hosts", [])


def test_the_operator_keeps_their_wallet_on_their_own_machine(named):
    """A process-wide flag would either lock the operator out of his own wallet or
    serve it to the world. Two processes over one state directory is how a
    database gets two writers. So it is decided per request."""
    app, _ = named
    for path in ("/wallet", "/messages", "/contacts", "/backup"):
        assert app.get(path, headers=LOCAL).status_code == 200, path


def test_the_same_routes_are_refused_from_the_public_name(named):
    app, _ = named
    for path in ("/wallet", "/messages", "/contacts", "/backup"):
        answer = app.get(path, headers={"host": "node.dogecoinarcade.com"})
        assert answer.status_code == 404, path


def test_anything_that_crossed_the_edge_is_public_whatever_it_claims(named):
    """A request carrying Cloudflare's own headers came from outside,
    whatever Host it names -- so a hostname nobody remembered to configure
    still lands on the safe side."""
    app, state = named
    state.set_setting("public_hosts", [])          # nothing configured at all
    assert app.get("/wallet", headers=EDGE).status_code == 404
    assert app.get("/wallet", headers={"host": "x", "cdn-loop": "cloudflare"}
                   ).status_code == 404
    assert app.get("/wallet", headers=LOCAL).status_code == 200


def test_the_front_page_follows_the_request_too(named):
    app, _ = named
    mine = app.get("/", headers=LOCAL).text
    theirs = app.get("/", headers={"host": "node.dogecoinarcade.com"}).text
    assert "Known contacts" in mine, "the operator gets their overview"
    assert "Known contacts" not in theirs, "a stranger gets the splash"
    assert "seats free" in theirs or "No seats free" in theirs


def test_the_navigation_follows_the_request_too(named):
    app, _ = named
    mine = app.get("/feed", headers=LOCAL).text
    theirs = app.get("/feed", headers={"host": "node.dogecoinarcade.com"}).text
    assert 'href="/wallet"' in mine
    assert 'href="/wallet"' not in theirs, "a menu that 404s is worse than no menu"


# --- the one account a node belongs to ----------------------------------------

def _seat(app, headers=LOCAL):
    """Sign in, and return the key."""
    from nacl.signing import SigningKey

    from arcade import accounts

    key = SigningKey.generate()
    challenge = app.get("/auth/challenge", headers=headers).json()
    signature = key.sign(accounts.login_message(
        challenge["origin"], challenge["nonce"])).signature
    answer = app.post("/auth/login", headers=headers, json={
        "pubkey": key.verify_key.encode().hex(), "nonce": challenge["nonce"],
        "signature": signature.hex(), "join": True})
    assert answer.status_code == 200, answer.text
    return key.verify_key.encode().hex()


def test_the_operator_reaches_their_wallet_from_outside(named):
    """Whoever runs a node has to be able to use it from somewhere other
    than the room it is in. One account, not one secret."""
    app, state = named
    mine = _seat(app)
    assert app.get("/wallet", headers=EDGE).status_code == 404, "not yet"

    claimed = app.post("/auth/operator", headers=LOCAL)
    assert claimed.status_code == 200
    assert claimed.json()["operator"] == mine
    assert state.operator == mine

    assert app.get("/wallet", headers=EDGE).status_code == 200
    assert app.get("/messages", headers=EDGE).status_code == 200
    assert app.get("/contacts", headers=EDGE).status_code == 200


def test_the_operator_is_shown_their_own_pages_not_the_splash(named):
    app, _ = named
    _seat(app)
    app.post("/auth/operator", headers=LOCAL)
    body = app.get("/", headers=EDGE).text
    assert "Known contacts" in body, "their overview, not a splash"
    assert 'href="/wallet"' in body, "and their own navigation with it"


def test_somebody_else_s_session_opens_nothing(named):
    """A perfectly good seat is still not this node."""
    app, _ = named
    _seat(app)
    app.post("/auth/operator", headers=LOCAL)
    app.cookies.clear()
    _seat(app, headers=EDGE)                # a stranger, signed in from outside
    assert app.get("/auth/who", headers=EDGE).json()["pubkey"]
    for path in ("/wallet", "/messages", "/contacts", "/backup"):
        assert app.get(path, headers=EDGE).status_code == 404, path


def test_it_can_only_be_claimed_from_the_machine_itself(named):
    """The whole security of it: becoming the operator requires already
    being where the node is."""
    app, state = named
    _seat(app, headers=EDGE)
    # The door gets there first and answers 404, which is the better
    # refusal -- it does not confirm the route exists. The route's own 403
    # is defence in depth for a node where the door is configured
    # differently, and both end the same way: unclaimed.
    refused = app.post("/auth/operator", headers=EDGE)
    assert refused.status_code in (403, 404)
    assert state.operator == ""


def test_a_node_with_an_operator_is_not_taken_over_by_the_next_person(named):
    """A node that hands itself to whoever signs in next is a node anybody
    can take by signing in."""
    app, state = named
    first = _seat(app)
    app.post("/auth/operator", headers=LOCAL)
    app.cookies.clear()
    _seat(app)                               # somebody else, at the machine
    refused = app.post("/auth/operator", headers=LOCAL)
    assert refused.status_code == 403
    assert "already has an operator" in refused.json()["detail"]
    assert state.operator == first


def test_no_operator_means_no_way_in_from_outside(named):
    """The default. A node nobody has claimed serves the public surface to
    everybody, including whoever is standing next to it."""
    app, state = named
    assert state.operator == ""
    _seat(app, headers=EDGE)
    assert app.get("/wallet", headers=EDGE).status_code == 404


# --- whose name a page shows (D-158) ------------------------------------------

def test_the_feed_never_shows_the_operator_s_name_to_a_visitor(named):
    """Found live: signed in as @gx1, the feed offered "Say something as
    @bigchiefenergy" -- the NODE's tag. A lie about who the reader is, and
    a disclosure about who runs the node."""
    app, state = named
    index = state.token_index(state.messaging)
    with index.open() as db:
        db.conn.execute(
            "INSERT INTO block(height,hash,prev_hash,time,tx_count,processed_at)"
            " VALUES(1,'h','p',0,0,0)")
        db.conn.execute(
            "INSERT INTO tag(tag,address,claimed_txid,block_height,position)"
            " VALUES(?,?,?,?,?)",
            ("bigchiefenergy", "nOperator", "aa" * 32, 1, 0))
        db.conn.commit()
    with state.store() as store:
        store.set_meta(f"identity_address:{state.messaging.network}",
                       "nOperator")

    # A stranger, and then somebody signed in: neither is the operator.
    body = app.get("/feed", headers=EDGE).text
    assert "bigchiefenergy" not in body
    assert "Make an account" in body

    _seat(app)
    body = app.get("/feed", headers=EDGE).text
    assert "bigchiefenergy" not in body, "still not the operator's name"


def test_an_account_is_greeted_by_its_own_name(named):
    app, state = named
    pubkey = _seat(app)
    state.vault().put("gx1", pubkey, "nTheirs", '{"sealed":"00"}')
    body = app.get("/feed", headers=EDGE).text
    assert "@gx1" in body
    assert 'href="/me"' in body, "and told where posting happens"


def test_the_operator_still_sees_their_own_composer(named):
    """On the machine itself nothing changes: it is their feed and their
    node and their name on it."""
    app, state = named
    index = state.token_index(state.messaging)
    with index.open() as db:
        db.conn.execute(
            "INSERT OR IGNORE INTO block(height,hash,prev_hash,time,tx_count,"
            "processed_at) VALUES(1,'h','p',0,0,0)")
        db.conn.execute(
            "INSERT OR REPLACE INTO tag(tag,address,claimed_txid,"
            "block_height,position) VALUES(?,?,?,?,?)",
            ("bigchiefenergy", "nOperator", "aa" * 32, 1, 0))
        db.conn.commit()
    with state.store() as store:
        store.set_meta(f"identity_address:{state.messaging.network}",
                       "nOperator")
    body = app.get("/feed", headers=LOCAL).text
    assert "Say something as @bigchiefenergy" in body
    assert 'action="/feed/post"' in body
