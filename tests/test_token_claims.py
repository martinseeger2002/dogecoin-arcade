"""Token claims: a prize pool of token lots behind one phrase (2026-09-28:
games that let players EARN a token; tester-e5's GHOST PROTOCOL pays 25 Ghost
Credits to whoever cracks its vault).

A lot is a leg like a listed NFT's with a Simple Send where the swap was: the
seller's coin at input 0 makes the seller its sender, and the tokens go to the
transaction's reference, which the claim always makes the claimer. The page is
given one listing id for the whole pool; each claim takes the next open lot.
"""

import hashlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import node, _seated, _settled                 # noqa: F401,E402
from test_account_order import _bookcoin, _held, _signed, COIN, HELD   # noqa: E402
from test_funding import _sign                                          # noqa: E402
from arcade import funding                                              # noqa: E402

PHRASE = "the vault on level five"


def _lots(book, count=3, lot="25", price="0.01", phrase=PHRASE):
    """Make `count` lots the way the Tokens page does: split if asked, sign each."""
    who, secret, pubkey = book["client"], book["secret"], book["pubkey"]
    ask = {"property_id": book["pid"], "lot": lot, "count": count, "price": price,
           "days": 30}
    said = who.post("/account/claimlots", json=ask)
    assert said.status_code == 200, said.text
    if said.json().get("needs_split"):
        _signed(who, secret, pubkey, said)
        _settled(book["state"], book["rpc"])
        said = who.post("/account/claimlots", json=ask)
        assert said.status_code == 200, said.text
    legs = said.json()["legs"]
    assert len(legs) == count
    ids = []
    for leg in legs:
        filed = who.post("/account/list/sign", json={
            "raw": leg["raw"], "amount": price, "pubkey": pubkey.hex(), "days": 30,
            "claim_hash": hashlib.sha256(phrase.encode()).hexdigest(),
            "signatures": [_sign(secret, bytes.fromhex(d), funding.SINGLE_ANYONECANPAY).hex()
                           for d in leg["sighashes"]]})
        assert filed.status_code == 200, filed.text
        assert filed.json()["claim"]
        ids.append(filed.json()["listed"])
    return ids, legs


def _claim(buyer, listing, phrase=PHRASE):
    client, secret, pubkey, _address = buyer
    asked = client.post("/account/buy", json={"listing": listing, "secret": phrase})
    assert asked.status_code == 200, asked.text
    said = asked.json()
    done = client.post("/account/buy/sign", json={
        "raw": said["raw"], "listing": listing, "secret": phrase, "pubkey": pubkey.hex(),
        "signatures": [_sign(secret, bytes.fromhex(d)).hex() for d in said["sighashes"]]})
    assert done.status_code == 200, done.text
    return said, done.json()


def test_a_pool_pays_each_claimer_one_lot_from_one_listing_id(node):
    book = _bookcoin(node, 81)
    state, rpc = book["state"], book["rpc"]
    ids, legs = _lots(book, count=3)
    net = state.messaging.network
    public = [r["id"] for r in state.listings.open_listings(net, limit=1000)]
    assert not set(ids) & set(public), "no public reader sees a lot"
    assert legs[0]["send"]["name"] == book["name"] and legs[0]["send"]["amount"] == "25"

    first = _seated(node[0], state, rpc, 82)
    said, _ = _claim(first, ids[0])
    assert said["lot"] and said["claim"], said
    assert said["piece"] == f"25 {book['name']}", said["piece"]
    _settled(state, rpc)
    # The tokens go to the last output that is not the seller's: the claimer's.
    assert _held(state, first[3], book["pid"])[0] == 25 * COIN, "the claimer holds the lot"

    # The same id again, from somebody else: the next open lot, not a refusal.
    second = _seated(node[0], state, rpc, 83)
    _claim(second, ids[0])
    _settled(state, rpc)
    assert _held(state, second[3], book["pid"])[0] == 25 * COIN
    assert _held(state, book["address"], book["pid"])[0] == HELD - 50 * COIN


def test_a_lot_needs_its_phrase(node):
    book = _bookcoin(node, 84)
    ids, _ = _lots(book, count=1)
    client = _seated(node[0], book["state"], book["rpc"], 85)[0]
    bare = client.post("/account/buy", json={"listing": ids[0]})
    assert bare.status_code == 400 and "claim phrase" in bare.json()["detail"]
    wrong = client.post("/account/buy", json={"listing": ids[0], "secret": "nope"})
    assert wrong.status_code == 400 and "raw" not in wrong.json()


def test_lots_cannot_promise_more_than_the_seller_holds(node):
    book = _bookcoin(node, 86)
    too_many = book["client"].post("/account/claimlots", json={
        "property_id": book["pid"], "lot": "300", "count": 2, "price": "0.01"})
    assert too_many.status_code == 400 and "not already standing" in too_many.json()["detail"]


def test_a_token_lot_is_only_ever_a_claim(node):
    book = _bookcoin(node, 87)
    who, secret, pubkey = book["client"], book["secret"], book["pubkey"]
    ask = {"property_id": book["pid"], "lot": "5", "count": 1, "price": "0.01"}
    said = who.post("/account/claimlots", json=ask)
    if said.json().get("needs_split"):
        _signed(who, secret, pubkey, said)
        _settled(book["state"], book["rpc"])
        said = who.post("/account/claimlots", json=ask)
    leg = said.json()["legs"][0]
    public = who.post("/account/list/sign", json={
        "raw": leg["raw"], "amount": "0.01", "pubkey": pubkey.hex(),
        "signatures": [_sign(secret, bytes.fromhex(d), funding.SINGLE_ANYONECANPAY).hex()
                       for d in leg["sighashes"]]})
    assert public.status_code == 400 and "only ever a claim" in public.json()["detail"]


def test_an_account_on_the_public_site_can_reach_the_pool_route():
    """The first live try answered 404: the door refuses every POST it does
    not name, and the tests above run as the node's own machine."""
    from arcade.web import door
    assert "/account/claimlots" in door.PUBLIC_POST
