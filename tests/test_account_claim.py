"""Claiming a name with a key the node has never seen, end to end.

The first thing an account does on the chain, and the shape everything
else will follow: the node builds and explains, somebody else signs, the
node checks what came back is what it offered, and broadcasts.

Against a real regtest node, because the parts that can be wrong here --
the sighash, the assembled scriptSig, what the engine makes of the
result -- are all parts that look right on paper.
"""

import contextlib
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_funding import _pubkey, _sign                          # noqa: E402

from arcade import tags as taglib, utxos                         # noqa: E402
from arcade.db import StateDB                                    # noqa: E402
from arcade.script import b58check_encode, hash160                # noqa: E402

COIN = 100_000_000
SECRET = 0x7788990011223344778899001122334477889900112233447788990011223344


@pytest.fixture
def arcade(tmp_path, regtest):
    """The application pointed at a regtest node, with nothing in it."""
    from fastapi.testclient import TestClient

    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    # The node picked a free port and a free datadir, so the chain is told
    # exactly where it is rather than left to the regtest defaults --
    # pointing at 18332 finds whatever else happens to be there, or
    # nothing, and the failure then reads like a bug in the code under test.
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
    """A seat, the ordinary way, with an Ed25519 key made here."""
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


def _catch_up(state, rpc):
    """Let the index read every block the node has.

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
        tip = rpc.call("getblockcount")
        if index.status(tip)["current"]:
            return index
        if index.stopped is not None:
            raise AssertionError(f"the index stopped: {index.stopped}")
        index.sync(max_blocks=500)
        time.sleep(0.05)
    raise AssertionError(f"the index will not catch up: {index.status(tip)}")


def test_an_account_claims_a_name_with_a_key_the_node_never_saw(arcade):
    app, state, rpc = arcade
    _sign_in(app)

    pubkey = _pubkey(SECRET)
    mine = b58check_encode(state.messaging.params.pubkeyhash_version,
                           hash160(pubkey))
    assert not rpc.call("validateaddress", mine).get("ismine"), \
        "the node has no key for this address, which is the whole point"

    # The browser says where to watch. The node checks the shape and
    # believes the rest, because only the machine that made it can say.
    rpc.call("generate", 101)
    _catch_up(state, rpc)
    told = app.post("/account/address", json={"address": mine})
    assert told.status_code == 200, told.text

    # With no coins there is nothing to build, and it says so rather than
    # offering something unpayable.
    broke = app.post("/account/claim", json={"tag": "robin"})
    assert broke.status_code == 400
    assert "not enough" in broke.json()["detail"]

    # Pay it, and let the index see.
    rpc.call("sendtoaddress", mine, 4.0)
    rpc.call("generate", 1)
    _catch_up(state, rpc)
    said = app.get("/account").json()
    assert said["address"] == mine
    assert said["balance"] == int(4.0 * COIN), "the index followed the coins"
    assert said["tag"] == "", "and no name yet"

    # The offer. Nothing is broadcast, and it says what it will do.
    offered = app.post("/account/claim", json={"tag": "robin"})
    assert offered.status_code == 200, offered.text
    offer = offered.json()
    assert offer["what"] == "claim @robin"
    assert offer["sighashes"] and len(offer["sighashes"]) == len(offer["inputs"])
    # And the material to check them with. A browser that recomputes these
    # hashes rather than trusting them needs the transaction they come from
    # and the index where this key's own coins start; an offer that stopped
    # carrying either would quietly put the signing back in the node's hands.
    assert len(offer["raw"]) >= 20 and offer["signed_from"] == 0
    assert all(i["txid"] and i["value"] > 0 for i in offer["inputs"])
    assert offer["fee"] > 0 and offer["change"] > 0
    assert rpc.call("getrawmempool") == [], "and nothing has gone out"

    # Signed somewhere else entirely.
    signatures = [_sign(SECRET, bytes.fromhex(h)).hex()
                  for h in offer["sighashes"]]
    done = app.post("/account/sign", json={
        "offer": offer["offer"], "signatures": signatures,
        "pubkey": pubkey.hex()})
    assert done.status_code == 200, done.text
    txid = done.json()["txid"]
    assert txid in rpc.call("getrawmempool"), "the node took it"

    # And the engine reads it as the claim it is.
    rpc.call("generate", 1)
    index = _catch_up(state, rpc)
    assert index.address_of("robin") == mine
    assert index.tag_of(mine) == "robin"
    assert app.get("/account").json()["tag"] == "robin"


def test_an_offer_cannot_be_signed_twice(arcade):
    """Two transactions spending one coin: the network takes one and
    refuses the other, after somebody was told both had gone."""
    app, state, rpc = arcade
    _sign_in(app)
    pubkey = _pubkey(SECRET + 1)
    mine = b58check_encode(state.messaging.params.pubkeyhash_version,
                           hash160(pubkey))
    rpc.call("generate", 101)
    _catch_up(state, rpc)
    app.post("/account/address", json={"address": mine})
    rpc.call("sendtoaddress", mine, 4.0)
    rpc.call("generate", 1)
    _catch_up(state, rpc)

    offer = app.post("/account/claim", json={"tag": "twice"}).json()
    signatures = [_sign(SECRET + 1, bytes.fromhex(h)).hex()
                  for h in offer["sighashes"]]
    first = app.post("/account/sign", json={
        "offer": offer["offer"], "signatures": signatures,
        "pubkey": pubkey.hex()})
    assert first.status_code == 200
    again = app.post("/account/sign", json={
        "offer": offer["offer"], "signatures": signatures,
        "pubkey": pubkey.hex()})
    assert again.status_code == 400
    assert "not one this node is holding" in again.json()["detail"]


def test_an_offer_belongs_to_the_account_that_asked_for_it(arcade):
    app, state, rpc = arcade
    _sign_in(app)
    pubkey = _pubkey(SECRET + 2)
    mine = b58check_encode(state.messaging.params.pubkeyhash_version,
                           hash160(pubkey))
    rpc.call("generate", 101)
    _catch_up(state, rpc)
    app.post("/account/address", json={"address": mine})
    rpc.call("sendtoaddress", mine, 4.0)
    rpc.call("generate", 1)
    _catch_up(state, rpc)
    offer = app.post("/account/claim", json={"tag": "mine"}).json()

    app.cookies.clear()
    _sign_in(app)                              # somebody else entirely
    stolen = app.post("/account/sign", json={
        "offer": offer["offer"], "signatures": ["00"], "pubkey": pubkey.hex()})
    assert stolen.status_code == 400
    assert "somebody else" in stolen.json()["detail"]


def test_a_name_already_taken_is_refused_before_anything_is_paid_for(arcade):
    app, state, rpc = arcade
    _sign_in(app)
    pubkey = _pubkey(SECRET + 3)
    mine = b58check_encode(state.messaging.params.pubkeyhash_version,
                           hash160(pubkey))
    rpc.call("generate", 101)
    _catch_up(state, rpc)
    app.post("/account/address", json={"address": mine})
    rpc.call("sendtoaddress", mine, 4.0)
    rpc.call("generate", 1)
    index = _catch_up(state, rpc)

    offer = app.post("/account/claim", json={"tag": "taken"}).json()
    signatures = [_sign(SECRET + 3, bytes.fromhex(h)).hex()
                  for h in offer["sighashes"]]
    app.post("/account/sign", json={"offer": offer["offer"],
                                    "signatures": signatures,
                                    "pubkey": pubkey.hex()})
    rpc.call("generate", 1)
    _catch_up(state, rpc)

    app.cookies.clear()
    _sign_in(app)
    other = _pubkey(SECRET + 4)
    theirs = b58check_encode(state.messaging.params.pubkeyhash_version,
                             hash160(other))
    app.post("/account/address", json={"address": theirs})
    rpc.call("sendtoaddress", theirs, 4.0)
    rpc.call("generate", 1)
    _catch_up(state, rpc)
    refused = app.post("/account/claim", json={"tag": "taken"})
    assert refused.status_code == 400
    assert "is taken" in refused.json()["detail"]


def test_a_name_that_is_not_a_name_is_refused(arcade):
    app, state, rpc = arcade
    _sign_in(app)
    pubkey = _pubkey(SECRET + 5)
    mine = b58check_encode(state.messaging.params.pubkeyhash_version,
                           hash160(pubkey))
    rpc.call("generate", 101)
    _catch_up(state, rpc)
    app.post("/account/address", json={"address": mine})
    for bad in ("", "a b", "@@", "x" * 40, "Мартин"):
        refused = app.post("/account/claim", json={"tag": bad})
        assert refused.status_code == 400, bad


def test_an_address_for_the_wrong_chain_is_refused(arcade):
    app, state, rpc = arcade
    _sign_in(app)
    from arcade.config import NETWORKS
    mainnet = b58check_encode(NETWORKS["main"].pubkeyhash_version, b"\x11" * 20)
    refused = app.post("/account/address", json={"address": mainnet})
    assert refused.status_code == 400
