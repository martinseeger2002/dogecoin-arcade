"""Who pays a lock's fees (2026-10-21): the sender, on top, and the claimer gets exactly the amount.

"We need to have the sender of each lock script pay the fee so it doesn't come out of the amount of the coin received.
We need to add an operator fee output to each lock script." So a coin lock holds the amount, the claim's network fee
and the locker's operator fee; the claim pays the operator as an output of its own and the claimer the rest; a refund
gives everything back less its own fee. Each arcade works every number out for itself from the terms -- here in Python
and in the browser (wallet_js.js), held to the same satoshi -- and terms from an arcade before that day (no fee fields)
still trade, the old way.
"""

import dataclasses
import pathlib
import re
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import _seated, _settled, node                # noqa: E402,F401
from test_escrow import Owner, _piece                                  # noqa: E402
from test_funding import _sign                                         # noqa: E402
from test_htlc import Key, _lock                                       # noqa: E402
from test_web import app_state, client                               # noqa: F401,E402

from arcade import escrow as E, htlc as H, node_trader as NT, trade as T  # noqa: E402
from arcade import trade_messages as M, trade_orders as O               # noqa: E402
from arcade.config import NETWORKS                                     # noqa: E402
from arcade.script import b58check_encode, hash160                     # noqa: E402
from arcade.txbuild import p2pkh_script                                # noqa: E402

COIN = 100_000_000
OPERATOR = Key(30).address           # a Dogecoin address: where an arcade's operator is paid
OPERATOR_PEP = Key(111).address


def _coin(network, amount, bps=None, to=OPERATOR, a=None, b=None):
    a, b = a or Key(), b or Key()
    return T.Leg(network, "coin", "", amount, a.pub.hex(), b.pub.hex(), bps, to if bps else "")


# --- the numbers ----------------------------------------------------------------------------------

def test_the_operator_fee_is_a_rate_of_the_coin_rounded_down_never_under_the_dust_line():
    assert T.operator_fee(_coin("doge", 1_000 * COIN, 50)) == 5 * COIN, "0.5% of 1,000 DOGE"
    assert T.operator_fee(_coin("doge", 12_345_678_901, 25)) == 30_864_197, "rounded down to the koinu"
    assert T.operator_fee(_coin("doge", COIN, 50)) == 1_000_000, "0.005 DOGE is under the line: 0.01"
    assert T.operator_fee(_coin("ltc", 5_000_000, 10)) == 10_000, "Litecoin's own dust line"
    assert T.operator_fee(_coin("ltc", 100 * COIN, 10)) == 10_000_000
    assert T.operator_fee(_coin("doge", COIN, 0)) == 0, "Off: no output and no floor"
    assert T.operator_fee(_coin("doge", COIN, None)) == 0, "old terms: rate 0"
    assert T.operator_fee(dataclasses.replace(_coin("doge", COIN, 50), fee_to="")) == 0, "nobody to pay: none"
    assert T.operator_fee(_coin("doge", 10 * COIN, 99_999)) == 10 * COIN, "never more than all of it"


def test_a_token_lock_takes_its_percent_of_coin_on_the_same_chain_else_only_the_floor():
    k1, k2 = Key(), Key()
    doginal = T.Leg("doge", "doginal", "ab" * 32 + "i0", 1, k1.pub.hex(), k2.pub.hex(), 100, OPERATOR)
    doge = _coin("doge", 250 * COIN)
    pep = _coin("main", 250 * COIN)
    assert T.operator_fee(doginal, doge) == int(2.5 * COIN), "1% of the DOGE it is traded for"
    assert T.operator_fee(doginal, pep) == T.fee_floor("doge") == 1_000_000, "another chain: the floor"
    token = T.Leg("regtest", "token", "150", 40, k1.pub.hex(), k2.pub.hex(), 100, OPERATOR_PEP)
    nft = T.Leg("regtest", "nft", "c3" * 32, 1, k2.pub.hex(), k1.pub.hex(), 100, OPERATOR_PEP)
    assert T.operator_fee(token, nft) == 1_000_000, "not coin: the floor"
    assert T.doginal_reserve(doginal, doge) == H.DOGINAL_RESERVE + int(2.5 * COIN)


def test_what_a_lock_holds_and_what_the_other_side_demands():
    new = _coin("doge", 10 * COIN, 50)
    assert T.lock_value(new) == 10 * COIN + T.claim_fee("doge") + 5_000_000
    assert T.fund_value(new) == T.lock_value(new)
    off = _coin("doge", 10 * COIN, 0)
    assert T.lock_value(off) == 10 * COIN + 1_000_000, "Off: still the network fee on top"
    old = _coin("doge", 10 * COIN, None)
    assert T.lock_value(old) == 10 * COIN, "an old arcade's leg is checked the old way"
    assert T.fund_value(old) == 10 * COIN + 1_000_000, "but our own lock always pays the network fee"
    ltc = _coin("ltc", COIN, 0)
    assert T.lock_value(ltc) == COIN + T.claim_fee("ltc") == COIN + H.spend_fee(NETWORKS["ltc"])
    token = T.Leg("regtest", "token", "150", 40, Key().pub.hex(), Key().pub.hex(), 100, OPERATOR_PEP)
    assert T.lock_value(token) == T.LOCK_RESERVE + 1_000_000
    assert T.lock_value(dataclasses.replace(token, fee_bps=None, fee_to="")) == T.LOCK_RESERVE


def _trade(give, get, role=T.RESPONDER):
    _, h = H.make_secret()
    now = int(time.time())
    return T.Trade("tr-fees01", role, give, get, h.hex(), "", now + 7200, now + 3600)


def test_check_lock_holds_their_lock_to_the_fees_their_terms_name():
    a, b = Key(), Key()
    give = T.Leg("regtest", "coin", "", 3 * COIN, a.pub.hex(), b.pub.hex(), 0)
    get = T.Leg("doge", "coin", "", 2 * COIN, b.pub.hex(), a.pub.hex(), 100, OPERATOR)
    tr = _trade(give, get)
    script = H.p2sh_script(tr.their_redeem()).hex()
    need = 2 * COIN + 1_000_000 + 2_000_000
    assert T.check_lock(tr, T.LockSeen("ab" * 32, 0, need, script, 1), theirs=True) == ""
    assert T.check_lock(tr, T.LockSeen("ab" * 32, 0, need + 7, script, 1), theirs=True) == "", "more is fine"
    bad = T.check_lock(tr, T.LockSeen("ab" * 32, 0, need - 1, script, 1), theirs=True)
    assert "network fee" in bad and "operator fee" in bad
    assert T.check_lock(tr, T.LockSeen("ab" * 32, 0, 2 * COIN, script, 1), theirs=True), "the old amount is short"
    old = dataclasses.replace(tr, get=dataclasses.replace(get, fee_bps=None, fee_to=""))
    assert T.check_lock(old, T.LockSeen("ab" * 32, 0, 2 * COIN, script, 1), theirs=True) == "", \
        "an arcade not yet updated locks the amount alone, and that still passes"


def test_check_lock_holds_a_doginal_reserve_to_its_operator_fee():
    a, b = Key(), Key()
    give = T.Leg("doge", "coin", "", 100 * COIN, a.pub.hex(), b.pub.hex(), 0)
    get = T.Leg("doge", "doginal", "ab" * 32 + "i0", 1, b.pub.hex(), a.pub.hex(), 100, OPERATOR)
    tr = _trade(give, get)
    script = H.p2sh_script(tr.their_redeem()).hex()
    reserve = H.DOGINAL_RESERVE + COIN                          # 1% of 100 DOGE
    assert T.check_lock(tr, T.LockSeen("ab" * 32, 0, 100_000, script, 1, reserve=reserve), theirs=True) == ""
    assert "reserve" in T.check_lock(tr, T.LockSeen("ab" * 32, 0, 100_000, script, 1, reserve=reserve - 1),
                                     theirs=True)


# --- the claim and the refund -------------------------------------------------------------------

def test_a_coin_claim_pays_the_operator_and_the_claimer_exactly_the_amount():
    leg = _coin("doge", 10 * COIN, 50)
    coin = {"txid": "38" * 32, "vout": 0, "value": T.lock_value(leg)}
    me = Key(30).address
    _, outputs = H.pay_out(coin, me, fee=T.claim_fee("doge"), operator=T.payee(leg))
    assert outputs == [(5_000_000, p2pkh_script(OPERATOR)), (10 * COIN, p2pkh_script(me))]
    # A refund takes no operator output: the whole lock less the refund's own fee comes back, at least the amount.
    _, back = H.pay_out(coin, me, fee=T.claim_fee("doge"))
    assert back == [(10 * COIN + 5_000_000, p2pkh_script(me))] and back[0][0] >= leg.amount
    # Off, the old shape exactly: one output, the amount.
    off = _coin("doge", 10 * COIN, 0)
    _, plain = H.pay_out({**coin, "value": T.lock_value(off)}, me, fee=T.claim_fee("doge"), operator=T.payee(off))
    assert plain == [(10 * COIN, p2pkh_script(me))]
    with pytest.raises(H.HtlcError):
        H.pay_out(coin, me, operator=(999, OPERATOR))           # under the dust line


def test_a_token_claim_pays_the_operator_before_the_claimer_who_stays_the_ledgers_recipient():
    from arcade.config import NETWORKS
    lock = H.address_of(b"\x51", NETWORKS["regtest"])
    coin = {"txid": "39" * 32, "vout": 1, "value": T.LOCK_RESERVE + 1_000_000}
    me = Key().address
    _, outputs = H.move_out(coin, lock, b"data", me, operator=(1_000_000, OPERATOR_PEP))
    assert outputs[0][1][0] == 0x6A and outputs[1] == (1_000_000, p2pkh_script(OPERATOR_PEP))
    assert outputs[-1] == (H.DUST, p2pkh_script(me)), "the claimer last, the reference"
    _, back = H.move_out(coin, lock, b"data", me)
    assert back[-1] == (T.LOCK_RESERVE + 1_000_000 - H.SPEND_FEE, p2pkh_script(me)) and len(back) == 2, \
        "a refund brings the operator fee home, not back into the lock"
    plain = H.move_out({**coin, "value": T.LOCK_RESERVE}, lock, b"data", me)
    assert plain == E.move_out({**coin, "value": T.LOCK_RESERVE}, lock, b"data", me), "no fee: as it always was"


def test_a_doginal_claim_keeps_the_inscription_first_and_pays_the_operator_from_the_reserve():
    lock = {"txid": "3a" * 32, "vout": 0, "value": 100_000}
    reserve = {"txid": "3a" * 32, "vout": 1, "value": H.DOGINAL_RESERVE + COIN}
    me = Key(30).address
    _, claim = H.doginal_out(lock, reserve, me, operator=(COIN, OPERATOR))
    assert claim == [(100_000, p2pkh_script(me)), (COIN, p2pkh_script(OPERATOR))]
    _, refund = H.doginal_out(lock, reserve, me, operator=(COIN, OPERATOR), refund=True)
    assert refund == [(100_000, p2pkh_script(me)), (COIN, p2pkh_script(me))], "the fee comes home on a refund"
    _, plain = H.doginal_out(lock, {**reserve, "value": H.DOGINAL_RESERVE}, me)
    assert plain == [(100_000, p2pkh_script(me))], "no fee: as it always was"
    with pytest.raises(H.HtlcError):
        H.doginal_out(lock, {**reserve, "value": H.DOGINAL_RESERVE}, me, operator=(COIN, OPERATOR))


# --- the terms on the wire ------------------------------------------------------------------------

def test_legs_carry_their_fee_terms_and_old_legs_carry_none():
    leg = _coin("doge", 2 * COIN, 50)
    said = M.leg_json(leg)
    assert said["fee_bps"] == 50 and said["fee_to"] == OPERATOR
    assert M.leg_from(said) == leg
    old = dataclasses.replace(leg, fee_bps=None, fee_to="")
    assert set(M.leg_json(old)) == {"network", "kind", "asset", "amount", "giver_key", "taker_key"}, \
        "exactly what an arcade before 2026-10-21 sends"
    assert M.leg_from(M.leg_json(old)).fee_bps is None
    off = M.leg_from({**M.leg_json(old), "fee_bps": 0})
    assert off.fee_bps == 0 and off.fee_to == ""
    for bad in ({"fee_bps": 50, "fee_to": "nowhere"}, {"fee_bps": 50}, {"fee_bps": -1}, {"fee_bps": 10_001},
                {"fee_bps": 1.5, "fee_to": OPERATOR},
                {"fee_bps": 50, "fee_to": b58check_encode(22, hash160(Key().pub))}):    # a script hash
        with pytest.raises(M.TradeMessageError):
            M.leg_from({**M.leg_json(old), **bad})
    raw = M.build("propose", "tr-fees02", order="ord-0123456789abcdef", give=M.leg_json(leg),
                  get=M.leg_json(_coin("regtest", COIN, 25, OPERATOR_PEP)), secret_hash="ab" * 32,
                  t1=1_900_000_000, t2=1_899_990_000)
    assert M.parse(raw)["give"]["fee_bps"] == 50


def test_an_order_names_its_lock_fee_and_an_old_one_does_not():
    now = time.time()
    base = {"id": "ord-0123456789abcdef", "trade_key": "02" + "11" * 32, "msg_key": "ab" * 32,
            "node": "cd" * 32, "expires": int(now) + 3600,
            "get": {"network": "regtest", "kind": "coin", "amount": COIN}}
    fee = O.check_order({**base, "give": {"network": "doge", "kind": "coin", "amount": COIN, "fee_bps": 50,
                                          "fee_to": OPERATOR}}, now)
    assert fee["give"]["fee_bps"] == 50 and fee["give"]["fee_to"] == OPERATOR
    old = O.check_order({**base, "give": {"network": "doge", "kind": "coin", "amount": COIN}}, now)
    assert "fee_bps" not in old["give"]
    with pytest.raises(O.OrderError):
        O.check_order({**base, "give": {"network": "doge", "kind": "coin", "amount": COIN, "fee_bps": 50}}, now)


def test_a_lister_locks_only_under_the_fee_terms_it_listed():
    stood = {"network": "doge", "kind": "coin", "amount": COIN, "fee_bps": 50, "fee_to": OPERATOR}
    leg = _coin("doge", COIN, 50)
    assert NT.fee_terms_fit(leg, stood)
    assert NT.fee_terms_fit(dataclasses.replace(leg, fee_bps=None, fee_to=""), stood), "an old taker: rate 0"
    assert NT.fee_terms_fit(dataclasses.replace(leg, fee_bps=0, fee_to=""), stood)
    assert not NT.fee_terms_fit(dataclasses.replace(leg, fee_to=Key(30).address), stood), "somebody else's payee"
    assert not NT.fee_terms_fit(dataclasses.replace(leg, fee_bps=51), stood), "a higher rate"
    assert not NT.fee_terms_fit(leg, {"network": "doge", "kind": "coin", "amount": COIN}), "listed with none"


def test_an_arcade_names_its_own_rate_and_address_or_nothing():
    class Chain:
        network = "doge"

    class State:
        home = "/tmp/lock-fees-test"
        messaging = type("M", (), {"network": "regtest"})()

        def __init__(self, cut, to):
            self.cut, self.to = cut, to

        def setting(self, key, default=None):
            return self.cut if key == "trade_cut" else default

        def keyed(self, chain):
            return False

        def home_address(self, chain):
            return self.to

    NT._payees.clear()
    assert NT.lock_fee(State(0.5, OPERATOR), Chain()) == {"bps": 50, "to": OPERATOR}
    NT._payees.clear()
    assert NT.lock_fee(State(0, OPERATOR), Chain()) == {}, "Off"
    NT._payees.clear()
    assert NT.lock_fee(State(1, ""), Chain()) == {}, "no address on that chain: no fee there"
    NT._payees.clear()
    assert T.with_fee(_coin("doge", COIN), {"bps": 50, "to": OPERATOR}).fee_bps == 50
    assert T.with_fee(_coin("doge", COIN), {}).fee_bps == 0


# --- on a chain ------------------------------------------------------------------------------------

def test_an_account_claims_with_the_operator_fee_paid_and_its_amount_whole(node):
    """The tab's claim, built by its node (/account/trade/spend) and checked at /sign: the lock held the amount and
    both fees; the claimer gets exactly the amount, the other side's operator exactly its fee."""
    app, state, rpc = node
    who, secret_int, pubkey, address = _seated(app, state, rpc, 74, key=False)
    payer = Owner(state, rpc)
    secret, h = H.make_secret()
    redeem = H.redeem_script(h, pubkey, payer.pubkey, int(time.time()) + 7200)
    leg = T.Leg("regtest", "coin", "", 2 * COIN, payer.pubkey.hex(), pubkey.hex(), 50, OPERATOR_PEP)
    coin = _lock(rpc, payer, redeem, T.lock_value(leg))
    rpc.call("generate", 1)
    base = {"chain": "regtest", "redeem": redeem.hex(), "txid": coin["txid"], "vout": coin["vout"],
            "kind": "claim", "secret": secret.hex(),
            "leg": {"kind": "coin", "amount": leg.amount, "fee": T.operator_fee(leg), "fee_to": OPERATOR_PEP}}
    asked = who.post("/account/trade/spend", json=base)
    assert asked.status_code == 200, asked.text
    offer = asked.json()
    sig = _sign(secret_int, bytes.fromhex(offer["sighash"])).hex()
    # The page cannot be talked into a different operator fee at /sign.
    worse = who.post("/account/trade/spend/sign", json={**base, "raw": offer["raw"], "signature": sig,
                                                        "leg": {**base["leg"], "fee": 1_000_001}})
    assert worse.status_code == 400 and "operator" in worse.json()["detail"]
    nofee = who.post("/account/trade/spend/sign", json={**base, "raw": offer["raw"], "signature": sig,
                                                        "leg": {"kind": "coin", "amount": leg.amount}})
    assert nofee.status_code == 400, "without the terms, an output to anybody else is refused"
    done = who.post("/account/trade/spend/sign", json={**base, "raw": offer["raw"], "signature": sig})
    assert done.status_code == 200, done.text
    tx = rpc.call("getrawtransaction", done.json()["txid"], 1)
    paid = {o["scriptPubKey"]["addresses"][0]: round(o["value"] * COIN) for o in tx["vout"]}
    assert paid == {OPERATOR_PEP: 1_000_000, address: 2 * COIN}, "the operator its fee, the claimer the amount"


def test_an_account_refund_brings_back_the_fees_it_locked(node):
    app, state, rpc = node
    who, secret_int, pubkey, address = _seated(app, state, rpc, 75, key=False)
    payer, stranger = Owner(state, rpc), Key()
    _, h = H.make_secret()
    redeem = H.redeem_script(h, stranger.pub, pubkey, int(time.time()) - 7200)
    leg = T.Leg("regtest", "coin", "", COIN, pubkey.hex(), stranger.pub.hex(), 100, OPERATOR_PEP)
    coin = _lock(rpc, payer, redeem, T.lock_value(leg))
    rpc.call("generate", 1)
    said = {"chain": "regtest", "redeem": redeem.hex(), "txid": coin["txid"], "vout": coin["vout"], "kind": "refund",
            "leg": {"kind": "coin", "amount": leg.amount, "fee": T.operator_fee(leg), "fee_to": OPERATOR_PEP}}
    asked = who.post("/account/trade/spend", json=said)
    assert asked.status_code == 200, asked.text
    offer = asked.json()
    sig = _sign(secret_int, bytes.fromhex(offer["sighash"])).hex()
    done = who.post("/account/trade/spend/sign", json={**said, "raw": offer["raw"], "signature": sig})
    assert done.status_code == 200, done.text
    tx = rpc.call("getrawtransaction", done.json()["txid"], 1)
    assert [(o["scriptPubKey"]["addresses"], round(o["value"] * COIN)) for o in tx["vout"]] == \
        [([address], T.lock_value(leg) - T.claim_fee("regtest"))], "everything back but the refund's own fee"


def test_an_nft_claimed_with_an_operator_fee_still_goes_to_the_claimer(node):
    """The reference rule on a real ledger: the operator's output before the claimer's leaves the claimer the NFT's
    recipient."""
    app, state, rpc = node
    seller, buyer = Owner(state, rpc), Key()
    _settled(state, rpc)
    piece = "c5" * 32
    _piece(state, piece, seller.address)
    secret, h = H.make_secret()
    redeem = H.redeem_script(h, buyer.pub, seller.pubkey, int(time.time()) + 7200)
    lock = H.address_of(redeem, state.messaging.params)
    coin = seller.send(rpc, E.nft_payload(piece), H.p2sh_script(redeem), T.LOCK_RESERVE + 1_000_000)
    _settled(state, rpc)
    index = state.token_index(state.messaging)
    assert index.inscription(piece)["owner"] == lock
    inputs, outputs = H.move_out(coin, lock, E.nft_payload(piece), buyer.address, operator=(1_000_000, OPERATOR_PEP))
    txid = rpc.call("sendrawtransaction", H.claimed(inputs, outputs, redeem, secret, buyer.sign))
    _settled(state, rpc)
    assert index.inscription(piece)["owner"] == buyer.address, "claimed to the buyer, not the operator"
    tx = rpc.call("getrawtransaction", txid, 1)
    assert any(o["scriptPubKey"].get("addresses") == [OPERATOR_PEP] and round(o["value"] * COIN) == 1_000_000
               for o in tx["vout"])


# --- the browser does the same arithmetic ----------------------------------------------------------

def test_the_browser_keeps_the_same_fee_tables():
    js = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates/wallet_js.js").read_text()

    def table(name):
        body = re.search(name + r": \{([^}]*)\}", js).group(1)
        return {k: int(v.replace("_", "")) for k, v in re.findall(r"(\w+): ([0-9_]+)", body)}
    assert table("CLAIM_FEE") == {n: T.claim_fee(n) for n in NETWORKS}
    assert table("FEE_FLOOR") == {n: T.fee_floor(n) for n in NETWORKS}
    assert int(re.search(r"DOGINAL_RESERVE: ([0-9_]+)", js).group(1).replace("_", "")) == H.DOGINAL_RESERVE
    versions = re.search(r"PAYEE_VERSIONS: \[([0-9, ]+)\]", js).group(1)
    assert {int(v) for v in versions.split(",")} == set(T.PAYEE_VERSIONS)
    assert int(re.search(r"FEE_BPS_CEILING: ([0-9_]+)", js).group(1).replace("_", "")) == T.FEE_BPS_CEILING


def _legs():
    """Legs of every kind and chain, with and without fee terms, each beside the leg it trades against."""
    a, b = Key(), Key()
    out = []
    for net, to in (("doge", OPERATOR), ("main", Key(56).address), ("ltc", Key(48).address), ("regtest", OPERATOR_PEP)):
        for amount in (1, 999_999, COIN, 12_345_678_901, 98_765_432_109_876, 10 ** 15):
            for bps in (None, 0, 1, 10, 25, 50, 100, 200, 333, 10_000):
                leg = T.Leg(net, "coin", "", amount, a.pub.hex(), b.pub.hex(), bps, to if bps else "")
                out.append((leg, T.Leg("doge" if net != "doge" else "main", "coin", "", COIN, b.pub.hex(),
                                       a.pub.hex())))
        for kind, asset in (("token", "150"), ("nft", "c3" * 32), ("doginal", "ab" * 32 + "i0")):
            for bps in (None, 0, 50, 100):
                leg = T.Leg(net, kind, asset, 40 if kind == "token" else 1, a.pub.hex(), b.pub.hex(), bps,
                            to if bps else "")
                for other_net in (net, "doge" if net != "doge" else "main"):
                    out.append((leg, T.Leg(other_net, "coin", "", 123_456_789_012, b.pub.hex(), a.pub.hex())))
    return out


def test_the_browser_works_out_every_fee_as_the_python_does(tmp_path):
    pytest.importorskip("playwright.sync_api")
    import uvicorn
    from playwright.sync_api import sync_playwright
    from test_htlc_browser import _free_port
    from arcade.web.app import create_app
    from arcade.web.state import AppState, ChainContext
    state = AppState(home=tmp_path, messaging=ChainContext(network="regtest", role="messaging",
                     label="Testnet", datadir=pathlib.Path("/nonexistent")),
                     ledger=ChainContext(network="main", role="ledger", label="Mainnet",
                                         datadir=pathlib.Path("/nonexistent")))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(state), host="127.0.0.1", port=port,
                                           log_level="error", timeout_graceful_shutdown=1.0))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    cases = []
    for leg, other in _legs():
        cases.append([dataclasses.asdict(leg), dataclasses.asdict(other),
                      [T.operator_fee(leg, other), T.lock_value(leg, other), T.fund_value(leg, other),
                       T.doginal_reserve(leg, other)]])
    # check_lock: their lock, exactly enough, one short, the old amount alone.
    a, b = Key(), Key()
    now = int(time.time())
    checks = []
    for get in (T.Leg("doge", "coin", "", 2 * COIN, b.pub.hex(), a.pub.hex(), 100, OPERATOR),
                T.Leg("doge", "coin", "", 2 * COIN, b.pub.hex(), a.pub.hex(), 0),
                T.Leg("doge", "coin", "", 2 * COIN, b.pub.hex(), a.pub.hex()),
                T.Leg("regtest", "token", "150", 40, b.pub.hex(), a.pub.hex(), 50, OPERATOR_PEP)):
        give = T.Leg("main" if get.network == "doge" else "doge", "coin", "", 3 * COIN, a.pub.hex(), b.pub.hex(), 0)
        _, h = H.make_secret()
        tr = T.Trade("t", T.INITIATOR, give, get, h.hex(), "", now + 7200, now + 3600)
        script = H.p2sh_script(tr.their_redeem()).hex()
        for value in (T.lock_value(get, give), T.lock_value(get, give) - 1, get.amount, T.LOCK_RESERVE):
            seen = T.LockSeen("ab" * 32, 0, value, script, 1)
            checks.append([{"role": tr.role, "give": dataclasses.asdict(give), "get": dataclasses.asdict(get),
                            "secretHash": tr.secret_hash, "t1": tr.initiator_locktime, "t2": tr.responder_locktime,
                            "myRedeem": tr.my_redeem().hex(), "theirRedeem": tr.their_redeem().hex()},
                           dataclasses.asdict(seen), T.check_lock(tr, seen, theirs=True)])
    payees = [[OPERATOR, True], [OPERATOR_PEP, True], [b58check_encode(22, hash160(a.pub)), False],
              [OPERATOR[:-1] + ("2" if OPERATOR[-1] != "2" else "3"), False], ["", False]]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            tab = browser.new_page()
            tab.goto(f"http://127.0.0.1:{port}/join")
            got = tab.evaluate("""async ([cases, checks, payees]) => {
                const w = await import('/wallet.js'), c = await import('/coins.js');
                const fees = cases.map(([leg, other]) => [w.operatorFee(leg, other), w.lockValue(leg, other),
                                                          w.fundValue(leg, other), w.doginalReserve(leg, other)]);
                const said = [];
                for (const [t, seen] of checks) {
                  t.myRedeem = c.unhex(t.myRedeem); t.theirRedeem = c.unhex(t.theirRedeem);
                  said.push(await w.checkLock(t, seen, true));
                }
                const ok = [];
                for (const [a] of payees) ok.push(await w.payeeOk(a));
                return [fees, said, ok]; }""", [[c[:2] for c in cases], [c[:2] for c in checks], payees])
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    fees, said, ok = got
    assert len(fees) == len(cases) > 200
    for (leg, other, want), have in zip(cases, fees):
        assert have == want, (leg, other, have, want)
    for (_, _, want), have in zip(checks, said):
        assert have == want, (have, want)
    assert ok == [p[1] for p in payees]
    assert all(T.payee_ok(a) == want for a, want in payees)
