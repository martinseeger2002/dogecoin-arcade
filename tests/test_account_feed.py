"""An account posting, liking and tipping with its own coins.

Nothing here is encrypted and nothing ever was: every row of the feed was
readable by anybody with a node the moment it was mined. What an account
changes is who pays for it and whose name is on it -- and the name is read
from the chain, so nobody posts under one they do not hold.
"""

import contextlib
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_funding import _pubkey, _sign                          # noqa: E402

from arcade import utxos                                         # noqa: E402
from arcade.messaging import feed as feedlib                     # noqa: E402
from arcade.script import b58check_encode, hash160                # noqa: E402

COIN = 100_000_000
SECRET = 0x515151515151515151515151515151515151515151515151515151515151


@pytest.fixture
def arcade(tmp_path, regtest):
    from fastapi.testclient import TestClient

    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    class Pointed(ChainContext):
        def credentials(self):
            return regtest.rpc._creds

        @property
        def params(self):
            return regtest.params

    chain = Pointed(network="regtest", role="messaging", label="Testnet",
                    datadir=regtest.datadir)
    state = AppState(home=tmp_path, messaging=chain,
                     ledger=ChainContext(network="main", role="ledger",
                                         label="Mainnet",
                                         datadir=pathlib.Path("/nonexistent")))
    (tmp_path / "tokens-chain").write_text("regtest\n")
    return TestClient(create_app(state)), state, regtest.rpc


def _sign_in(app):
    from nacl.signing import SigningKey

    from arcade import accounts

    key = SigningKey.generate()
    challenge = app.get("/auth/challenge").json()
    signature = key.sign(accounts.login_message(
        challenge["origin"], challenge["nonce"])).signature
    answer = app.post("/auth/login", json={
        "pubkey": key.verify_key.encode().hex(), "nonce": challenge["nonce"],
        "signature": signature.hex(), "join": True})
    assert answer.status_code == 200, answer.text
    return key.verify_key.encode().hex()


def _catch_up(state):
    """Index every block the node has before the test reads anything.

    `sync()` answers None when another thread is already mid-pass -- a request
    still being served by the same state, usually -- and that is not the same
    thing as being caught up. Reading it as caught up is why these files passed
    alone and failed in a full run: alone, one pass covers the session node's
    short chain, and in a full run that node has mined thousands of blocks and
    one lost pass leaves the rest unwalked. `current` is what a page shows, and
    it counts an index as current when the floor is above the tip and there is
    genuinely nothing to read.
    """
    index = state.token_index(state.messaging)
    tip = 0
    for _ in range(200):
        with state.messaging.rpc() as rpc:
            tip = rpc.get_block_count()
        if index.status(tip)["current"]:
            return index
        if index.stopped is not None:
            raise AssertionError(f"the index stopped: {index.stopped}")
        index.sync(max_blocks=500)
        time.sleep(0.05)
    raise AssertionError(f"the index will not catch up: {index.status(tip)}")


def _do(app, where, body, secret, pubkey):
    """Ask for an offer, sign it, send it -- as a browser would."""
    offered = app.post(where, json=body)
    if offered.status_code != 200:
        return offered
    offer = offered.json()
    signatures = [_sign(secret, bytes.fromhex(h)).hex()
                  for h in offer["sighashes"]]
    return app.post("/account/sign", json={
        "offer": offer["offer"], "signatures": signatures,
        "pubkey": pubkey.hex()})


@pytest.fixture
def account(arcade):
    """A funded account with a claimed name."""
    app, state, rpc = arcade
    _sign_in(app)
    pubkey = _pubkey(SECRET)
    mine = b58check_encode(state.messaging.params.pubkeyhash_version,
                           hash160(pubkey))
    rpc.call("generate", 120)
    _catch_up(state)
    app.post("/account/address", json={"address": mine,
                                       "coin_pubkey": pubkey.hex()})
    rpc.call("sendtoaddress", mine, 20.0)
    rpc.call("generate", 1)
    _catch_up(state)
    return app, state, rpc, pubkey, mine


def test_a_name_is_needed_before_a_post(account):
    """Everything in the feed has a person behind it, and a byline that is
    an address is not a person."""
    app, state, rpc, pubkey, mine = account
    refused = app.post("/account/post", json={"text": "hello"})
    assert refused.status_code == 400
    assert "claim your name first" in refused.json()["detail"]


def test_an_account_posts_with_its_own_coins(account):
    app, state, rpc, pubkey, mine = account
    claimed = _do(app, "/account/claim", {"tag": "poster"}, SECRET, pubkey)
    assert claimed.status_code == 200, claimed.text
    rpc.call("generate", 1)
    _catch_up(state)

    said = _do(app, "/account/post", {"text": "posted by an account"},
               SECRET, pubkey)
    assert said.status_code == 200, said.text
    txid = said.json()["txid"]
    assert txid in rpc.call("getrawmempool")

    rpc.call("generate", 1)
    _catch_up(state)
    from arcade.messaging.scanner import Scanner
    with state.messaging.rpc() as node:
        with state.store() as store:
            # One scan() is 2000 blocks. Keep going until a pass reads nothing,
            # so a chain as long as a full run leaves it does not hide the post.
            for _ in range(50):
                if Scanner(node, state.messaging.params, store,
                           identity=None).scan().blocks == 0:
                    break
        with state.store() as store:
            posts = store.feed_posts(state.messaging.network, limit=20)
    assert any(p["text"] == "posted by an account" for p in posts), \
        [p["text"] for p in posts]
    mine_post = [p for p in posts if p["text"] == "posted by an account"][0]
    assert mine_post["sender"] == mine, "and it is theirs, by address"


def test_an_account_likes_a_post(account):
    app, state, rpc, pubkey, mine = account
    _do(app, "/account/claim", {"tag": "liker"}, SECRET, pubkey)
    rpc.call("generate", 1)
    _catch_up(state)
    posted = _do(app, "/account/post", {"text": "like this"}, SECRET, pubkey)
    target = posted.json()["txid"]
    rpc.call("generate", 1)
    _catch_up(state)

    liked = _do(app, "/account/react",
                {"txid": target, "kind": feedlib.LIKE}, SECRET, pubkey)
    assert liked.status_code == 200, liked.text
    assert liked.json()["what"] == "like"


def test_a_reaction_to_nothing_is_refused(account):
    app, state, rpc, pubkey, mine = account
    for bad in ("", "nothex", "ab" * 10):
        refused = app.post("/account/react",
                           json={"txid": bad, "kind": feedlib.LIKE})
        assert refused.status_code == 400, bad
    refused = app.post("/account/react",
                       json={"txid": "ab" * 32, "kind": 99})
    assert refused.status_code == 400
    assert "not something that can be done" in refused.json()["detail"]


def test_a_tip_pays_the_author_in_the_same_transaction(account):
    """One transaction: the coins move and the same transaction says which
    post they were for, so nothing has to be reconciled afterwards."""
    app, state, rpc, pubkey, mine = account
    _do(app, "/account/claim", {"tag": "tipper"}, SECRET, pubkey)
    rpc.call("generate", 1)
    _catch_up(state)
    posted = _do(app, "/account/post", {"text": "worth a tip"}, SECRET, pubkey)
    target = posted.json()["txid"]
    rpc.call("generate", 1)
    _catch_up(state)
    from arcade.messaging.scanner import Scanner
    with state.messaging.rpc() as node:
        with state.store() as store:
            # One scan() is 2000 blocks. Keep going until a pass reads nothing,
            # so a chain as long as a full run leaves it does not hide the post.
            for _ in range(50):
                if Scanner(node, state.messaging.params, store,
                           identity=None).scan().blocks == 0:
                    break

    offered = app.post("/account/react", json={
        "txid": target, "kind": feedlib.TIP, "amount": "2"})
    assert offered.status_code == 200, offered.text
    offer = offered.json()
    assert offer["paid"] == 2 * COIN
    assert offer["what"] == "tip"

    signatures = [_sign(SECRET, bytes.fromhex(h)).hex()
                  for h in offer["sighashes"]]
    done = app.post("/account/sign", json={
        "offer": offer["offer"], "signatures": signatures,
        "pubkey": pubkey.hex()})
    assert done.status_code == 200, done.text
    # It pays the author, who here is this same account -- so the coins
    # come home and what is proved is that the output was built at all.
    decoded = rpc.call("decoderawtransaction",
                       rpc.call("getrawtransaction", done.json()["txid"]))
    paid = [v for v in decoded["vout"]
            if abs(float(v["value"]) - 2.0) < 1e-8]
    assert paid, "the tip output is in the transaction"


def test_a_tip_to_somebody_with_no_address_is_refused(account):
    app, state, rpc, pubkey, mine = account
    refused = app.post("/account/react", json={
        "txid": "cd" * 32, "kind": feedlib.TIP, "amount": "1"})
    assert refused.status_code == 400
    assert "nowhere to send it" in refused.json()["detail"]


def test_a_tip_too_small_to_relay_is_refused(account):
    """Under 0.01 the tip is itself dust: peers would refuse the transaction and it
    would wait for its block for good. Said before anything is built."""
    app, state, rpc, pubkey, mine = account
    refused = app.post("/account/react", json={
        "txid": "cd" * 32, "kind": feedlib.TIP, "amount": "0.005"})
    assert refused.status_code == 400
    assert "smallest tip" in refused.json()["detail"]


def _read(state):
    """Every index caught up, AND the feed rows filed.

    `_catch_up` walks the token index, which is where balances and names come
    from. Posts and the things done to them are filed by the messaging scanner
    into `group_post` and `feed_act`, and those two tables are what a feed page
    reads -- so a page drawn after `_catch_up` alone is a page saying "nothing
    here yet" about a post that is three blocks old and perfectly good. The
    loop is the one `test_an_account_posts_with_its_own_coins` uses: a pass is
    2000 blocks and a node that has been up all day has more than that.
    """
    _catch_up(state)
    from arcade.messaging.scanner import Scanner
    with state.messaging.rpc() as node:
        with state.store() as store:
            for _ in range(50):
                if Scanner(node, state.messaging.params, store,
                           identity=None).scan().blocks == 0:
                    break


def _mined(state, rpc):
    """One block, and everything this node can say about it said."""
    rpc.call("generate", 1)
    _read(state)


def _named(app, state, rpc, secret, pubkey, tag):
    """A name, in a block, read. The buttons need it; the chain does not."""
    claimed = _do(app, "/account/claim", {"tag": tag}, secret, pubkey)
    assert claimed.status_code == 200, claimed.text
    _mined(state, rpc)


def _posted(app, state, rpc, secret, pubkey, text):
    said = _do(app, "/account/post", {"text": text}, secret, pubkey)
    assert said.status_code == 200, said.text
    _mined(state, rpc)
    return said.json()["txid"]


def _reacted(app, state, rpc, secret, pubkey, txid, kind, amount=None):
    body = {"txid": txid, "kind": kind}
    if amount is not None:
        body["amount"] = amount
    said = _do(app, "/account/react", body, secret, pubkey)
    assert said.status_code == 200, said.text
    _mined(state, rpc)
    return said.json()["txid"]


def test_both_feed_pages_are_ones_an_account_can_act_on(account):
    """The buttons were always there; the key to press them was not.

    `/feed` had a script that could sign and `/u/<tag>` had none, which is
    backwards -- a profile page is the page you are standing on when you decide
    to answer somebody. And `/feed/<txid>/like` is not a route a public
    instance opens, so a card that only submitted its form was a card that
    looked like buttons and answered 403.
    """
    app, state, rpc, pubkey, mine = account
    _named(app, state, rpc, SECRET, pubkey, "bothpages")
    target = _posted(app, state, rpc, SECRET, pubkey, "answer this somewhere")

    state.public = True
    try:
        pages = {"feed": app.get("/feed"), "profile": app.get("/u/bothpages")}
    finally:
        state.public = False

    for where, page in pages.items():
        assert page.status_code == 200, page.text
        assert target in page.text, f"the post is not on {where}"
        # The form is the operator's, untouched, and the script under it is
        # what turns it into this account's transaction.
        assert f'action="/feed/{target}/like"' in page.text, where
        assert f'id="tip-{target}"' in page.text, where
        assert "/wallet.js" in page.text, where
        assert 'id="acts-said"' in page.text, where
        # The words come out of feed.py rather than being typed into a script,
        # because a word that has drifted from its byte is a wrong transaction.
        for name in feedlib.NAMES.values():
            assert f'"{name}"' in page.text, f"{name} is not in {where}'s map"


def test_a_public_reader_is_shown_the_numbers_and_not_the_doing(account):
    """Four buttons that each answered 403 is not a feed, it is a trap.

    What a stranger gets is the count -- which is the post's, and was on the
    chain before this page existed -- and one sentence saying who can do the
    rest.
    """
    app, state, rpc, pubkey, mine = account
    _named(app, state, rpc, SECRET, pubkey, "shownumbers")
    target = _posted(app, state, rpc, SECRET, pubkey, "read but not answered")
    _reacted(app, state, rpc, SECRET, pubkey, target, feedlib.LIKE)

    app.cookies.clear()                  # the reader is nobody in particular
    state.public = True
    try:
        page = app.get("/feed")
    finally:
        state.public = False
    assert page.status_code == 200, page.text
    assert "read but not answered" in page.text, "the post is still there to read"
    assert "Make an account" in page.text
    # The numbers are the post's, taken from rows everybody counts the same
    # way, so a stranger reads them too. What is gone is the doing of them: no
    # card on a public page offers a route the door will shut, and a card with
    # a heart on it that answers 403 is not a button, it is a trap.
    assert "1 like" in page.text
    assert 'action="/feed/' not in page.text
    assert 'href="/feed/' not in page.text
    assert 'id="acts-said"' not in page.text


def test_an_account_sees_its_own_likes_not_the_nodes(account):
    """A like that reads back as ♡ is a like somebody presses a second time.

    The page asked the node who was looking. Publicly that is a stranger's
    page answering with the machine's address, so every account saw the
    operator's likes and none of its own -- and liking twice costs twice.
    """
    app, state, rpc, pubkey, mine = account
    _named(app, state, rpc, SECRET, pubkey, "ownlikes")
    target = _posted(app, state, rpc, SECRET, pubkey, "liked by its own author")
    _reacted(app, state, rpc, SECRET, pubkey, target, feedlib.LIKE)

    state.public = True
    try:
        own = app.get("/feed")
        # And it is theirs to delete, which `p.mine` never meant: that column
        # is this machine's book of what IT sent, and the node never held a
        # transaction it did not have the key for.
        assert f'action="/feed/{target}/unlike"' in own.text
        assert f'action="/feed/{target}/delete"' in own.text
        app.cookies.clear()
        stranger = app.get("/feed")
    finally:
        state.public = False
    # What a stranger is shown is the number -- which is the chain's, and not
    # the node's either -- and no control at all. The like above belongs to
    # this account, and a card that offered it to them would be a card that
    # spends somebody's coins for a name that is not theirs.
    assert "1 like" in stranger.text
    assert 'action="/feed/' not in stranger.text
    assert f'action="/feed/{target}/unlike"' not in stranger.text
    assert f'action="/feed/{target}/delete"' not in stranger.text


def test_the_operator_keeps_the_forms_the_routes_expect(arcade):
    """Every branch added for a public page has to leave the LAN page alone.

    The routes, the csrf token and the redirect back are still the operator's
    way of doing this, and on a machine that is not behind a door they are the
    only way. Rows are put in the store rather than mined: this test is about
    what a page draws for the person who runs it.
    """
    app, state, rpc = arcade
    theirs = "nThemAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    theirs_id, posted = "ab" * 32, "cd" * 32
    with state.store() as store:
        store.add_group_post(state.messaging.network, "", theirs_id, 100,
                             1000, theirs, "", "somebody else wrote this")
        store.add_group_post(state.messaging.network, "", posted, 101, 1010,
                             theirs, "", "this node posted", mine=True)

    page = app.get("/feed")
    assert page.status_code == 200, page.text
    assert "somebody else wrote this" in page.text
    assert "acts-said" not in page.text, "no wallet on an operator's page"
    for doing in ("like", "reply", "share", "mute"):
        assert f'action="/feed/{theirs_id}/{doing}"' in page.text, doing
    assert f'href="/feed/{theirs_id}/tip"' in page.text, "tip is still a page"
    assert f'action="/feed/{posted}/delete"' in page.text
    assert f'action="/feed/{theirs_id}/delete"' not in page.text


def test_the_page_is_listening_before_a_press_arrives():
    """A button that is not wired yet is a word somebody clicks.

    Both scripts in `feed.html` used to wait for the key this tab holds before
    they attached anything, and looking that key up is a second or two of work
    done after the page is already on screen. A press inside that gap answered
    with nothing at all -- no error, no word, no transaction -- and it is
    invisible from outside the tab, because the page arrives, works a moment
    later, and looks the same either way. The live run found it by pressing as
    soon as the button existed; a person finds it by pressing twice.

    On the cards it was worse than silence. The listener that stops
    `/feed/<txid>/like` from being SUBMITTED was itself the thing being waited
    for, so an early press went to the server as an ordinary form and a public
    instance's door answered it -- the exact 403 this feature was built to
    remove, surviving in the first second after the page loads.

    Asserted on the source rather than in a browser because the gap is a few
    hundred milliseconds wide, and a test that races it passes either way.
    """
    source = pathlib.Path("arcade/web/templates/feed.html").read_text()
    assert "await wallet.opened(" not in source, \
        "the key must be started and taken up by the press, not waited for"
    assert "wallet.opened(CHAIN)" in source, "the tab's key is still picked up"
    for wiring in ('$("account-post").onclick', "$(\"account-open\").onclick",
                   "$(\"acts-open\").onclick",
                   'document.addEventListener("submit"'):
        assert wiring in source, f"{wiring} is gone, so nothing answers a press"
    # The refusal to submit comes before anything is awaited, so a press that
    # arrives while the key is still being found is queued, not sent. Read from
    # the listener on, because there is another preventDefault further down the
    # page and the one that matters is inside this handler.
    listener = source[source.index('document.addEventListener("submit"'):]
    assert (listener.index("event.preventDefault()")
            < listener.index("const held = await take()"))
