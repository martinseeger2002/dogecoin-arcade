"""An account posting, liking and tipping with its own coins.

Nothing here is encrypted and nothing ever was: every row of the feed was
readable by anybody with a node the moment it was mined. What an account
changes is who pays for it and whose name is on it -- and the name is read
from the chain, so nobody posts under one they do not hold.
"""

import contextlib
import pathlib
import sys

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
    index = state.token_index(state.messaging)
    for _ in range(50):
        result = index.sync(max_blocks=500)
        if result is None or not result.connected:
            break
    return index


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
            Scanner(node, state.messaging.params, store, identity=None).scan()
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
            Scanner(node, state.messaging.params, store, identity=None).scan()

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
