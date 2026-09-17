"""The shop, the offer and the two signatures (arcade/swap.py).

Two wallets, neither trusting the other: the seller offers exactly what the
inscription's JSON says, the buyer signs only what it was shown, the seller
signs only what it offered. A fake node decodes the real bytes, so what these
check is the transaction as it would go out.
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest

from arcade import inscriptions as I
from arcade import swap as S
from arcade.config import NETWORKS
from arcade.ledger import COIN
from arcade.script import b58check_encode

TEST = NETWORKS["test"]


def addr(n: int) -> str:
    return b58check_encode(TEST.pubkeyhash_version, bytes([n]) * 20)


SELLER, BUYER, OTHER = addr(1), addr(2), addr(3)
SHOP = "5" * 64
PIECE = "a" * 64          # an inscription the seller holds
PIECE2 = "b" * 64         # one the buyer holds
GOOF = ["c" * 63 + str(i) for i in range(4)]   # a collection the seller made


class FakeIndex:
    def __init__(self):
        self.props = {3: {"property_id": 3, "name": "Arcade Test", "divisible": True}}
        self.indexed: dict[str, dict] = {}     # swap txids the ledger has read
        self.balances = {(BUYER, 3): 50 * 10 ** 8, (SELLER, 3): 1000 * 10 ** 8}
        self.rows = {
            SHOP: {"txid": SHOP, "number": 1, "creator": SELLER, "owner": SELLER,
                   "json": json.dumps({"shop": {"node": "ab" * 32, "listings": [
                       {"give": {"token": 3, "amount": "100"}, "take": {"coins": "2"}},
                       {"give": {"collection": "Goofball", "pick": "random"},
                        "take": {"token": 3, "amount": "10"}},
                       {"give": {"inscription": PIECE}, "take": {"inscription": PIECE2}},
                       {"give": {"coins": "1.5"}, "take": {"token": 3, "amount": "1"}},
                   ]}})},
            PIECE: {"txid": PIECE, "number": 2, "creator": OTHER, "owner": SELLER, "json": ""},
            PIECE2: {"txid": PIECE2, "number": 3, "creator": OTHER, "owner": BUYER, "json": ""},
        }
        for i, txid in enumerate(GOOF):
            self.rows[txid] = {"txid": txid, "number": 10 + i, "creator": SELLER,
                               "owner": SELLER if i < 3 else OTHER, "json": "",
                               "collection": "Goofball", "edition": i + 1}

    def property(self, pid):
        return self.props.get(pid)

    def balance(self, address, pid):
        return self.balances.get((address, pid), 0)

    def inscription(self, key):
        if isinstance(key, int):
            return next((r for r in self.rows.values() if r["number"] == key), None)
        return self.rows.get(key)

    def collection_items(self, creator, name, limit=100, offset=0):
        return [r for r in self.rows.values()
                if r.get("collection") == name and r["creator"] == creator]

    def indexed_height(self):
        return 100

    def address_of(self, tag):
        return None

    def transaction(self, txid):
        return self.indexed.get(txid)


def _p2pkh_address(script: bytes) -> str | None:
    if len(script) == 25 and script[:3] == b"\x76\xa9\x14" and script[-2:] == b"\x88\xac":
        return b58check_encode(TEST.pubkeyhash_version, script[3:23])
    return None


def decode(raw_hex: str) -> dict:
    """Enough of decoderawtransaction to check a swap: inputs, outputs, txid."""
    raw = bytes.fromhex(raw_hex)
    at = 4
    n, at = raw[at], at + 1
    vin = []
    for _ in range(n):
        txid = raw[at:at + 32][::-1].hex(); vout = int.from_bytes(raw[at + 32:at + 36], "little")
        at += 36
        slen, at = raw[at], at + 1
        at += slen + 4
        vin.append({"txid": txid, "vout": vout})
    n, at = raw[at], at + 1
    vout = []
    for i in range(n):
        value = int.from_bytes(raw[at:at + 8], "little"); at += 8
        slen, at = raw[at], at + 1
        script = raw[at:at + slen]; at += slen
        address = _p2pkh_address(script)
        vout.append({"value": value / COIN, "n": i, "scriptPubKey": {
            "hex": script.hex(),
            "type": "pubkeyhash" if address else ("nulldata" if script[:1] == b"\x6a" else "?"),
            "addresses": [address] if address else []}})
    txid = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[::-1].hex()
    return {"txid": txid, "vin": vin, "vout": vout}


class FakeNode:
    """One wallet's node. `mine` is the addresses it can sign for."""

    def __init__(self, mine: set[str], unspent: list[tuple[str, int, str, int]]):
        self.mine = mine
        # outpoint -> (address, sats); everybody's, since gettxout sees the chain
        self.chain = {(t, v): (a, s) for t, v, a, s in unspent}
        self.locked: set[tuple[str, int]] = set()
        self.sent: list[str] = []
        self.calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_block_count(self):
        return 100

    def call(self, method, *args):
        self.calls.append((method, *args))
        if method == "listunspent":
            addresses = args[2] if len(args) > 2 else None
            return [{"txid": t, "vout": v, "address": a, "amount": s / COIN, "spendable": True}
                    for (t, v), (a, s) in self.chain.items()
                    if (addresses is None or a in addresses) and a in self.mine
                    and (t, v) not in self.locked]
        if method == "listreceivedbyaddress":
            # Under the arcade's own account: the wallet only sees what this
            # application made (D-046), and everything in a swap test is its.
            return [{"address": a, "account": "arcade-identity"} for a in self.mine]
        if method == "lockunspent":
            unlock, points = args
            for p in points:
                (self.locked.discard if unlock else self.locked.add)((p["txid"], p["vout"]))
            return True
        if method == "gettxout":
            txid, vout, _ = args
            found = self.chain.get((txid, vout))
            return {"scriptPubKey": {"addresses": [found[0]]}, "value": found[1] / COIN} \
                if found else None
        if method == "decoderawtransaction":
            return decode(args[0])
        if method == "signrawtransaction":
            # The bytes are not changed -- there is no key here -- but the
            # verdict is real: an input this wallet cannot sign is reported,
            # and `complete` says whether every input has been signed by the
            # wallets the hex passed through (marked in a side table).
            raw = args[0]
            signed_by = _SIGNED.setdefault(raw, set()) | self.mine
            _SIGNED[raw] = signed_by
            errors = []
            for entry in decode(raw)["vin"]:
                owner = self.chain[(entry["txid"], entry["vout"])][0]
                if owner not in signed_by:
                    errors.append({"txid": entry["txid"], "vout": entry["vout"],
                                   "error": "Input not found or already spent"})
            return {"hex": raw, "complete": not errors, "errors": errors}
        if method == "sendrawtransaction":
            self.sent.append(args[0])
            return decode(args[0])["txid"]
        raise AssertionError(f"unexpected rpc {method}")


_SIGNED: dict[str, set[str]] = {}


@pytest.fixture
def world(tmp_path):
    _SIGNED.clear()
    index = FakeIndex()
    unspent = [("1" * 64, 0, SELLER, 3 * COIN), ("1" * 64, 1, SELLER, int(0.5 * COIN)),
               ("1" * 64, 2, SELLER, int(0.02 * COIN)), ("1" * 64, 3, SELLER, int(0.02 * COIN)),
               ("1" * 64, 4, SELLER, 2 * COIN),
               ("2" * 64, 0, BUYER, 5 * COIN), ("2" * 64, 1, BUYER, 1 * COIN),
               ("3" * 64, 0, OTHER, 9 * COIN)]
    seller = FakeNode({SELLER}, unspent)
    buyer = FakeNode({BUYER}, unspent)
    offers = S.Offers(tmp_path / "swaps.sqlite")
    return index, seller, buyer, offers


def shop_row(index):
    return index.rows[SHOP]


# --- the shop ----------------------------------------------------------------

def test_a_shop_is_read_from_the_inscriptions_json(world):
    index, *_ = world
    shop = S.shop_of(shop_row(index))
    assert shop["node"] == "ab" * 32 and len(shop["listings"]) == 4
    for bad, why in ((json.dumps({"shop": {"listings": []}}), "list of listings"),
                     (json.dumps({"shop": {"listings": [{"give": {"coins": "1"}}]}}), "take"),
                     (json.dumps({"shop": {"listings": [{"give": {"coins": "1"},
                                                          "take": {"collection": "x"}}]}}),
                      "random item, not take"),
                     (json.dumps({"shop": {"listings": [{"give": {"token": 3},
                                                          "take": {"coins": "1"}}]}}),
                      "needs an amount"),
                     ("", "no JSON"), (json.dumps({"x": 1}), "no \"shop\"")):
        with pytest.raises(S.SwapError, match=why):
            S.shop_of({"json": bad})


def test_legs_are_made_concrete_from_the_ledger(world):
    index, *_ = world
    leg = S.leg_of({"token": 3, "amount": "100"}, index)
    assert leg == I.Leg(I.LEG_TOKEN, property_id=3, amount=100 * 10 ** 8)
    assert S.leg_of({"coins": "2"}, index) == I.Leg(I.LEG_COINS, amount=2 * COIN)
    assert S.leg_of({"inscription": 2}, index).txid.hex() == PIECE
    assert S.leg_of({"inscription": PIECE}, index).txid.hex() == PIECE
    with pytest.raises(S.SwapError, match="no token 9"):
        S.leg_of({"token": 9, "amount": "1"}, index)
    shown = S.leg_json(leg, index)
    assert shown == {"kind": "token", "propertyid": 3, "name": "Arcade Test",
                     "amount": "100", "units": 100 * 10 ** 8}
    assert S.leg_from_json(shown) == leg
    assert S.describe_leg(shown) == "100 Arcade Test"
    assert S.describe_leg(S.leg_json(S.leg_of({"inscription": GOOF[0]}, index), index)) \
        == "inscription #10 (Goofball #1)"


# --- the seller offers ------------------------------------------------------

def test_an_offer_locks_an_output_and_prices_the_listing(world):
    index, seller, _, offers = world
    offer = S.make_offer(seller, index, offers, "test", shop_row(index), 0, BUYER, "ff" * 32,
                         own=[SELLER])
    assert offer["seller"] == SELLER and offer["buyer"] == BUYER and offer["shop"] == SHOP
    assert offer["give"]["kind"] == "token" and offer["give"]["amount"] == "100"
    assert offer["take"] == {"kind": "coins", "amount": "2.00000000", "sats": 2 * COIN}
    # The smallest output that can carry its own value back: 0.02 fits.
    assert offer["outpoint"] == {"txid": "1" * 64, "vout": 2, "value": int(0.02 * COIN)}
    assert ("1" * 64, 2) in seller.locked, "nothing else in the wallet may spend it"
    assert offer["expires"] - offer["created"] == S.OFFER_TTL
    assert offers.get(offer["id"])["status"] == "open"
    assert "buyer_pubkey" not in offer, "bookkeeping stays in the book"

    # A second offer cannot take the same output; it takes the next one, and
    # so on until the wallet has nothing left to offer from.
    taken = [offer["outpoint"]["vout"]]
    for _ in range(4):
        again = S.make_offer(seller, index, offers, "test", shop_row(index), 0, OTHER, "",
                             own=[SELLER])
        taken.append(again["outpoint"]["vout"])
    assert taken == [2, 3, 1, 4, 0], "smallest first, never the same one twice"
    # Every output is spoken for. The refusal says so plainly rather than
    # "no output worth 0.00000000", which is what a holder with tokens and
    # no coins used to be told (D-045).
    with pytest.raises(S.SwapError, match="holds no spendable coins") as refused:
        S.make_offer(seller, index, offers, "test", shop_row(index), 0, OTHER, "", own=[SELLER])
    assert "offers already holding its outputs" in str(refused.value)


def test_only_the_creator_who_still_holds_the_shop_may_sell(world):
    index, seller, _, offers = world
    row = dict(shop_row(index), owner=OTHER)
    with pytest.raises(S.SwapError, match="did not create this shop, or no longer holds"):
        S.make_offer(seller, index, offers, "test", row, 0, BUYER, "", own=[SELLER, OTHER])
    with pytest.raises(S.SwapError, match="did not create"):
        S.make_offer(seller, index, offers, "test", shop_row(index), 0, BUYER, "", own=[BUYER])
    with pytest.raises(S.SwapError, match="own wallet"):
        S.make_offer(seller, index, offers, "test", shop_row(index), 0, SELLER, "", own=[SELLER])
    with pytest.raises(S.SwapError, match="no listing 9"):
        S.make_offer(seller, index, offers, "test", shop_row(index), 9, BUYER, "", own=[SELLER])


def test_what_neither_side_holds_is_not_offered(world):
    index, seller, _, offers = world
    index.balances[(BUYER, 3)] = 0
    with pytest.raises(S.SwapError, match="the buyer cannot give that.*holds 0"):
        S.make_offer(seller, index, offers, "test", shop_row(index), 1, BUYER, "", own=[SELLER])
    index.rows[PIECE]["owner"] = OTHER
    with pytest.raises(S.SwapError, match="the shop cannot give that.*held by"):
        S.make_offer(seller, index, offers, "test", shop_row(index), 2, BUYER, "", own=[SELLER])
    assert seller.locked == set(), "nothing was locked for an offer that was not made"


def test_a_random_listing_picks_what_the_shop_still_holds(world):
    index, seller, _, offers = world
    picked = set()
    for _ in range(3):
        offer = S.make_offer(seller, index, offers, "test", shop_row(index), 1, BUYER, "",
                             own=[SELLER])
        assert offer["give"]["kind"] == "inscription"
        assert offer["give"]["collection"] == "Goofball"
        picked.add(offer["give"]["txid"])
    assert picked == set(GOOF[:3]), "each open offer holds a different item; the fourth is not ours"
    with pytest.raises(S.SwapError, match="nothing of Goofball is left"):
        S.make_offer(seller, index, offers, "test", shop_row(index), 1, BUYER, "", own=[SELLER])


def test_an_offer_expires_and_unlocks(world, monkeypatch):
    index, seller, _, offers = world
    offer = S.make_offer(seller, index, offers, "test", shop_row(index), 0, BUYER, "",
                         own=[SELLER])
    later = time.time() + S.OFFER_TTL + 1
    monkeypatch.setattr(S.time, "time", lambda: later)
    assert S.expire(seller, offers, "test") == 1
    assert offers.get(offer["id"])["status"] == "expired"
    assert seller.locked == set()


# --- the buyer builds, the seller countersigns -------------------------------

def _offer(world, listing=0):
    index, seller, _, offers = world
    return S.make_offer(seller, index, offers, "test", shop_row(index), listing, BUYER,
                        "ff" * 32, own=[SELLER])


def test_the_buyer_signs_half_and_the_seller_completes_it(world):
    index, seller, buyer, offers = world
    offer = _offer(world, 0)               # 100 Arcade Test for 2 coins
    checked = S.check_offer(offer, shop=SHOP, own=[BUYER], height=TEST.swaps_from, params=TEST)
    built = S.build(buyer, index, checked, own=[BUYER])
    assert built.what == "2 coins for 100 Arcade Test"
    assert built.buyer == BUYER and built.seller == SELLER
    decoded = decode(built.hex)
    assert decoded["vin"][0] == {"txid": "1" * 64, "vout": 2}, "the seller's output is first"
    assert all(buyer.chain[(v["txid"], v["vout"])][0] == BUYER for v in decoded["vin"][1:])
    paid_seller = sum(o["value"] for o in decoded["vout"]
                      if o["scriptPubKey"]["addresses"] == [SELLER])
    assert paid_seller == pytest.approx(0.02 + 2.0), "its input back, plus the price"
    assert [o["scriptPubKey"]["type"] for o in decoded["vout"]][0] == "nulldata"
    assert built.fee_sats == S.FEE_PER_KB and built.outputs[1]["is_recipient"]
    assert built.outputs[2]["is_change"] and built.outputs[2]["where"] == BUYER
    assert _SIGNED[built.hex] == {BUYER}, "signed by the buyer only"

    assert seller.locked == {("1" * 64, 2)}, "held while the offer is open"
    txid = S.countersign(seller, index, offers, offers.get(offer["id"]), built.hex)
    assert seller.sent == [built.hex]
    assert seller.locked == set(), "a sold output is not left locked (D-027)"
    assert offers.get(offer["id"])["status"] == "sent"
    assert offers.get(offer["id"])["txid"] == txid == decode(built.hex)["txid"]
    with pytest.raises(S.SwapError, match="that offer is sent"):
        S.countersign(seller, index, offers, offers.get(offer["id"]), built.hex)


def test_every_kind_of_leg_builds(world):
    index, seller, buyer, offers = world
    for listing, phrase in ((1, "10 Arcade Test for inscription #1"),
                            (2, "inscription #3 for inscription #2"),
                            (3, "1 Arcade Test for 1.5 coins")):
        offer = _offer(world, listing)
        built = S.build(buyer, index, offer, own=[BUYER])
        assert built.what.startswith(phrase.split(" for ")[0]), built.what
        decoded = decode(built.hex)
        paid_seller = sum(o["value"] for o in decoded["vout"]
                          if o["scriptPubKey"]["addresses"] == [SELLER])
        owed = offer["outpoint"]["value"] + S.coins_in(S.leg_from_json(offer["take"])) \
            - S.coins_in(S.leg_from_json(offer["give"]))
        assert round(paid_seller * COIN) == owed
        S.countersign(seller, index, offers, offers.get(offer["id"]), built.hex)
        # Building holds the buyer's inputs (D-044). This fake node honours
        # that, and three swaps in a row out of one wallet would run it dry;
        # a real wallet's coins come back when the swap lands or is dropped.
        buyer.locked.clear()


def test_the_seller_signs_nothing_it_did_not_offer(world):
    """Every way a buyer might edit the transaction after the offer."""
    index, seller, buyer, offers = world
    from arcade import payload as P
    from arcade.encoding import encode_class_c
    from arcade.txbuild import build_raw_tx, op_return_script, p2pkh_script
    offer = _offer(world, 0)
    give, take = S.leg_from_json(offer["give"]), S.leg_from_json(offer["take"])
    ret = op_return_script(encode_class_c(P.AnyData(data=I.Swap(give=give, take=take).encode()).encode()))
    seller_in = (offer["outpoint"]["txid"], offer["outpoint"]["vout"])
    buyer_in = ("2" * 64, 0)
    fair = [(0, ret), (int(2.02 * COIN), p2pkh_script(SELLER)), (int(2.9 * COIN), p2pkh_script(BUYER))]

    def signed_by_buyer(inputs, outputs):
        return buyer.call("signrawtransaction", build_raw_tx(inputs, outputs))["hex"]

    def refused(inputs, outputs, why):
        with pytest.raises(S.SwapError, match=why):
            S.countersign(seller, index, offers, offers.get(offer["id"]),
                          signed_by_buyer(inputs, outputs))
        assert seller.sent == []

    refused([buyer_in, seller_in], fair, "not the first input")
    refused([seller_in], fair, "seller's input and the buyer's")
    refused([seller_in, ("1" * 64, 0), buyer_in], fair, "is the seller's")
    refused([seller_in, ("3" * 64, 0)], fair, "not the buyer's")
    refused([seller_in, buyer_in], [(0, ret), (int(1.9 * COIN), p2pkh_script(SELLER))],
            "paid 1.90000000, not the 2.02000000")
    refused([seller_in, buyer_in], fair[1:], "exactly one OP_RETURN")
    cheaper = op_return_script(encode_class_c(P.AnyData(
        data=I.Swap(give=give, take=I.Leg(I.LEG_COINS, amount=1)).encode()).encode()))
    refused([seller_in, buyer_in], [(0, cheaper)] + fair[1:], "not carry the legs")
    refused([seller_in, buyer_in], [(0, ret), (0, ret)] + fair[1:], "exactly one OP_RETURN")
    refused([seller_in, buyer_in], [(0, b"\x6a\x03abc")] + fair[1:], "not carry the legs")

    # And what it did offer, it signs.
    S.countersign(seller, index, offers, offers.get(offer["id"]),
                  signed_by_buyer([seller_in, buyer_in], fair))
    assert len(seller.sent) == 1


def test_the_buyer_refuses_an_offer_that_is_not_for_it(world):
    index, seller, buyer, offers = world
    offer = _offer(world, 0)
    ok = dict(offer)
    S.check_offer(ok, shop=SHOP, own=[BUYER], height=None, params=TEST)
    for change, why in ((dict(shop="6" * 64), "different shop"),
                        (dict(buyer=OTHER), "cannot sign for"),
                        (dict(seller=BUYER), "seller's address is in this wallet"),
                        (dict(expires=time.time() - 1), "expired"),
                        (dict(give={"kind": "token"}), "malformed token leg")):
        with pytest.raises(S.SwapError, match=why):
            S.check_offer(dict(ok, **change), shop=SHOP, own=[BUYER], height=None, params=TEST)
    with pytest.raises(S.SwapError, match="read from block"):
        S.check_offer(ok, shop=SHOP, own=[BUYER], height=TEST.swaps_from - 1, params=TEST)
    with pytest.raises(S.SwapError, match="not read on this chain"):
        S.check_offer(ok, shop=SHOP, own=[BUYER], height=None, params=NETWORKS["main"])
    with pytest.raises(S.SwapError, match="not an offer"):
        S.check_offer({"id": 1}, shop=SHOP, own=[BUYER], height=None, params=TEST)
    with pytest.raises(S.SwapError, match="is not this wallet's"):
        S.build(buyer, index, offer, own=[OTHER])


def test_a_buyer_short_of_coins_is_told_what_it_needs(world):
    index, seller, buyer, offers = world
    offer = _offer(world, 0)
    for point in [("2" * 64, 0)]:
        del buyer.chain[point]
    with pytest.raises(S.SwapError, match="holds 1.00000000 spendable, and this swap needs 2.01"):
        S.build(buyer, index, offer, own=[BUYER])


def test_a_page_sees_the_listings_as_this_nodes_ledger_reads_them(world):
    index, *_ = world
    shown = S.listings_json(shop_row(index), index)
    assert [e["text"] for e in shown] == [
        "100 Arcade Test for 2 coins",
        "a random Goofball (3 left) for 10 Arcade Test",
        "inscription #2 for inscription #3",
        "1.5 coins for 1 Arcade Test"]
    assert all(e["available"] is None for e in shown)
    index.rows[PIECE]["owner"] = OTHER
    for row in GOOF:
        index.rows[row]["owner"] = OTHER
    shown = S.listings_json(shop_row(index), index)
    assert shown[1]["available"] == "nothing of Goofball is left"
    assert shown[2]["available"].startswith("inscription #2 is held by")


def test_a_sold_item_is_not_offered_again_before_its_block(world):
    """Between broadcast and indexing, a sold item still reads as the seller's.

    Without holding it back, a second buyer is offered what has just been
    sold. The engine refuses the second swap when it lands, so nothing moves
    -- but the buyer has paid a message fee to be told no (D-033).
    """
    index, seller, buyer, offers = world
    # One Goofball left to the shop: put the other two in offers nobody takes.
    held = [S.make_offer(seller, index, offers, "test", shop_row(index), 1, BUYER, "ff" * 32,
                         own=[SELLER]) for _ in range(2)]
    offer = S.make_offer(seller, index, offers, "test", shop_row(index), 1, BUYER, "ff" * 32,
                         own=[SELLER])
    sold = offer["give"]["txid"]

    built = S.build(buyer, index, S.check_offer(offer, shop=SHOP, own=[BUYER],
                                                height=TEST.swaps_from, params=TEST),
                    own=[BUYER])
    txid = S.countersign(seller, index, offers, offers.get(offer["id"]), built.hex)
    assert offers.get(offer["id"])["status"] == "sent"
    # The ledger has not read the block yet: the seller still owns it.
    assert index.rows[sold]["owner"] == SELLER
    with pytest.raises(S.SwapError, match="nothing of Goofball is left"):
        S.make_offer(seller, index, offers, "test", shop_row(index), 1, BUYER, "ff" * 32,
                     own=[SELLER])

    # Once the swap is indexed the offer stops reserving anything -- and the
    # item is genuinely gone, so the answer is the same for a better reason.
    index.indexed[txid] = {"txid": txid}
    index.rows[sold]["owner"] = BUYER
    with pytest.raises(S.SwapError, match="nothing of Goofball is left"):
        S.make_offer(seller, index, offers, "test", shop_row(index), 1, BUYER, "ff" * 32,
                     own=[SELLER])
    assert len(held) == 2


def test_a_chart_is_drawn_from_swaps_and_keeps_the_gaps(tmp_path):
    """Prices come from trades, and days with no trade stay empty (D-039)."""
    from arcade import charts

    day = charts.DAY
    now = 1_700_000_000
    trades = [
        {"when": now - 2 * day, "height": 10, "txid": "a" * 64,
         "give": I.Leg(I.LEG_TOKEN, property_id=3, amount=100 * COIN),
         "take": I.Leg(I.LEG_COINS, amount=COIN)},
        {"when": now - 2 * day + 60, "height": 11, "txid": "b" * 64,
         "give": I.Leg(I.LEG_COINS, amount=4 * COIN),
         "take": I.Leg(I.LEG_TOKEN, property_id=3, amount=100 * COIN)},
        {"when": now, "height": 12, "txid": "c" * 64,
         "give": I.Leg(I.LEG_TOKEN, property_id=3, amount=1000 * COIN),
         "take": I.Leg(I.LEG_COINS, amount=8 * COIN)},
        # Another token's trade is not this token's chart.
        {"when": now, "height": 13, "txid": "d" * 64,
         "give": I.Leg(I.LEG_TOKEN, property_id=9, amount=COIN),
         "take": I.Leg(I.LEG_COINS, amount=COIN)},
    ]
    points = charts.token_prices(trades, 3)
    assert [p["price"] for p in sorted(points, key=lambda p: p["when"])] == [0.01, 0.04, 0.008]

    slots = charts.candles(points, buckets=4, span=day, now=now)
    assert len(slots) == 4
    traded = [s for s in slots if s["count"]]
    assert len(traded) == 2, "two days traded, two did not"
    first, last = traded
    assert (first["open"], first["high"], first["low"], first["close"]) == \
        (0.01, 0.04, 0.01, 0.04)
    assert first["count"] == 2 and first["volume"] == 200
    assert last["close"] == 0.008
    empty = [s for s in slots if not s["count"]]
    assert empty and all(s["open"] is None and s["close"] is None for s in empty), \
        "a day with no trade carries no price at all"

    stats = charts.summary(points)
    assert stats["last"] == 0.008 and stats["trades"] == 3
    assert round(stats["change"], 1) == -20.0

    # An NFT sale is priced in what it was paid in, and only in one thing.
    piece = I.Leg(I.LEG_INSCRIPTION, txid=bytes.fromhex(PIECE))
    sales = [{"when": now, "height": 20, "txid": "e" * 64, "give": piece,
              "take": I.Leg(I.LEG_TOKEN, property_id=3, amount=10 * COIN)}]
    assert charts.nft_prices(sales, None) == [], "not paid in coins, so not a coin price"
    (sold,) = charts.nft_prices(sales, None, property_id=3)
    assert sold["price"] == 10
    assert charts.nft_currencies(sales) == [{"kind": "token", "property_id": 3,
                                             "trades": 1}]


def test_building_a_half_holds_what_it_spends(world):
    """The offer and the signed half travel as messages, which take blocks
    and cost fees out of the same wallet. Without holding them, the buyer's
    wallet spends its own swap input on the very message carrying the swap,
    and the seller -- checking a block later -- finds it gone (D-044)."""
    index, seller, buyer, offers = world
    offer = _offer(world, 0)
    checked = S.check_offer(offer, shop=SHOP, own=[BUYER], height=TEST.swaps_from,
                            params=TEST)
    built = S.build(buyer, index, checked, own=[BUYER])
    spent = {(v["txid"], v["vout"]) for v in decode(built.hex)["vin"]}
    held = {(t, v) for t, v in buyer.locked}
    assert held, "the buyer's inputs are held"
    assert held <= spent, "only what this half spends"
    assert (offer["outpoint"]["txid"], offer["outpoint"]["vout"]) not in held, \
        "the seller's output is the seller's to hold"

    # And what is held is not offered to the next thing that needs coins.
    left = [u for u in buyer.call("listunspent", 1, 9_999_999, [BUYER])]
    assert all((u["txid"], u["vout"]) not in held for u in left)


def test_what_is_away_from_home_is_found_and_walked_back():
    """One address a chain; what lands elsewhere is fetched (D-046)."""
    from arcade import gather

    class Index:
        rows = [{"address": "nElse", "property_id": 3, "name": "Arcade Test",
                 "balance": 250 * COIN, "display": "250"}]
        pieces = [{"txid": "ab" * 32, "number": 7, "owner": "nElse"}]

        def balances(self, addresses):
            return [r for r in self.rows if r["address"] in addresses]

        def inscriptions(self, owner=None, limit=50):
            return [p for p in self.pieces if p["owner"] == owner]

    class Node:
        def __init__(self, unspent):
            self.unspent = unspent

        def call(self, method, *args):
            assert method == "listunspent"
            return self.unspent

    own = ["nHome", "nElse", "nBroke"]
    index = Index()
    index.pieces.append({"txid": "cd" * 32, "number": 8, "owner": "nBroke"})

    # nElse can pay its own way; nBroke holds a piece and not enough to move
    # it. "Has an output" is not the same as "can pay" -- an address with a
    # hundredth of a coin was asked for 1.01 on every pass (D-046).
    node = Node([{"address": "nElse", "txid": "11" * 32, "vout": 0, "amount": 5.0,
                  "spendable": True},
                 {"address": "nHome", "txid": "22" * 32, "vout": 0, "amount": 9.0,
                  "spendable": True},
                 {"address": "nBroke", "txid": "55" * 32, "vout": 0, "amount": 0.01,
                  "spendable": True},
                 {"address": "nElse", "txid": "33" * 32, "vout": 1, "amount": 0.0001,
                  "spendable": True}])
    found = gather.stray(node, index, "nHome", own)
    assert found["coins"] == [], \
        "nElse still holds a token, so its coins are what will move it"
    assert found["needs_coins"] == ["nBroke"], "it holds a piece and cannot move it"
    assert len(found["tokens"]) == 1 and len(found["pieces"]) == 2
    assert all(c["address"] != "nHome" for c in found["coins"]), "home is not stray"

    # First pass: nothing but the seed, because everything else waits on it.
    sent = []
    def coins(sender, to, sats, outpoint=None):
        sent.append(("coins", sender, to, sats)); return f"tx{len(sent)}"
    def token(sender, to, pid, units):
        sent.append(("token", sender, to, pid, units)); return f"tx{len(sent)}"
    def piece(sender, to, txid):
        sent.append(("piece", sender, to, txid)); return f"tx{len(sent)}"

    gather.walk_home(node, index, "nHome", own, send_coins=coins,
                     send_token=token, send_piece=piece)
    assert sent == [("coins", "nHome", "nBroke", gather.SEED)], sent

    # Once it can pay, the things themselves travel -- and the stray coins go
    # last, so a sweep cannot take the fee a token move is about to need.
    sent.clear()
    node.unspent.append({"address": "nBroke", "txid": "44" * 32, "vout": 0,
                         "amount": 3.0, "spendable": True})
    gather.walk_home(node, index, "nHome", own, send_coins=coins,
                     send_token=token, send_piece=piece, limit=9)
    assert [s[0] for s in sent] == ["token", "piece", "piece"], \
        "coins stay where they are until the things they move have gone"
    assert sent[0][1:] == ("nElse", "nHome", 3, 250 * COIN)
    assert all(s[2] == "nHome" for s in sent), "everything goes to one address"

    # With nothing left on them, their coins come home too.
    index.rows.clear()
    index.pieces.clear()
    sent.clear()
    gather.walk_home(node, index, "nHome", own, send_coins=coins,
                     send_token=token, send_piece=piece, limit=9)
    assert [s[0] for s in sent] == ["coins", "coins"], sent
    assert {s[1] for s in sent} == {"nElse", "nBroke"}


# --- filling a standing order -----------------------------------------------

def _an_order(index, txid="or" + "d" * 62, tokens=1000 * COIN, coins=8 * COIN,
              address=SELLER):
    """One row of the book, as the index would hand it over."""
    index.orders = getattr(index, "orders", {})
    index.orders[txid] = {
        "txid": txid, "block_height": 500, "position": 0, "address": address,
        "sale_property": 3, "sale_amount": tokens, "want_property": 0,
        "want_amount": coins, "reserved": tokens}
    index.order = lambda key: index.orders.get(str(key))
    return txid


def test_an_order_is_filled_at_the_makers_own_price(world):
    """The price comes from the book, never from the question.

    What the taker cannot work out alone is which of the maker's outputs will
    carry the swap. It gets that here, in an offer it can build on -- which is
    why an order does not publish an outpoint and does not need to (D-063).
    """
    index, seller, _, offers = world
    order = _an_order(index)                       # 1,000 at 0.008
    offer = S.offer_for_order(seller, index, offers, "test", order, 250 * COIN,
                              BUYER, "ff" * 32, own=[SELLER])
    assert offer["seller"] == SELLER and offer["buyer"] == BUYER
    assert offer["give"] == {"kind": "token", "propertyid": 3, "name": "Arcade Test",
                             "amount": "250", "units": 250 * COIN}
    assert offer["take"]["sats"] == 2 * COIN, "a quarter of the order, a quarter of the coins"
    assert offer["outpoint"]["txid"] and ("1" * 64, offer["outpoint"]["vout"]) in seller.locked
    assert offers.get(offer["id"])["order"] == order, "written down against the order"


def test_the_rounding_never_pays_the_maker_less(world):
    """The engine's guard is integer arithmetic on the same numbers (D-062);
    an offer that rounded the other way would be signed by both wallets and
    then refused by the chain."""
    index, seller, _, offers = world
    order = _an_order(index, tokens=3 * COIN, coins=2 * COIN)      # 2 for 3
    offer = S.offer_for_order(seller, index, offers, "test", order, 1 * COIN,
                              BUYER, "", own=[SELLER])
    coins, tokens = offer["take"]["sats"], 1 * COIN
    assert coins * (3 * COIN) >= (2 * COIN) * tokens, "the guard the engine applies"
    assert coins == 66_666_667, "rounded up, by one satoshi"


def test_what_is_promised_to_one_buyer_is_not_offered_to_another(world):
    index, seller, _, offers = world
    order = _an_order(index, tokens=100 * COIN, coins=1 * COIN)
    S.offer_for_order(seller, index, offers, "test", order, 60 * COIN, BUYER, "",
                      own=[SELLER])
    with pytest.raises(S.SwapError, match="has 4000000000 left"):
        S.offer_for_order(seller, index, offers, "test", order, 60 * COIN, OTHER, "",
                          own=[SELLER])
    assert "promised to other buyers" in _refusal(
        lambda: S.offer_for_order(seller, index, offers, "test", order, 60 * COIN,
                                  OTHER, "", own=[SELLER]))
    # What is left still is.
    rest = S.offer_for_order(seller, index, offers, "test", order, 40 * COIN, OTHER,
                             "", own=[SELLER])
    assert rest["give"]["units"] == 40 * COIN


def test_an_order_that_is_not_this_wallets_is_refused(world):
    index, seller, _, offers = world
    order = _an_order(index, address=OTHER)
    with pytest.raises(S.SwapError, match="not this wallet's to fill"):
        S.offer_for_order(seller, index, offers, "test", order, 10 * COIN, BUYER, "",
                          own=[SELLER])


def test_a_wallet_cannot_fill_its_own_order(world):
    index, seller, _, offers = world
    order = _an_order(index)
    with pytest.raises(S.SwapError, match="cannot fill its own order"):
        S.offer_for_order(seller, index, offers, "test", order, 10 * COIN, SELLER, "",
                          own=[SELLER])


def test_an_order_that_is_not_there_is_not_a_refusal_about_something_else(world):
    index, seller, _, offers = world
    _an_order(index)
    with pytest.raises(S.SwapError, match="no such order on this node"):
        S.offer_for_order(seller, index, offers, "test", "ff" * 32, 10 * COIN, BUYER,
                          "", own=[SELLER])


def test_a_bid_is_not_filled_this_way(world):
    """The coin side has to be funded by the wallet that holds the coins."""
    index, seller, _, offers = world
    order = _an_order(index)
    index.orders[order].update(sale_property=0, want_property=3)
    with pytest.raises(S.SwapError, match="only an order selling a token for coins"):
        S.offer_for_order(seller, index, offers, "test", order, 10 * COIN, BUYER, "",
                          own=[SELLER])


def _refusal(call) -> str:
    try:
        call()
    except S.SwapError as exc:
        return str(exc)
    raise AssertionError("that should have been refused")


def test_a_column_added_later_reaches_a_database_that_already_exists(tmp_path):
    """CREATE TABLE IF NOT EXISTS does nothing to a table it finds.

    `order` was added to the schema and reached new installations only. On
    every wallet that had ever made an offer the column was missing, and the
    insert that names it did not fail loudly: SQLite reads a double-quoted
    name with no matching column as a STRING LITERAL, so four live rows came
    back with the word "order" in them and nothing complained. The first fill
    on such a wallet would have been refused (D-081).
    """
    import sqlite3

    path = tmp_path / "swaps.sqlite"
    # A store as it was before the column existed.
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE offer (id TEXT PRIMARY KEY, network TEXT NOT NULL, "
        "shop TEXT NOT NULL, listing INTEGER NOT NULL, seller TEXT NOT NULL, "
        "buyer TEXT NOT NULL, buyer_pubkey TEXT NOT NULL DEFAULT '', "
        "give TEXT NOT NULL, take TEXT NOT NULL, outpoint_txid TEXT NOT NULL, "
        "outpoint_vout INTEGER NOT NULL, outpoint_value INTEGER NOT NULL, "
        "created REAL NOT NULL, expires REAL NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'open', txid TEXT NOT NULL DEFAULT '', "
        "error TEXT NOT NULL DEFAULT '');")
    old.commit()
    old.close()

    offers = S.Offers(path)
    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(offer)")}
    assert "order" in columns, "an existing database has to get the column too"

    # And it round-trips, which is the thing the missing column broke.
    offers.add({"id": "abc", "network": "test", "shop": "", "order": "f" * 64,
                "listing": -1, "seller": "nSeller", "buyer": "nBuyer",
                "buyer_pubkey": "", "give": {"kind": "coins", "amount": "1", "sats": 1},
                "take": {"kind": "coins", "amount": "2", "sats": 2},
                "outpoint": {"txid": "a" * 64, "vout": 0, "value": 10},
                "created": 1.0, "expires": 2.0})
    assert offers.get("abc")["order"] == "f" * 64
