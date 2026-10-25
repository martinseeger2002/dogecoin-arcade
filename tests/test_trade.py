"""The trade state machine (arcade/trade.py): decided from the chains, never from a message.

First the decisions on their own, then two traders driving a real PEP<->DOGE swap on two
regtest chains with nothing but `next_step` and what each chain shows -- once to the
end, once with the responder vanishing so the initiator refunds.
"""

import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_htlc import Key, Payer, _lock, doge                         # noqa: E402,F401
from test_funding import _sign                                         # noqa: E402

from arcade import htlc as H, trade as T                               # noqa: E402

COIN = 100_000_000


def _trade(role, give_net="regtest", get_net="regtest", t1=None, t2=None):
    a, b = Key(), Key()
    secret, h = H.make_secret()
    now = int(time.time())
    give = T.Leg(give_net, "coin", "", 3 * COIN, a.pub.hex(), b.pub.hex())
    get = T.Leg(get_net, "coin", "", 2 * COIN, b.pub.hex(), a.pub.hex())
    tr = T.Trade(id="t1", role=role, give=give, get=get, secret_hash=h.hex(),
                 secret=secret.hex() if role == T.INITIATOR else "",
                 initiator_locktime=t1 or now + 6 * 3600, responder_locktime=t2 or now + 3 * 3600)
    return tr, a, b, secret


def _seen(tr, theirs, conf=5, value=None, spent="", by_me=False):
    redeem = tr.their_redeem() if theirs else tr.my_redeem()
    leg = tr.get if theirs else tr.give
    return T.LockSeen("ab" * 32, 0, leg.amount if value is None else value,
                      H.p2sh_script(redeem).hex(), conf, True, spent, by_me)


# --- decisions ------------------------------------------------------------------------------

def test_the_initiator_locks_first_then_claims_only_a_confirmed_agreed_lock():
    tr, *_ = _trade(T.INITIATOR)
    now = int(time.time())
    assert T.next_step(tr, T.Facts(), now)[0] == T.LOCK
    assert T.next_step(tr, T.Facts(mine=_seen(tr, False)), now)[0] == T.WAIT
    unconfirmed = T.Facts(mine=_seen(tr, False), theirs=_seen(tr, True, conf=0))
    # Below the big line the lock in the mempool is enough (2026-10-17); a swap that
    # waits for a confirmation (at or above the line) still does.
    assert T.next_step(tr, unconfirmed, now)[0] == T.CLAIM
    tr.need = 1
    assert T.next_step(tr, unconfirmed, now)[0] == T.WAIT
    tr.need = 0
    good = T.Facts(mine=_seen(tr, False), theirs=_seen(tr, True))
    assert T.next_step(tr, good, now)[0] == T.CLAIM
    short = T.Facts(mine=_seen(tr, False), theirs=_seen(tr, True, value=COIN))
    step, why = T.next_step(tr, short, now)
    assert step == T.WAIT and "less than" in why, "a short lock is never claimed against"


def test_the_initiator_never_claims_too_close_to_the_responders_time():
    now = int(time.time())
    tr, *_ = _trade(T.INITIATOR, t1=now + 7200, t2=now + T.CLAIM_MARGIN - 60)
    facts = T.Facts(mine=_seen(tr, False), theirs=_seen(tr, True))
    assert T.next_step(tr, facts, now)[0] == T.WAIT
    assert T.next_step(tr, facts, now + 7201)[0] == T.REFUND


def test_the_responder_locks_only_after_the_initiator_and_claims_once_the_secret_shows():
    tr, a, b, secret = _trade(T.RESPONDER)
    now = int(time.time())
    assert T.next_step(tr, T.Facts(), now)[0] == T.WAIT, "never first"
    assert T.next_step(tr, T.Facts(theirs=_seen(tr, True)), now)[0] == T.LOCK
    claimed_mine = H.claim_script_sig(b"\x30" * 70, secret, tr.my_redeem()).hex()
    facts = T.Facts(mine=_seen(tr, False, spent=claimed_mine), theirs=_seen(tr, True))
    assert T.next_step(tr, facts, now)[0] == T.CLAIM and tr.secret == secret.hex()
    claimed_theirs = H.claim_script_sig(b"\x30" * 70, secret, tr.their_redeem()).hex()
    done = T.Facts(mine=_seen(tr, False, spent=claimed_mine), theirs=_seen(tr, True, spent=claimed_theirs))
    assert T.next_step(tr, done, now)[0] == T.DONE


def test_done_is_told_by_the_branch_a_spend_took_never_by_who_it_pays():
    """2026-10-25 hardening: a spend that pays this trader a little is not therefore this trader's own."""
    tr, a, b, secret = _trade(T.RESPONDER)
    now = int(time.time())
    # The initiator's claim of MY lock, paying me a crumb on the way: still the secret to read, then my claim.
    claimed_mine = H.claim_script_sig(b"\x30" * 70, secret, tr.my_redeem()).hex()
    facts = T.Facts(mine=_seen(tr, False, spent=claimed_mine, by_me=True), theirs=_seen(tr, True))
    assert T.next_step(tr, facts, now) == (T.CLAIM, "the secret is on the chain: claim their lock")
    assert tr.secret == secret.hex()
    # Mine refunded (the ELSE branch, my key): done, whoever the refund paid.
    refunded_mine = H.refund_script_sig(b"\x30" * 70, tr.my_redeem()).hex()
    assert T.next_step(tr, T.Facts(mine=_seen(tr, False, spent=refunded_mine), theirs=_seen(tr, True)),
                       now + 4 * 3600)[0] == T.DONE
    # The initiator: their lock refunded back to them, paying me a crumb, is not my claim of it.
    ini, *_ = _trade(T.INITIATOR)
    their_refund = H.refund_script_sig(b"\x30" * 70, ini.their_redeem()).hex()
    facts = T.Facts(mine=_seen(ini, False), theirs=_seen(ini, True, spent=their_refund, by_me=True))
    assert T.next_step(ini, facts, now)[0] == T.WAIT
    assert T.next_step(ini, facts, now + 7 * 3600)[0] == T.REFUND
    # A claim of some OTHER lock passed off as theirs is not my claim of theirs either.
    other = H.redeem_script(H.make_secret()[1], bytes.fromhex(ini.get.taker_key), bytes.fromhex(ini.get.giver_key),
                            ini.responder_locktime)
    elsewhere = H.claim_script_sig(b"\x30" * 70, b"\x11" * 32, other).hex()
    facts = T.Facts(mine=_seen(ini, False), theirs=_seen(ini, True, spent=elsewhere, by_me=True))
    assert T.next_step(ini, facts, now + 7 * 3600)[0] == T.REFUND
    # And the initiator's own claim of theirs: done.
    mine_claim = H.claim_script_sig(b"\x30" * 70, bytes.fromhex(ini.secret), ini.their_redeem()).hex()
    assert T.next_step(ini, T.Facts(mine=_seen(ini, False), theirs=_seen(ini, True, spent=mine_claim)),
                       now)[0] == T.DONE


def test_the_branch_of_a_spend_is_read_as_the_script_runs_it():
    _, h = H.make_secret()
    redeem = H.redeem_script(h, Key().pub, Key().pub, int(time.time()) + 3600)
    sig = b"\x30" * 71
    assert H.spend_branch(H.claim_script_sig(sig, b"\x22" * 32, redeem), redeem) == H.CLAIM
    assert H.spend_branch(H.refund_script_sig(sig, redeem), redeem) == H.REFUND
    from arcade.txbuild import push
    # Any true selector takes the IF branch, any false one (an empty push, a zero, negative zero) the ELSE.
    for selector, branch in ((b"\x52", H.CLAIM), (b"\x4f", H.CLAIM), (push(b"\x00\x01"), H.CLAIM),
                             (push(b"\x00"), H.REFUND), (push(b"\x80"), H.REFUND), (push(b"\x00\x80"), H.REFUND)):
        assert H.spend_branch(push(sig) + selector + push(redeem), redeem) == branch, selector
    assert H.spend_branch(H.refund_script_sig(sig, redeem), redeem[:-1] + b"\x00") == ""
    assert H.spend_branch(b"\x00", redeem) == "" and H.spend_branch(b"\x4c", redeem) == ""
    assert H.spend_branch(b"\x76" + push(redeem), redeem) == "", "not push-only: no branch"


def test_a_wrong_lock_is_refused_and_a_late_one_too():
    tr, *_ = _trade(T.RESPONDER)
    now = int(time.time())
    wrong = T.LockSeen("ab" * 32, 0, 3 * COIN, "a914" + "00" * 20 + "87", 5)
    assert T.next_step(tr, T.Facts(theirs=wrong), now)[0] == T.ABORT
    late, *_ = _trade(T.RESPONDER, t1=now + 3600, t2=now + 1800)
    assert T.next_step(late, T.Facts(theirs=_seen(late, True)), now)[0] == T.ABORT


def test_a_book_remembers_a_trade_and_its_secret(tmp_path):
    book = T.Book(tmp_path / "trades.sqlite")
    tr, *_ = _trade(T.INITIATOR)
    book.save(tr)
    again = book.get("t1")
    assert again == tr and [t.id for t in book.open_trades()] == ["t1"]


# --- two traders, two chains ---------------------------------------------------------------

def _observe(rpc, lock, redeem, my_addresses):
    """A lock as a chain shows it, found from its own txid (a message only says where)."""
    if lock is None:
        return None
    tx = rpc.call("getrawtransaction", lock["txid"], 1)
    out = tx["vout"][lock["vout"]]
    seen = T.LockSeen(lock["txid"], lock["vout"], round(out["value"] * COIN),
                      out["scriptPubKey"]["hex"], int(tx.get("confirmations", 0)))
    if rpc.call("gettxout", lock["txid"], lock["vout"], True) is None:
        spender = _spender(rpc, lock)
        if spender is not None:
            vin = next(v for v in spender["vin"] if v["txid"] == lock["txid"] and v["vout"] == lock["vout"])
            seen.spent_by_script_sig = vin["scriptSig"]["hex"]
            seen.spent_by_me = any(a in my_addresses for o in spender["vout"]
                                   for a in o["scriptPubKey"].get("addresses", []))
    return seen


def _spender(rpc, lock):
    for txid in rpc.call("getrawmempool"):
        tx = rpc.call("getrawtransaction", txid, 1)
        if any(v.get("txid") == lock["txid"] and v.get("vout") == lock["vout"] for v in tx["vin"]):
            return tx
    tip = rpc.call("getblockcount")
    for height in range(tip, max(0, tip - 30), -1):
        block = rpc.call("getblock", rpc.call("getblockhash", height))
        for txid in block["tx"]:
            tx = rpc.call("getrawtransaction", txid, 1)
            if any(v.get("txid") == lock["txid"] and v.get("vout") == lock["vout"] for v in tx["vin"]):
                return tx
    return None


class Trader:
    """One side: its trade, its key and coins, and the two chains it watches."""

    def __init__(self, trade, key, payer, give_rpc, get_rpc, get_address):
        self.trade, self.key, self.payer = trade, key, payer
        self.give_rpc, self.get_rpc, self.get_address = give_rpc, get_rpc, get_address
        self.my_lock = None

    def step(self, their_lock, now=None):
        facts = T.Facts(
            mine=_observe(self.give_rpc, self.my_lock, self.trade.my_redeem(), {self.payer.address}),
            theirs=_observe(self.get_rpc, their_lock, self.trade.their_redeem(), {self.get_address}))
        step, why = T.next_step(self.trade, facts, int(now or time.time()))
        if step == T.LOCK:
            self.my_lock = _lock(self.give_rpc, self.payer, self.trade.my_redeem(), self.trade.give.amount)
        elif step == T.CLAIM:
            inputs, outputs = H.pay_out(their_lock, self.get_address)
            self.get_rpc.call("sendrawtransaction", H.claimed(
                inputs, outputs, self.trade.their_redeem(), bytes.fromhex(self.trade.secret), self.key.sign))
        elif step == T.REFUND:
            inputs, outputs = H.pay_out(self.my_lock, self.payer.address)
            self.give_rpc.call("sendrawtransaction", H.refunded(
                inputs, outputs, self.trade.my_redeem(), lambda d: _sign(self.payer.secret, d)))
        return step, why


def _pair(pep_rpc, doge_rpc, t1, t2):
    """Alice (initiator) gives 3 PEP on Pepecoin regtest; Bob (responder) 2 DOGE on Dogecoin."""
    alice_pep, bob_doge = Payer(pep_rpc), Payer(doge_rpc)
    alice_doge, bob_pep = Key(), Key()
    secret, h = H.make_secret()
    a_leg = T.Leg("regtest", "coin", "", 3 * COIN, alice_pep.pubkey.hex(), bob_pep.pub.hex())
    b_leg = T.Leg("regtest", "coin", "", 2 * COIN, bob_doge.pubkey.hex(), alice_doge.pub.hex())
    alice = Trader(T.Trade("s1", T.INITIATOR, a_leg, b_leg, h.hex(), secret.hex(), t1, t2),
                   alice_doge, alice_pep, pep_rpc, doge_rpc, alice_doge.address)
    bob = Trader(T.Trade("s1", T.RESPONDER, b_leg, a_leg, h.hex(), "", t1, t2),
                 bob_pep, bob_doge, doge_rpc, pep_rpc, bob_pep.address)
    return alice, bob


def _mine(*rpcs):
    for rpc in rpcs:
        rpc.call("generate", 1)


def test_two_traders_finish_a_pep_doge_swap_from_the_chains_alone(regtest, doge):
    pep_rpc, doge_rpc = regtest.rpc, doge.rpc
    if float(pep_rpc.call("getbalance")) < 10:
        pep_rpc.call("generate", 101)
    now = int(time.time())
    alice, bob = _pair(pep_rpc, doge_rpc, now + 6 * 3600, now + 3 * 3600)
    seen = []
    for _ in range(12):
        a = alice.step(bob.my_lock)
        b = bob.step(alice.my_lock)
        seen.append((a[0], b[0]))
        _mine(pep_rpc, doge_rpc)
        if a[0] == T.DONE and b[0] == T.DONE:
            break
    assert seen[-1] == (T.DONE, T.DONE), seen
    # The initiator locks first; below the big line the responder locks the moment that lock is in
    # the mempool, in the same pass (2026-10-17).
    assert seen[0] == (T.LOCK, T.LOCK), seen
    assert (T.CLAIM, T.WAIT) in seen or any(s[0] == T.CLAIM for s in seen)
    assert any(s[1] == T.CLAIM for s in seen), "the responder claimed with the secret it read"


def test_when_the_responder_vanishes_the_initiator_refunds(regtest, doge):
    pep_rpc, doge_rpc = regtest.rpc, doge.rpc
    if float(pep_rpc.call("getbalance")) < 10:
        pep_rpc.call("generate", 101)
    now = int(time.time())
    # Lock times already past on the chain's clock, so the refund is spendable now.
    alice, bob = _pair(pep_rpc, doge_rpc, now - 7200, now - 10800)
    assert alice.step(None, now=now - 9000)[0] == T.LOCK
    _mine(pep_rpc)
    # Bob never locks. Once Alice's time has passed she takes hers back.
    assert alice.step(None)[0] == T.REFUND
    _mine(pep_rpc)
    assert alice.step(None)[0] == T.DONE


def test_a_small_swap_finishes_in_the_mempool_without_a_block(regtest, doge):
    """2026-10-17: "Allow 0-conf for less than 1000 doge and 1000000 pep" -- both locks and
    both claims, every one of them unconfirmed, and the swap is done before any block."""
    pep_rpc, doge_rpc = regtest.rpc, doge.rpc
    if float(pep_rpc.call("getbalance")) < 10:
        pep_rpc.call("generate", 101)
    _mine(pep_rpc, doge_rpc)                      # the traders' own coins confirmed beforehand
    now = int(time.time())
    alice, bob = _pair(pep_rpc, doge_rpc, now + 6 * 3600, now + 3 * 3600)
    _mine(pep_rpc, doge_rpc)
    heights = (pep_rpc.call("getblockcount"), doge_rpc.call("getblockcount"))
    seen = []
    for _ in range(8):
        a = alice.step(bob.my_lock)
        b = bob.step(alice.my_lock)
        seen.append((a[0], b[0]))
        if a[0] == T.DONE and b[0] == T.DONE:
            break
    assert seen[-1] == (T.DONE, T.DONE), seen
    assert (pep_rpc.call("getblockcount"), doge_rpc.call("getblockcount")) == heights, "no block was needed"


def test_one_confirmation_and_three_for_a_big_swap():
    """2026-10-13: one confirmation, more for big trades (the line is the operator's)."""
    k = "02" + "11" * 32
    small = T.Leg("doge", "coin", "", 10 * COIN, k, k)
    big = T.Leg("doge", "coin", "", T.BIG["doge"], k, k)
    pep = T.Leg("main", "coin", "", 5 * COIN, k, k)
    test = T.Leg("regtest", "coin", "", 10 ** 15, k, k)
    assert T.confirmations_for(small, pep) == 0, "below the line: the lock in the mempool is enough (2026-10-17)"
    assert T.confirmations_for(pep, big) == 1, "either leg at its line: one confirmation (2026-10-17)"
    assert T.confirmations_for(test, test) == 0, "testnet coins never count as big"
    assert T.confirmations_for(small, pep, {"doge": 5 * COIN}) == 1, "the operator's own line"
    assert T.LOCK_SECONDS["mainnet"] == 2 * 3600, "the operator: 2 h locks"
