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

        def pointed(node, role, label, network):
            class Pointed(ChainContext):
                def credentials(self):
                    return node.rpc._creds

                @property
                def params(self):
                    return node.params
            return Pointed(network=network, role=role, label=label, datadir=node.datadir)

        # the stand-in mainnet is NAMED main (is_mainnet, its own address keys, its own
        # offers) while running regtest's rules, so the two chains are never mistaken
        state = AppState(home=tmp_path, messaging=pointed(test, "messaging", "Testnet", "regtest"),
                         ledger=pointed(main, "ledger", "Mainnet", "main"))
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
    # Counted from the mempool (2026-10-09: "This should be instant from the
    # mempool"): the deposit opens the order before any block carries it.
    clerk.tick()
    assert book.get(sell["id"])["status"] == OPEN, "a deposit in the mempool counts"
    _mined(state, test_rpc, 1)

    # the buyer's order: 8 PEPE for the 10 tokens, deposited on "mainnet"
    buy = book.place(owner="buyer", side="buy", kind="token", asset=str(pid), amount=10 * COIN,
                     pepe=8 * COIN, pay_test=buyer_test, pay_main=buyer_main_refund)
    book.deposited(buy["id"], main_rpc.call("sendtoaddress", clerk.address("main"), 8.0))
    main_rpc.call("generate", 2)

    # a crash between building the payouts and sending them
    real = clerk.broadcast
    monkeypatch.setattr(clerk, "broadcast", lambda p: (_ for _ in ()).throw(RuntimeError("power cut")))
    clerk.tick()
    _mined(state, test_rpc, 1)        # the exchange address's fee coins, topped up on the first pass
    clerk.tick()
    assert book.get(buy["id"])["status"] == FILLED and book.get(sell["id"])["status"] == FILLED
    built = {p["id"]: p["txid"] for p in book.payouts()}
    assert len(built) == 3 and all(built.values()), ("built and saved, not sent",
                                                     {p["id"]: p["error"] for p in book.payouts()})
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


from fastapi.testclient import TestClient                               # noqa: E402
from test_account_claim import _sign_in                                 # noqa: E402
from test_funding import _pubkey, _sign                                 # noqa: E402
from arcade.script import b58check_encode, hash160                      # noqa: E402


def person(app, state, which):
    who = TestClient(app.app)
    _sign_in(who)
    secret = int.from_bytes(bytes([0x71, which]) + bytes(30), "big")
    pub = _pubkey(secret)
    test_addr = b58check_encode(state.messaging.params.pubkeyhash_version, hash160(pub))
    main_addr = b58check_encode(state.ledger.params.pubkeyhash_version, hash160(pub))
    said = who.post("/account/address", json={"address": test_addr, "coin_pubkey": pub.hex()})
    assert said.status_code == 200, said.text
    # the stand-in mainnet runs regtest's address versions, which /account/address rightly
    # refuses as a mainnet address: written where that route writes a real one
    account = next(k[len("address:"):] for k, v in state.settings().items()
                   if k.startswith("address:") and k.count(":") == 1 and v == test_addr)
    state.set_setting(f"address:main:{account}", main_addr)
    state.set_setting(f"coinkey:main:{account}", pub.hex())
    state.set_setting(f"mainnet:{account}", "yes")      # said yes to real coins (the Backup page's check)
    import contextlib
    from arcade import utxos as utxoslib
    index = state.token_index(state.ledger)                # and its coins followed, as the route does
    with contextlib.closing(index.open()) as db:
        utxoslib.watch(db, main_addr, index.indexed_height() or 0, "xchain test")
    account_key = who.get("/account/me").json().get("pubkey") if who.get("/account/me").status_code == 200 else None
    return who, secret, pub, test_addr, main_addr, account_key

def signed(who, secret, pub, offer):
    done = who.post("/account/sign", json={"offer": offer["offer"], "pubkey": pub.hex(),
                                           "signatures": [_sign(secret, bytes.fromhex(d)).hex()
                                                          for d in offer["sighashes"]]})
    assert done.status_code == 200, done.text
    return done.json()["txid"]



def _ledger_caught_up(state, rpc):
    index = state.token_index(state.ledger)
    for _ in range(200):
        if index.status(rpc.call("getblockcount"))["current"]:
            return
        index.sync()


def test_an_account_sells_a_token_and_another_buys_it_with_mainnet_pepe_through_the_pages(two_chains):
    """The whole road a person takes: the page builds the deposit, the account signs
    it in its own tab, the node broadcasts it, and the rest happens with nobody there."""
    app, state, test_rpc, main_rpc = two_chains
    state.set_setting("xchain:enabled", True)
    clerk, book = state.xchain, state.xchain.book

    seller, s_secret, s_pub, s_test, s_main, _ = person(app, state, 1)
    buyer, b_secret, b_pub, b_test, b_main, _ = person(app, state, 2)

    issuer, pid = _token(state, test_rpc, "Xchain Logs 3")
    sender = TokenSender(test_rpc, state.messaging.params)
    sender.broadcast(sender.prepare(issuer, send_payload(pid, 50 * COIN), s_test))
    test_rpc.call("sendtoaddress", s_test, 2.0)
    main_rpc.call("sendtoaddress", b_main, 20.0)
    _mined(state, test_rpc, 1)
    main_rpc.call("generate", 1)
    _ledger_caught_up(state, main_rpc)

    sold = seller.post("/account/x/order", json={"kind": "token", "asset": str(pid), "side": "sell",
                                                 "amount": "10", "price": "0.5"})
    assert sold.status_code == 200, sold.text
    assert "Not trustless" in sold.json()["trust"]
    signed(seller, s_secret, s_pub, sold.json())
    bought = buyer.post("/account/x/order", json={"kind": "token", "asset": str(pid), "side": "buy",
                                                  "amount": "10", "price": "0.5"})
    assert bought.status_code == 200, bought.text
    signed(buyer, b_secret, b_pub, bought.json())

    _mined(state, test_rpc, 6)
    main_rpc.call("generate", 2)
    clerk.tick()                       # both deposits count; the book matches them
    _mined(state, test_rpc, 1)         # the exchange address's fee coins
    clerk.tick()
    clerk.tick()
    assert all(p["status"] == "sent" for p in book.payouts()), [(p["id"], p["error"]) for p in book.payouts()]
    _mined(state, test_rpc, 1)
    main_rpc.call("generate", 1)
    index = state.token_index(state.messaging)
    assert index.balance(b_test, pid) == 10 * COIN, "the buyer's account holds the tokens on testnet"
    paid = next(p for p in book.payouts() if p["id"].endswith(":pepe"))
    tx = main_rpc.call("getrawtransaction", paid["txid"], 1)
    assert tx.get("confirmations", 0) >= 1, "the payout is in a mainnet block"
    got = sum(int(round(o["value"] * COIN)) for o in tx["vout"]
              if s_main in ((o.get("scriptPubKey") or {}).get("addresses") or []))
    assert got == 5 * COIN - fee_of(5 * COIN), "the seller's account was paid on mainnet, less 0.5%"


def _ready(two_chains):
    app, state, test_rpc, main_rpc = two_chains
    state.set_setting("xchain:enabled", True)
    seller = person(app, state, 3)
    buyer = person(app, state, 4)
    test_rpc.call("sendtoaddress", seller[3], 5.0)
    main_rpc.call("sendtoaddress", buyer[4], 20.0)
    _mined(state, test_rpc, 1)
    main_rpc.call("generate", 1)
    _ledger_caught_up(state, main_rpc)
    return app, state, test_rpc, main_rpc, seller, buyer


def _settle(state, test_rpc, main_rpc, passes=3):
    _mined(state, test_rpc, 6)
    main_rpc.call("generate", 2)
    for _ in range(passes):
        state.xchain.tick()
        _mined(state, test_rpc, 1)
        main_rpc.call("generate", 1)


def test_an_nft_is_sold_for_mainnet_pepe(two_chains):
    from test_account_offer import _inscribed
    app, state, test_rpc, main_rpc, seller, buyer = _ready(two_chains)
    who, secret, pub, s_test, s_main, _ = seller
    piece = _inscribed(who, state, test_rpc, secret, pub, "a frog worth a coin")
    sold = who.post("/account/x/order", json={"kind": "nft", "asset": piece, "side": "sell", "price": "3"})
    assert sold.status_code == 200, sold.text
    signed(who, secret, pub, sold.json())
    b = buyer
    bought = b[0].post("/account/x/order", json={"kind": "nft", "asset": piece, "side": "buy", "price": "3"})
    assert bought.status_code == 200, bought.text
    signed(b[0], b[1], b[2], bought.json())
    _settle(state, test_rpc, main_rpc)
    book = state.xchain.book
    assert all(p["status"] == "sent" for p in book.payouts()), [(p["id"], p["error"]) for p in book.payouts()]
    _mined(state, test_rpc, 1)
    assert state.token_index(state.messaging).inscription(piece)["owner"] == b[3], "the buyer owns the piece"


def test_testnet_coins_are_sold_for_mainnet_pepe_and_a_cancel_refunds_on_chain(two_chains):
    app, state, test_rpc, main_rpc, seller, buyer = _ready(two_chains)
    who, secret, pub, s_test, s_main, _ = seller
    sold = who.post("/account/x/order", json={"kind": "coin", "asset": "coin", "side": "sell",
                                              "amount": "2", "price": "0.25"})
    assert sold.status_code == 200, sold.text
    signed(who, secret, pub, sold.json())
    b = buyer
    bought = b[0].post("/account/x/order", json={"kind": "coin", "asset": "coin", "side": "buy",
                                                 "amount": "1", "price": "0.25"})
    signed(b[0], b[1], b[2], bought.json())
    _settle(state, test_rpc, main_rpc)
    book = state.xchain.book
    sell = book.get(sold.json()["order"])
    assert sell["status"] == "open" and sell["left_amount"] == COIN, "half sold, half still standing"
    cancel = who.post("/account/x/cancel", json={"order": sell["id"]})
    assert cancel.status_code == 200, cancel.text
    _settle(state, test_rpc, main_rpc, passes=2)
    refund = next(p for p in book.payouts() if p["id"] == f"refund:{sell['id']}")
    assert refund["status"] == "sent" and refund["amount"] == COIN and refund["to_addr"] == s_test
    tx = test_rpc.call("getrawtransaction", refund["txid"], 1)
    assert sum(int(round(o["value"] * COIN)) for o in tx["vout"]
               if s_test in ((o.get("scriptPubKey") or {}).get("addresses") or [])) == COIN
