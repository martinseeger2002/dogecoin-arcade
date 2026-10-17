"""Coins carrying Doginals are never spent by an ordinary Dogecoin send (2026-10-14: "identify which UTXOs are
doginals to make sure they are not spent unless somebody is trying to send a doginal to someone else"). Doge Soft is
faked: arcade/doginals.py asks it per transaction and keeps the answer."""

import pytest

from arcade import doginals, funding

T1, T2, T3 = "11" * 32, "22" * 32, "33" * 32


def _fake(answers, calls):
    def fetch(txid):
        calls.append(txid)
        if txid not in answers:
            raise OSError("down")
        return answers[txid]
    return fetch


def test_an_inscribed_coin_is_left_out(tmp_path):
    calls = []
    g = doginals.Doginals(tmp_path / "d.sqlite", fetch=_fake(
        {T1: {"txid": T1, "inscriptions": [{"vout": 0, "inscription_id": T1 + "i0", "sat_offset": 0}]}}, calls))
    coins = [{"txid": T1, "vout": 0, "value": 100_000}, {"txid": T1, "vout": 1, "value": 50_000_000}]
    assert g.spendable(coins) == [coins[1]], "the Doginal's coin stays, its sibling change may be spent"


def test_a_small_coin_nobody_vouched_for_is_left_out_a_big_one_is_not(tmp_path):
    g = doginals.Doginals(tmp_path / "d.sqlite", fetch=_fake({}, []))   # Doge Soft is down
    small, big = {"txid": T2, "vout": 0, "value": 100_000}, {"txid": T2, "vout": 1, "value": 200_000_000}
    assert g.spendable([small, big]) == [big]


def test_a_clean_small_coin_may_be_spent(tmp_path):
    g = doginals.Doginals(tmp_path / "d.sqlite", fetch=_fake({T3: {"txid": T3, "inscriptions": []}}, []))
    coin = {"txid": T3, "vout": 0, "value": 100_000}
    assert g.spendable([coin]) == [coin]


def test_each_transaction_is_asked_about_once(tmp_path):
    calls = []
    answers = {T3: {"txid": T3, "inscriptions": []}}
    g = doginals.Doginals(tmp_path / "d.sqlite", fetch=_fake(answers, calls))
    g.spendable([{"txid": T3, "vout": 0, "value": 1}, {"txid": T3, "vout": 1, "value": 2}])
    g2 = doginals.Doginals(tmp_path / "d.sqlite", fetch=_fake(answers, calls))   # a restart keeps the answers
    g2.spendable([{"txid": T3, "vout": 2, "value": 3}])
    assert calls == [T3]


def test_funding_choose_skips_a_doginal_on_a_dogecoin_address(tmp_path, monkeypatch):
    g = doginals.Doginals(tmp_path / "d.sqlite", fetch=_fake(
        {T1: {"txid": T1, "inscriptions": [{"vout": 0, "inscription_id": T1 + "i0", "sat_offset": 0}]}}, []))
    coins = [{"txid": T1, "vout": 0, "value": 100_000}, {"txid": T1, "vout": 1, "value": 5_000_000}]
    monkeypatch.setattr(funding.utxos, "unspent", lambda db, address: [dict(c) for c in coins])
    monkeypatch.setattr(funding, "GUARD", g)
    got = funding.choose(None, "DDogeAddress", 1_000_000)
    assert [(c["txid"], c["vout"]) for c in got] == [(T1, 1)], "the Doginal's coin is never chosen"
    with pytest.raises(funding.FundingError):
        funding.choose(None, "DDogeAddress", 5_050_000)   # only the Doginal could cover it: refused, not spent
    pep = funding.choose(None, "nPepAddress", 50_000)   # a Pepecoin address is not watched
    assert pep and pep[0]["vout"] == 1 or pep[0]["vout"] == 0


def test_a_doginal_is_sent_first_whole_and_fees_come_from_clean_coins(tmp_path, monkeypatch):
    from arcade.config import NETWORKS
    from arcade.script import b58check_encode
    me, them = b58check_encode(30, bytes(20)), b58check_encode(30, bytes(range(20)))
    ins = T1 + "i0"
    g = doginals.Doginals(tmp_path / "d.sqlite", fetch=_fake({
        T1: {"txid": T1, "inscriptions": [{"vout": 0, "inscription_id": ins, "sat_offset": 0}]},
        T2: {"txid": T2, "inscriptions": [{"vout": 0, "inscription_id": T2 + "i0", "sat_offset": 0}]},
        T3: {"txid": T3, "inscriptions": []}}, []))
    coins = [{"txid": T1, "vout": 0, "value": 100_000},            # the Doginal being sent
             {"txid": T2, "vout": 0, "value": 100_000},            # ANOTHER Doginal: must never pay the fee
             {"txid": T3, "vout": 0, "value": 300_000_000}]        # a clean coin
    monkeypatch.setattr(funding.utxos, "unspent", lambda db, address: [dict(c) for c in coins])
    monkeypatch.setattr(funding, "GUARD", g)
    u = funding.build_doginal_send(None, NETWORKS["doge"], me, coins[0], ins, them, rate=100_000_000)
    assert [(c["txid"], c["vout"]) for c in u.inputs] == [(T1, 0), (T3, 0)], "the Doginal first, then a clean coin"
    assert u.outputs[0][0] == 100_000 and u.outputs[0][1] == funding.p2pkh_script(them), "the whole coin to the recipient, first"
    assert all((c["txid"]) != T2 for c in u.inputs), "the other Doginal is never spent"
    assert sum(c["value"] for c in u.inputs) == sum(v for v, _ in u.outputs) + u.fee


def test_a_doginal_send_is_refused_unless_the_coin_holds_exactly_it(tmp_path, monkeypatch):
    from arcade.config import NETWORKS
    from arcade.script import b58check_encode
    me, them = b58check_encode(30, bytes(20)), b58check_encode(30, bytes(range(20)))
    g = doginals.Doginals(tmp_path / "d.sqlite", fetch=_fake({
        T1: {"txid": T1, "inscriptions": [{"vout": 0, "inscription_id": T1 + "i0", "sat_offset": 0},
                                           {"vout": 0, "inscription_id": T1 + "i1", "sat_offset": 5000}]}}, []))
    coin = {"txid": T1, "vout": 0, "value": 100_000}
    monkeypatch.setattr(funding.utxos, "unspent", lambda db, address: [dict(coin)])
    monkeypatch.setattr(funding, "GUARD", g)
    with pytest.raises(funding.FundingError):   # two inscriptions on one coin: never sent together by accident
        funding.build_doginal_send(None, NETWORKS["doge"], me, coin, T1 + "i0", them, rate=100_000_000)
