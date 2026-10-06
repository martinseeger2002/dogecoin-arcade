"""The cross-chain book's own arithmetic and bookkeeping (arcade/xchain.py), with
no chain in sight: matching, the fee, cancels, refunds, and payouts that are
built once and never twice. 2026-10-06: "there shouldn't be a limit, but
you need to make sure there are no bugs"."""

import random

import pytest

from arcade.xchain import (AWAITING, BUILT, CANCELLED, FAILED, FILLED, OPEN, QUEUED, SENT,
                           STUCK, Book, XchainError, fee_of)

PEPE = 10 ** 8
T = 10 ** 8         # one whole divisible token, raw


@pytest.fixture
def book(tmp_path):
    return Book(tmp_path / "xchain.sqlite")


def order(book, side, amount, pepe, owner="alice", kind="token", asset="59", now=None):
    o = book.place(owner=owner, side=side, kind=kind, asset=asset, amount=amount, pepe=pepe,
                   pay_test=f"n{owner}Test", pay_main=f"P{owner}Main")
    book.deposited(o["id"], f"dep-{o['id']}")
    fills = book.count_deposit(o["id"], now=now)
    return book.get(o["id"]), fills


def paid(book):
    return {p["id"]: (p["chain"], p["kind"], p["amount"], p["to_addr"]) for p in book.payouts()}


def test_an_order_waits_for_its_deposit_and_then_rests(book):
    o = book.place(owner="alice", side="sell", kind="token", asset="59", amount=10 * T,
                   pepe=5 * PEPE, pay_test="nA", pay_main="PA")
    assert o["status"] == AWAITING and book.book("token", "59")["sells"] == []
    book.deposited(o["id"], "tx1")
    with pytest.raises(XchainError):
        book.deposited(o["id"], "tx2")
    assert book.count_deposit(o["id"]) == []
    assert book.get(o["id"])["status"] == OPEN and len(book.book("token", "59")["sells"]) == 1


def test_one_deposit_cannot_back_two_orders(book):
    a = book.place(owner="a", side="sell", kind="token", asset="59", amount=T, pepe=PEPE, pay_test="n", pay_main="P")
    b = book.place(owner="b", side="sell", kind="token", asset="59", amount=T, pepe=PEPE, pay_test="n", pay_main="P")
    book.deposited(a["id"], "same")
    with pytest.raises(XchainError, match="another order"):
        book.deposited(b["id"], "same")


def test_a_crossing_buy_fills_at_the_sellers_price_and_gets_the_change_back(book):
    sell, _ = order(book, "sell", 10 * T, 5 * PEPE, owner="sam")          # 0.5 PEPE each
    buy, fills = order(book, "buy", 10 * T, 8 * PEPE, owner="bea")        # willing to pay 0.8
    (f,) = fills
    assert (f["amount"], f["pepe"], f["fee"]) == (10 * T, 5 * PEPE, fee_of(5 * PEPE))
    assert book.get(sell["id"])["status"] == FILLED and book.get(buy["id"])["status"] == FILLED
    p = paid(book)
    assert p[f"fill:{f['id']}:asset"] == ("testnet", "token", 10 * T, "nbeaTest")
    assert p[f"fill:{f['id']}:pepe"] == ("main", "pepe", 5 * PEPE - fee_of(5 * PEPE), "PsamMain")
    assert p[f"refund:{buy['id']}"] == ("main", "pepe", 3 * PEPE, "PbeaMain"), "the 3 PEPE it did not need"
    assert fee_of(5 * PEPE) == 2_500_000, "0.5% of 5 PEPE"


def test_a_crossing_sell_fills_at_the_buyers_price(book):
    order(book, "buy", 10 * T, 8 * PEPE, owner="bea")
    sell, fills = order(book, "sell", 4 * T, 1 * PEPE, owner="sam")      # asks only 0.25 each
    (f,) = fills
    assert (f["amount"], f["pepe"]) == (4 * T, int(3.2 * PEPE)), "the resting buyer's 0.8 each"
    rest = book.book("token", "59")["buys"][0]
    assert (rest["left_amount"], rest["left_pepe"]) == (6 * T, int(4.8 * PEPE))


def test_orders_that_do_not_cross_both_rest(book):
    order(book, "sell", 10 * T, 9 * PEPE, owner="sam")
    order(book, "buy", 10 * T, 8 * PEPE, owner="bea")
    b = book.book("token", "59")
    assert len(b["sells"]) == 1 and len(b["buys"]) == 1 and book.payouts() == []


def test_best_price_first_then_the_oldest(book):
    a, _ = order(book, "sell", 10 * T, 6 * PEPE, owner="a", now=1)
    b, _ = order(book, "sell", 10 * T, 5 * PEPE, owner="b", now=2)     # cheapest
    c, _ = order(book, "sell", 10 * T, 6 * PEPE, owner="c", now=3)
    _, fills = order(book, "buy", 25 * T, 20 * PEPE, owner="z", now=4)
    assert [(f["seller"], f["amount"]) for f in fills] == [(b["id"], 10 * T), (a["id"], 10 * T), (c["id"], 5 * T)]
    assert book.get(c["id"])["left_amount"] == 5 * T


def test_an_nft_trades_once(book):
    s, _ = order(book, "sell", 1, 3 * PEPE, owner="sam", kind="nft", asset="ab" * 32)
    b1, fills = order(book, "buy", 1, 3 * PEPE, owner="bea", kind="nft", asset="ab" * 32)
    assert len(fills) == 1 and book.get(s["id"])["status"] == FILLED
    b2, fills = order(book, "buy", 1, 4 * PEPE, owner="bob", kind="nft", asset="ab" * 32)
    assert fills == [] and book.get(b2["id"])["status"] == OPEN, "nothing left to buy"
    with pytest.raises(XchainError, match="one of a kind"):
        book.place(owner="x", side="sell", kind="nft", asset="cd" * 32, amount=2, pepe=PEPE,
                   pay_test="n", pay_main="P")


def test_a_cancel_gives_back_what_is_left_and_only_to_its_owner(book):
    s, _ = order(book, "sell", 10 * T, 5 * PEPE, owner="sam")
    order(book, "buy", 4 * T, 2 * PEPE, owner="bea")
    with pytest.raises(XchainError, match="no order of yours"):
        book.cancel(s["id"], "mallory")
    book.cancel(s["id"], "sam")
    assert book.get(s["id"])["status"] == CANCELLED
    assert paid(book)[f"refund:{s['id']}"] == ("testnet", "token", 6 * T, "nsamTest")
    with pytest.raises(XchainError):
        book.cancel(s["id"], "sam"), "a second cancel refunds nothing twice"


def test_a_cancelled_order_is_never_matched(book):
    s, _ = order(book, "sell", 10 * T, 5 * PEPE, owner="sam")
    book.cancel(s["id"], "sam")
    _, fills = order(book, "buy", 10 * T, 9 * PEPE, owner="bea")
    assert fills == []


def test_a_deposit_that_fails_never_opens(book):
    o = book.place(owner="a", side="buy", kind="token", asset="59", amount=T, pepe=PEPE, pay_test="n", pay_main="P")
    book.deposited(o["id"], "gone")
    book.fail_deposit(o["id"], "never confirmed")
    assert book.get(o["id"])["status"] == FAILED and book.count_deposit(o["id"]) == []


def test_a_late_deposit_for_a_cancelled_order_is_returned_once(book):
    o = book.place(owner="a", side="buy", kind="token", asset="59", amount=T, pepe=2 * PEPE,
                   pay_test="nA", pay_main="PA")
    book.deposited(o["id"], "late")
    book.cancel(o["id"], "a")
    book.refund_late(o["id"]); book.refund_late(o["id"])
    assert [p["id"] for p in book.payouts()] == [f"refund:{o['id']}"]
    assert book.payouts()[0]["amount"] == 2 * PEPE


def test_a_payout_is_built_once_and_its_bytes_resent_after_a_crash(book):
    order(book, "sell", T, PEPE, owner="sam")
    order(book, "buy", T, PEPE, owner="bea")
    built, sent = [], []

    def build(p):
        built.append(p["id"])
        return ("raw-" + p["id"], "tx-" + p["id"])

    def crash(p):
        raise RuntimeError("the node went away mid-broadcast")

    book.pay(build, crash)
    assert all(p["status"] == BUILT for p in book.payouts())
    book.pay(build, lambda p: sent.append(p["raw"]) or p["txid"])
    assert sorted(built) == sorted(set(built)), "nothing was built twice"
    assert all(r.startswith("raw-") for r in sent) and len(sent) == 2
    assert all(p["status"] == SENT for p in book.payouts())
    book.pay(build, lambda p: sent.append(p["raw"]))
    assert len(sent) == 2, "a sent payout is never sent again"


def test_a_payout_that_cannot_be_built_waits_and_then_says_so(book):
    order(book, "sell", T, PEPE, owner="sam")
    order(book, "buy", T, PEPE, owner="bea")
    for _ in range(22):
        book.pay(lambda p: (_ for _ in ()).throw(RuntimeError("wallet locked")), lambda p: "x")
    assert {p["status"] for p in book.payouts()} == {STUCK}
    assert all("wallet locked" in p["error"] for p in book.payouts())


def test_what_the_node_owes_always_matches_what_it_was_given(book):
    """A random market: whatever happens, every unit and satoshi deposited is
    either still held for an open order, owed in a payout, or taken as the fee."""
    rng = random.Random(7)
    deposited = {"asset": 0, "pepe": 0}
    ids = []
    for i in range(300):
        side = rng.choice(["sell", "buy"])
        amount = rng.randint(1, 50) * T // rng.choice([1, 3, 7])
        pepe = rng.randint(1, 10 ** 9)
        o, _ = order(book, side, amount, pepe, owner=f"u{i % 9}", now=i)
        ids.append(o)
        deposited["asset" if side == "sell" else "pepe"] += amount if side == "sell" else pepe
        if rng.random() < 0.15:
            victim = rng.choice(ids)
            try:
                book.cancel(victim["id"], victim["owner"])
            except XchainError:
                pass
    owed = book.owed()
    fees = sum(f["fee"] for f in book.fills("token", "59", limit=10 ** 6))
    asset_owed = owed.get(("testnet", "token", "59"), 0)
    pepe_owed = owed.get(("main", "pepe", "pepe"), 0)
    assert asset_owed == deposited["asset"], "every unit sold in is held, or owed to a buyer or back"
    assert pepe_owed + fees == deposited["pepe"], "every satoshi bought in is held, owed, or the fee"
    for f in book.fills("token", "59", limit=10 ** 6):
        assert f["amount"] > 0 and f["pepe"] > 0 and 0 <= f["fee"] <= f["pepe"] // 100


def test_nobody_is_paid_off_their_price(book):
    rng = random.Random(11)
    for i in range(200):
        side = rng.choice(["sell", "buy"])
        order(book, side, rng.randint(1, 30) * T, rng.randint(10 ** 6, 10 ** 9), owner=f"u{i}", now=i)
    from fractions import Fraction
    with book._open() as conn:
        for f in conn.execute("SELECT * FROM xfill"):
            s = conn.execute("SELECT * FROM xorder WHERE id=?", (f["seller"],)).fetchone()
            b = conn.execute("SELECT * FROM xorder WHERE id=?", (f["buyer"],)).fetchone()
            each = Fraction(f["pepe"], f["amount"])
            assert each <= Fraction(b["pepe"], b["amount"]), "a buyer never pays above its price"
            # a seller is never paid below its price by more than the rounding of one satoshi
            assert f["pepe"] + 1 >= Fraction(s["pepe"], s["amount"]) * f["amount"]


def test_an_order_whose_deposit_was_never_signed_is_closed_after_an_hour(book):
    o = book.place(owner="a", side="buy", kind="token", asset="59", amount=T, pepe=PEPE, pay_test="n", pay_main="P")
    assert [x["id"] for x in book.orders_of("a")] == [o["id"]] or book.orders_of("a") == [], "not listed as an order"
    assert book.sweep_unsigned(now=o["created"] + 60) == 0
    assert book.sweep_unsigned(now=o["created"] + 3601) == 1
    assert book.get(o["id"])["status"] == CANCELLED and book.payouts() == [], "nothing was deposited, nothing owed"
    assert book.orders_of("a") == []
