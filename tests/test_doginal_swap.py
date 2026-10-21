"""A Doginal as one leg of a lock-script swap ([Claude 2026-10-15]; the operator: "use the atomic swap feature we have to trade
doginals"). Doginals move by koinu order, first in, first out, so the lock, the claim and the refund must each keep the
inscribed coin first and whole: the inscription at offset 0 of output 0 at every step, no other Doginal ever paying a fee,
and a coin that does not hold exactly the named Doginal refused. The Doginals source is faked, as in test_doginals.py."""

import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_doginals import _fake                                        # noqa: E402

from arcade import doginals, funding, htlc as H, trade as T            # noqa: E402
from arcade import trade_messages as M, trade_orders as O              # noqa: E402
from arcade.config import NETWORKS                                     # noqa: E402
from arcade.script import b58check_encode                              # noqa: E402
from arcade.txbuild import p2pkh_script                                # noqa: E402

T1, T2, T3, T4 = "11" * 32, "22" * 32, "33" * 32, "44" * 32
INS = T1 + "i0"
ME = b58check_encode(30, bytes(20))
THEM = b58check_encode(30, bytes(range(20)))
CLAIMER, REFUNDER = bytes([2]) + bytes(range(1, 33)), bytes([3]) + bytes(range(2, 34))


def where(inputs: list[int], outputs: list[int], n: int, offset: int = 0):
    """Which output, at which offset, koinu `offset` of input `n` lands in, by ord's first-in-first-out rule; None when it
    goes to the fee."""
    at = sum(inputs[:n]) + offset
    for k, value in enumerate(outputs):
        if at < value:
            return k, at
        at -= value
    return None


def _redeem(locktime=None):
    _, h = H.make_secret()
    return H.redeem_script(h, CLAIMER, REFUNDER, locktime or int(time.time()) + 7200)


def _guard(tmp_path, extra=None):
    answers = {
        T1: {"txid": T1, "inscriptions": [{"vout": 0, "inscription_id": INS, "sat_offset": 0}]},
        T2: {"txid": T2, "inscriptions": [{"vout": 0, "inscription_id": T2 + "i0", "sat_offset": 0}]},
        T3: {"txid": T3, "inscriptions": []},
        **(extra or {})}
    return doginals.Doginals(tmp_path / "d.sqlite", fetch=_fake(answers, []))


@pytest.fixture
def wallet(tmp_path, monkeypatch):
    coins = [{"txid": T1, "vout": 0, "value": 100_000},            # the Doginal being traded
             {"txid": T2, "vout": 0, "value": 100_000},            # ANOTHER Doginal: must never pay anything
             {"txid": T3, "vout": 0, "value": 300_000_000}]        # a clean coin
    monkeypatch.setattr(funding.utxos, "unspent", lambda db, address: [dict(c) for c in coins])
    monkeypatch.setattr(funding, "GUARD", _guard(tmp_path))
    return coins


# --- the lock ---------------------------------------------------------------------------------

def test_the_lock_puts_the_doginal_whole_at_output_0_and_its_reserve_beside_it(wallet):
    redeem = _redeem()
    u = funding.build_doginal_lock(None, NETWORKS["doge"], ME, wallet[0], INS, H.p2sh_script(redeem),
                                   H.DOGINAL_RESERVE, rate=funding.fees.MIN_FEE_PER_KB)
    assert [(c["txid"], c["vout"]) for c in u.inputs] == [(T1, 0), (T3, 0)], "the Doginal first, then a clean coin"
    lock = H.p2sh_script(redeem)
    assert u.outputs[:2] == [(100_000, lock), (H.DOGINAL_RESERVE, lock)]
    assert u.outputs[-1][1] == p2pkh_script(ME), "change last, back to the giver"
    assert where([c["value"] for c in u.inputs], [v for v, _ in u.outputs], 0) == (0, 0), \
        "the inscription is the lock output's first koinu"
    assert sum(c["value"] for c in u.inputs) - sum(v for v, _ in u.outputs) == u.fee


@pytest.mark.parametrize("held, why", [
    ({"vout": 0, "inscription_id": T4 + "i0", "sat_offset": 0}, "exactly"),          # another inscription
    ({"vout": 0, "inscription_id": T4 + "i0", "sat_offset": 50_000, "two": 1}, "exactly"),  # two on one coin
    ({"vout": 0, "inscription_id": INS, "sat_offset": 500}, "start of its coin"),   # not at offset 0
])
def test_a_coin_that_does_not_hold_exactly_that_doginal_is_never_locked(tmp_path, monkeypatch, held, why):
    found = [{"vout": 0, "inscription_id": INS, "sat_offset": 0}, held] if held.pop("two", 0) else [held]
    coin = {"txid": T4, "vout": 0, "value": 100_000}
    monkeypatch.setattr(funding.utxos, "unspent", lambda db, a: [dict(coin), {"txid": T3, "vout": 0, "value": 10 ** 9}])
    monkeypatch.setattr(funding, "GUARD", _guard(tmp_path, {T4: {"txid": T4, "inscriptions": found}}))
    with pytest.raises(funding.FundingError, match=why):
        funding.build_doginal_lock(None, NETWORKS["doge"], ME, coin, INS, H.p2sh_script(_redeem()),
                                   H.DOGINAL_RESERVE, rate=funding.fees.MIN_FEE_PER_KB)


def test_the_lock_is_refused_rather_than_paid_for_with_another_doginal(wallet):
    del wallet[2]                                                  # only the other Doginal could pay
    with pytest.raises(funding.FundingError):
        funding.build_doginal_lock(None, NETWORKS["doge"], ME, wallet[0], INS, H.p2sh_script(_redeem()),
                                   H.DOGINAL_RESERVE, rate=funding.fees.MIN_FEE_PER_KB)


# --- the claim and the refund -----------------------------------------------------------------

LOCK_TXID = "55" * 32


def _spent(raw: str) -> tuple[list[tuple[str, int]], list[int], int]:
    """(inputs, output values, locktime) read back off a signed spend."""
    b = bytes.fromhex(raw)
    at = 4
    n, at = b[at], at + 1
    ins = []
    for _ in range(n):
        txid, vout = b[at:at + 32][::-1].hex(), int.from_bytes(b[at + 32:at + 36], "little")
        at += 36
        size = b[at]
        if size == 0x4C:
            size, at = b[at + 1], at + 1
        elif size == 0x4D:
            size, at = int.from_bytes(b[at + 1:at + 3], "little"), at + 2
        at += 1 + size + 4
        ins.append((txid, vout))
    outs = []
    n, at = b[at], at + 1
    for _ in range(n):
        outs.append(int.from_bytes(b[at:at + 8], "little"))
        at += 8 + 1 + b[at + 8]
    return ins, outs, int.from_bytes(b[-4:], "little")


@pytest.mark.parametrize("kind", ["claim", "refund"])
def test_a_claim_or_refund_moves_the_doginal_first_and_whole_and_the_reserve_pays(kind):
    from test_htlc import Key
    claimer, refunder = Key(), Key()
    secret, h = H.make_secret()
    locktime = int(time.time()) + 7200
    redeem = H.redeem_script(h, claimer.pub, refunder.pub, locktime)
    lock = {"txid": LOCK_TXID, "vout": 0, "value": 100_000}
    reserve = {"txid": LOCK_TXID, "vout": 1, "value": H.DOGINAL_RESERVE}
    inputs, outputs = H.doginal_out(lock, reserve, THEM)
    assert [(c["txid"], c["vout"]) for c in inputs] == [(LOCK_TXID, 0), (LOCK_TXID, 1)], \
        "the lock's Doginal first, its reserve second, nothing of anybody else's"
    assert outputs == [(100_000, p2pkh_script(THEM))], "the Doginal's coin whole, to one address"
    assert where([100_000, H.DOGINAL_RESERVE], [100_000], 0) == (0, 0)
    assert where([100_000, H.DOGINAL_RESERVE], [100_000], 1) is None, "the reserve is the fee"
    if kind == "claim":
        raw = H.claimed(inputs, outputs, redeem, secret, claimer.sign)
    else:
        raw = H.refunded(inputs, outputs, redeem, refunder.sign)
    ins, outs, lt = _spent(raw)
    assert ins == [(LOCK_TXID, 0), (LOCK_TXID, 1)] and outs == [100_000]
    assert lt == (locktime if kind == "refund" else 0)


def test_a_doginal_spend_takes_only_the_lock_and_its_own_reserve():
    lock = {"txid": LOCK_TXID, "vout": 0, "value": 100_000}
    with pytest.raises(H.HtlcError):
        H.doginal_out(lock, {"txid": T3, "vout": 1, "value": H.DOGINAL_RESERVE}, THEM)   # a coin from elsewhere
    with pytest.raises(H.HtlcError):
        H.doginal_out({**lock, "vout": 1}, {"txid": LOCK_TXID, "vout": 0, "value": H.DOGINAL_RESERVE}, THEM)
    with pytest.raises(H.HtlcError):
        H.doginal_out(lock, {"txid": LOCK_TXID, "vout": 1, "value": H.SPEND_FEE - 1}, THEM)


# --- reading the other side's lock ------------------------------------------------------------

def _vtx(txid, vin, vout):
    return {"txid": txid, "vin": [{"txid": t, "vout": n} for t, n in vin],
            "vout": [{"n": k, "value": v / 1e8, "scriptPubKey": {"hex": s.hex()}} for k, (v, s) in enumerate(vout)]}


def test_the_other_lock_is_read_from_the_coin_it_spent(tmp_path):
    redeem = _redeem()
    p2sh = H.p2sh_script(redeem)
    leg = T.Leg("doge", "doginal", INS, 1, REFUNDER.hex(), CLAIMER.hex())
    parent = _vtx(T1, [], [(100_000, p2pkh_script(ME))])
    good = _vtx(LOCK_TXID, [(T1, 0), (T3, 0)], [(100_000, p2sh), (H.DOGINAL_RESERVE, p2sh), (10 ** 8, p2pkh_script(ME))])
    g = _guard(tmp_path)
    assert T.doginal_at(g, leg, redeem, good, parent) is True
    assert T.doginal_at(g, T.Leg("doge", "doginal", T2 + "i0", 1, "", ""), redeem, good, parent) is False
    short = _vtx(LOCK_TXID, [(T1, 0)], [(90_000, p2sh), (H.DOGINAL_RESERVE, p2sh)])
    assert T.doginal_at(g, leg, redeem, short, parent) is False, "not the coin's whole value"
    bare = _vtx(LOCK_TXID, [(T1, 0)], [(100_000, p2sh)])
    assert T.doginal_at(g, leg, redeem, bare, parent) is False, "no reserve to pay the claim"
    away = doginals.Doginals(tmp_path / "e.sqlite", fetch=_fake({}, []))
    assert T.doginal_at(away, leg, redeem, good, parent) is None, "the index away: not yet, never wrong"
    trade = T.Trade(id="tr-x", role=T.RESPONDER, give=T.Leg("regtest", "coin", "", 1, CLAIMER.hex(), REFUNDER.hex()),
                    get=T.Leg("doge", "doginal", INS, 1, REFUNDER.hex(), CLAIMER.hex()),
                    secret_hash=H.parse(redeem)["secret_hash"].hex(), initiator_locktime=H.parse(redeem)["locktime"])
    seen = T.LockSeen(LOCK_TXID, 0, 100_000, p2sh.hex(), 1)
    assert T.check_lock(trade, seen, theirs=True) == ""
    assert T.check_lock(trade, T.LockSeen(LOCK_TXID, 1, 100_000, p2sh.hex(), 1), theirs=True)
    assert T.check_lock(trade, T.LockSeen(LOCK_TXID, 0, 100_000, p2sh.hex(), 1, asset_ok=False), theirs=True)


def test_orders_and_messages_name_a_doginal_by_its_inscription_id():
    side = {"network": "doge", "kind": "doginal", "asset": INS, "amount": 1}
    assert O._side(side) == side
    for bad in ({**side, "network": "test"}, {**side, "asset": T1}, {**side, "amount": 2}):
        with pytest.raises(O.OrderError):
            O._side(bad)
    leg = {**side, "giver_key": REFUNDER.hex(), "taker_key": CLAIMER.hex()}
    assert M.leg_from(leg).asset == INS
    with pytest.raises(M.TradeMessageError):
        M.leg_from({**leg, "network": "test"})
