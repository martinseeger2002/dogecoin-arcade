"""A page talking to another node, with a key the node has never seen.

The other three doors of an inscribed page got their account paths with the
decisions that opened them: a shop buys through the shop door, a page
remembers in the browser that shows it. This is the fourth, and the one that
says a thing rather than porting one (D-169): `/account/talk` builds the
carrier for a message the node cannot read, and offers it for a signature it
cannot make.

The seal is produced HERE the way `sealForProgram` produces it in the browser
-- ciphertext only, the framing header left for the route to write -- because
a message the recipient cannot open is a message somebody paid for and never
spoke, and the only way to know that is to put one on a real chain and read
it back with the key it was sealed to.
"""

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_claim import _catch_up, _sign_in                # noqa: E402
from test_funding import _pubkey, _sign                           # noqa: E402
from test_web import app_state, client                            # noqa: E402,F401

from arcade.messaging import api as apilib                        # noqa: E402
from arcade.messaging import contact as contactlib                # noqa: E402
from arcade.messaging import envelope                             # noqa: E402
from arcade.messaging.keys import Identity, fingerprint_of        # noqa: E402
from arcade.messaging.scanner import Scanner                      # noqa: E402
from arcade.script import b58check_encode, hash160                # noqa: E402

COIN = 100_000_000
SECRET = 0x1a2b3c4d0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c


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
    assert app.post("/account/address", json={
        "address": mine, "coin_pubkey": pubkey.hex()}).status_code == 200
    rpc.call("sendtoaddress", mine, 4.0)
    rpc.call("generate", 1)
    _catch_up(state, rpc)
    return app, state, rpc, pubkey, mine


def _node(state) -> str:
    """The key a page here would speak to: this node's own messaging key."""
    return state.ensure_identity().public_bytes.hex()


def _seal(me: Identity, to: str, body: bytes) -> bytes:
    """What the browser's `sealForProgram` hands the route, in Python.

    The API stamp goes inside the ciphertext and the envelope's own framing
    header does not: writing that is the route's job, so a page relaying
    through an account never prefixes its envelope twice.
    """
    return envelope.seal_ciphertext(
        me, bytes.fromhex(to), envelope.Header(type=envelope.TYPE_API),
        apilib.stamp() + body)


def _ask(app, body: dict):
    return app.post("/account/talk", json=body)


def _sign_and_send(app, pubkey, offer):
    signatures = [_sign(SECRET, bytes.fromhex(digest)).hex()
                  for digest in offer["sighashes"]]
    return app.post("/account/sign", json={
        "offer": offer["offer"], "signatures": signatures,
        "pubkey": pubkey.hex()})


def _api_rows(state, rpc):
    """What this node can open, read as the node -- mine, scanned, opened."""
    me = state.ensure_identity()
    with state.store() as store:
        scanner = Scanner(rpc, state.messaging.params, store, identity=me)
        for _ in range(50):
            if scanner.scan().blocks == 0:
                break
        # scan() opens what it fetched and nothing else, and the tail is
        # never reached once the cursor sits at the tip -- `scanner` here has
        # the same problem `_opened` in test_many_accounts documents.
        scanner.open_pending()
        return list(store.api_messages(fingerprint_of(me.public_bytes),
                                       state.messaging.network))


def test_a_page_speaks_with_a_key_the_node_never_saw(seated):
    app, state, rpc, pubkey, mine = seated
    me, body = Identity.generate(), json.dumps({"order": "hat"}).encode()
    sealed = _seal(me, _node(state), body)

    offered = _ask(app, {"op": "send", "to": _node(state),
                         "sealed": sealed.hex()})
    assert offered.status_code == 200, offered.text
    offer = offered.json()
    assert offer["bytes"] == len(sealed)
    assert offer["what"] == "a page's message to another node"
    assert offer["chain"] == "regtest" and offer["to"] == _node(state)
    assert rpc.call("getrawmempool") == [], "nothing has gone out"

    sent = _sign_and_send(app, pubkey, offer)
    assert sent.status_code == 200, sent.text
    txid = sent.json()["txid"]
    assert txid in rpc.call("getrawmempool"), "the node took it"

    rpc.call("generate", 1)
    _catch_up(state, rpc)
    row = {r["txid"]: r for r in _api_rows(state, rpc)}.get(txid)
    assert row is not None, "the node read the carrier and opened what was inside"
    assert bytes(row["body"]) == body, "and it is the body the page sealed"
    assert bytes(row["sender_pubkey"]) == me.public_bytes, \
        "the message speaks with the key that sealed it, not with the coins " \
        "that paid for the transaction"
    assert apilib.compatible(row["protocol"], bytes(row["fingerprint"]))


def test_the_node_says_what_its_API_speaks(seated):
    app, state, rpc, pubkey, mine = seated
    said = _ask(app, {"op": "identity"})
    assert said.status_code == 200, said.text
    out = said.json()
    assert out["ok"] and out["network"] == "regtest"
    assert out["stamp"] == apilib.stamp().hex()
    assert out["maxbytes"] == apilib.MAX_API_PAYLOAD
    assert bytes.fromhex(out["stamp"])[0:2] == b"DA", \
        "the browser checks these two bytes before it seals anything to them"


def test_ask_says_which_key_the_seal_goes_to_and_reads_nothing(seated):
    app, state, rpc, pubkey, mine = seated
    to = Identity.generate().public_bytes.hex()
    asked = _ask(app, {"op": "ask", "to": to})
    assert asked.status_code == 200, asked.text
    assert asked.json()["seal_to"] == to
    assert asked.json()["stamp"] == apilib.stamp().hex()

    # A contact code is the same key said another way, and both are the
    # browser's to spell: the route reads neither one as an address, and an
    # ask costs the chain nothing either way.
    code = contactlib.encode(state.messaging.network, Identity.generate().public_bytes)
    again = _ask(app, {"op": "ask", "to": code})
    assert again.status_code == 200, again.text
    assert again.json()["seal_to"] == contactlib.decode(code)[1].hex()

    for bad in ("", "not a key", "ab" * 31, "zz" * 32):
        refused = _ask(app, {"op": "ask", "to": bad})
        assert refused.status_code == 400, bad
        assert refused.json()["detail"], bad
    guess = _ask(app, {"op": "guess", "to": to})
    assert guess.status_code == 400
    assert "op must be" in guess.json()["detail"]
    assert rpc.call("getrawmempool") == [], "an ask offers nothing to sign"


def test_a_message_too_big_for_one_transaction_is_refused_before_it_costs(seated):
    app, state, rpc, pubkey, mine = seated
    sealed = _seal(Identity.generate(), _node(state), b'"too big"' * 20000)
    refused = _ask(app, {"op": "send", "to": _node(state),
                         "sealed": sealed.hex()})
    assert refused.status_code == 400, refused.text
    assert "one transaction" in refused.json()["detail"]
    assert rpc.call("getrawmempool") == []


def test_an_account_that_lives_on_mainnet_is_told_why_it_cannot(tmp_path):
    """Sealed messages are testnet's, only and always (D-010). The refusal is
    a property of the protocol rather than of any setting, and it happens in
    the route rather than in the tab, so the account on real coins is not
    offered a transaction the messaging chain would never carry."""
    from fastapi.testclient import TestClient

    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    nowhere = pathlib.Path("/nonexistent")
    state = AppState(home=tmp_path,
                     messaging=ChainContext(network="main", role="messaging",
                                           label="Mainnet", datadir=nowhere),
                     ledger=ChainContext(network="main", role="ledger",
                                         label="Mainnet", datadir=nowhere))
    app = TestClient(create_app(state))
    _sign_in(app)
    refused = app.post("/account/talk", json={"op": "identity"})
    assert refused.status_code == 400, refused.text
    said = refused.json()["detail"]
    assert "testnet" in said and "mainnet" in said, said


def test_the_hour_is_the_accounts_and_not_the_pages(seated):
    """A count kept in a tab resets with the tab, so a page's own hourly
    tally could only ever be theatre: the meter here is the account's
    `message` dial, and the operator's dial on the Overview closes it the
    same way it closes posts and inscriptions."""
    app, state, rpc, pubkey, mine = seated
    state.set_setting("quota:message", 0)
    refused = _ask(app, {"op": "send", "to": _node(state), "sealed": "00"})
    assert refused.status_code == 400, refused.text
    assert "not taking" in refused.json()["detail"], refused.json()
    assert rpc.call("getrawmempool") == []

    state.set_setting("quota:message", 2)
    sealed = _seal(Identity.generate(), _node(state), b'"ping"').hex()
    for _ in range(2):
        offered = _ask(app, {"op": "send", "to": _node(state), "sealed": sealed})
        assert offered.status_code == 200, offered.text
        assert _sign_and_send(app, pubkey, offered.json()).status_code == 200
    over = _ask(app, {"op": "send", "to": _node(state), "sealed": sealed})
    assert over.status_code == 400, over.text
    assert "messages" in over.json()["detail"], over.json()


def test_nobody_else_can_ask_the_node_to_carry_a_page(client):
    """A stranger with no seat gets the door, not a broadcast: this route is
    in `door.PUBLIC_POST` because an account's own key pays for what goes
    out, and that is also the only reason it opens at all."""
    from arcade.web import door

    app, _ = client
    assert "/account/talk" in door.PUBLIC_POST
    refused = app.post("/account/talk", json={"op": "identity"})
    assert refused.status_code == 403, refused.text
