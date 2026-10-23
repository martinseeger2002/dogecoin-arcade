"""A tab's own lock-script trades (trustless-trading-plan.md phase 2).

On regtest, with an account whose key this node never holds: the node hands it the
unsigned spend of a lock, the account signs the digest itself (as coins.js does), and the
node puts the signature in place and broadcasts -- a claim with the secret, a refund only
after the time, and nothing at all for a lock that names somebody else. Then the browser's
state machine (wallet.js nextTradeStep) against arcade/trade.py's, scenario for scenario.
"""

import pathlib
import secrets
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_account_offer import _seated, _settled, node                # noqa: E402,F401
from test_escrow import Owner                                          # noqa: E402
from test_funding import _sign                                         # noqa: E402
from test_htlc import Key, _lock                                       # noqa: E402
from test_web import app_state, client                               # noqa: F401,E402

from arcade import htlc as H, trade as T                               # noqa: E402

COIN = 100_000_000


def _spend(who, secret_int, said):
    asked = who.post("/account/trade/spend", json=said)
    assert asked.status_code == 200, asked.text
    offer = asked.json()
    sig = _sign(secret_int, bytes.fromhex(offer["sighash"])).hex()
    return who.post("/account/trade/spend/sign", json={
        **said, "raw": offer["raw"], "signature": sig})


def test_an_account_claims_a_lock_that_pays_it_with_the_secret(node):
    app, state, rpc = node
    who, secret_int, pubkey, address = _seated(app, state, rpc, 71, key=False)
    payer = Owner(state, rpc)
    secret, h = H.make_secret()
    redeem = H.redeem_script(h, pubkey, payer.pubkey, int(time.time()) + 7200)
    coin = _lock(rpc, payer, redeem, 2 * COIN)
    rpc.call("generate", 1)
    base = {"chain": "regtest", "redeem": redeem.hex(), "txid": coin["txid"], "vout": coin["vout"]}
    wrong = _spend(who, secret_int, {**base, "kind": "claim", "secret": "00" * 32})
    assert wrong.status_code == 400 and "secret" in wrong.json()["detail"]
    done = _spend(who, secret_int, {**base, "kind": "claim", "secret": secret.hex()})
    assert done.status_code == 200, done.text
    rpc.call("generate", 1)
    tx = rpc.call("getrawtransaction", done.json()["txid"], 1)
    assert [o["scriptPubKey"]["addresses"] for o in tx["vout"]] == [[address]], "paid to the account alone"
    facts = who.post("/account/trade/facts", json={"chain": "regtest", **{k: base[k] for k in ("txid", "vout")}})
    seen = facts.json()["seen"]
    assert seen["spent_by_me"] and H.secret_from_script_sig(bytes.fromhex(seen["spent_by_script_sig"]), redeem) == secret


def test_an_account_refunds_its_lock_only_after_the_time(node):
    app, state, rpc = node
    who, secret_int, pubkey, address = _seated(app, state, rpc, 72, key=False)
    payer, stranger = Owner(state, rpc), Key()
    _, h = H.make_secret()
    later = H.redeem_script(h, stranger.pub, pubkey, int(time.time()) + 7200)
    coin = _lock(rpc, payer, later, COIN)
    rpc.call("generate", 1)
    early = _spend(who, secret_int, {"chain": "regtest", "redeem": later.hex(), "txid": coin["txid"],
                                     "vout": coin["vout"], "kind": "refund"})
    assert early.status_code == 400, "the chain refuses a refund before its time"
    past = H.redeem_script(h, stranger.pub, pubkey, int(time.time()) - 7200)
    coin = _lock(rpc, payer, past, COIN)
    rpc.call("generate", 1)
    done = _spend(who, secret_int, {"chain": "regtest", "redeem": past.hex(), "txid": coin["txid"],
                                    "vout": coin["vout"], "kind": "refund"})
    assert done.status_code == 200, done.text


def test_a_lock_naming_somebody_else_gets_nothing_from_the_node(node):
    app, state, rpc = node
    who, secret_int, pubkey, address = _seated(app, state, rpc, 73, key=False)
    payer, stranger = Owner(state, rpc), Key()
    secret, h = H.make_secret()
    redeem = H.redeem_script(h, stranger.pub, payer.pubkey, int(time.time()) + 7200)
    coin = _lock(rpc, payer, redeem, COIN)
    rpc.call("generate", 1)
    asked = who.post("/account/trade/spend", json={"chain": "regtest", "redeem": redeem.hex(),
                                                   "txid": coin["txid"], "vout": coin["vout"], "kind": "claim"})
    assert asked.status_code == 400 and "pays" in asked.json()["detail"]


# --- the browser's state machine against the Python one ---------------------------------

def _scenarios():
    """(role, facts-maker) pairs covering every branch of next_step."""
    now = int(time.time())
    out = []
    for role in (T.INITIATOR, T.RESPONDER):
        a, b = Key(), Key()
        secret, h = H.make_secret()
        give = T.Leg("regtest", "coin", "", 3 * COIN, a.pub.hex(), b.pub.hex())
        get = T.Leg("regtest", "coin", "", 2 * COIN, b.pub.hex(), a.pub.hex())
        for t1, t2 in ((now + 6 * 3600, now + 3 * 3600), (now + 7200, now + 1800), (now - 60, now - 3600)):
            tr = T.Trade("t", role, give, get, h.hex(), secret.hex() if role == T.INITIATOR else "", t1, t2)
            def seen(theirs, conf=5, value=None, spent="", by_me=False, tr=tr):
                redeem = tr.their_redeem() if theirs else tr.my_redeem()
                leg = tr.get if theirs else tr.give
                return T.LockSeen("ab" * 32, 0, leg.amount if value is None else value,
                                  H.p2sh_script(redeem).hex(), conf, True, spent, by_me)
            claim = H.claim_script_sig(b"\x30" * 70, secret, tr.my_redeem()).hex()
            for facts in (T.Facts(), T.Facts(mine=seen(False)), T.Facts(theirs=seen(True)),
                          T.Facts(theirs=seen(True, conf=0)), T.Facts(mine=seen(False), theirs=seen(True)),
                          T.Facts(mine=seen(False), theirs=seen(True, value=1)),
                          T.Facts(mine=seen(False, spent=claim), theirs=seen(True)),
                          T.Facts(mine=seen(False), theirs=seen(True, spent="00", by_me=True)),
                          T.Facts(mine=seen(False, spent="00", by_me=True))):
                out.append((tr, facts))
    # A token leg each way: the reserve, and the ledger's word on the asset.
    for role in (T.INITIATOR, T.RESPONDER):
        a, b = Key(), Key()
        secret, h = H.make_secret()
        give = T.Leg("regtest", "token", "150", 40, a.pub.hex(), b.pub.hex())
        get = T.Leg("regtest", "nft", "c3" * 32, 1, b.pub.hex(), a.pub.hex())
        tr = T.Trade("t", role, give, get, h.hex(), secret.hex() if role == T.INITIATOR else "",
                     now + 6 * 3600, now + 3 * 3600)
        def seen(theirs, value=T.LOCK_RESERVE, asset_ok=True, tr=tr):
            redeem = tr.their_redeem() if theirs else tr.my_redeem()
            return T.LockSeen("ab" * 32, 0, value, H.p2sh_script(redeem).hex(), 5, asset_ok)
        for facts in (T.Facts(theirs=seen(True)), T.Facts(theirs=seen(True, asset_ok=False)),
                      T.Facts(theirs=seen(True, value=T.LOCK_RESERVE - 1)),
                      T.Facts(mine=seen(False), theirs=seen(True)),
                      T.Facts(mine=seen(False), theirs=seen(True, asset_ok=False))):
            out.append((tr, facts))
    return out


def test_the_browser_keeps_the_same_reserve():
    import re
    js = (pathlib.Path(__file__).resolve().parent.parent / "arcade/web/templates/wallet_js.js").read_text()
    assert int(re.search(r"LOCK_RESERVE: ([0-9_]+)", js).group(1).replace("_", "")) == T.LOCK_RESERVE
    num = lambda pattern: eval(re.search(pattern, js).group(1).replace("_", ""))   # noqa: E731
    assert num(r"BIG: \{main: ([0-9_ *]+),") == T.BIG["main"]
    assert num(r"doge: ([0-9_ *]+)[,}]") == T.BIG["doge"]
    assert num(r"ltc: ([0-9_ *]+)[,}]") == T.BIG["ltc"], "Litecoin's big line, the same both sides"
    assert num(r"bel: ([0-9_ *]+)[,}]") == T.BIG["bel"] and num(r"lky: ([0-9_ *]+)\}") == T.BIG["lky"], \
        "Bellscoin's and Luckycoin's too (2026-10-22)"
    assert num(r"CLAIM_MARGIN: ([0-9 *]+),") == T.CLAIM_MARGIN and num(r"LOCK_MARGIN: ([0-9 *]+),") == T.LOCK_MARGIN


def test_the_browser_decides_every_step_as_the_python_does(tmp_path):
    pytest.importorskip("playwright.sync_api")
    import dataclasses
    import uvicorn
    import threading
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
    now = int(time.time())
    cases = []
    for tr, facts in _scenarios():
        expected = T.next_step(dataclasses.replace(tr), facts, now)
        js_trade = {"role": tr.role, "give": dataclasses.asdict(tr.give), "get": dataclasses.asdict(tr.get),
                    "secret": tr.secret, "secretHash": tr.secret_hash, "t1": tr.initiator_locktime,
                    "t2": tr.responder_locktime, "myRedeem": tr.my_redeem().hex(),
                    "theirRedeem": tr.their_redeem().hex()}
        js_facts = {k: (dataclasses.asdict(v) if v else None) for k, v in (("mine", facts.mine), ("theirs", facts.theirs))}
        cases.append((js_trade, js_facts, list(expected)))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            tab = browser.new_page()
            tab.goto(f"http://127.0.0.1:{port}/join")
            got = tab.evaluate("""async ([cases, now]) => {
                const w = await import('/wallet.js'), c = await import('/coins.js');
                const out = [];
                for (const [t, f] of cases) {
                  t.myRedeem = c.unhex(t.myRedeem); t.theirRedeem = c.unhex(t.theirRedeem);
                  out.push(await w.nextTradeStep(t, f, now));
                }
                return out; }""", [[[c[0], c[1]] for c in cases], now])
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert len(got) == len(cases) > 40
    for (_, _, want), have in zip(cases, got):
        assert have == want, (have, want)
