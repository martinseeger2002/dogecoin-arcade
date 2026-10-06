"""Token/token pairs from the accounts' side (2026-10-06, the operator: "token to token
pairs on the token exchange so that people can exchange one token for another").

Two accounts, each holding a token of its own, issued on the chain and paid over
for real (the engine matches only what it read out of a block). One sells its
token priced in the other's; the other buys at that price; when the second
order's block lands the chain has swapped them, with nobody pressing "fill".
Seats 160-161: the shared regtest chain hands every test file its own seats.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_order import _bookcoin, _held, _signed      # noqa: E402
from test_account_offer import node, _settled                 # noqa: F401,E402

COIN = 100_000_000


def test_two_accounts_trade_one_token_for_another_through_the_book(node):
    seller, buyer = _bookcoin(node, 160), _bookcoin(node, 161)
    A, B = seller["pid"], buyer["pid"]
    state, rpc = seller["state"], seller["rpc"]

    asked = seller["client"].post("/account/pair-order", json={
        "base": A, "quote": B, "side": "sell", "amount": "20", "price": "1.5"})
    assert asked.status_code == 200, asked.text
    _signed(seller["client"], seller["secret"], seller["pubkey"], asked)
    _settled(state, rpc)
    held = _held(state, seller["address"], A)
    assert held[1] == 20 * COIN, f"the sell holds its 20 back: {held}"

    page = seller["client"].get(f"/exchange/pairs/{A}/{B}")
    assert page.status_code == 200 and "(yours)" in page.text

    # More than the buyer holds is refused before it costs a fee.
    too_much = buyer["client"].post("/account/pair-order", json={
        "base": A, "quote": B, "side": "buy", "amount": "100000", "price": "1.5"})
    assert too_much.status_code == 400 and "holds" in too_much.json()["detail"]

    took = buyer["client"].post("/account/pair-order", json={
        "base": A, "quote": B, "side": "buy", "amount": "20", "price": "1.5"})
    assert took.status_code == 200, took.text
    _signed(buyer["client"], buyer["secret"], buyer["pubkey"], took)
    _settled(state, rpc)

    assert _held(state, buyer["address"], A) == (20 * COIN, 0), "the buyer has the 20"
    assert _held(state, seller["address"], B)[0] == 30 * COIN, "the seller was paid 30 of the other token"
    assert _held(state, seller["address"], A)[1] == 0, "nothing of the sell is held back any more"

    index = state.token_index(state.messaging)
    (trade,) = index.pair_trades(A, B)
    assert (trade["base"], trade["quote"]) == (20 * COIN, 30 * COIN)
    page = buyer["client"].get(f"/exchange/pairs/{A}/{B}")
    assert page.status_code == 200 and "Recent trades" in page.text and "1.5" in page.text
    tab = buyer["client"].get("/exchange?tab=tokens")
    assert tab.status_code == 200 and f"/exchange/pairs/{min(A, B)}/{max(A, B)}" in tab.text


def test_a_pair_of_one_token_with_itself_is_no_pair(node):
    seat = _bookcoin(node, 162)
    said = seat["client"].post("/account/pair-order", json={
        "base": seat["pid"], "quote": seat["pid"], "side": "sell", "amount": "1", "price": "1"})
    assert said.status_code == 400 and "two different" in said.json()["detail"]
    assert seat["client"].get(f"/exchange/pairs/{seat['pid']}/{seat['pid']}").status_code == 404
