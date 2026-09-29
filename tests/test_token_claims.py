"""Token claims: a prize pool of token lots behind one phrase (2026-09-28:
games that let players EARN a token; tester-e5's GHOST PROTOCOL pays 25 Ghost
Credits to whoever cracks its vault).

A lot is a leg like a listed NFT's with a Simple Send where the swap was: the
seller's coin at input 0 makes the seller its sender, and the tokens go to the
transaction's reference, which the claim always makes the claimer. The page is
given one listing id for the whole pool; each claim takes the next open lot.
"""

import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import node, _seated, _settled                 # noqa: F401,E402
from test_account_order import _bookcoin, _held, _signed, COIN, HELD   # noqa: E402
from test_funding import _sign                                          # noqa: E402
from arcade import funding                                              # noqa: E402
funding_mod = funding

PHRASE = "the vault on level five"


def _lots(book, count=3, lot="25", price="0.01", phrase=PHRASE, bound=""):
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
            "bound": bound,
            "claim_hash": hashlib.sha256(phrase.encode()).hexdigest(),
            "signatures": [_sign(secret, bytes.fromhex(d), funding.SINGLE_ANYONECANPAY).hex()
                           for d in leg["sighashes"]]})
        assert filed.status_code == 200, filed.text
        assert filed.json()["claim"]
        ids.append(filed.json()["listed"])
    return ids, legs


def _claim(buyer, listing, phrase=PHRASE, page=""):
    client, secret, pubkey, _address = buyer
    asked = client.post("/account/buy", json={"listing": listing, "secret": phrase, "page": page})
    assert asked.status_code == 200, asked.text
    said = asked.json()
    done = client.post("/account/buy/sign", json={
        "raw": said["raw"], "listing": listing, "secret": phrase, "page": page,
        "pubkey": pubkey.hex(),
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


def test_a_bound_pool_pays_only_while_its_seller_holds_the_page(node, monkeypatch):
    """2026-09-29: "only NFTs that are in your wallet should be able to send out
    your tokens or NFTs." A pool bound to a page pays out only while its seller
    holds that page; sending the page away retires it."""
    from test_account_offer import _inscribed
    from arcade import ledger
    book = _bookcoin(node, 91)
    state, rpc = book["state"], book["rpc"]
    page = _inscribed(book["client"], state, rpc, book["secret"], book["pubkey"], "the game")
    ids, _ = _lots(book, count=2, lot="10", bound=page)

    pool = book["client"].get(f"/r/claimpool/{page}").json()
    assert pool["open"] and pool["lots_left"] == 2 and pool["listing"] in ids
    assert "10 " in pool["what"]
    assert "secret" not in pool and PHRASE not in str(pool)

    player = _seated(node[0], state, rpc, 92)
    elsewhere = player[0].post("/account/buy", json={
        "listing": ids[0], "secret": PHRASE, "page": "ab" * 32})
    assert elsewhere.status_code == 400
    _claim(player, pool["listing"], page=page)
    _settled(state, rpc)
    assert _held(state, player[3], book["pid"])[0] == 10 * COIN

    real = ledger.LedgerIndex.inscription

    def moved(self, key):
        row = real(self, key)
        if row is not None and row["txid"] == page:
            row = dict(row, owner="nSomebodyElse")
        return row
    monkeypatch.setattr(ledger.LedgerIndex, "inscription", moved)
    gone = player[0].post("/account/buy", json={"listing": ids[1], "secret": PHRASE, "page": page})
    assert gone.status_code == 400 and "no longer" in gone.json()["detail"]
    assert book["client"].get(f"/r/claimpool/{page}").json()["open"] is False


def test_only_a_page_its_seller_holds_can_be_bound(node):
    book = _bookcoin(node, 93)
    other = _bookcoin(node, 94)
    from test_account_offer import _inscribed
    theirs = _inscribed(other["client"], other["state"], other["rpc"], other["secret"],
                        other["pubkey"], "not yours")
    who, secret, pubkey = book["client"], book["secret"], book["pubkey"]
    ask = {"property_id": book["pid"], "lot": "5", "count": 1, "price": "0.01"}
    said = who.post("/account/claimlots", json=ask)
    if said.json().get("needs_split"):
        _signed(who, secret, pubkey, said)
        _settled(book["state"], book["rpc"])
        said = who.post("/account/claimlots", json=ask)
    leg = said.json()["legs"][0]
    refused = who.post("/account/list/sign", json={
        "raw": leg["raw"], "amount": "0.01", "pubkey": pubkey.hex(), "bound": theirs,
        "claim_hash": hashlib.sha256(PHRASE.encode()).hexdigest(),
        "signatures": [_sign(secret, bytes.fromhex(d), funding.SINGLE_ANYONECANPAY).hex()
                       for d in leg["sighashes"]]})
    assert refused.status_code == 400 and "not yours" in refused.json()["detail"]


def test_a_pool_is_listed_and_withdrawn_in_one_transaction(node):
    """2026-09-29: "how does @vex withdraw the old pool?" Withdrawing spends
    every lot's coins back to the seller, and the tokens it held are free for
    a new pool."""
    book = _bookcoin(node, 95)
    state, rpc, who = book["state"], book["rpc"], book["client"]
    ids, _ = _lots(book, count=3, lot="100")
    pools = who.get("/account/claimpools").json()["pools"]
    assert len(pools) == 1 and pools[0]["lots"] == 3 and pools[0]["pool"] in ids
    too_big = who.post("/account/claimlots", json={
        "property_id": book["pid"], "lot": "100", "count": 3, "price": "0.01"})
    assert too_big.status_code == 400, "300 of 500 already stand in lots"

    offer = who.post("/account/list/cancel", json={"pool": ids[1]})
    assert offer.status_code == 200, offer.text
    assert sorted(offer.json()["listings"]) == sorted(ids)
    _signed(who, book["secret"], book["pubkey"], offer)
    _settled(state, rpc)
    assert who.get("/account/claimpools").json()["pools"] == []
    fits = who.post("/account/claimlots", json={
        "property_id": book["pid"], "lot": "100", "count": 3, "price": "0.01"})
    assert fits.status_code == 200, fits.text


def test_a_pool_at_its_own_address_pays_claims_and_closes(node):
    """2026-09-29: "Can't we split a transaction and lock some of the coins?"
    A pool's tokens and coins live at an address of its own, so nothing but a
    claim or closing the pool can spend them; closing brings everything back."""
    from test_account_offer import _pubkey
    from arcade.script import b58check_encode, hash160
    book = _bookcoin(node, 97)
    state, rpc, who = book["state"], book["rpc"], book["client"]
    pool_secret = int.from_bytes(bytes([0x62, 97]) + bytes(30), "big")
    pool_pub = _pubkey(pool_secret)
    opened = who.post("/account/pools/open", json={"index": 1, "pubkey": pool_pub.hex()})
    assert opened.status_code == 200, opened.text
    pool = opened.json()["address"]
    assert pool == b58check_encode(state.messaging.params.pubkeyhash_version, hash160(pool_pub))

    funding = who.post("/account/pools/fund", json={
        "pool": pool, "property_id": book["pid"], "lot": "40", "count": 2, "price": "0.01",
        "claim_hash": hashlib.sha256(PHRASE.encode()).hexdigest()})
    assert funding.status_code == 200, funding.text
    early = who.post("/account/claimlots", json={"pool": pool})
    assert early.status_code == 409 and early.json()["waiting"], "nothing until the block"
    _signed(who, book["secret"], book["pubkey"], funding)
    _settled(state, rpc)
    assert _held(state, pool, book["pid"])[0] == 80 * COIN, "the pool holds its tokens"
    assert _held(state, book["address"], book["pid"])[0] == HELD - 80 * COIN

    built = who.post("/account/claimlots", json={"pool": pool})
    assert built.status_code == 200, built.text
    ids = []
    for leg in built.json()["legs"]:
        assert leg["owner"] == pool
        filed = who.post("/account/list/sign", json={
            "raw": leg["raw"], "amount": "0.01", "pubkey": pool_pub.hex(), "owner": pool,
            "claim_hash": leg["claim_hash"],
            "signatures": [_sign(pool_secret, bytes.fromhex(d), funding_mod.SINGLE_ANYONECANPAY).hex()
                           for d in leg["sighashes"]]})
        assert filed.status_code == 200, filed.text
        ids.append(filed.json()["listed"])
    listed = who.get("/account/claimpools").json()
    assert listed["pools"][0]["address"] == pool and listed["pools"][0]["lots"] == 2
    assert listed["next_index"] == 2

    player = _seated(node[0], state, rpc, 98)
    _claim(player, ids[0])
    _settled(state, rpc)
    assert _held(state, player[3], book["pid"])[0] == 40 * COIN
    assert _held(state, pool, book["pid"])[0] == 40 * COIN

    closing = who.post("/account/pools/close", json={"pool": pool})
    assert closing.status_code == 200, closing.text
    done = who.post("/account/sign", json={
        "offer": closing.json()["offer"], "pubkey": pool_pub.hex(),
        "signatures": [_sign(pool_secret, bytes.fromhex(d)).hex()
                       for d in closing.json()["sighashes"]]})
    assert done.status_code == 200, done.text
    _settled(state, rpc)
    assert _held(state, pool, book["pid"])[0] == 0, "the pool is empty"
    assert _held(state, book["address"], book["pid"])[0] == HELD - 40 * COIN, \
        "everything unclaimed came home"
    after = who.get("/account/claimpools").json()
    assert after["pools"] == [] and after["setup"] == []
    late = player[0].post("/account/buy", json={"listing": ids[1], "secret": PHRASE})
    assert late.status_code == 400, "no lot of a closed pool can be claimed"


def test_a_prize_pool_inscribed_on_the_chain_pays_and_is_deleted_by_another(node):
    """2026-09-29: pools "created with an inscription and then ... deleted with
    another inscription", working "across many instances of Dogecoin arcade"
    with no double spending. The claim is built only from what the chain says:
    the pool inscription's JSON, and signatures the claimer opened with the
    phrase (here handed over directly; the browser decrypts them)."""
    import base64
    from test_account_offer import _pubkey, _inscribed
    book = _bookcoin(node, 99)
    state, rpc, who = book["state"], book["rpc"], book["client"]
    game = _inscribed(who, state, rpc, book["secret"], book["pubkey"], "the game")
    pool_secret = int.from_bytes(bytes([0x63, 99]) + bytes(30), "big")
    pool_pub = _pubkey(pool_secret)
    opened = who.post("/account/pools/open", json={"index": 123456, "pubkey": pool_pub.hex()})
    assert opened.status_code == 200, opened.text
    pool = opened.json()["address"]
    funding = who.post("/account/pools/fund", json={
        "pool": pool, "property_id": book["pid"], "lot": "30", "count": 2, "price": "0.01",
        "bound": game, "claim_hash": hashlib.sha256(PHRASE.encode()).hexdigest()})
    assert funding.status_code == 200, funding.text
    fund = _signed(who, book["secret"], book["pubkey"], funding).json()["txid"]

    legs = who.post("/account/pools/legs", json={"pool": pool, "fund": fund})
    assert legs.status_code == 200, legs.text
    sigs = [[_sign(pool_secret, bytes.fromhex(d), funding_mod.SINGLE_ANYONECANPAY).hex()
             for d in leg["sighashes"]] for leg in legs.json()["legs"]]
    public = {**legs.json()["prizepool"], "pubkey": pool_pub.hex()}
    assert public["game"] == game and public["count"] == 2
    made = who.post("/account/inscribe", json={
        "content": base64.b64encode(b"sealed signatures").decode(),
        "content_type": "application/vnd.arcade.prizepool",
        "json": json.dumps({"name": "Prize pool", "prizepool": public})})
    assert made.status_code == 200, made.text
    pool_txid = _signed(who, book["secret"], book["pubkey"], made).json()["txid"]
    _settled(state, rpc)

    info = who.get(f"/r/claimpool/{game}").json()
    assert info["kind"] == "chain" and info["pool"] == pool_txid
    assert info["open"] and info["free"] == [0, 1], info

    client, secret, pubkey, address = _seated(node[0], state, rpc, 100)
    bad = client.post("/account/prize", json={"pool": pool_txid, "lot": 0,
                                              "signatures": sigs[0], "secret": "wrong"})
    assert bad.status_code == 400 and "phrase" in bad.json()["detail"]
    asked = client.post("/account/prize", json={"pool": pool_txid, "lot": 0, "page": game,
                                                "signatures": sigs[0], "secret": PHRASE})
    assert asked.status_code == 200, asked.text
    said = asked.json()
    done = client.post("/account/prize/sign", json={
        "pool": pool_txid, "lot": 0, "page": game, "signatures": sigs[0], "secret": PHRASE,
        "raw": said["raw"], "pubkey": pubkey.hex(),
        "claimer": [_sign(secret, bytes.fromhex(d)).hex() for d in said["sighashes"]]})
    assert done.status_code == 200, done.text
    again = client.post("/account/prize", json={"pool": pool_txid, "lot": 0,
                                                "signatures": sigs[0], "secret": PHRASE})
    assert again.status_code == 400, "a lot claimed in the pool cannot be claimed twice"
    _settled(state, rpc)
    assert _held(state, address, book["pid"])[0] == 30 * COIN
    assert who.get(f"/r/prizepool/{pool_txid}").json()["free"] == [1]

    closing = who.post("/account/pools/close", json={"prize": pool_txid})
    assert closing.status_code == 200, closing.text
    closed = who.post("/account/sign", json={
        "offer": closing.json()["offer"], "pubkey": pool_pub.hex(),
        "signatures": [_sign(pool_secret, bytes.fromhex(d)).hex()
                       for d in closing.json()["sighashes"]]})
    assert closed.status_code == 200, closed.text
    gone = who.post("/account/inscribe", json={
        "content": base64.b64encode(b"prize pool deleted").decode(),
        "content_type": "text/plain; charset=utf-8",
        "json": json.dumps({"prizepool_delete": {"pool": pool_txid}})})
    _signed(who, book["secret"], book["pubkey"], gone)
    _settled(state, rpc)
    assert _held(state, pool, book["pid"])[0] == 0
    assert _held(state, book["address"], book["pid"])[0] == HELD - 30 * COIN
    after = who.get(f"/r/prizepool/{pool_txid}").json()
    assert after["deleted"] and not after["open"]
    assert who.get(f"/r/claimpool/{game}").json()["open"] is False


def test_tokens_on_their_way_out_cannot_be_sent_again(node):
    """2026-09-29: "a bug that allows me to transfer more plasma to the game
    than what I have in my wallet if I just keep pressing the button while the
    transaction is being confirmed. We're gonna have to read this stuff from
    the mempool." """
    book = _bookcoin(node, 101)
    state, rpc, who = book["state"], book["rpc"], book["client"]
    to = rpc.call("getnewaddress")
    ask = {"property_id": book["pid"], "to": to, "amount": "300"}

    first = who.post("/account/token/send", json=ask)
    assert first.status_code == 200, first.text
    # Two offered before either goes out: the second is refused at broadcast.
    racing = who.post("/account/token/send", json=ask)
    assert racing.status_code == 200, "only the pool can tell, and nothing is in it yet"
    assert _signed(who, book["secret"], book["pubkey"], first).status_code == 200
    late = who.post("/account/sign", json={
        "offer": racing.json()["offer"], "pubkey": book["pubkey"].hex(),
        "signatures": [_sign(book["secret"], bytes.fromhex(d)).hex()
                       for d in racing.json()["sighashes"]]})
    assert late.status_code == 400 and "on its way out" in late.json()["detail"], late.text

    again = who.post("/account/token/send", json=ask)
    assert again.status_code == 400 and "on its way out" in again.json()["detail"]
    small = who.post("/account/token/send", json={**ask, "amount": "200"})
    assert small.status_code == 200, "what is really left can still go"
    sell = who.post("/account/order", json={"side": "ask", "property_id": book["pid"],
                                            "amount": "300", "price": "0.01"})
    assert sell.status_code == 400, "the pool's send counts against a sell order too"

    _settled(state, rpc)
    assert _held(state, book["address"], book["pid"])[0] == HELD - 300 * COIN
    after = who.post("/account/token/send", json={**ask, "amount": "200"})
    assert after.status_code == 200, "once mined, the rest is spendable again"


def test_an_nft_sold_for_a_token_moves_both_in_one_transaction(node):
    """2026-09-29: sell pieces "FOR PLASMA (token #19), not coins", game-agnostic:
    any piece, any token. The swap takes the token; the engine moves the token
    and the piece in the same block, or neither."""
    from test_account_offer import _inscribed
    buyer_book = _bookcoin(node, 103)
    state, rpc = buyer_book["state"], buyer_book["rpc"]
    seller, s_secret, s_pubkey, s_address = _seated(node[0], state, rpc, 104)
    piece = _inscribed(seller, state, rpc, s_secret, s_pubkey, "a fighter")

    leg = seller.post("/account/list", json={"piece": piece, "amount": "200",
                                             "token": buyer_book["pid"]})
    assert leg.status_code == 200, leg.text
    assert leg.json()["price"] == 0 and leg.json()["take"]["kind"] == "token"
    filed = seller.post("/account/list/sign", json={
        "raw": leg.json()["raw"], "amount": "0", "pubkey": s_pubkey.hex(),
        "signatures": [_sign(s_secret, bytes.fromhex(d), funding.SINGLE_ANYONECANPAY).hex()
                       for d in leg.json()["sighashes"]]})
    assert filed.status_code == 200, filed.text
    listing = filed.json()["listed"]

    poor = _seated(node[0], state, rpc, 105)
    refused = poor[0].post("/account/buy", json={"listing": listing})
    assert refused.status_code == 400 and "does not hold" in refused.json()["detail"]

    who, b_secret, b_pubkey = buyer_book["client"], buyer_book["secret"], buyer_book["pubkey"]
    asked = who.post("/account/buy", json={"listing": listing})
    assert asked.status_code == 200, asked.text
    assert asked.json()["price_text"].startswith("200 "), asked.json()["price_text"]
    done = who.post("/account/buy/sign", json={
        "raw": asked.json()["raw"], "listing": listing, "pubkey": b_pubkey.hex(),
        "signatures": [_sign(b_secret, bytes.fromhex(d)).hex()
                       for d in asked.json()["sighashes"]]})
    assert done.status_code == 200, done.text
    _settled(state, rpc)
    index = state.token_index(state.messaging)
    assert index.inscription(piece)["owner"] == buyer_book["address"], "the buyer holds it"
    assert _held(state, s_address, buyer_book["pid"])[0] == 200 * COIN, "the seller was paid"
    assert _held(state, buyer_book["address"], buyer_book["pid"])[0] == HELD - 200 * COIN


def test_an_nft_prize_pool_hands_out_pieces_once_per_wallet_and_closes(node):
    """2026-09-29: a page airdrops pieces of a limited collection, once per
    wallet, from a pool any node reads; closing sends what is left home.
    Game-agnostic: any page, any pieces."""
    import base64
    from test_account_offer import _pubkey, _inscribed
    app, state, rpc = node
    who, secret, pubkey, me = _seated(app, state, rpc, 106)
    game = _inscribed(who, state, rpc, secret, pubkey, "a game")
    pieces = [_inscribed(who, state, rpc, secret, pubkey, f"weapon {n}") for n in range(3)]
    pool_secret = int.from_bytes(bytes([0x64, 106]) + bytes(30), "big")
    pool_pub = _pubkey(pool_secret)
    pool = who.post("/account/pools/open", json={"index": 777, "pubkey": pool_pub.hex()}).json()["address"]
    funding = who.post("/account/pools/fund", json={
        "pool": pool, "kind": "nft", "pieces": pieces, "price": "0.01", "bound": game,
        "once": True, "claim_hash": hashlib.sha256(PHRASE.encode()).hexdigest()})
    assert funding.status_code == 200, funding.text
    fund = _signed(who, secret, pubkey, funding).json()["txid"]
    for piece in pieces:
        moved = who.post("/account/nft/send", json={"piece": piece, "to": pool})
        assert moved.status_code == 200, moved.text
        assert _signed(who, secret, pubkey, moved).status_code == 200
    legs = who.post("/account/pools/legs", json={"pool": pool, "fund": fund})
    assert legs.status_code == 200, legs.text
    sigs = [[_sign(pool_secret, bytes.fromhex(d), funding_mod.SINGLE_ANYONECANPAY).hex()
             for d in leg["sighashes"]] for leg in legs.json()["legs"]]
    made = who.post("/account/inscribe", json={
        "content": base64.b64encode(b"sealed").decode(),
        "content_type": "application/vnd.arcade.prizepool",
        "json": json.dumps({"name": "Arsenal drops",
                            "prizepool": {**legs.json()["prizepool"], "pubkey": pool_pub.hex()}})})
    pool_txid = _signed(who, secret, pubkey, made).json()["txid"]
    _settled(state, rpc)

    listed = who.get(f"/r/claimpools/{game}").json()["pools"]
    assert [(p["kind"], p["total"], p["free"], p["once"]) for p in listed] == \
        [("nft", 3, [0, 1, 2], True)], listed

    def claim(seat, n):
        client, s, pk, _ = seat
        asked = client.post("/account/prize", json={"pool": pool_txid, "lot": n, "page": game,
                                                    "signatures": sigs[n], "secret": PHRASE})
        if asked.status_code != 200:
            return asked
        return client.post("/account/prize/sign", json={
            "pool": pool_txid, "lot": n, "page": game, "signatures": sigs[n], "secret": PHRASE,
            "raw": asked.json()["raw"], "pubkey": pk.hex(),
            "claimer": [_sign(s, bytes.fromhex(d)).hex() for d in asked.json()["sighashes"]]})

    player = _seated(app, state, rpc, 107)
    got = claim(player, 0)
    assert got.status_code == 200, got.text
    assert got.json()["piece"].startswith("#")
    _settled(state, rpc)
    index = state.token_index(state.messaging)
    assert index.inscription(pieces[0])["owner"] == player[3], "the claimer holds the piece"
    twice = claim(player, 1)
    assert twice.status_code == 400 and "already claimed" in twice.json()["detail"]

    closing = who.post("/account/pools/close", json={"prize": pool_txid})
    assert closing.status_code == 200, closing.text
    for offer in closing.json()["offers"]:
        done = who.post("/account/sign", json={
            "offer": offer["offer"], "pubkey": pool_pub.hex(),
            "signatures": [_sign(pool_secret, bytes.fromhex(d)).hex() for d in offer["sighashes"]]})
        assert done.status_code == 200, done.text
    _settled(state, rpc)
    assert [index.inscription(p)["owner"] for p in pieces[1:]] == [me, me], "home again"
    assert who.get(f"/r/prizepool/{pool_txid}").json()["open"] is False
