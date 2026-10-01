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
    # What one account may do here is the operator's to set, on their own
    # page. A public instance serves the splash, so the form is not even
    # drawn -- and a stranger must not be able to move the numbers either.
    "/settings/quotas",
    # Same for what the node takes of a trade (§1d): it is the operator's
    # number, and it is spent by other people's wallets.
    "/settings/cut",
]


@pytest.mark.parametrize("path", CUSTODIAL)
def test_a_stranger_reaches_nothing_custodial(public, path):
    app, _ = public
    answer = app.get(path, headers=LOCAL)
    assert answer.status_code == 404, f"{path} answered {answer.status_code}"
    assert "Not here" in answer.text


PUBLIC = ["/", "/join", "/feed", "/guide", "/collections", "/tokens",
          "/nfts", "/inscriptions", "/exchange", "/listings", "/favicon.ico",
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
    answer = app.get("/", headers=LOCAL)
    # Since 2026-09-27 the public front page is the feed (Robin: "When the
    # arcade app opens, it should be open to the feed tab"). The point of this
    # test stands: whatever `/` shows a stranger, it is not the wallet.
    assert str(answer.url).endswith("/feed")
    body = answer.text
    assert "Spendable" not in body
    assert "Known contacts" not in body


def test_every_form_is_refused(public):
    """Every form in this application spends the NODE's wallet, and none of
    it is per-account yet. Signing in is the exception, and it is the only
    one."""
    app, state = public
    for path in ("/feed/post", "/wallet/send", "/tokens/send", "/contacts/save",
                 "/inscriptions/collection/start", "/exchange/offer",
                 "/settings/updates", "/settings/quotas", "/settings/cut",
                 "/messages/start"):
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


#: Every public page, drawn as a stranger sees it and as an account sees it.
#: The second drawing is the one that let the 2026-09-24 bug out: `/nfts`
#: offers an account different controls than it offers a stranger, and a page
#: checked only as a stranger is a page that was never looked at as the person
#: the fix was for. `/u/x` and `/tokens/5` are here because a page under a
#: public tree is a public page, whatever the tree was written for.
PUBLIC_PAGE = ["/", "/join", "/feed", "/guide", "/docs", "/clone",
               "/collections", "/tokens", "/tokens/5", "/nfts", "/inscriptions",
               "/exchange", "/listings", "/u/x",
               "/me", "/me/nfts", "/me/runs", "/me/wallet",
               "/me/wallet/tokens", "/me/messages", "/me/contacts",
               "/me/backup"]

#: A form that names a route the door shuts because the tab answers it rather
#: than submitting it. `feed.html` keeps the operator's form markup so the
#: button a person presses is the one that has always been on the page, and
#: intercepts the submission in its own script -- the last test in
#: `test_account_feed.py` is what pins that the listening starts before a press
#: can arrive. This is the whole allowance, and it is a named pair rather than
#: a rule about `/feed/`: a page not listed here may not name a refused route
#: however its script behaves.
ANSWERED_IN_THE_TAB = {"/feed": "/feed/"}


def _attr(name: str, tag: str) -> str:
    import re

    found = re.search(name + r"""\s*=\s*["']([^"']*)["']""", tag, re.I)
    return found.group(1) if found else ""


def _offered(body: str, page: str) -> list[tuple[str, str]]:
    """Every place this page sends the browser, as a browser reads it.

    Links and form targets, and a form with no `action` is read as submitting
    to the page it was served on, which is what a browser does with it. A form
    with no `method` is a GET. Both defaults matter: a form that spends through
    the omission of one word is a form this test would otherwise walk past.
    """
    import re

    out = []
    for href in re.findall(r"""href\s*=\s*["'](/[^"'#?]*)""", body):
        out.append((href, "GET"))
    for tag in re.findall(r"<form\b[^>]*>", body, flags=re.I | re.S):
        action, method = _attr("action", tag), (_attr("method", tag) or "GET")
        target = action if action.startswith("/") else page
        out.append((target.split("?")[0], method.upper() or "GET"))
    return out


def _asked(body: str) -> list[tuple[str, str]]:
    """Every path a page's own script asks for behind the scenes.

    The links-and-forms census is not the whole of what a page offers. A page
    can also go and ask something on load, and a background question the door
    refuses fails without ever being seen: `/account/run` was asked as a GET by
    the NFTs page for as long as it had a run to show, at a route registered as
    a post and let through the door only as a post. The page drew no error the
    reader could act on and simply never found the run it was in the middle of,
    so the promise printed above the button -- that a closed tab loses a run
    nothing -- was false on every reload.

    Only a literal path counts. A `fetch` built out of a variable lives in one
    of the shared modules served at `/wallet.js` and its friends, where it is
    not readable from a rendered page, and those are pinned by their own tests.
    A page's own `post(where, body)` helper counts, with a post as its default,
    because that is what the helper does -- `account_run.html` has one.
    """
    import re

    out = []
    for verb, target, rest in re.findall(
            r"""\b(fetch|post)\(\s*["'`](/[^"'`]*)["'`]([^)]*)""", body):
        if "${" in target:
            continue        # built out of a variable: not a path from here
        if target.endswith("/") and rest.lstrip().startswith("+"):
            # The other spelling of the same thing: `fetch("/content/" + root)`
            # is built out of a variable as surely as the backtick form, and
            # what is literal in it is a whole tree rather than a page. So ask
            # the door about a child of that tree, which is the promise the
            # prefix actually makes -- `/content` is not a page and
            # `/content/<id>` is. A page that went asking for `/wallet/` plus
            # anything is still caught here, which is the point: dropping the
            # concatenation form the way the backtick form is dropped would let
            # a refused route out of this census by spelling its path in two.
            target = target + "-"
        said = re.search(r"""method\s*:\s*["'`]?([A-Za-z]+)""", rest)
        default = "POST" if verb == "post" else "GET"
        out.append((target.split("?")[0], (said.group(1) if said else default)
                    .upper()))
    return out


@pytest.mark.parametrize("page", PUBLIC_PAGE)
@pytest.mark.parametrize("signed_in", [False, True])
def test_every_public_page_links_only_where_a_stranger_may_go(public, page,
                                                              signed_in):
    """A public page pointing at a route a public instance refuses is a site
    broken for exactly the people it is for.

    Robin met this on `/nfts` on 2026-09-24: the page carried the NODE's
    inscribe controls, which spend the node's wallet, and every one of them
    answered "Not here". It is the shape of the bug rather than the page -- any
    public page can carry a control the door shuts -- so this walks every
    public page rather than that one, in both of the ways it can be drawn.
    """
    app, _ = public
    if signed_in:
        _seat(app)
    answer = app.get(page, headers=LOCAL, follow_redirects=False)
    who = "an account" if signed_in else "a stranger"
    if answer.status_code != 200:
        pytest.skip(f"{page} is not a page for {who} "
                    f"(answered {answer.status_code})")
    bad = []
    for target, method in (_offered(answer.text, page)
                           + _asked(answer.text)):
        if target.startswith(ANSWERED_IN_THE_TAB.get(page, "\0")) \
                and method != "GET":
            continue
        probe = target.rstrip("/") or "/"
        if not door.public_path(probe, method):
            bad.append(f"{page} ({who}) {method} -> {target}")
    assert bad == [], bad


#: Somebody's address, for an inscription that has to belong to someone.
KEEPER = "mqxyzWHvgSMmDYPg9aWpcmXWnkouLUDbWg"


def _an_interactive_inscription(state, txid: str) -> str:
    """One inscription whose bytes are HTML and are kept on this node.

    That is the row which makes the piece page take its third branch: `held` is
    `content IS NOT NULL`, and `renders` is the content type starting with
    `text/html`. Only that branch runs the sandbox, and only the sandbox has
    anything to approve.
    """
    index = state.token_index(state.messaging)
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO inscription"
            "(txid, number, creator, owner, block_height, position, "
            " content_type, content_len, sha256, json, chunks, content) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,1,?)",
            (txid, 401, KEEPER, KEEPER, 100, 0, "text/html", 34, "00" * 32,
             "{}", b"<html><body>it asks for things</body></html>"))
        db.conn.commit()
    return txid


def test_an_inscription_page_asks_nothing_the_door_shuts(public):
    """The approval poll ran on a page that can never be answered.

    `inscription_view.html` asks `/approvals/waiting` every three seconds -- the
    queue of transactions asked of THIS node's wallet, which is what lets an
    inscribed page say "ask this wallet to send" and have the person looking at
    it approve or refuse the transaction. The door shuts that route on a public
    instance, so on test.dogecoinarcade.com every stranger who opened an
    interactive inscription polled a 404 three times a minute until they closed
    the tab, with the failure eaten by a `.catch(function(){})` at the end of
    the chain. Nothing was spent and nothing was shown, which is why it was
    never reported: it is the same shape as the `/account/run` mistake -- a page
    asking a question this node will never answer, and a script that cannot tell
    the difference between "no" and "not here".
    """
    app, state = public
    txid = _an_interactive_inscription(state, "ab" * 32)
    body = app.get(f"/inscriptions/{txid}/view", headers=LOCAL).text
    assert "/approvals/waiting" not in body, "a public page polling the node"
    assert "/approvals/" not in body, "and no frame of the node's approval page"
    assert "ask this account to sign" in body, \
        "the sentence says what this reader's page can actually do"
    state.public = False
    mine = app.get(f"/inscriptions/{txid}/view").text
    assert "/approvals/waiting" in mine, "the operator's page still polls it"
    assert 'id="ask"' in mine, "and still carries the dialog it polls for"


def test_no_public_page_offers_a_chain(public):
    """Found by walking every public page rather than one of them.

    The chain tag used to sit in these headings. On a public instance it could
    not be pressed -- `/tokens/chain` moves the NODE's view of which chain it is
    on (`state.switch_token_chain` writes the choice into a file and every page
    on the machine follows it), so the door shuts it -- and an inert button was
    then replaced by an inert tag whose tooltip said this node shows one chain
    at a time. That is still a choice being pointed at, which is what Robin's
    call of 2026-09-24 rules out: a visitor and an account have one chain, the
    one their name and posts live on, and a page says the chain in a sentence
    when the sentence needs it rather than in a chip that hints at another.

    The operator's own pages keep the switch, because on those it is not a
    choice being dangled but the setting of a machine somebody runs.
    """
    app, state = public
    for page in ("/tokens", "/nfts", "/collections", "/exchange"):
        body = app.get(page, headers=LOCAL).text
        assert 'action="/tokens/chain"' not in body, page
        assert "to switch" not in body, page
        assert "one chain at a time" not in body, page
        assert "<h1" in body, page
        heading = body.split("<h1", 2)[1].split("</h1>", 1)[0]
        assert 'class="tag ' not in heading, f"{page}: {heading}"
    state.public = False
    assert 'action="/tokens/chain"' in app.get("/nfts").text, \
        "the operator's own pages still switch"


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
    """A process-wide flag would either lock Robin out of his own wallet or
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
    assert "Known contacts" not in theirs, "a stranger gets the feed, not the overview"


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


def test_the_operator_reaches_the_admin_panel_from_outside(named):
    """Whoever runs a node has to be able to use it from somewhere other than
    the room it is in. Since 2026-09-25 that is their account plus /admin, and
    /admin asks for the admin password -- a session alone opens nothing of the
    node's."""
    app, state = named
    mine = _seat(app)
    claimed = app.post("/auth/operator", headers=LOCAL)
    assert claimed.status_code == 200
    assert claimed.json()["operator"] == mine
    assert state.operator == mine

    assert app.get("/wallet", headers=EDGE).status_code == 404
    assert app.get("/admin/api/state", headers=EDGE).status_code == 401, \
        "through the door, and asked for the password"


def test_the_operator_is_shown_their_account_and_an_admin_tab(named):
    app, _ = named
    _seat(app)
    app.post("/auth/operator", headers=LOCAL)
    body = app.get("/me", headers=EDGE).text
    assert 'href="/admin"' in body
    assert 'href="/wallet"' not in body


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


def test_the_pages_host_never_speaks_for_the_operator_from_outside(client):
    """pages.<domain>/r/wallet handed every stranger the operator's addresses,
    tag and balances, and a page opened by any account greeted it as the
    operator (tester-e5, 2026-09-26). From outside, the page API says the
    wallet is off; the operator's own local pages still see their wallet."""
    app, state = client
    state.set_setting("pages_host", "pages.example")
    try:
        edge = {"host": "pages.example", "cf-ray": "abc"}
        answer = app.get("/r/wallet", headers=edge)
        assert answer.status_code == 403
        assert answer.json()["wallet"] == "off"
        assert "addresses" not in answer.json()
        assert answer.headers.get("access-control-allow-origin") == "*"
        assert app.post("/r/send", json={"to": "x"}, headers=edge).status_code == 403
        assert app.get("/r/send/abc", headers=edge).status_code == 403
        # The rest of the page API is still there for pages.
        assert app.get("/r/blockheight", headers=edge).status_code != 403
        # Here, on the operator's own machine, it is still their wallet.
        local = app.get("/r/wallet", headers={"host": "pages.example"})
        assert local.status_code != 403 or "wallet" not in local.json()
    finally:
        state.set_setting("pages_host", "")


def test_the_page_api_wallet_is_whoever_is_looking(client):
    """/r/wallet answers as the signed-in account viewing the piece, not the
    node (2026-09-26: "it should be showing the user who is viewing
    the NFT's balance, not the node balance"). The frame carries a ticket on
    its URL, which its fetches bring back as their Referer."""
    import re

    from test_me_page import _seat

    app, state = client
    viewer = "mqxyzWHvgSMmDYPg9aWpcmXWnkouLUDbWg"
    _seat(app)
    app.post("/account/address", json={"address": viewer}, headers=LOCAL)
    index = state.token_index(state.token_chain)
    txid, other = "ab" * 32, "cd" * 32
    with index.open() as db:
        for t, n in ((txid, 901), (other, 902)):
            db.conn.execute(
                "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,block_height,"
                "position,content_type,content_len,sha256,json,chunks,content) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (t, n, "nMe", "nSomebodyElse", 100, 0, "text/html", 2, "ab" * 32,
                 None, 1, b"hi"))
        db.conn.commit()
    state.set_setting("pages_host", "pages.example")
    was, state.public = state.public, True
    try:
        page = app.get(f"/inscriptions/{txid}/view").text
        found = re.search(rf"/content/{txid}\?v=([A-Za-z0-9_-]+)", page)
        assert found, "the frame carries the viewer's ticket"
        ticket = found.group(1)
        edge = {"host": "pages.example", "cf-ray": "abc"}

        mine = app.get("/r/wallet", headers={
            **edge, "referer": f"https://pages.example/content/{txid}?v={ticket}"})
        assert mine.status_code == 200, mine.text
        assert mine.json()["addresses"] == [viewer]
        assert mine.json()["wallet"] == "account"

        elsewhere = app.get("/r/wallet", headers={
            **edge, "referer": f"https://pages.example/content/{other}?v={ticket}"})
        assert elsewhere.status_code == 403, "one page's ticket, not every page's"
        assert app.get("/r/wallet", headers=edge).json()["wallet"] == "off"
        assert app.get("/r/wallet", headers={**edge, "referer":
                       f"https://pages.example/content/{txid}?v=forged"}).status_code == 403
    finally:
        state.public = was
        state.set_setting("pages_host", "")

    served = app.get(f"/content/{txid}")
    assert served.headers.get("referrer-policy") == "unsafe-url"
    assert "connect-src 'self'" in served.headers["content-security-policy"]

    # Safari reads 'self' in a sandboxed (opaque-origin) page as nothing, so an
    # iPhone refused every /r/ call and picture #59 asked for (Robin,
    # 2026-09-27). The host the browser asked is named beside it.
    edge = app.get(f"/content/{txid}", headers={
        "host": "pages.example", "x-forwarded-proto": "https"})
    policy = edge.headers["content-security-policy"]
    assert "connect-src 'self' https://pages.example;" in policy, policy
    assert "img-src 'self' https://pages.example data:" in policy, policy
    odd = app.get(f"/content/{txid}", headers={"host": "bad host;script-src *"})
    assert "script-src *" not in odd.headers.get("content-security-policy", "")


def test_a_stranger_cannot_make_the_node_pay_to_ask_another(client, monkeypatch):
    """/r/ask for a piece held elsewhere is a node-to-node message this node's
    wallet pays for; from outside it is refused rather than sent."""
    app, state = client
    index = state.token_index(state.token_chain)
    txid = "ef" * 32
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,block_height,"
            "position,content_type,content_len,sha256,json,chunks,content) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (txid, 903, "nMe", "nSomebodyElse", 100, 0, "text/html", 2, "ab" * 32,
             None, 1, b"hi"))
        db.conn.commit()
    state.set_setting("pages_host", "pages.example")
    try:
        answer = app.post("/r/ask", json={"inscription": txid, "route": "x"},
                          headers={"host": "pages.example", "cf-ray": "abc"})
    finally:
        state.set_setting("pages_host", "")
    assert answer.status_code == 403, answer.text
    assert "does not pay" in answer.json()["error"]


def test_an_arena_asking_about_an_account_s_piece_is_answered_here(client):
    """D-091 routes are lookups in what the creator inscribed, so a piece held by
    an account on this node is answered here, free -- not by a paid message to
    the account, and not refused from outside."""
    import json as _json

    from arcade import utxos as utxoslib

    app, state = client
    index = state.token_index(state.token_chain)
    txid, holder = "fa" * 32, "mqxyzWHvgSMmDYPg9aWpcmXWnkouLUDbWg"
    meta = _json.dumps({"api": {"power": {"const": 7}}})
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,block_height,"
            "position,content_type,content_len,sha256,json,chunks,content) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (txid, 904, "nMe", holder, 100, 0, "text/html", 2, "ab" * 32,
             meta, 1, b"hi"))
        utxoslib.watch(db, holder, 1, "account")
        db.conn.commit()
    state.set_setting("pages_host", "pages.example")
    try:
        answer = app.post("/r/ask", json={"inscription": txid, "route": "power"},
                          headers={"host": "pages.example", "cf-ray": "abc"})
    finally:
        state.set_setting("pages_host", "")
    assert answer.status_code != 403, answer.text
    assert answer.json().get("from") == "here", answer.text
    assert answer.json()["answer"] == 7


@pytest.mark.parametrize("signed_in", [False, True])
def test_a_collection_market_offers_nothing_the_door_refuses(public, signed_in):
    """/exchange/collection/<creator>/<name> drew the operator's Buy / Make offer
    forms (POST /exchange/offer) for every account, and each landed on "Not
    here" (tester-e5, 2026-09-26). The census above never visited one, because
    a market page needs a collection on the chain to draw anything."""
    app, state = public
    if signed_in:
        _seat(app)
        # A seat that holds nothing draws no "Yours" table at all, and a census
        # of a table that never rendered checks nothing. KEEPER owns both
        # pieces below, so this makes them this account's -- the same call a
        # tab makes when it first tells the node where its money is.
        assert app.post("/account/address", json={"address": KEEPER},
                        headers=LOCAL).status_code == 200
    index = state.token_index(state.token_chain)
    creator = "nCreatorCCCCCCCCCCCCCCCCCCCCCCCCC"
    with index.open() as db:
        for n in (1, 2):
            txid = f"{n:02d}" * 32
            db.conn.execute(
                "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,block_height,"
                "position,content_type,content_len,sha256,json,chunks,content) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (txid, 950 + n, creator, KEEPER, 100, n, "image/png", 2, "ab" * 32,
                 None, 1, b"hi"))
            db.conn.execute("INSERT OR REPLACE INTO collection_item(txid,creator,collection,"
                            "edition,name) VALUES(?,?,?,?,?)",
                            (txid, creator, "Market Set", n, ""))
        db.conn.commit()
    page = f"/exchange/collection/{creator}/Market%20Set"
    answer = app.get(page, headers=LOCAL)
    assert answer.status_code == 200, answer.text[:300]
    bad = [f"{m} -> {t}" for t, m in _offered(answer.text, page) + _asked(answer.text)
           if not door.public_path(t.rstrip("/") or "/", m)]
    assert bad == [], bad
    # And the LINKS, which the two scrapes above do not read. This table's own
    # row carried `<a href="/exchange/sell/<txid>">` and
    # `<a href="/inscriptions/<txid>/send">`, and neither is a route a request
    # through the door can open -- so the page that answers "what can I sell"
    # answered it with two doors that shut (the door census, 2026-09-27). The
    # account's sell and send live on its own NFTs page, which is where the tile
    # grid under this table already sent them; a census that follows only form
    # actions walks straight past both halves of that.
    if signed_in:
        assert "Yours in this collection" in answer.text, \
            "the table this half of the census is about has to have rendered"
        assert 'href="/me/nfts"' in answer.text
    assert "/exchange/sell/" not in answer.text, \
        "your own piece, and a link to a route you cannot use"
    assert f"/inscriptions/{'01' * 32}/send" not in answer.text


def test_a_viewer_frame_gets_the_ticket_shim_and_the_bytes_stay_the_bytes(client):
    """A page written the documented way calls plain fetch('/r/wallet'); the
    node adds a marked shim to the HTML it serves a viewer's frame (?v=) so
    those calls carry the ticket (tester-e5, 2026-09-26). Without a ticket, or
    for a download, the inscription's bytes are served exactly."""
    app, state = client
    index = state.token_index(state.token_chain)
    txid = "5a" * 32
    page = b"<!DOCTYPE html><html><body><script>fetch('/r/wallet')</script></body></html>"
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,block_height,"
            "position,content_type,content_len,sha256,json,chunks,content) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (txid, 905, "nMe", "nSomebody", 100, 0, "text/html", len(page), "ab" * 32,
             None, 1, page))
        db.conn.commit()
    plain = app.get(f"/content/{txid}")
    assert plain.content == page
    shown = app.get(f"/content/{txid}?v=tick3t")
    assert shown.status_code == 200
    assert shown.content.startswith(b"<!DOCTYPE html><script>/* DogecoinArcade viewer shim")
    assert shown.content.endswith(page[len(b"<!DOCTYPE html>"):])
    assert shown.headers["cache-control"] == "no-store"
    assert "sandbox" in shown.headers["content-security-policy"]
    assert app.get(f"/content/{txid}?v=tick3t&download=1").content == page



# --- site audit (tester-e5, 2026-09-26) ----------------------------------------

def test_sign_in_brings_you_back_to_the_page_that_sent_you(public):
    app, _ = public
    answer = app.get("/me/wallet", headers=LOCAL, follow_redirects=False)
    assert answer.status_code == 303
    assert answer.headers["location"] == "/join?next=%2Fme%2Fwallet"
    page = app.get("/join?next=%2Fme%2Fwallet", headers=LOCAL).text
    assert "const NEXT" in page and "location.href = NEXT" in page


def test_a_stranger_is_told_how_to_join_on_every_public_page(public):
    app, _ = public
    body = app.get("/feed", headers=LOCAL).text
    assert "Join or sign in" in body and "/join?next=/feed" in body
    assert "Join or sign in" not in app.get("/join", headers=LOCAL).text


def test_the_not_here_page_has_a_way_home_and_a_title(public):
    app, _ = public
    answer = app.get("/wallet/nfts", headers=LOCAL)
    assert answer.status_code in (403, 404)
    assert '<a href="/">Home</a>' in answer.text
    assert "<title>" in answer.text and "· DogecoinArcade</title>" in answer.text


def test_the_address_book_finds_a_name_by_its_address(public):
    from arcade.db import Database
    from arcade.state import install_schema

    app, state = public
    _seat(app)
    db = Database(state.home / f"{state.messaging.network}-ledger.sqlite")
    install_schema(db)
    db.conn.execute("INSERT OR REPLACE INTO tag(tag,address,claimed_txid,block_height,position) "
                    "VALUES('findme',?,?,100,0)", (KEEPER, "f" * 64))
    db.conn.commit()
    db.close()
    said = app.get(f"/account/find?q={KEEPER}", headers=LOCAL).json()
    assert said["matches"] == [{"tag": "findme", "address": KEEPER}]


def test_a_sensitive_name_is_walled_wherever_it_is_shown(public, monkeypatch):
    """2026-09-26: anything shown that may be sensitive sits behind a
    "show me" wall -- names, tokens, collections -- while links keep working."""
    from arcade.db import Database
    from arcade.state import install_schema

    app, state = public

    class Judge:
        enabled = True
        def check_text(self, words, now=False):
            return "sensitive" if "rudename" in words else "ok"
        def check_image(self, *a):
            return "ok"
    monkeypatch.setattr(state, "screen", lambda: Judge())
    db = Database(state.home / f"{state.messaging.network}-ledger.sqlite")
    install_schema(db)
    db.conn.execute("INSERT OR REPLACE INTO tag(tag,address,claimed_txid,block_height,position) "
                    "VALUES('rudename',?,?,100,0)", (KEEPER, "r" * 64))
    db.conn.commit()
    db.close()
    page = app.get("/u/rudename", headers=LOCAL).text
    head = page[page.index("<h1"):page.index("</h1>")]
    assert "sens-btn" in head and "<template>@rudename</template>" in head
    assert "<title>Sensitive" in page or "<title>A profile" in page


#: Somebody's own words, each one judged before a single page is drawn. The
#: four kinds are the four routes user-chosen text takes onto a public page: a
#: token's name; its description, which reaches some tables only through
#: `_pairs` and so is not proven by a wall on the token page; a collection's
#: name, filed by `collection_item` rather than `property`; and an `@tag`, the
#: `who` filter that appears on nearly every page. The tag is seeded twice
#: because a verdict is about one exact string, and the filter asks about
#: `nudeship` while a page's title asks about `@nudeship`.
JUDGED = ["Nude Yacht Club", "A description written for this token by its maker",
          "Nude Whales", "nudeship", "@nudeship"]

#: Whose they are: one address, so the `who` filter resolves and the pieces
#: have an owner.
OWNER = "mqxyzWHvgSMmDYPg9aWpcmXWnkouLUDbWg"
MAKER = "nMakerCCCCCCCCCCCCCCCCCCCCCCCCCC"
PIECE = ("b1" * 32, "b2" * 32)


def _words_on_the_chain(state):
    """One token, one set of two pieces, one claimed name.

    Written straight into the index, because this fixture has no node and no
    wallet, and a sweep that began by mining would be testing the chain rather
    than the pages. Every row below is what a synced node would have written
    for itself."""
    from urllib.parse import quote

    index = state.token_index(state.token_chain)
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO property(property_id,ecosystem,property_type,"
            "issuer,name,data,total_tokens,creation_txid,creation_block) "
            "VALUES(104,2,2,?,?,?,?,?,1)",
            (MAKER, JUDGED[0], JUDGED[1], 100000000000, "cd" * 32))
        db.conn.execute("INSERT OR REPLACE INTO balance(address,property_id,balance) "
                        "VALUES(?,104,100000000000)", (OWNER,))
        for n, txid in enumerate(PIECE, start=1):
            db.conn.execute(
                "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,"
                "block_height,position,content_type,content_len,sha256,json,"
                "chunks,content) VALUES(?,?,?,?,?,?,?,?,?,?,1,?)",
                (txid, 1200 + n, MAKER, OWNER, 100, n, "image/png", 8,
                 "ab" * 32, None, b"\x89PNG\r\n"))
            db.conn.execute(
                "INSERT OR REPLACE INTO collection_item(txid,creator,collection,"
                "edition,name) VALUES(?,?,?,?,?)", (txid, MAKER, JUDGED[2], n, ""))
        db.conn.execute(
            "INSERT OR REPLACE INTO tag(tag,address,claimed_txid,block_height,"
            "position) VALUES(?,?,?,100,0)", (JUDGED[3], OWNER, "ef" * 32))
        db.conn.commit()
    return ["/tokens/104", "/exchange/pair/104",
            f"/collections/{MAKER}/{quote(JUDGED[2])}",
            f"/exchange/collection/{MAKER}/{quote(JUDGED[2])}",
            f"/inscriptions/{PIECE[0]}/view", f"/u/{JUDGED[3]}"]


#: Attributes that may carry a judged name in the clear, named one by one with
#: the reason -- the same shape as `ANSWERED_IN_THE_TAB` above, because an
#: allowance has to be a pair rather than a rule about attributes. The line
#: against `data-market` on the market table, which holds a COPY of the name to
#: match a search against and was emptied: an attribute may keep the words only
#: where blanking them would break the thing the page does, not merely narrow
#: it.
CARRIED_WHOLE = ["data-tag"]


def test_no_public_page_draws_judged_words_at_a_stranger(public, monkeypatch):
    """The check behind the wall: not one page in particular, every page.

    Every fix in this class has been found by a person, which means the next
    one is a template nobody thought to look at. So this asks the general
    question instead: with the screening holding a verdict against four things
    somebody chose, does ANY page a stranger can open draw any of them? The
    words may be in the HTML -- that is the design, a cover is not a delete --
    but a browser must not draw them until the reader taps, which means every
    copy sits inside a `<template>`, or inside a script no browser draws at all.

    A page that 404s is skipped rather than failed: which pages a stranger may
    open is the census above, and this one is about what a page says once it
    answers. The last line is the one that keeps this honest -- if the seeding
    ever stops rendering, a sweep with nothing to catch passes, and a test that
    passes because it saw nothing is worse than no test."""
    import re

    from arcade import moderation as mod

    app, state = public
    monkeypatch.setattr(mod.Screen, "_ask",
                        lambda self, content: ("ok", "answered in this test"))
    state.set_setting("moderation", {"url": "http://127.0.0.1:9/v1",
                                     "model": "test-model"})
    screen = state.screen()
    for said in JUDGED:
        screen._keep(mod.digest_of(said), "text", mod.SENSITIVE, "seeded here")
        assert screen.check_text(said) == mod.SENSITIVE, said
    pages = ["/", "/feed", "/tokens", "/nfts", "/inscriptions", "/collections",
             "/exchange", "/exchange?tab=tokens", "/exchange?tab=nfts",
             "/listings"] + _words_on_the_chain(state)

    def undraw(page: str) -> str:
        """The page as a browser draws it before anybody taps: no script, no
        `<template>`, and no link target.

        A destination is not a drawing. Robin's rule for this class of fix is
        that the links keep working (2026-09-26) -- the way to a hidden name's
        page has to stay on the page or the cover would be a deleted thing
        rather than a covered one -- so `href` and `src` go, while `alt` and
        `title` stay in the text: a tooltip and the words beside a broken image
        are both drawn. `CARRIED_WHOLE` goes too, on the same grounds and named
        one by one there.
        """
        names = "|".join(CARRIED_WHOLE + ["href", "src", "action"])
        page = re.sub(rf"""\b(?:{names})\s*=\s*("[^"]*"|'[^']*')""", "", page,
                      flags=re.I)
        return re.sub(r"<(script|template)[^>]*>.*?</\1>", "", page, flags=re.S)

    shown, covered = [], set()
    for path in pages:
        answer = app.get(path, headers=LOCAL, follow_redirects=False)
        if answer.status_code != 200:
            continue
        for said in JUDGED:
            if said in undraw(answer.text):
                shown.append(f"{path}: {said!r}")
            elif said in answer.text:
                covered.add(said)
    assert shown == [], shown
    assert set(JUDGED) <= covered, \
        f"nothing walled these, so the sweep saw nothing: {sorted(set(JUDGED) - covered)}"


def test_a_price_only_ask_is_not_shown_to_buyers_as_for_sale():
    """2026-09-27: a listed NFT is bought with the Buy button, no offer.
    A price put on the chain alone cannot be bought, so the pages buyers read
    leave it out; the Buy buttons carry the listing they buy."""
    root = pathlib.Path(__file__).resolve().parents[1]
    app_src = (root / "arcade/web/app.py").read_text()
    assert "def _prices_for(index, chain, asks: bool = True)" in app_src
    assert "asks=not _account_view(request)" in app_src
    body = (root / "arcade/web/templates/market_collection.html").read_text()
    assert "data-buy-listing=" in body and '_buy_listing.html' in body
    # The Exchange tab lists collections only (2026-09-28).
    assert "data-buy-listing=" not in (root / "arcade/web/templates/exchange.html").read_text()


def test_a_launch_thread_is_public_and_nests_replies(public, monkeypatch):
    """2026-09-27: anyone can read a launch's comments and answer a
    comment, as on the feed. A reply to a comment is a REPLY aimed at it."""
    from arcade.messaging import feed as feedlib
    from test_feed_web import an_act

    app, state = public
    launch = "1a" * 32
    index = state.token_index(state.token_chain)
    monkeypatch.setattr(type(index), "launches", lambda self, limit=300: [
        {"kind": "token", "id": 7, "name": "Talk Token", "creator": KEEPER,
         "txid": launch, "time": 0}])
    an_act(state, "2b" * 32, feedlib.REPLY, launch, author=KEEPER, text="first comment")
    an_act(state, "3c" * 32, feedlib.REPLY, "2b" * 32, author=KEEPER, text="a reply to it")
    assert door.public_path(f"/launches/{launch}")
    page = app.get(f"/launches/{launch}", headers=LOCAL)
    assert page.status_code == 200, page.text[:300]
    assert "first comment" in page.text and "a reply to it" in page.text
    assert page.text.index("first comment") < page.text.index("a reply to it")
    assert app.get(f"/launches/{'9' * 64}", headers=LOCAL).status_code == 404


def test_any_piece_can_be_shared_to_the_feed_from_its_card(client):
    """2026-09-27: "We should be able to share any NFT to the feed
    directly from the NFT card with the option to say something about it".
    Every card names the piece on a Share button; base.html's one handler
    opens the sheet and posts the words plus the piece's /content/<id>, which
    the feed draws, or sends somebody signed out to join first."""
    from test_me_page import _seat

    app, state = client
    _seat(app)
    index = state.token_index(state.token_chain)
    txid = "5e" * 32
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,block_height,"
            "position,content_type,content_len,sha256,json,chunks,content) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (txid, 905, "nMe", "nSomebodyElse", 100, 0, "image/png", 2, "ab" * 32,
             None, 1, b"hi"))
        db.conn.commit()
    button = f'data-share-nft="{txid}"'
    piece = app.get(f"/inscriptions/{txid}/view").text
    assert button in piece and "Share to feed" in piece
    assert 'data-kind="image/png"' in piece
    assert button in app.get("/inscriptions").text, "the browse cards too"
    assert "/join?next=" in piece and 'LIMIT - ref.length' in piece, \
        "the handler: signed out goes to join, the note leaves room for the piece"

    # What the feed makes of such a post: the words, then the piece drawn.
    from arcade.web.app import post_html
    drawn = post_html(f"look at this one\n\n/content/{txid}", {txid: "image/png"})
    assert "look at this one" in drawn and f'src="/content/{txid}"' in drawn


def test_a_mintpad_link_may_name_its_seller_by_name(client):
    """tester-e5, 2026-09-27: /mintpad/<@name>/<collection> is the link people
    share, and it bounced to the Mintpads tab. A name resolves to its address
    first; one nobody holds still lands on the tab, not an error."""
    app, state = client
    for who in ("@nobody_here", "nobody_here"):
        answer = app.get(f"/mintpad/{who}/Pixel%20Skull", follow_redirects=False)
        assert answer.status_code in (200, 303), answer.text[:200]


def test_full_screen_carries_the_wallet_into_the_page(client):
    """2026-09-29: "there should be a way to open the content link itself so
    that it is full screen. Make sure the Wallet login carries into the full
    screen content links." The full-screen page frames the piece in the same
    bridge frame, with the signed-in reader's ticket on its address."""
    import re

    from test_me_page import _seat

    app, state = client
    viewer = "mqxyzWHvgSMmDYPg9aWpcmXWnkouLUDbWg"
    _seat(app)
    app.post("/account/address", json={"address": viewer}, headers=LOCAL)
    index = state.token_index(state.token_chain)
    page_tx, pic_tx, file_tx = "a1" * 32, "b2" * 32, "c3" * 32
    with index.open() as db:
        for t, n, kind in ((page_tx, 911, "text/html"), (pic_tx, 912, "image/png"),
                           (file_tx, 913, "application/zip")):
            db.conn.execute(
                "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,block_height,"
                "position,content_type,content_len,sha256,json,chunks,content) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (t, n, "nMe", "nSomebodyElse", 100, 0, kind, 2, "ab" * 32,
                 '{"name": "Ghost Fleet"}' if kind == "text/html" else None, 1, b"hi"))
        db.conn.commit()
    state.set_setting("pages_host", "pages.example")
    was, state.public = state.public, True
    try:
        full = app.get(f"/inscriptions/{page_tx}/full").text
        assert 'class="inscription-frame fullframe"' in full, "the frame the bridge answers"
        assert re.search(rf"pages\.example/content/{page_tx}\?v=[A-Za-z0-9_-]+", full), \
            "the reader's ticket rides on it, so the wallet carries in"
        assert "Ghost Fleet" in full
        # 2026-09-30: the whole screen is the game -- no bar, no back button,
        # nothing of the app's drawn over it -- and the wallet still reads.
        assert 'id="full-back"' not in full and 'class="fullbar"' not in full
        assert 'id="unlockbar"' not in full and '<footer class="version">' not in full
        assert "Pull to refresh" not in full, "a game's touch controls stay the game's"
        assert 'class="fullscreen"' in full
        for bridge in ('m.arcade === "claim"', 'm.arcade !== "mint"'):
            assert bridge in full, f"the wallet bridge is still here: {bridge}"
        pic = app.get(f"/inscriptions/{pic_tx}/full").text
        assert f'src="/content/{pic_tx}"' in pic
        other = app.get(f"/inscriptions/{file_tx}/full", follow_redirects=False)
        assert other.status_code == 303 and other.headers["location"].endswith("/view")
        assert f"/inscriptions/{page_tx}/full" in app.get(f"/inscriptions/{page_tx}/view").text
    finally:
        state.public = was


def test_a_page_in_a_post_offers_full_screen():
    from arcade.web.app import post_html
    drawn = post_html(f"look /content/{'a1' * 32}", {"a1" * 32: "text/html"})
    assert f'href="/inscriptions/{"a1" * 32}/full"' in drawn


def test_a_page_asks_the_signed_in_reader_to_send(client):
    """2026-09-29: "I'm trying to load some plasma from my wallet to the game,
    but I get an error this Wallet does not tell inscriptions who is looking."
    A frame carrying its reader's ticket files a question for that reader; the
    reader's own app page answers it; the page's polling sees the answer."""
    import re

    from test_me_page import _seat

    app, state = client
    viewer = "mqxyzWHvgSMmDYPg9aWpcmXWnkouLUDbWg"
    _seat(app)
    app.post("/account/address", json={"address": viewer}, headers=LOCAL)
    index = state.token_index(state.token_chain)
    txid = "d4" * 32
    with index.open() as db:
        db.conn.execute(
            "INSERT OR REPLACE INTO inscription(txid,number,creator,owner,block_height,"
            "position,content_type,content_len,sha256,json,chunks,content) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (txid, 931, "nMe", "nSomebodyElse", 100, 0, "text/html", 2, "ab" * 32,
             None, 1, b"hi"))
        db.conn.commit()
    state.set_setting("pages_host", "pages.example")
    was, state.public = state.public, True
    try:
        page = app.get(f"/inscriptions/{txid}/full").text
        ticket = re.search(rf"/content/{txid}\?v=([A-Za-z0-9_-]+)", page).group(1)
        edge = {"host": "pages.example", "cf-ray": "abc",
                "referer": f"https://pages.example/content/{txid}?v={ticket}"}
        asked = app.post("/r/send", json={"kind": "coins", "to": "@vex", "amount": "1",
                                          "label": "GHOST FLEET", "note": "top up"},
                         headers=edge)
        assert asked.status_code == 202, asked.text
        rid = asked.json()["id"]
        assert asked.json()["status"] == "pending"

        waiting = app.get("/account/pagesends").json()["requests"]
        assert [(r["id"], r["label"], r["amount"]) for r in waiting] == [(rid, "GHOST FLEET", "1")]
        lying = app.post("/account/pagesends/answer", json={"id": rid, "status": "sent",
                                                            "txid": "00" * 32})
        assert lying.status_code == 400, "a txid this node never saw is not an answer"
        assert app.post("/account/pagesends/answer",
                        json={"id": rid, "status": "denied"}).status_code == 200
        assert app.get(f"/r/send/{rid}", headers=edge).json()["status"] == "denied"
        assert app.get("/account/pagesends").json()["requests"] == []

        stranger = {"host": "pages.example", "cf-ray": "abc"}
        assert app.post("/r/send", json={"kind": "coins", "to": "x", "amount": "1"},
                        headers=stranger).status_code == 403
    finally:
        state.public = was


# --- bringing an account over from another arcade (2026-09-30) ----------------

def test_a_password_operator_hands_the_node_to_an_account_with_its_password(named):
    """A friend "already made an account on my node [and] wants to transfer his
    account ... to be his operator account", on a node whose installer set a
    username and password. The password, typed on the machine, hands it over;
    the same username and password then open the new operator."""
    app, state = named
    assert app.post("/auth/set-password", headers=LOCAL, json={
        "username": "mini", "password": "a long enough password"}).status_code == 200
    first = state.operator
    mine = _seat(app)
    assert app.get("/auth/who", headers=LOCAL).json()["handover"] is True
    refused = app.post("/auth/operator", headers=LOCAL, json={})
    assert refused.status_code == 403 and state.operator == first
    wrong = app.post("/auth/operator", headers=LOCAL, json={"password": "nope nope nope"})
    assert wrong.status_code == 403 and state.operator == first
    outside = app.post("/auth/operator", headers=EDGE,
                       json={"password": "a long enough password"})
    assert outside.status_code in (403, 404) and state.operator == first
    handed = app.post("/auth/operator", headers=LOCAL,
                      json={"password": "a long enough password"})
    assert handed.status_code == 200, handed.text
    assert state.operator == mine
    again = app.post("/auth/password", headers=LOCAL, json={
        "username": "mini", "password": "a long enough password"})
    assert again.status_code == 200 and again.json()["pubkey"] == mine, \
        "the same username and password open the new operator"


def test_an_account_is_fetched_from_another_arcade_only_for_the_machine_itself(named, monkeypatch):
    app, state = named
    import requests as _requests

    class Answer:
        status_code = 200

        def json(self):
            return {"tag": "friend", "pubkey": "ab" * 32, "address": "mxyz",
                    "blob": {"sealed": "00", "salt": "00", "iterations": 1, "nonce": "00"}}

    asked = []
    monkeypatch.setattr(_requests, "get", lambda url, **k: asked.append(url) or Answer())
    got = app.get("/signin-import?node=app.dogecoinarcade.com&tag=@friend", headers=LOCAL)
    assert got.status_code == 200, got.text
    assert asked == ["https://app.dogecoinarcade.com/signin/friend"]
    assert got.json()["blob"]["sealed"] == "00" and got.json()["tag"] == "friend"
    assert app.get("/signin-import?node=app.dogecoinarcade.com&tag=friend",
                   headers=EDGE).status_code in (403, 404), "not a proxy for strangers"
    assert app.get("/signin-import?node=file:///etc&tag=friend",
                   headers=LOCAL).status_code == 400


# --- the node's own machine, signed in as an account (2026-09-30) -------------

def test_an_account_signed_in_on_the_nodes_own_machine_sees_its_own_arcade(named):
    """A friend installed an arcade, signed in there as the account he already
    had, and was shown the installer's wallet: no name, no coins, an offer to
    claim a name that was his. On the node's own machine a signed-in account
    now sees its own tabs, and can switch to the node's built-in wallet."""
    app, state = named
    mine = _seat(app)
    state.set_setting(f"address:{mine}", "n4sZQy4dMCQLJKHkLNpPEj4dpv2CxA8VUj")
    page = app.get("/feed", headers=LOCAL).text
    assert 'href="/me"' in page and 'href="/me/accounts"' in page
    assert 'href="/wallet"' not in page, "the node's wallet is behind the switch"
    assert 'href="/admin"' in page, "the machine's own Admin stays"

    node = app.get("/view/node", headers=LOCAL, follow_redirects=False)
    assert node.status_code == 303 and "arcade_view=node" in node.headers["set-cookie"]
    app.cookies.set("arcade_view", "node")
    page = app.get("/feed", headers=LOCAL).text
    assert 'href="/wallet"' in page and 'href="/me/accounts"' in page

    back = app.get("/view/account", headers=LOCAL, follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == "/me"
    app.cookies.delete("arcade_view")
    assert 'href="/wallet"' not in app.get("/feed", headers=LOCAL).text


def test_a_password_only_operator_still_sees_the_node(named):
    """The installer's username and password open the NODE: it has no wallet of
    its own to show instead."""
    app, state = named
    assert app.post("/auth/set-password", headers=LOCAL, json={
        "username": "owner", "password": "a long enough password"}).status_code == 200
    assert app.post("/auth/password", headers=LOCAL, json={
        "username": "owner", "password": "a long enough password"}).status_code == 200
    page = app.get("/feed", headers=LOCAL).text
    assert 'href="/wallet"' in page and 'href="/me/accounts"' not in page


def test_the_switch_is_not_on_the_public_site(named):
    app, state = named
    _seat(app, headers=EDGE)
    assert app.get("/view/node", headers=EDGE).status_code in (403, 404)
    assert 'href="/me/accounts"' not in app.get("/feed", headers=EDGE).text
    assert app.get("/me/accounts", headers=EDGE).status_code in (403, 404)
    assert app.get("/auth/accounts", headers=EDGE).status_code in (403, 404)


def test_several_accounts_signed_in_on_the_nodes_own_machine_can_be_switched(named):
    """2026-09-30: "a way to have multiple wallets imported into the local web
    ui". A second sign-in keeps the first; the list shows both; switching back
    keeps the other one in turn."""
    app, state = named
    first = _seat(app)
    state.set_setting(f"address:{first}", "n4sZQy4dMCQLJKHkLNpPEj4dpv2CxA8VUj")
    second = _seat(app)
    state.set_setting(f"address:{second}", "mk3gxtreqachy2EGax1pALfmBwFW8qAPiy")
    listed = app.get("/auth/accounts", headers=LOCAL).json()["accounts"]
    assert [(a["pubkey"], a["current"]) for a in listed] == [(second, True), (first, False)]
    assert app.get("/me/accounts", headers=LOCAL).status_code == 200

    switched = app.post("/auth/switch", headers=LOCAL, json={"pubkey": first})
    assert switched.status_code == 200, switched.text
    assert app.get("/auth/who", headers=LOCAL).json()["pubkey"] == first
    listed = app.get("/auth/accounts", headers=LOCAL).json()["accounts"]
    assert [(a["pubkey"], a["current"]) for a in listed] == [(first, True), (second, False)]
    assert app.post("/auth/switch", headers=LOCAL,
                    json={"pubkey": "ab" * 32}).status_code == 404, "only accounts signed in here"


def test_a_public_sign_in_remembers_nobody_before_it(named):
    """A stranger's computer should not keep the last person signed in."""
    app, state = named
    _seat(app, headers=EDGE)
    second = _seat(app, headers=EDGE)
    assert "arcade_others" not in app.cookies
    assert app.get("/auth/who", headers=EDGE).json()["pubkey"] == second
