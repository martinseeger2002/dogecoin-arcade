"""The Doginal pieces that were not built (2026-10-15: "Build the things that are not built"):

* a tab's own lock-script swap can give a Doginal: the node builds its lock (the Doginal whole at output 0, its reserve
  at output 1) and its spend -- two inputs under the lock, so two signatures (the "two-signature claim");
* a Doginal order says what it is ("Doginal 1a2b3c4d…i0");
* the node's own DOGE coin legs (sendtoaddress) never spend a coin carrying a Doginal;
* what this arcade broadcasts from clean coins is vouched clean, so a token transaction's dust is spendable at once.
The Doginals source is faked; nothing here asks a real one."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_web import app_state, client                          # noqa: F401,E402
from test_doginal_wallet import _doge_account, T1, T2, T3, INS, ME   # noqa: E402
from test_htlc import doge                                        # noqa: E402,F401

from arcade import doginals, funding, htlc as H, trade_orders as O, trade_watch as W   # noqa: E402
from arcade.funding import p2pkh_script                           # noqa: E402

COIN = 100_000_000


def test_a_tab_locks_its_doginal_whole_at_output_0_with_its_reserve_beside_it(client, monkeypatch, tmp_path):
    from test_funding import _pubkey
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    pub = _pubkey(int("31" * 32, 16))                       # ME's key: the account's Dogecoin coin key
    said = app.post("/account/address", json={"address": ME, "chain": "doge", "coin_pubkey": pub.hex()})
    assert said.status_code == 200, said.text
    other = bytes.fromhex("02" + "ab" * 32)
    leg = {"kind": "doginal", "asset": INS, "amount": 1}
    bad = H.redeem_script(bytes(32), other, bytes.fromhex("02" + "cd" * 32), 1_900_000_000)
    refused = app.post("/account/trade/lock", json={"chain": "doge", "redeem": bad.hex(), "leg": leg})
    assert refused.status_code == 400 and "only lock your own" in refused.json()["detail"]
    redeem = H.redeem_script(bytes(32), other, pub, 1_900_000_000)
    built, real = [], funding.build_doginal_lock
    monkeypatch.setattr(funding, "build_doginal_lock", lambda *a, **k: built.append(real(*a, **k)) or built[-1])
    got = app.post("/account/trade/lock", json={"chain": "doge", "redeem": redeem.hex(), "leg": leg})
    assert got.status_code == 200, got.text
    offer = got.json()
    ins = [(i["txid"], i["vout"]) for i in offer["inputs"]]
    assert ins[0] == (T1, 0), "the Doginal's coin is input 0"
    assert (T2, 0) not in ins, "another Doginal never pays for it"
    lock = H.p2sh_script(redeem)
    outs = built[0].outputs
    assert outs[0] == (100_000, lock), "the whole coin into the lock, output 0"
    assert outs[1] == (H.DOGINAL_RESERVE, lock), "its reserve beside it, output 1"
    assert len(offer["sighashes"]) == len(offer["inputs"]), "signed in the tab, like every account send"
    nope = app.post("/account/trade/lock", json={"chain": "doge", "redeem": redeem.hex(),
                                                 "leg": {"kind": "doginal", "asset": T3 + "i0", "amount": 1}})
    assert nope.status_code == 400 and "does not hold Doginal" in nope.json()["detail"]
    off = app.post("/account/trade/lock", json={"chain": state.messaging.network, "redeem": redeem.hex(), "leg": leg})
    assert off.status_code == 400


def test_a_doginal_spend_puts_one_signature_in_each_input():
    redeem = H.redeem_script(bytes(32), bytes.fromhex("02" + "11" * 32), bytes.fromhex("02" + "22" * 32), 1_900_000_000)
    lock = {"txid": "aa" * 32, "vout": 0, "value": 100_000}
    reserve = {"txid": "aa" * 32, "vout": 1, "value": H.DOGINAL_RESERVE}
    inputs, outputs = H.doginal_out(lock, reserve, ME)
    from arcade import escrow as E
    raw = E.serialize(inputs, outputs, 0)
    one, two = b"\x30" + b"\x01" * 70 + b"\x01", b"\x30" + b"\x02" * 70 + b"\x01"
    signed = W.complete(raw, f"{one.hex()},{two.hex()}", redeem, "refund")
    got = doginals._inputs_of(signed)
    assert [(p, v) for p, v, _ in got] == [("aa" * 32, 0), ("aa" * 32, 1)]
    assert got[0][2] == H.refund_script_sig(one, redeem) and got[1][2] == H.refund_script_sig(two, redeem)
    with pytest.raises(W.WatchError):
        W.complete(raw, one.hex(), redeem, "refund")          # two inputs, one signature


def test_the_two_signature_claim_moves_a_doginal_out_on_a_real_chain(doge):
    """A Doginal's coin and its reserve on Dogecoin regtest under one lock; the claim spends both with two signatures
    by the same key (put_scripts), and the Doginal's coin comes out first and whole to the claimer."""
    from test_funding import _sign
    from test_htlc import Payer
    from arcade import escrow as E
    from arcade.txbuild import push
    rpc = doge.rpc
    giver, claimer = Payer(rpc), Payer(rpc)
    secret = bytes(range(32))
    redeem = H.redeem_script(H.hash_of(secret), claimer.pubkey, giver.pubkey, 1_900_000_000)
    lock_script, mine = H.p2sh_script(redeem), p2pkh_script(giver.address)
    inputs = [{"txid": giver.coin["txid"], "vout": giver.coin["vout"]}]
    outputs = [(100_000, lock_script), (H.DOGINAL_RESERVE, lock_script),
               (giver.coin["value"] - 100_000 - H.DOGINAL_RESERVE - 2_000_000, mine)]
    sig = _sign(giver.secret, E.sighash(inputs, outputs, 0, mine))
    lock_txid = rpc.call("sendrawtransaction",
                         E.serialize([{**inputs[0], "script_sig": push(sig) + push(giver.pubkey)}], outputs).hex())
    rpc.call("generate", 1)
    ins, outs = H.doginal_out({"txid": lock_txid, "vout": 0, "value": 100_000},
                              {"txid": lock_txid, "vout": 1, "value": H.DOGINAL_RESERVE}, claimer.address)
    sigs = [_sign(claimer.secret, E.sighash(ins, outs, n, redeem, 0)) for n in range(2)]
    done = W.put_scripts(E.serialize(ins, outs, 0), [H.claim_script_sig(x, secret, redeem) for x in sigs])
    txid = rpc.call("sendrawtransaction", done.hex())
    tx = rpc.call("getrawtransaction", txid, 1)
    assert len(tx["vin"]) == 2 and tx["vout"][0]["scriptPubKey"]["addresses"] == [claimer.address]
    assert round(tx["vout"][0]["value"] * COIN) == 100_000, "the Doginal's coin, whole and first"
    assert H.secret_from_script_sig(bytes.fromhex(tx["vin"][0]["scriptSig"]["hex"]), redeem) == secret, \
        "and the secret is on the chain for the other side"


def test_the_nodes_doge_coin_leg_sets_doginal_coins_aside(tmp_path):
    from arcade.node_trader import NodeTrader
    calls = []

    class Rpc:
        def call(self, method, *args):
            calls.append((method, args))
            if method == "listunspent":
                return [{"txid": T1, "vout": 0, "amount": 0.001}, {"txid": T3, "vout": 0, "amount": 3.0},
                        {"txid": "55" * 32, "vout": 0, "amount": 0.001}]
            if method == "sendtoaddress":
                return "ff" * 32
            return None
    guard = doginals.Doginals(tmp_path / "g.sqlite", fetch=lambda txid: {
        T1: {"txid": T1, "inscriptions": [{"vout": 0, "inscription_id": INS, "sat_offset": 0}]},
        T3: {"txid": T3, "inscriptions": []}}[txid])
    guard.held(T1); guard.held(T3)                    # what is already known; the send asks nobody
    guard.fetch = lambda txid: (_ for _ in ()).throw(AssertionError("asked the index during a send"))
    trader = NodeTrader.__new__(NodeTrader)
    trader._doginals = guard
    assert trader._send_coins(Rpc(), "doge", "DLockAddress", 2 * COIN) == "ff" * 32
    names = [m for m, _ in calls]
    assert names == ["listunspent", "lockunspent", "sendtoaddress", "lockunspent"]
    locked = calls[1][1]
    assert locked[0] is False and {c["txid"] for c in locked[1]} == {T1, "55" * 32}, \
        "the Doginal and the small coin the index never vouched for stay out of the send"
    assert calls[3][1][0] is True and calls[3][1][1] == locked[1], "and are let go after"
    calls.clear()
    trader._send_coins(Rpc(), "main", "PAddress", COIN)
    assert [m for m, _ in calls] == ["sendtoaddress"], "off Dogecoin there is nothing to set aside"


def test_what_the_arcade_broadcasts_from_clean_coins_is_clean(tmp_path):
    from arcade import escrow as E
    asked = []
    guard = doginals.Doginals(tmp_path / "v.sqlite", fetch=lambda txid: asked.append(txid) or
                              {"txid": txid, "inscriptions": []})
    guard.held(T3)                                            # a coin the index vouched for
    dust = [(100_000, p2pkh_script(ME)), (50_000, p2pkh_script(ME))]
    raw = E.serialize([{"txid": T3, "vout": 0}], dust, 0)
    assert guard.vouch_broadcast(raw.hex(), "77" * 32)
    asked.clear()
    assert guard.spendable([{"txid": "77" * 32, "vout": 0, "value": 100_000},
                            {"txid": "77" * 32, "vout": 1, "value": 50_000}]) != [], "the dust spends at once"
    assert asked == [], "without asking the index"
    unknown = E.serialize([{"txid": "88" * 32, "vout": 0}], dust, 0)
    assert not guard.vouch_broadcast(unknown.hex(), "99" * 32), "an input not known clean: the index decides"
    assert not guard.vouch_broadcast(_with_script(raw, b"\x03ord\x51").hex(), "aa" * 32), "an inscription envelope"


def _with_script(raw: bytes, script: bytes) -> bytes:
    at = 4 + 1 + 36
    return raw[:at] + bytes([len(script)]) + script + raw[at + 1:]


def test_a_doginal_says_what_it_is_and_has_a_market_key(client):
    app, state = client
    side = {"network": "doge", "kind": "doginal", "asset": INS, "amount": 1}
    assert O.leg_key(side) == f"doge-doginal-{INS}"
    assert O.parse_leg_key(f"doge-doginal-{INS}") == {"network": "doge", "kind": "doginal", "asset": INS}
    b, q = O.base_first({"network": "main", "kind": "coin", "asset": "", "amount": COIN}, side)
    assert b["kind"] == "doginal", "a Doginal is priced in what it is traded for"
    page_js = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates/_tab_swaps.html").read_text()
    assert 'leg.kind === "doginal" ? `Doginal' in page_js


def test_a_doginal_can_be_priced_in_any_coin_or_held_token(client, monkeypatch, tmp_path):
    """2026-10-15: "It says I can only list for PEP. On doginals. The user should be able to list for any of the
    coins and any of the tokens that they possess." A Doginal's market page offers every chain's coin (and the reader's
    tokens); DOGE -- its own chain -- opens the Doginals tab (one transaction), any other its swap market."""
    app, state = client
    _doge_account(app, state, monkeypatch, tmp_path)
    key = f"doge-doginal-{INS}"
    # The market page no longer lists every other pairing at its top (2026-10-17: "get rid of
    # the list of other pairings at the top of the pairing page"); any coin is still one market away.
    page = app.get(f"/exchange/swap/{key}/main-coin").text
    assert "Price it in" not in page
    go = lambda q: app.get(f"/exchange/pairs?base={key}&quote={q}", follow_redirects=False).headers["location"]  # noqa: E731
    assert go("doge-coin") == f"/exchange/swap/{key}/doge-coin", "for DOGE: its market of offers (lock scripts)"
    offers = app.get(f"/exchange/swap/{key}/doge-coin")
    assert offers.status_code == 200 and "list it on the Doginals tab" in offers.text, "listings stay on the tab"
    assert go(f"{state.messaging.network}-coin") == f"/exchange/swap/{key}/{state.messaging.network}-coin"


def test_an_offer_in_doge_for_a_doginal_is_an_order_lock_scripts_settle():
    """A swap on one chain is still refused -- except a Doginal against a coin on Dogecoin, which has no other way to make
    an offer (2026-10-15)."""
    import secrets as S
    import time as T
    base = {"trade_key": "02" + S.token_hex(32), "msg_key": S.token_hex(32), "node": S.token_hex(32),
            "expires": int(T.time()) + 3600}
    offer = {"id": "ofr-00000001", "give": {"network": "doge", "kind": "coin", "asset": "", "amount": 5 * COIN},
             "get": {"network": "doge", "kind": "doginal", "asset": INS, "amount": 1}, **base}
    O.batch_json([offer])                                      # accepted
    same = {**offer, "id": "ofr-00000002", "get": {"network": "doge", "kind": "coin", "asset": "", "amount": COIN}}
    with pytest.raises(O.OrderError):
        O.batch_json([same])
    pep = {**offer, "id": "ofr-00000003", "give": {"network": "main", "kind": "coin", "asset": "", "amount": COIN},
           "get": {"network": "main", "kind": "token", "asset": "5", "amount": 1}}
    with pytest.raises(O.OrderError):
        O.batch_json([pep])
