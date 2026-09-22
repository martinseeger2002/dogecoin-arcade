"""An account inscribing a piece, with a key the node has never seen.

The bytes are the same bytes the node's own wallet would put up, and so is the
transaction shape. The only difference is who is able to sign for it -- which
is the difference between a node that holds everybody's money and a node that
carries other people's coins.

Against a real regtest node, for the reason every account test before this one
gives: the parts that can be wrong here are the ones that look right on paper,
and an inscription the index cannot see is a file somebody paid for and never
got. This one goes further than the message tests do for the same reason --
`inscribe.py`'s own docstring records an inscription that was paid for,
accepted by the chain, filed as a valid type-200 transaction, and simply never
appeared, because it was wrapped twice.
"""

import base64
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_claim import _catch_up, _sign_in                # noqa: E402
from test_funding import _pubkey, _sign                           # noqa: E402
from test_web import app_state, client                            # noqa: E402,F401

from arcade.script import b58check_encode, hash160                 # noqa: E402

COIN = 100_000_000
SECRET = 0x5e5e5e5e0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c


@pytest.fixture
def arcade(tmp_path, regtest):
    """The application pointed at a regtest node, with nothing in it."""
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


@pytest.fixture
def seated(arcade):
    """A seat, an address this node cannot sign for, and four coins on it."""
    app, state, rpc = arcade
    _sign_in(app)
    pubkey = _pubkey(SECRET)
    mine = b58check_encode(state.messaging.params.pubkeyhash_version,
                           hash160(pubkey))
    assert not rpc.call("validateaddress", mine).get("ismine"), \
        "the node has no key for this address, which is the whole point"
    rpc.call("generate", 101)
    _catch_up(state, rpc)
    # The coin key travels with the address because a Class B payload puts
    # the sender's key into every data output, and the node cannot derive it:
    # the one thing it is not allowed to have is the key itself.
    assert app.post("/account/address", json={
        "address": mine, "coin_pubkey": pubkey.hex()}).status_code == 200
    rpc.call("sendtoaddress", mine, 4.0)
    rpc.call("generate", 1)
    _catch_up(state, rpc)
    return app, state, rpc, pubkey, mine


def _ask(app, content: bytes, kind: str = "text/plain; charset=utf-8", **extra):
    return app.post("/account/inscribe", json={
        "content": base64.b64encode(content).decode(),
        "content_type": kind, **extra})


def _sign_and_send(app, pubkey, offer):
    signatures = [_sign(SECRET, bytes.fromhex(digest)).hex()
                  for digest in offer["sighashes"]]
    return app.post("/account/sign", json={
        "offer": offer["offer"], "signatures": signatures,
        "pubkey": pubkey.hex()})


def test_an_account_inscribes_one_piece_with_a_key_the_node_never_saw(seated):
    app, state, rpc, pubkey, mine = seated
    text = "the first thing this account put on the chain by itself"

    offered = _ask(app, text.encode(), name="a note")
    assert offered.status_code == 200, offered.text
    offer = offered.json()
    assert offer["what"] == "inscribe a note"
    assert offer["sighashes"] and len(offer["sighashes"]) == len(offer["inputs"])
    assert offer["bytes"] == len(text.encode())
    assert rpc.call("getrawmempool") == [], "nothing has gone out"

    done = _sign_and_send(app, pubkey, offer)
    assert done.status_code == 200, done.text
    txid = done.json()["txid"]
    assert txid in rpc.call("getrawmempool"), "the node took it"

    rpc.call("generate", 1)
    index = _catch_up(state, rpc)
    row = index.inscription(txid)
    assert row is not None, "the engine filed it as an inscription"
    assert row["creator"] == mine and row["owner"] == mine
    assert row["content_len"] == len(text.encode())
    assert index.inscription_content(txid) == (
        "text/plain; charset=utf-8", text.encode()), "the file is the file"

    # And the account's own page can see it, which is how they find out it
    # landed -- without this, a piece that only the index could name.
    held = app.get("/account/nfts").json()["chains"][0]["pieces"]
    assert [piece["txid"] for piece in held] == [txid]


def test_more_than_one_piece_is_refused_before_anything_is_paid(seated):
    """Half an inscription is the failure to avoid, so it is refused first.

    A second piece spends the first one's output, so it cannot even be built
    until that one is in a block. A route that started anyway would take the
    fee for piece one and leave a file that assembles into nothing.
    """
    app, state, rpc, pubkey, mine = seated
    answer = _ask(app, b"x" * 20_000, kind="image/png", name="a big one")
    assert answer.status_code == 400
    said = answer.json()["detail"]
    assert "3 pieces" in said, said
    assert "wait for a block" in said, said
    assert rpc.call("getrawmempool") == []
    assert app.get("/account").json()["balance"] == int(4.0 * COIN), \
        "a refusal costs nothing"


def test_an_operator_who_closed_inscriptions_says_which_it_is(seated):
    """The dial on the Overview is the one an operator most wants: an
    inscription is the one thing here that cannot be taken back."""
    app, state, rpc, pubkey, mine = seated
    state.set_setting("quota:inscribe", 0)
    answer = _ask(app, b"closed for business")
    assert answer.status_code == 400
    assert "not taking inscriptions" in answer.json()["detail"]
    assert rpc.call("getrawmempool") == []


def test_the_bytes_of_an_inscription_count_against_the_day(seated):
    """Whatever carries them, the ceiling is on what the chain has to keep."""
    app, state, rpc, pubkey, mine = seated
    state.set_setting("quota:bytes", 30)
    answer = _ask(app, b"y" * 200)
    assert answer.status_code == 400
    assert "bytes a day" in answer.json()["detail"]
    assert rpc.call("getrawmempool") == []


def test_every_account_route_is_one_a_public_node_will_reach(client):
    """The door lists its POST paths one at a time, so a route left off the
    list works on every test machine and 403s on the instances where the
    people actually are."""
    from arcade.web import door

    app, _ = client
    asked = {route.path for route in app.app.routes
             if (getattr(route, "methods", None) or set()) & {"POST"}
             and route.path.startswith("/account/")}
    listed = {path.rstrip("/") or "/" for path in door.PUBLIC_POST}
    assert asked - listed == set(), \
        f"{sorted(asked - listed)} needs adding to door.PUBLIC_POST"
