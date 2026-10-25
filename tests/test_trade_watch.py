"""A node finishing its accounts' swaps with their tabs closed (arcade/trade_watch.py;
2026-10-13: each side "pre-signs" its claim and refund and hands them to its own node).

On regtest, with an account whose key this node never holds: the account signs a lock spend
the way its tab does and hands it in at /account/trade/watch; the node checks it, holds it,
and sends it when its moment comes -- a refund once the lock's time has passed, a claim once
the secret shows up on the chain. What it will not hold: a spend paying anybody else, a
signature that is not the account's, a lock that is not the account's to spend.
"""

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import _seated, node                          # noqa: E402,F401
from test_escrow import Owner                                          # noqa: E402
from test_funding import _sign                                         # noqa: E402
from test_htlc import Key, _lock                                       # noqa: E402
from test_web import app_state, client                               # noqa: F401,E402

from arcade import htlc as H                                           # noqa: E402

COIN = 100_000_000


def _presign(who, secret_int, said):
    """What wallet.tradePresign does: the node builds it, the account signs it."""
    asked = who.post("/account/trade/spend", json=said)
    assert asked.status_code == 200, asked.text
    offer = asked.json()
    return offer["raw"], _sign(secret_int, bytes.fromhex(offer["sighash"])).hex()


def _hold(who, said, raw, signature, **extra):
    return who.post("/account/trade/watch", json={**said, "raw": raw, "signature": signature, **extra})


def _tick(state):
    return state.trade_watch.tick(state.chain_rpc_for)


def test_a_held_refund_goes_out_once_the_lock_time_has_passed(node):
    app, state, rpc = node
    who, secret_int, pubkey, address = _seated(app, state, rpc, 81, key=False)
    payer, stranger = Owner(state, rpc), Key()
    _, h = H.make_secret()
    soon = H.redeem_script(h, stranger.pub, pubkey, int(time.time()) + 7200)
    past = H.redeem_script(h, stranger.pub, pubkey, int(time.time()) - 7200)
    held = {}
    for name, redeem in (("soon", soon), ("past", past)):
        coin = _lock(rpc, payer, redeem, COIN)
        said = {"chain": "regtest", "redeem": redeem.hex(), "txid": coin["txid"], "vout": coin["vout"],
                "kind": "refund"}
        raw, sig = _presign(who, secret_int, said)
        answer = _hold(who, said, raw, sig)
        assert answer.status_code == 200, answer.text
        held[name] = answer.json()["job"]
    rpc.call("generate", 1)
    done = dict(_tick(state))
    assert done.get(held["past"]) == "sent refund", done
    assert held["soon"] not in done, "two hours to go: held, not sent"
    rpc.call("generate", 1)
    jobs = {j["id"]: j for j in state.trade_watch.jobs()}
    tx = rpc.call("getrawtransaction", jobs[held["past"]]["sent_txid"], 1)
    assert [o["scriptPubKey"]["addresses"] for o in tx["vout"]] == [[address]], "back to the account alone"
    assert dict(_tick(state)).get(held["past"]) == "spent", "seen spent, and not sent twice"
    assert {j["id"]: j["status"] for j in state.trade_watch.jobs()}[held["past"]] == "spent"


def test_a_held_claim_takes_the_secret_off_the_chain(node):
    """The responder's case: its own lock (B) is claimed by the initiator, which puts the
    secret on the chain; the node reads it there and sends the account's held claim of the
    initiator's lock (A) -- with the tab long closed."""
    _held_claim(node, 82, H.make_secret()[0])


def test_a_held_claim_takes_a_secret_of_any_length_off_the_chain(node):
    """2026-10-25: the lock checks only the secret's hash, so a claim of B with a secret that is not 32 bytes opens it
    -- and the node must read that secret all the same."""
    _held_claim(node, 83, b"\x42" * 48)


def _held_claim(node, seat, secret):
    app, state, rpc = node
    who, secret_int, pubkey, address = _seated(app, state, rpc, seat, key=False)
    initiator = Owner(state, rpc)
    h = H.hash_of(secret)
    now = int(time.time())
    lock_a = H.redeem_script(h, pubkey, initiator.pubkey, now + 7200)       # pays the account
    lock_b = H.redeem_script(h, initiator.pubkey, pubkey, now + 3600)       # the account's own
    coin_a = _lock(rpc, initiator, lock_a, 2 * COIN)
    funder = Owner(state, rpc)
    coin_b = _lock(rpc, funder, lock_b, COIN)                              # (paid for the account)
    rpc.call("generate", 1)
    said = {"chain": "regtest", "redeem": lock_a.hex(), "txid": coin_a["txid"], "vout": coin_a["vout"],
            "kind": "claim"}
    raw, sig = _presign(who, secret_int, said)
    where = {"network": "regtest", "txid": coin_b["txid"], "vout": coin_b["vout"], "redeem": lock_b.hex()}
    answer = _hold(who, said, raw, sig, secret_lock=where)
    assert answer.status_code == 200, answer.text
    job = answer.json()["job"]
    assert dict(_tick(state)).get(job) is None, "no secret on the chain yet"
    # The initiator claims B, revealing the secret.
    inputs, outputs = H.pay_out({**coin_b}, initiator.address)
    rpc.call("sendrawtransaction", H.claimed(inputs, outputs, lock_b, secret,
                                             lambda d: _sign(initiator.secret, d)))
    rpc.call("generate", 1)
    assert dict(_tick(state)).get(job) == "sent claim"
    rpc.call("generate", 1)
    sent = next(j for j in state.trade_watch.jobs() if j["id"] == job)
    tx = rpc.call("getrawtransaction", sent["sent_txid"], 1)
    assert [o["scriptPubKey"]["addresses"] for o in tx["vout"]] == [[address]]
    assert sent["secret"] == secret.hex()


def test_a_held_claim_of_a_swap_with_a_data_asset_waits_a_confirmation(node):
    """2026-10-25 (SadFrog): "data assets like ordinals should always have a conf." The initiator's held claim (it
    carries the secret) of a coin-only swap below the line goes out from the mempool as before; in a swap with a token,
    NFT or Doginal on either leg the node holds it to one confirmation, whatever min_conf the tab asked."""
    app, state, rpc = node
    who, secret_int, pubkey, address = _seated(app, state, rpc, 84, key=False)
    responder = Owner(state, rpc)
    jobs = {}
    for name, extra in (("coin", {}), ("nft", {"other": {"kind": "nft"}}), ("doginal", {"leg": {"kind": "coin"},
                                                                                       "other": {"kind": "doginal"}})):
        secret, h = H.make_secret()
        lock = H.redeem_script(h, pubkey, responder.pubkey, int(time.time()) + 3600)    # pays the account
        coin = _lock(rpc, responder, lock, COIN)                                       # left unconfirmed
        said = {"chain": "regtest", "redeem": lock.hex(), "txid": coin["txid"], "vout": coin["vout"], "kind": "claim"}
        raw, sig = _presign(who, secret_int, said)
        answer = _hold(who, said, raw, sig, secret=secret.hex(), min_conf=0, **extra)
        assert answer.status_code == 200, answer.text
        jobs[name] = answer.json()["job"]
    held = {j["id"]: j for j in state.trade_watch.jobs()}
    assert [held[jobs[n]]["min_conf"] for n in ("coin", "nft", "doginal")] == [0, 1, 1]
    done = dict(_tick(state))
    assert done.get(jobs["coin"]) == "sent claim", "coin below the line: the mempool is enough"
    assert jobs["nft"] not in done and jobs["doginal"] not in done, "an asset swap waits for its block"
    rpc.call("generate", 1)
    done = dict(_tick(state))
    assert done.get(jobs["nft"]) == "sent claim" and done.get(jobs["doginal"]) == "sent claim"


def test_the_node_holds_only_the_accounts_own_good_spends(node):
    app, state, rpc = node
    who, secret_int, pubkey, address = _seated(app, state, rpc, 83, key=False)
    payer, stranger = Owner(state, rpc), Key()
    secret, h = H.make_secret()
    redeem = H.redeem_script(h, pubkey, payer.pubkey, int(time.time()) + 7200)
    coin = _lock(rpc, payer, redeem, COIN)
    rpc.call("generate", 1)
    said = {"chain": "regtest", "redeem": redeem.hex(), "txid": coin["txid"], "vout": coin["vout"],
            "kind": "claim"}
    raw, sig = _presign(who, secret_int, said)
    # Somebody else's signature.
    forged = _sign(payer.secret, bytes.fromhex(who.post("/account/trade/spend", json=said).json()["sighash"])).hex()
    bad = _hold(who, said, raw, forged, secret=secret.hex())
    assert bad.status_code == 400 and "signature" in bad.json()["detail"]
    # Paying somebody else: the account's address swapped for the stranger's.
    mine_script = bytes.fromhex(raw).hex().find(H.p2sh_script(redeem).hex())
    from arcade.script import b58check_encode, hash160
    other = b58check_encode(state.messaging.params.pubkeyhash_version, hash160(stranger.pub))
    from arcade.txbuild import p2pkh_script
    swapped = raw.replace(p2pkh_script(address).hex(), p2pkh_script(other).hex())
    assert swapped != raw and mine_script == -1
    bad = _hold(who, said, swapped, sig, secret=secret.hex())
    assert bad.status_code == 400 and "somebody besides" in bad.json()["detail"]
    # A refund of a lock that refunds somebody else.
    bad = _hold(who, {**said, "kind": "refund"}, raw, sig)
    assert bad.status_code == 400
    # A claim with neither the secret nor where it will show up.
    bad = _hold(who, said, raw, sig)
    assert bad.status_code == 400 and "secret" in bad.json()["detail"]
    good = _hold(who, said, raw, sig, secret=secret.hex(), min_conf=1)
    assert good.status_code == 200, good.text
    assert dict(_tick(state)).get(good.json()["job"]) == "sent claim"


def test_the_admin_sees_held_swaps_and_sets_the_big_line(node):
    from test_admin import WRITE
    from test_admin import LOCAL
    app, state, rpc = node
    who, secret_int, pubkey, address = _seated(app, state, rpc, 84, key=False)
    payer, stranger = Owner(state, rpc), Key()
    _, h = H.make_secret()
    redeem = H.redeem_script(h, stranger.pub, pubkey, int(time.time()) + 7200)
    coin = _lock(rpc, payer, redeem, COIN)
    said = {"chain": "regtest", "redeem": redeem.hex(), "txid": coin["txid"], "vout": coin["vout"], "kind": "refund"}
    raw, sig = _presign(who, secret_int, said)
    assert _hold(who, said, raw, sig).status_code == 200
    seen = app.get("/admin/api/swaps", headers=LOCAL).json()
    assert seen["counts"]["waiting"] >= 1
    assert any(j["kind"] == "refund" and j["status"] == "waiting" for j in seen["held"])
    went = app.post("/admin/api/settings", headers={**LOCAL, **WRITE}, json={"swap_big": {"doge": "250"}})
    assert went.status_code == 200, went.text
    assert app.get("/admin/api/state", headers=LOCAL).json()["swap_big"]["doge"] == 250
    from arcade.node_trader import swap_big
    assert swap_big(state)["doge"] == 250 * COIN
