"""A trade between two players, agreed in a game: NFT for a token, NFT for an NFT.

The answered-offer path with the offer taken out (app._trade_terms): the side
giving an NFT signs a leg for ONE named buyer, and the buyer finishes it with
/account/fill, naming the trade it agreed to (`expect`). Two accounts whose
keys this node never holds, against a real regtest node, because what can be
wrong here -- whether the engine moves an NFT the BUYER pays with, whether a
leg paid in the ledger can be stretched -- only shows in a block.
"""

import contextlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import _inscribed, _pair, _settled, node      # noqa: E402,F401
from test_account_accept import _priced_in                             # noqa: E402
from test_funding import _sign                                         # noqa: E402
from test_web import app_state, client                               # noqa: F401,E402

from arcade import funding                                            # noqa: E402

COIN = 100_000_000


def _leg(pair, take: dict, **kw):
    """The holder's leg for this trade, signed and handed back."""
    client, secret, pubkey, _address = pair["holder"]
    body = {"give": {"inscription": pair["piece"]}, "take": take,
            "buyer": pair["bidder"][3], **kw}
    asked = client.post("/account/trade/leg", json=body)
    assert asked.status_code == 200, asked.text
    leg = asked.json()
    assert not leg.get("needs_split"), "the fixture seats two coins"
    signed = client.post("/account/trade/leg/sign", json={
        **body, "raw": leg["raw"], "pubkey": pubkey.hex(),
        "signatures": [_sign(secret, bytes.fromhex(d), funding.SINGLE_ANYONECANPAY).hex()
                       for d in leg["sighashes"]]})
    return leg, signed


def _finish(pair, leg: dict, expect: dict):
    client, secret, pubkey, _address = pair["bidder"]
    asked = client.post("/account/fill", json={"leg": leg, "expect": expect})
    if asked.status_code != 200:
        return asked
    said = asked.json()
    return client.post("/account/fill/sign", json={
        "leg": leg, "expect": expect, "raw": said["raw"], "pubkey": pubkey.hex(),
        "signatures": [_sign(secret, bytes.fromhex(d)).hex() for d in said["sighashes"]]})


def _valid(pair, txid: str) -> None:
    index = pair["state"].token_index(pair["state"].messaging)
    with contextlib.closing(index.open()) as db:
        verdict = db.conn.execute(
            "SELECT valid, invalid_reason FROM arcade_tx WHERE txid = ?", (txid,)).fetchone()
    assert verdict["valid"] == 1, verdict["invalid_reason"]


def test_an_nft_traded_for_a_token_between_two_players(node):
    pair = _pair(node, 81, 82)
    pid = _priced_in(pair["state"], pair["bidder"][3], 50 * COIN, 126)
    take = {"token": pid, "amount": "25"}
    leg, signed = _leg(pair, take)
    assert signed.status_code == 200, signed.text
    handed = signed.json()
    assert handed["seal_to"], "the leg goes to the buyer sealed, never in clear"
    assert pair["rpc"].call("getrawmempool") == [], "signing a leg sends nothing"

    done = _finish(pair, handed["leg"], {"give": {"inscription": pair["piece"]}, "take": take})
    assert done.status_code == 200, done.text
    _settled(pair["state"], pair["rpc"])
    _valid(pair, done.json()["txid"])
    index = pair["state"].token_index(pair["state"].messaging)
    assert index.inscription(pair["piece"])["owner"] == pair["bidder"][3]
    assert int(index.balance(pair["holder"][3], pid)) == 25 * COIN
    assert int(index.balance(pair["bidder"][3], pid)) == 25 * COIN


def test_an_nft_traded_for_an_nft_between_two_players(node):
    pair = _pair(node, 83, 84)
    bclient, bsecret, bpubkey, _ = pair["bidder"]
    theirs = _inscribed(bclient, pair["state"], pair["rpc"], bsecret, bpubkey, "their piece")
    _settled(pair["state"], pair["rpc"])
    take = {"inscription": theirs}
    leg, signed = _leg(pair, take)
    assert signed.status_code == 200, signed.text

    done = _finish(pair, signed.json()["leg"],
                   {"give": {"inscription": pair["piece"]}, "take": take})
    assert done.status_code == 200, done.text
    _settled(pair["state"], pair["rpc"])
    _valid(pair, done.json()["txid"])
    index = pair["state"].token_index(pair["state"].messaging)
    assert index.inscription(pair["piece"])["owner"] == pair["bidder"][3]
    assert index.inscription(theirs)["owner"] == pair["holder"][3], \
        "the NFT the buyer paid with crossed the other way"


def test_a_buyer_only_completes_the_trade_it_agreed_to(node):
    pair = _pair(node, 85, 86)
    pid = _priced_in(pair["state"], pair["bidder"][3], 50 * COIN, 127)
    _leg_, signed = _leg(pair, {"token": pid, "amount": "40"})
    assert signed.status_code == 200, signed.text
    done = _finish(pair, signed.json()["leg"],
                   {"give": {"inscription": pair["piece"]},
                    "take": {"token": pid, "amount": "10"}})
    assert done.status_code == 400 and "agreed" in done.text
    assert pair["rpc"].call("getrawmempool") == []


def test_a_trade_the_buyer_cannot_pay_or_a_piece_not_yours_is_refused(node):
    pair = _pair(node, 87, 88)
    pid = _priced_in(pair["state"], pair["bidder"][3], 5 * COIN, 128)
    client = pair["holder"][0]
    asked = client.post("/account/trade/leg", json={
        "give": {"inscription": pair["piece"]}, "take": {"token": pid, "amount": "6"},
        "buyer": pair["bidder"][3]})
    assert asked.status_code == 400 and "cannot pay" in asked.text
    asked = pair["bidder"][0].post("/account/trade/leg", json={
        "give": {"inscription": pair["piece"]}, "take": {"token": pid, "amount": "1"},
        "buyer": pair["holder"][3]})
    assert asked.status_code == 400 and "not this account" in asked.text
    asked = client.post("/account/trade/leg", json={
        "give": {"inscription": pair["piece"]}, "take": {"token": pid, "amount": "1"},
        "buyer": pair["holder"][3]})
    assert asked.status_code == 400 and "another player" in asked.text


def test_a_leg_signed_for_one_trade_is_not_handed_over_as_another(node):
    pair = _pair(node, 89, 90)
    pid = _priced_in(pair["state"], pair["bidder"][3], 50 * COIN, 129)
    client, secret, pubkey, _ = pair["holder"]
    body = {"give": {"inscription": pair["piece"]}, "take": {"token": pid, "amount": "40"},
            "buyer": pair["bidder"][3]}
    leg = client.post("/account/trade/leg", json=body).json()
    signed = client.post("/account/trade/leg/sign", json={
        **body, "take": {"token": pid, "amount": "4"}, "raw": leg["raw"],
        "pubkey": pubkey.hex(),
        "signatures": [_sign(secret, bytes.fromhex(d), funding.SINGLE_ANYONECANPAY).hex()
                       for d in leg["sighashes"]]})
    assert signed.status_code == 400 and "different trade" in signed.text


def test_a_piece_still_on_its_way_can_be_traded_and_settles_after_it(node):
    """2026-10-04, the operator: a piece picked up in a game is tradeable at once. The
    holder trades a piece that is still in the mempool on its way to them; the
    trade spends the coin that arrival paid them, so the chain settles the
    arrival first and the buyer ends up with the piece."""
    from test_account_offer import _seated, _signed
    pair = _pair(node, 172, 173)
    state, rpc = pair["state"], pair["rpc"]
    maker = _seated(*node, 174)
    gift = _inscribed(maker[0], state, rpc, maker[1], maker[2], "a ring picked up a moment ago")
    sent = maker[0].post("/account/nft/send", json={"piece": gift, "to": pair["holder"][3]})
    assert sent.status_code == 200, sent.text
    arrival = _signed(maker[0], maker[1], maker[2], sent.json())
    assert arrival.status_code == 200, arrival.text
    index = state.token_index(state.messaging)
    assert index.inscription(gift)["owner"] == maker[3], "not landed yet"

    pid = _priced_in(state, pair["bidder"][3], 50 * COIN, 140)
    take = {"token": pid, "amount": "5"}
    pair = {**pair, "piece": gift}
    leg, signed = _leg(pair, take)
    assert signed.status_code == 200, signed.text
    assert (leg["inputs"][0]["txid"], leg["inputs"][0]["vout"])[0] == arrival.json()["txid"], \
        "the trade spends what the arrival paid the holder"
    done = _finish(pair, signed.json()["leg"], {"give": {"inscription": gift}, "take": take})
    assert done.status_code == 200, done.text
    _settled(state, rpc)
    _valid(pair, done.json()["txid"])
    assert index.inscription(gift)["owner"] == pair["bidder"][3], "the buyer holds it"
    assert int(index.balance(pair["holder"][3], pid)) == 5 * COIN
