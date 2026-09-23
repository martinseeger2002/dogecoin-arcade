"""An account puts a piece on the shelf, with a signature the node cannot make.

A listing is not a transaction. Nothing here broadcasts: the node builds a leg,
somebody else signs it twice, and the node keeps those two signatures as a
promise it could not have made itself -- because making them needs a coin key
this node does not have and never asks for. What is tested hardest is therefore
not what a successful listing looks like but what a listing is *bound* to: the
bytes at output 0 that a buyer will be shown, the coin the seller's second
signature stands on, and the price that arithmetic leaves no room to fudge.

Two requests, with nothing kept between them. `/account/list` reads the index
and shows the leg; `/account/list/sign` takes the signatures back and files the
row through `Listings.register`, which re-derives everything from the leg's own
bytes and `gettxout`. So the second half works after a restart, from a leg
built somewhere else entirely, and cannot be talked into filing a row that the
signatures do not back.

Against a real regtest node, for the reason every account test before this one
gives: everything that could be wrong here looks right on paper -- the two
SINGLE|ANYONECANPAY preimages, what the chain says about the coin behind the
second signature, and whether the row this node files is the row a buyer will
be asked to complete.
"""

import base64
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_claim import _catch_up, _sign_in                   # noqa: E402
from test_funding import _pubkey, _sign                              # noqa: E402
from test_web import app_state, client                               # noqa: F401,E402

from arcade import encoding, fees, funding, inscriptions as I        # noqa: E402
from arcade import payload as P                                      # noqa: E402
from arcade.script import b58check_encode, hash160                   # noqa: E402

COIN = 100_000_000


@pytest.fixture
def shop(tmp_path, regtest):
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


def _seated(app, state, rpc, which: int, coins=(4.0, 1.0)):
    """An account, a coin key of its own, and the coins it can spend.

    `which` picks the coin key. Every account file on this session's node
    signs for one key, so the coins on that address belong to nobody in
    particular -- and a listing is precisely about how many coins an address
    holds, which needs an address one test owns.

    Two coins, not one, and the reason is what a Class C inscription is: the
    inscribe spends the coin it is funded with and pays the change back to the
    same address, so a funded-by-one account holds exactly one coin afterwards,
    which is the piece's own coin. A leg that says what it sells needs a second
    one to stand under the price, so a one-coin account could never get as far
    as the screen these tests are about. The last test funds itself with a
    single coin on purpose, to show what that refusal says.
    """
    _sign_in(app)
    secret = int.from_bytes(bytes([0x51, which]) + bytes(30), "big")
    pubkey = _pubkey(secret)
    mine = b58check_encode(state.messaging.params.pubkeyhash_version,
                           hash160(pubkey))
    assert not rpc.call("validateaddress", mine).get("ismine"), \
        "the node has no key for this address, which is the whole point"
    rpc.call("generate", 101)
    _catch_up(state, rpc)
    assert app.post("/account/address", json={
        "address": mine, "coin_pubkey": pubkey.hex()}).status_code == 200
    for amount in coins:
        rpc.call("sendtoaddress", mine, amount)
    rpc.call("generate", 1)
    _catch_up(state, rpc)
    return secret, pubkey, mine


def _inscribed(app, state, rpc, secret, pubkey, text: str) -> str:
    """One piece this account put on the chain itself, and its txid."""
    offered = app.post("/account/inscribe", json={
        "content": base64.b64encode(text.encode()).decode(),
        "content_type": "text/plain; charset=utf-8"})
    assert offered.status_code == 200, offered.text
    offer = offered.json()
    done = app.post("/account/sign", json={
        "offer": offer["offer"], "pubkey": pubkey.hex(),
        "signatures": [_sign(secret, bytes.fromhex(d)).hex()
                       for d in offer["sighashes"]]})
    assert done.status_code == 200, done.text
    rpc.call("generate", 1)
    _catch_up(state, rpc)
    return done.json()["txid"]


def _names(txid: str, price: int) -> bytes:
    """What a listing writes at output 0: the trade the finished swap IS.

    Written here as well as in `app._ask_payload` on purpose. These two have to
    agree byte for byte or the payload a buyer is shown is not the one the
    signatures stand over, and the only way to check that is for one of them to
    be written by somebody who did not write the other.
    """
    body = I.Swap(give=I.Leg(I.LEG_INSCRIPTION, txid=bytes.fromhex(txid)),
                  take=I.Leg(I.LEG_COINS, amount=price)).encode()
    return encoding.encode_class_c(P.AnyData(data=body).encode())


def _signed(app, secret, pubkey, said, signatures=None, amount: str = "1"):
    """Bring the signatures back. `signatures` overrides what gets sent."""
    body = {"raw": said["raw"], "amount": amount, "pubkey": pubkey.hex(),
            "signatures": signatures if signatures is not None else [
                _sign(secret, bytes.fromhex(dig),
                      funding.SINGLE_ANYONECANPAY).hex()
                for dig in said["sighashes"]]}
    return app.post("/account/list/sign", json=body)


def test_listing_a_piece_asks_for_two_signatures_and_says_what_they_promise(shop):
    """The screen before signing, which is the only place a seller decides.

    Two digests, because one signature commits to one output: the first stands
    over the bytes naming the piece, the second over the price. One digest on
    this screen would mean a leg whose payment is signed by nobody -- which is
    what it was until the second input existed.
    """
    app, state, rpc = shop
    secret, pubkey, mine = _seated(app, state, rpc, 1)
    piece = _inscribed(app, state, rpc, secret, pubkey, "a piece to list")

    asked = app.post("/account/list", json={"piece": piece, "amount": "1"})
    assert asked.status_code == 200, asked.text
    said = asked.json()
    assert len(said["sighashes"]) == 2 and said["sighashes"][0] != \
        said["sighashes"][1], "two outputs, so two digests"
    assert {coin["address"] for coin in said["inputs"]} == {mine}, \
        "both inputs are the seller's -- a leg is its seller's coins in"
    assert said["payload"] == _names(piece, COIN).hex(), \
        "the bytes it shows are the bytes the swap will be"
    assert f"inscription #{said['number']}" in said["what"], said["what"]
    assert "1 coins" in said["what"], said["what"]
    assert rpc.call("getrawmempool") == [], "showing a listing spends nothing"


def test_the_two_signatures_file_a_row_and_nothing_is_broadcast(shop):
    """The listing is a signature the node holds, not a transaction it made.

    So the mempool stays empty and the row says everything the leg says: the
    same payload, the same second coin, the price the arithmetic allows.
    """
    app, state, rpc = shop
    secret, pubkey, mine = _seated(app, state, rpc, 2)
    piece = _inscribed(app, state, rpc, secret, pubkey, "listed by its owner")
    said = app.post("/account/list",
                    json={"piece": piece, "amount": "1"}).json()

    done = _signed(app, secret, pubkey, said)
    assert done.status_code == 200, done.text
    row = state.listings.get(done.json()["listed"])
    assert row["owner"] == mine and int(row["price"]) == COIN
    assert row["payload"] == said["payload"]
    assert (row["coin"]["txid"], int(row["coin"]["vout"])) == \
        (said["inputs"][1]["txid"], int(said["inputs"][1]["vout"])), \
        "the row names the coin the second signature stands on"
    assert "gives inscription" in done.json()["what"], done.json()
    assert rpc.call("getrawmempool") == [], "a listing is not a transaction"


def test_the_second_signature_is_not_optional(shop):
    """One signature and no coin behind the payment is not a listing.

    It is the shape the node used to accept, and the reason it stopped: the
    payment at output 1 was then committed to by nothing, so whoever completed
    the swap decided what the seller was paid.
    """
    app, state, rpc = shop
    secret, pubkey, mine = _seated(app, state, rpc, 3)
    piece = _inscribed(app, state, rpc, secret, pubkey, "one signature short")
    said = app.post("/account/list",
                    json={"piece": piece, "amount": "1"}).json()
    one = [_sign(secret, bytes.fromhex(said["sighashes"][0]),
                 funding.SINGLE_ANYONECANPAY).hex()]

    refused = _signed(app, secret, pubkey, said, signatures=one)
    assert refused.status_code == 400
    assert "signature" in refused.json()["detail"], refused.json()["detail"]
    assert state.listings.for_piece(
        said["inputs"][0]["txid"], int(said["inputs"][0]["vout"])) == []
    assert rpc.call("getrawmempool") == []


def test_a_leg_built_without_this_node_files_the_same_row(shop):
    """There is no privileged path: the bytes and the chain are the whole test.

    The leg here comes from `funding.build_leg` and not from `/account/list`,
    and the row is the row it would have been. This is what makes the two
    requests safe to leave unconnected -- a node that forgot the first one
    files exactly what a node that remembered it would have filed.
    """
    app, state, rpc = shop
    secret, pubkey, mine = _seated(app, state, rpc, 4)
    piece = _inscribed(app, state, rpc, secret, pubkey, "built elsewhere")
    said = app.post("/account/list",
                    json={"piece": piece, "amount": "1"}).json()

    leg = funding.build_leg(state.messaging.params, mine, said["inputs"][0],
                            coins=COIN, rate=fees.MIN_FEE_PER_KB,
                            payload=bytes.fromhex(said["payload"]),
                            coin=said["inputs"][1])
    assert leg.sighashes == said["sighashes"], \
        "the same leg, whether this node built it or the wallet did"
    done = app.post("/account/list/sign", json={
        "raw": leg.raw,
        "signatures": [_sign(secret, bytes.fromhex(dig),
                             funding.SINGLE_ANYONECANPAY).hex()
                       for dig in leg.sighashes],
        "pubkey": pubkey.hex(), "amount": "1"})
    assert done.status_code == 200, done.text
    row = state.listings.get(done.json()["listed"])
    assert row["payload"] == said["payload"]
    assert int(row["price"]) == COIN and row["owner"] == mine


def test_a_piece_that_is_not_yours_is_not_yours_to_price(shop):
    """An ask is honoured only while its owner holds it, and so is a listing.

    The second account is not decoration: this is the only way to ask whether
    the route reads ownership from the chain or from whoever typed the piece
    in first.
    """
    app, state, rpc = shop
    secret, pubkey, mine = _seated(app, state, rpc, 5)
    piece = _inscribed(app, state, rpc, secret, pubkey, "somebody else's")

    _seated(app, state, rpc, 6)
    asked = app.post("/account/list", json={"piece": piece, "amount": "1"})
    assert asked.status_code == 400
    assert "only whoever holds a piece can price it" in asked.json()["detail"]
    assert rpc.call("getrawmempool") == []


def test_one_coin_cannot_list_a_piece_it_holds(shop):
    """A named listing needs two of the seller's coins, and says so.

    The piece is there and the price is fine -- the address simply holds one
    coin, and a leg that names what it sells signs two. The sentence has to
    carry the way out, which is to make a second coin.
    """
    app, state, rpc = shop
    secret, pubkey, mine = _seated(app, state, rpc, 7, coins=(4.0,))
    piece = _inscribed(app, state, rpc, secret, pubkey, "one coin here")

    asked = app.post("/account/list", json={"piece": piece, "amount": "1"})
    assert asked.status_code == 400
    detail = asked.json()["detail"]
    assert "needs two of them" in detail, detail
    assert "Send yourself a little change" in detail, detail

    rpc.call("sendtoaddress", mine, 1.0)
    rpc.call("generate", 1)
    _catch_up(state, rpc)
    again = app.post("/account/list", json={"piece": piece, "amount": "1"})
    assert again.status_code == 200, again.text
    assert len(again.json()["sighashes"]) == 2


def test_showing_a_leg_costs_nothing_and_filing_one_is_counted(shop):
    """The split is the allowance: one request costs a read, the other a row.

    An operator who closes listings to accounts must still let an account see
    the leg it could compute for itself -- holding the key is enough to
    compute it. What the dial has to stop is the second request, the one that
    puts a row on a public page.
    """
    app, state, rpc = shop
    secret, pubkey, mine = _seated(app, state, rpc, 8)
    piece = _inscribed(app, state, rpc, secret, pubkey, "the counted half")
    state.set_setting("quota:list", 0)

    asked = app.post("/account/list", json={"piece": piece, "amount": "1"})
    assert asked.status_code == 200, "closing listings does not close showing"
    said = asked.json()

    refused = _signed(app, secret, pubkey, said)
    assert refused.status_code == 400
    assert "not taking listings" in refused.json()["detail"]
    assert state.listings.for_piece(
        said["inputs"][0]["txid"], int(said["inputs"][0]["vout"])) == []
