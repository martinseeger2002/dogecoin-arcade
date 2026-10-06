"""The cross-chain book end to end, on two regtest chains: the session's node plays
testnet, a second one plays mainnet. A token is deposited for real, PEPE is
deposited for real, the book matches them, and both payouts land on their chains
-- including after a crash between building a payout and broadcasting it.
(2026-10-06: "there shouldn't be a limit, but you need to make sure there are
no bugs".)"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_claim import _catch_up                                # noqa: E402
from arcade.regtest import RegtestNode                                  # noqa: E402
from arcade.tokens import TokenSender, issuance_payload, send_payload  # noqa: E402
from arcade.xchain import FILLED, OPEN, SENT, fee_of                    # noqa: E402

COIN = 100_000_000


def _fresh(keypool: str = "-keypool=1") -> RegtestNode:
    """A throwaway node of our own with a one-key pool: on a busy machine a new
    wallet's hundred keys alone outlast the 60 s a node is given to come up."""
    n = RegtestNode(extra_args=(keypool,))
    try:
        n.start()
    except RuntimeError as exc:
        n.stop()
        pytest.skip(f"a regtest node is unavailable: {exc}")
    return n


@pytest.fixture
def two_chains(tmp_path):
    """The application with BOTH chains pointed at throwaway regtest nodes: one
    plays testnet (messaging, tokens), the other mainnet (the ledger)."""
    from fastapi.testclient import TestClient

    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext

    test, main = _fresh(), None
    try:
        main = _fresh()

        def pointed(node, role, label):
            class Pointed(ChainContext):
                def credentials(self):
                    return node.rpc._creds

                @property
                def params(self):
                    return node.params
            return Pointed(network="regtest", role=role, label=label, datadir=node.datadir)

        state = AppState(home=tmp_path, messaging=pointed(test, "messaging", "Testnet"),
                         ledger=pointed(main, "ledger", "Mainnet"))
        (tmp_path / "tokens-chain").write_text("regtest\n")
        app = TestClient(create_app(state))
        test.rpc.call("generate", 110)
        main.rpc.call("generate", 110)
        _catch_up(state, test.rpc)
        yield app, state, test.rpc, main.rpc
    finally:
        for n in (test, main):
            if n is not None:
                n.stop()


def _mined(state, rpc, n=1):
    rpc.call("generate", n)
    _catch_up(state, rpc)


def _token(state, rpc, name):
    issuer = rpc.call("getnewaddress")
    rpc.call("sendtoaddress", issuer, 5.0)
    _mined(state, rpc)
    sender = TokenSender(rpc, state.messaging.params)
    made = sender.broadcast(sender.prepare(issuer, issuance_payload(
        name=name, divisible=True, managed=False, amount=1000 * COIN)))
    _mined(state, rpc)
    index = state.token_index(state.messaging)
    pid = next(p["property_id"] for p in index.properties() if str(p["creation_txid"]) == made)
    return issuer, pid


def test_a_token_sells_for_mainnet_pepe_and_both_sides_are_paid(two_chains, monkeypatch):
    app, state, test_rpc, main_rpc = two_chains
    clerk = state.xchain
    book = clerk.book
    seller, pid = _token(state, test_rpc, "Xchain Logs 1")
    buyer_test = test_rpc.call("getnewaddress")
    seller_main = main_rpc.call("getnewaddress")
    buyer_main_refund = main_rpc.call("getnewaddress")

    # the seller's order, and its deposit: 10 tokens to the exchange address
    sell = book.place(owner="seller", side="sell", kind="token", asset=str(pid), amount=10 * COIN,
                      pepe=5 * COIN, pay_test=seller, pay_main=seller_main)
    sender = TokenSender(test_rpc, state.messaging.params)
    dep = sender.broadcast(sender.prepare(seller, send_payload(pid, 10 * COIN), clerk.address("testnet")))
    book.deposited(sell["id"], dep)
    _mined(state, test_rpc, 3)
    clerk.tick()
    assert book.get(sell["id"])["status"] == "awaiting", "three confirmations are not six"
    _mined(state, test_rpc, 3)
    clerk.tick()
    assert book.get(sell["id"])["status"] == OPEN

    # the buyer's order: 8 PEPE for the 10 tokens, deposited on "mainnet"
    buy = book.place(owner="buyer", side="buy", kind="token", asset=str(pid), amount=10 * COIN,
                     pepe=8 * COIN, pay_test=buyer_test, pay_main=buyer_main_refund)
    book.deposited(buy["id"], main_rpc.call("sendtoaddress", clerk.address("main"), 8.0))
    main_rpc.call("generate", 2)

    # a crash between building the payouts and sending them
    real = clerk.broadcast
    monkeypatch.setattr(clerk, "broadcast", lambda p: (_ for _ in ()).throw(RuntimeError("power cut")))
    clerk.tick()
    assert book.get(buy["id"])["status"] == FILLED and book.get(sell["id"])["status"] == FILLED
    built = {p["id"]: p["txid"] for p in book.payouts()}
    assert len(built) == 3 and all(built.values()), "built and saved, not sent"
    monkeypatch.setattr(clerk, "broadcast", real)
    clerk.tick()
    assert {p["id"]: p["txid"] for p in book.payouts()} == built, "the same transactions, not new ones"
    assert all(p["status"] == SENT for p in book.payouts())

    main_rpc.call("generate", 1)
    _mined(state, test_rpc, 1)
    index = state.token_index(state.messaging)
    assert index.balance(buyer_test, pid) == 10 * COIN, "the buyer has the tokens on testnet"
    paid = int(round(main_rpc.call("getreceivedbyaddress", seller_main) * COIN))
    assert paid == 5 * COIN - fee_of(5 * COIN), "the seller has its price on mainnet, less 0.5%"
    back = int(round(main_rpc.call("getreceivedbyaddress", buyer_main_refund) * COIN))
    assert back == 3 * COIN, "the buyer's 3 PEPE it did not need came back"


def test_a_deposit_that_is_not_what_the_order_says_is_returned_and_the_order_fails(two_chains):
    app, state, test_rpc, main_rpc = two_chains
    clerk, book = state.xchain, state.xchain.book
    refund_to = main_rpc.call("getnewaddress")
    buy = book.place(owner="b", side="buy", kind="token", asset="5", amount=COIN, pepe=2 * COIN,
                     pay_test=test_rpc.call("getnewaddress"), pay_main=refund_to)
    book.deposited(buy["id"], main_rpc.call("sendtoaddress", clerk.address("main"), 1.5))
    main_rpc.call("generate", 2)
    clerk.tick()
    assert book.get(buy["id"])["status"] == "failed"
    clerk.tick()
    main_rpc.call("generate", 1)
    assert int(round(main_rpc.call("getreceivedbyaddress", refund_to) * COIN)) == int(1.5 * COIN)


def test_a_payout_waits_rather_than_pay_one_person_from_anothers_deposit(two_chains, monkeypatch):
    app, state, test_rpc, main_rpc = two_chains
    clerk, book = state.xchain, state.xchain.book
    # an order whose PEPE is owed, and a wallet that says it holds less than that
    seller, pid = _token(state, test_rpc, "Xchain Logs 2")
    sell = book.place(owner="s", side="sell", kind="token", asset=str(pid), amount=COIN, pepe=COIN,
                      pay_test=seller, pay_main=main_rpc.call("getnewaddress"))
    sender = TokenSender(test_rpc, state.messaging.params)
    book.deposited(sell["id"], sender.broadcast(sender.prepare(seller, send_payload(pid, COIN),
                                                               clerk.address("testnet"))))
    _mined(state, test_rpc, 6)
    clerk.tick()
    buy = book.place(owner="b", side="buy", kind="token", asset=str(pid), amount=COIN, pepe=COIN,
                     pay_test=test_rpc.call("getnewaddress"), pay_main=main_rpc.call("getnewaddress"))
    book.deposited(buy["id"], main_rpc.call("sendtoaddress", clerk.address("main"), 1.0))
    main_rpc.call("generate", 2)
    real_call = main_rpc.call

    class Thin:
        _creds = main_rpc._creds

        def call(self, method, *a):
            return 0.0 if method == "getbalance" else real_call(method, *a)

    import contextlib
    monkeypatch.setattr(state.ledger, "rpc", lambda: contextlib.nullcontext(Thin()))
    clerk.tick()
    pepe = [p for p in book.payouts() if p["chain"] == "main"]
    assert pepe and all(p["status"] == "queued" and "top it up" in p["error"] for p in pepe)
