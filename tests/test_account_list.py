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
import contextlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_claim import _catch_up, _sign_in                   # noqa: E402
from test_funding import _pubkey, _sign                              # noqa: E402
from test_web import app_state, client                               # noqa: F401,E402

from arcade import encoding, fees, funding, inscriptions as I        # noqa: E402
from arcade import payload as P, utxos                               # noqa: E402
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
    assert "1 coin" in said["what"], said["what"]
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
    assert "Split it first" in detail, detail

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


def _balance(state, address: str) -> int:
    """What an address can spend, out of the index rather than the wallet.

    The node's `listunspent` answers about its own wallet, and these addresses
    are precisely the ones it holds no key for. The watched-coin index is the
    honest place to ask, and it is how the node builds a transaction for them.
    """
    index = state.token_index(state.messaging)
    with contextlib.closing(index.open()) as db:
        return utxos.balance(db, address)


def test_a_leg_answered_to_one_account_completes_without_a_listing(shop):
    """A leg sent to one buyer is a trade, not a listing -- and it still fills.

    The seller builds the leg exactly as it would for a shelf, signs the two
    SINGLE|ANYONECANPAY signatures, and hands the bytes and those signatures to
    one buyer instead of handing them to `Listings.register`. Filed, that leg
    would put one buyer's price on a public page for every stranger to take, so
    `record=False` runs every check and writes nothing at all, and the buyer's
    half is finished with `paste_leg` out of a row that exists for one request.

    Asserted hardest is that this is the SAME trade `/account/buy` finishes. The
    piece moves, and -- the part that was silently broken once before, when a
    completion kept back the fee a leg reserves and the engine read the seller as
    paid below its own price -- the index calls the swap VALID. Asserted just as
    hard is the difference: the listing book stays empty, before and after.
    """
    app, state, rpc = shop
    seller, seller_key, seller_addr = _seated(app, state, rpc, 11)
    piece = _inscribed(app, state, rpc, seller, seller_key,
                       "answered, not listed")
    asked = app.post("/account/list",
                     json={"piece": piece, "amount": "1"}).json()
    signing = {"raw": asked["raw"],
               "signatures": [_sign(seller, bytes.fromhex(d),
                                    funding.SINGLE_ANYONECANPAY).hex()
                              for d in asked["sighashes"]],
               "pubkey": seller_key.hex(), "seller": seller_addr,
               "amount": "1"}

    buyer, buyer_key, buyer_addr = _seated(app, state, rpc, 12)
    held, owed = _balance(state, seller_addr), _balance(state, buyer_addr)

    answered = app.post("/account/fill", json={"leg": signing})
    assert answered.status_code == 200, answered.text
    said = answered.json()
    assert said["answered"] is True and said["price"] == COIN
    assert said["seller"] == seller_addr, "whose piece this is, from the leg"
    assert said["signed_from"] == 2, \
        "the piece and the coin under its second signature are the seller's"
    assert {coin["address"] for coin in said["inputs"][2:]} == {buyer_addr}
    assert state.listings.open_listings("regtest") == [], "nothing advertised"
    assert rpc.call("getrawmempool") == [], "and nothing spent"

    done = app.post("/account/fill/sign", json={
        "leg": signing, "raw": said["raw"], "pubkey": buyer_key.hex(),
        "signatures": [_sign(buyer, bytes.fromhex(d)).hex()
                       for d in said["sighashes"]]})
    assert done.status_code == 200, done.text
    assert done.json()["txid"] in rpc.call("getrawmempool")
    assert done.json()["price"] == COIN
    assert state.listings.open_listings("regtest") == [], \
        "finishing a trade is not what turns it into a listing"

    rpc.call("generate", 1)
    _catch_up(state, rpc)
    index = state.token_index(state.messaging)
    with contextlib.closing(index.open()) as db:
        verdict = db.conn.execute(
            "SELECT valid, invalid_reason FROM arcade_tx WHERE txid = ?",
            (done.json()["txid"],)).fetchone()
        owner = db.conn.execute(
            "SELECT owner FROM inscription WHERE txid = ?", (piece,)).fetchone()
    assert verdict["valid"] == 1, verdict["invalid_reason"]
    assert owner["owner"] == buyer_addr, "the piece moved"
    assert _balance(state, seller_addr) == held + COIN, \
        "the seller nets the price to the satoshi, reservation handed back"
    assert owed - COIN - COIN // 10 < _balance(state, buyer_addr) < owed - COIN, \
        "the buyer pays the price and the whole fee, §1d"


def test_a_listing_put_on_the_chain_is_filed_by_a_node_that_never_saw_it(shop):
    """2026-09-27 ("On the chain, batched"): a mintpad sells on every
    node because its listings are inscribed and every node files them from the
    chain, through the same checks a browser's leg gets. Simulated on one node
    by forgetting the row and reading it back off the chain."""
    import sqlite3
    from arcade import listing_announce as la

    app, state, rpc = shop
    secret, pubkey, mine = _seated(app, state, rpc, 7, coins=(4.0, 1.0, 2.0))
    piece = _inscribed(app, state, rpc, secret, pubkey, "a piece sold everywhere")
    said = app.post("/account/list", json={"piece": piece, "amount": "1"}).json()
    listed = _signed(app, secret, pubkey, said)
    assert listed.status_code == 200, listed.text
    row = state.listings.get(listed.json()["listed"])

    raw, sigs, key = la.unsign(row["leg"])
    assert key == pubkey and len(sigs) == 2
    assert raw == said["raw"], "the leg without its scriptSigs is the leg that was signed"

    content = la.batch_json(state.messaging.network, [row])
    offered = app.post("/account/inscribe", json={
        "content": base64.b64encode(content).decode(), "content_type": la.CONTENT_TYPE})
    assert offered.status_code == 200, offered.text
    offer = offered.json()
    assert offer.get("chunks", 1) == 1
    done = app.post("/account/sign", json={
        "offer": offer["offer"], "pubkey": pubkey.hex(),
        "signatures": [_sign(secret, bytes.fromhex(d)).hex() for d in offer["sighashes"]]})
    assert done.status_code == 200, done.text
    rpc.call("generate", 1)
    _catch_up(state, rpc)

    with sqlite3.connect(state.listings.path) as conn:        # a node that never had it
        conn.execute("DELETE FROM listing WHERE id=?", (row["id"],))
    assert not state.listings.has_leg(state.messaging.network, row["leg"])

    assert la.import_announced(state, state.messaging) == 1
    again = [r for r in state.listings.open_listings(state.messaging.network)
             if r["leg"] == row["leg"]]
    assert len(again) == 1 and int(again[0]["price"]) == COIN and again[0]["owner"] == mine
    assert la.import_announced(state, state.messaging) == 0, "read once, filed once"


def test_a_listing_keeps_its_coins_after_a_restart(shop):
    """2026-09-27: listings on a mintpad went "spent" because the only thing
    keeping the seller's own sends off a listing's coins lived in memory, and
    every restart forgot it. The listings book keeps them reserved now."""
    from fastapi.testclient import TestClient
    from arcade.web.app import create_app

    app, state, rpc = shop
    secret, pubkey, mine = _seated(app, state, rpc, 8, coins=(4.0, 1.0, 2.0))
    piece = _inscribed(app, state, rpc, secret, pubkey, "a piece that stays for sale")
    said = app.post("/account/list", json={"piece": piece, "amount": "1"}).json()
    listed = _signed(app, secret, pubkey, said)
    assert listed.status_code == 200, listed.text
    row = state.listings.get(listed.json()["listed"])
    held = {(row["input"]["txid"], int(row["input"]["vout"]))}
    if row.get("coin"):
        held.add((row["coin"]["txid"], int(row["coin"]["vout"])))

    fresh = TestClient(create_app(state))          # a restart: nothing in memory
    _sign_in(fresh)
    assert fresh.post("/account/address", json={
        "address": mine, "coin_pubkey": pubkey.hex()}).status_code == 200
    # More than the one big coin holds, so a send that could reach the
    # listing's coins would: it must either leave them alone or be refused.
    offer = fresh.post("/account/send", json={"to": mine, "amount": "4.5"})
    if offer.status_code == 200:
        used = {(c["txid"], int(c["vout"])) for c in offer.json()["inputs"]}
        assert not (used & held), "a send must not spend the coins a listing stands on"
    else:
        assert "not enough" in offer.text or "enough" in offer.text, offer.text
