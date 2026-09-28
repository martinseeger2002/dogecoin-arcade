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
    # Since 2026-09-27 the public front page is the feed (the operator: "When the
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

    The operator met this on `/nfts` on 2026-09-24: the page carried the NODE's
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
    at a time. That is still a choice being pointed at, which is what the operator's
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
    operator (a tester, 2026-09-26). From outside, the page API says the
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
    # iPhone refused every /r/ call and picture #59 asked for (the operator,
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
    here" (a tester, 2026-09-26). The census above never visited one, because
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
    those calls carry the ticket (a tester, 2026-09-26). Without a ticket, or
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



# --- site audit (a tester, 2026-09-26) ----------------------------------------

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


def test_a_price_only_ask_is_not_shown_to_buyers_as_for_sale():
    """2026-09-27: a listed NFT is bought with the Buy button, no offer.
    A price put on the chain alone cannot be bought, so the pages buyers read
    leave it out; the Buy buttons carry the listing they buy."""
    root = pathlib.Path(__file__).resolve().parents[1]
    app_src = (root / "arcade/web/app.py").read_text()
    assert "def _prices_for(index, chain, asks: bool = True)" in app_src
    assert "asks=not _public_request(request)" in app_src
    assert "_buyable_listings(index, chain)" in app_src
    for page in ("market_collection.html", "exchange.html"):
        body = (root / "arcade/web/templates" / page).read_text()
        assert "data-buy-listing=" in body and '_buy_listing.html' in body, page


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
    """a tester, 2026-09-27: /mintpad/<@name>/<collection> is the link people
    share, and it bounced to the Mintpads tab. A name resolves to its address
    first; one nobody holds still lands on the tab, not an error."""
    app, state = client
    for who in ("@nobody_here", "nobody_here"):
        answer = app.get(f"/mintpad/{who}/Pixel%20Skull", follow_redirects=False)
        assert answer.status_code in (200, 303), answer.text[:200]
