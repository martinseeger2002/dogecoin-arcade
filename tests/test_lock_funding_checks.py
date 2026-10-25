"""A lock's funding is read off its own bytes before it is signed (2026-10-25, security review item 4).

A swap said yes to in the Take or List dialog funds its lock without asking again, and verifyOffer checks only that the
signatures are over the transaction's bytes -- not where those bytes send the coins. The node's `lock` field is its own
word. So wallet.js `fundMyLock` and `delegateOrder` read the transaction: exactly one output pays the lock's script (or
the delegated order's own key) exactly what the swap puts in, a token's or an NFT's payload is exactly the one agreed,
and every other output is this wallet's change. Run in Node against the real wallet.js and coins.js, with the node's
answers made here in Python; `/account/sign` is reached only by a transaction that passes.
"""

import pathlib
import secrets
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_htlc import Key                                                # noqa: E402
from test_trade_tab import run_wallet_js                                 # noqa: E402

from arcade import funding, htlc as H, trade as T                       # noqa: E402
from arcade.config import NETWORKS                                      # noqa: E402
from arcade.encoding import encode_class_c                              # noqa: E402
from arcade import inscriptions as I, payload as P, tokens as TK        # noqa: E402
from arcade.txbuild import build_raw_tx, op_return_script               # noqa: E402

NET = "test"
PARAMS = NETWORKS[NET]
COIN = 100_000_000
PIECE = "ab" * 32

#: The JS side: a wallet of one key, a node that answers with `data.answers[url]` (in order), and every /account/sign.
HARNESS = """
  globalThis.document = {body: {dataset: {}}};
  const keys = {network: data.net, version: data.version, key: c.unhex(data.priv), pubkey: c.unhex(data.pub),
                address: data.address};
  const wallet = {pubkey: "tester", seed: c.unhex(data.seed), on: {[data.net]: keys, doge: keys}};
  const run = async (answers, fn) => {
    const signed = [], left = JSON.parse(JSON.stringify(answers));
    globalThis.fetch = async (url, opts) => {
      const reply = (obj, status = 200) => ({ok: status < 400, status, json: async () => obj});
      if (url === "/account") return reply({chains: []});
      if (url === "/account/sign") {
        signed.push(JSON.parse(opts.body).offer);
        return reply({ok: true, txid: "cd".repeat(32)});
      }
      const next = (left[url] || []).shift();
      if (!next) return reply({detail: "no answer for " + url}, 400);
      return reply(next);
    };
    try { await fn(); return {signed, error: ""}; }
    catch (e) { return {signed, error: String(e.message || e)}; }
  };
"""


def _me():
    key = Key(version=PARAMS.pubkeyhash_version)
    return key, {"net": NET, "version": PARAMS.pubkeyhash_version, "priv": key.secret.to_bytes(32, "big").hex(),
                 "pub": key.pub.hex(), "address": key.address, "seed": secrets.token_bytes(64).hex()}


def _mine(key) -> bytes:
    return bytes([0x76, 0xA9, 0x14]) + funding.hash160(key.pub) + bytes([0x88, 0xAC])


def _offer(key, outputs, n_in=1, first_value=None):
    """An unsigned offer from `key`'s own coins paying `outputs`, as funding.build hands it over."""
    inputs = [{"txid": secrets.token_hex(32), "vout": i, "value": (first_value if i == 0 and first_value else 50 * COIN),
               "address": key.address} for i in range(n_in)]
    raw = build_raw_tx([(c["txid"], c["vout"]) for c in inputs], outputs)
    hashes = [funding.sighash(inputs, outputs, n, _mine(key)).hex() for n in range(len(inputs))]
    return {"offer": secrets.token_hex(8), "chain": NET, "raw": raw, "inputs": inputs, "sighashes": hashes,
            "what": "a lock"}


def _leg(kind, asset, amount, giver, taker):
    return {"network": NET, "kind": kind, "asset": asset, "amount": amount, "giver_key": giver.pub.hex(),
            "taker_key": taker.pub.hex(), "fee_bps": 0}


def _trade(kind, asset, amount, me, them):
    give = _leg(kind, asset, amount, me, them)
    get = _leg("coin", "", 3 * COIN, them, me)
    redeem = H.redeem_script(b"\x42" * 32, them.pub, me.pub, 1_800_000_000)
    return {"give": give, "get": get, "approved": True}, redeem


def _values(trade):
    leg = T.Leg(**{k: trade["give"][k] for k in ("network", "kind", "asset", "amount", "giver_key", "taker_key")},
                fee_bps=0, fee_to="")
    other = T.Leg(**{k: trade["get"][k] for k in ("network", "kind", "asset", "amount", "giver_key", "taker_key")},
                  fee_bps=0, fee_to="")
    return T.fund_value(leg, other)


def _fund(tmp_path, data, trade, redeem, cases):
    """Run fundMyLock once per case ({answers}); what each signed and said."""
    return run_wallet_js(tmp_path, HARNESS + """
      const out = [];
      const redeem = c.unhex(data.redeem);
      const lockAddress = await c.htlcAddress(redeem, 196);
      for (const answers of data.cases) {
        for (const list of Object.values(answers)) for (const a of list) if (a.lock === "LOCK") a.lock = lockAddress;
        out.push(await run(answers, () => w.fundMyLock(wallet, data.trade, redeem)));
      }
      out.push({fundValue: w.fundValue(data.trade.give, data.trade.get)});
      return out;""", {**data, "trade": trade, "redeem": redeem.hex(), "cases": cases})


def test_a_coin_lock_signs_only_a_send_of_exactly_the_amount_to_the_lock(tmp_path):
    me, data = _me()
    them = Key(version=PARAMS.pubkeyhash_version)
    trade, redeem = _trade("coin", "", 2 * COIN, me, them)
    fv = _values(trade)
    lock = H.p2sh_script(redeem)
    other_lock = H.p2sh_script(H.redeem_script(b"\x43" * 32, them.pub, me.pub, 1_800_000_000))
    stranger = Key(version=PARAMS.pubkeyhash_version)
    change = 50 * COIN - fv - 1_000_000
    cases = {
        "right": [(fv, lock), (change, _mine(me))],
        "less": [(fv - 1, lock), (change + 1, _mine(me))],
        "more": [(fv + COIN, lock), (change - COIN, _mine(me))],
        "another lock": [(fv, other_lock), (change, _mine(me))],
        "a stranger too": [(fv, lock), (COIN, _mine(stranger)), (change - COIN, _mine(me))],
        "the lock twice": [(fv, lock), (COIN, lock), (change - COIN, _mine(me))],
        "no lock": [(fv, _mine(stranger)), (change, _mine(me))],
    }
    got = _fund(tmp_path, data, trade, redeem,
                [{"/account/send": [_offer(me, outs)]} for outs in cases.values()])
    assert got[-1]["fundValue"] == fv
    results = dict(zip(cases, got))
    assert results["right"]["error"] == "" and len(results["right"]["signed"]) == 1, results["right"]
    for name in cases:
        if name == "right":
            continue
        assert results[name]["signed"] == [], (name, results[name])
        assert "Nothing was signed" in results[name]["error"], (name, results[name])
    assert "somebody other than the lock" in results["a stranger too"]["error"]
    assert "different amount" in results["less"]["error"]


def test_a_token_or_nft_lock_is_read_from_its_payload_not_the_nodes_lock_field(tmp_path):
    me, data = _me()
    them = Key(version=PARAMS.pubkeyhash_version)
    token, redeem = _trade("token", "7", 500, me, them)
    reserve = _values(token)
    assert reserve == T.LOCK_RESERVE
    lock = H.p2sh_script(redeem)
    pay = lambda pid, units: (0, op_return_script(encode_class_c(TK.send_payload(pid, units))))   # noqa: E731
    stranger = Key(version=PARAMS.pubkeyhash_version)
    change = 50 * COIN - reserve - 1_000_000
    shapes = {
        "right": [pay(7, 500), (reserve, lock), (change, _mine(me))],
        "more tokens": [pay(7, 501), (reserve, lock), (change, _mine(me))],
        "another token": [pay(8, 500), (reserve, lock), (change, _mine(me))],
        "to a stranger": [pay(7, 500), (reserve, _mine(stranger)), (change, _mine(me))],
        "no payload": [(reserve, lock), (change, _mine(me))],
        "two payloads": [pay(7, 500), pay(7, 500), (reserve, lock), (change, _mine(me))],
        "change twice": [pay(7, 500), (reserve, lock), (COIN, _mine(me)), (change - COIN, _mine(me))],
    }
    # The node's `lock` field always says the right address: only the bytes differ.
    got = _fund(tmp_path, data, token, redeem,
                [{"/account/trade/lock": [{**_offer(me, outs), "lock": "LOCK"}]} for outs in shapes.values()])
    results = dict(zip(shapes, got))
    assert results["right"]["error"] == "" and len(results["right"]["signed"]) == 1, results["right"]
    for name in shapes:
        if name != "right":
            assert results[name]["signed"] == [] and "Nothing was signed" in results[name]["error"], (name, results[name])

    nft, redeem = _trade("nft", PIECE, 1, me, them)
    lock = H.p2sh_script(redeem)
    piece = lambda txid: (0, op_return_script(encode_class_c(P.AnyData(data=I.Transfer(   # noqa: E731
        txid=bytes.fromhex(txid)).encode()).encode())))
    shapes = {
        "right": [piece(PIECE), (reserve, lock), (change, _mine(me))],
        "another piece": [piece("cd" * 32), (reserve, lock), (change, _mine(me))],
        "a stranger too": [piece(PIECE), (reserve, lock), (COIN, _mine(stranger)), (change - COIN, _mine(me))],
    }
    got = _fund(tmp_path / "nft", data, nft, redeem,
                [{"/account/trade/lock": [{**_offer(me, outs), "lock": "LOCK"}]} for outs in shapes.values()])
    results = dict(zip(shapes, got))
    assert results["right"]["error"] == "" and len(results["right"]["signed"]) == 1, results["right"]
    assert results["another piece"]["signed"] == [] and "the swap did not agree" in results["another piece"]["error"]
    assert results["a stranger too"]["signed"] == [] and "somebody other" in results["a stranger too"]["error"]


def test_a_doginal_lock_puts_the_coin_whole_and_its_reserve_in_the_lock(tmp_path):
    me, data = _me()
    them = Key(version=PARAMS.pubkeyhash_version)
    trade, redeem = _trade("doginal", "ef" * 32 + "i0", 1, me, them)
    reserve = _values(trade)
    lock = H.p2sh_script(redeem)
    coin = 100_000
    change = 50 * COIN - reserve - 1_000_000
    shapes = {
        "right": [(coin, lock), (reserve, lock), (change, _mine(me))],
        "less reserve": [(coin, lock), (reserve - 1, lock), (change + 1, _mine(me))],
        "coin elsewhere": [(coin, _mine(Key(version=PARAMS.pubkeyhash_version))), (reserve, lock), (change, _mine(me))],
        "swapped": [(reserve, lock), (coin, lock), (change, _mine(me))],
    }
    got = _fund(tmp_path, data, trade, redeem,
                [{"/account/trade/lock": [{**_offer(me, outs, n_in=2, first_value=coin), "lock": "LOCK"}]}
                 for outs in shapes.values()])
    results = dict(zip(shapes, got))
    assert results["right"]["error"] == "" and len(results["right"]["signed"]) == 1, results["right"]
    for name in shapes:
        if name != "right":
            assert results[name]["signed"] == [] and "Nothing was signed" in results[name]["error"], (name, results[name])


def test_a_delegated_orders_deposits_go_to_the_orders_own_key_only(tmp_path):
    me, data = _me()
    oid = "ord-" + secrets.token_hex(8)
    first = run_wallet_js(tmp_path / "a", HARNESS + """
      const k = await c.orderKey(wallet.seed, data.oid);
      return {pub: c.hex(k.pubkey)};""", {**data, "oid": oid})
    deposit = bytes([0x76, 0xA9, 0x14]) + funding.hash160(bytes.fromhex(first["pub"])) + bytes([0x88, 0xAC])
    stranger = _mine(Key(version=PARAMS.pubkeyhash_version))
    give = {"network": NET, "kind": "token", "asset": "7", "amount": 500}
    get = {"network": "doge", "kind": "coin", "asset": "", "amount": 3 * COIN}
    # fundValue of a token side on fee terms Off: the lock reserve; plus DELEGATED_FEES (wallet_js.js).
    coins_in = T.LOCK_RESERVE + 5_000_000
    token = (0, op_return_script(encode_class_c(TK.send_payload(7, 500))))
    told = {"msg_key": "00" * 32, "node": "n", "lock_fee": {}}
    change = 40 * COIN

    def case(token_outs, coin_outs):
        return {"/account/orders/delegate": [told], "/account/token/send": [_offer(me, token_outs)],
                "/account/send": [_offer(me, coin_outs)]}

    cases = {
        "right": case([token, (1_000_000, deposit), (change, _mine(me))], [(coins_in, deposit), (change, _mine(me))]),
        "tokens to a stranger": case([token, (1_000_000, stranger), (change, _mine(me))],
                                     [(coins_in, deposit), (change, _mine(me))]),
        "other tokens": case([(0, op_return_script(encode_class_c(TK.send_payload(7, 1)))), (1_000_000, deposit),
                              (change, _mine(me))], [(coins_in, deposit), (change, _mine(me))]),
        "coins short": case([token, (1_000_000, deposit), (change, _mine(me))],
                            [(coins_in - 1, deposit), (change, _mine(me))]),
        "coins to a stranger too": case([token, (1_000_000, deposit), (change, _mine(me))],
                                        [(coins_in, deposit), (COIN, stranger), (change, _mine(me))]),
    }
    got = run_wallet_js(tmp_path / "b", HARNESS + """
      const out = [];
      for (const answers of data.cases) {
        out.push(await run(answers, () => w.delegateOrder(wallet, data.oid, data.give, data.get, true)));
      }
      return out;""", {**data, "oid": oid, "give": give, "get": get, "cases": list(cases.values())})
    results = dict(zip(cases, got))
    assert results["right"]["error"] == "" and len(results["right"]["signed"]) == 2, results["right"]
    assert results["tokens to a stranger"]["signed"] == []
    assert results["other tokens"]["signed"] == []
    # The token went to the order's key; the coins beside it were refused before they were signed.
    assert len(results["coins short"]["signed"]) == 1 and "Nothing was signed" in results["coins short"]["error"]
    assert len(results["coins to a stranger too"]["signed"]) == 1
    assert "somebody other" in results["coins to a stranger too"]["error"]


def test_the_payload_scripts_are_the_ones_the_node_writes(tmp_path):
    got = run_wallet_js(tmp_path, """
      globalThis.document = {body: {dataset: {}}};
      return {token: c.hex(w.tokenSendScript("7", 1234)), big: c.hex(w.tokenSendScript("4294967295", "9007199254740993")),
              nft: c.hex(w.nftTransferScript(data.piece))};""", {"piece": PIECE})
    assert got["token"] == op_return_script(encode_class_c(TK.send_payload(7, 1234))).hex()
    assert got["big"] == op_return_script(encode_class_c(TK.send_payload(4294967295, 9007199254740993))).hex()
    assert got["nft"] == op_return_script(encode_class_c(P.AnyData(data=I.Transfer(
        txid=bytes.fromhex(PIECE)).encode()).encode())).hex()


def test_a_token20_lock_checks_the_funding_and_the_lock_steps(tmp_path):
    me, data = _me()
    them = Key(version=PARAMS.pubkeyhash_version)
    trade, redeem = _trade("token20", "frgy", 10, me, them)
    reserve = _values(trade)
    lock = H.p2sh_script(redeem)
    note = bytes([33]) + me.pub + bytes([0xAD, 0x75, 0x51])          # the note's commit script: this key's own
    commit = bytes([0xA9, 0x14]) + funding.hash160(note) + bytes([0x87])
    stranger = _mine(Key(version=PARAMS.pubkeyhash_version))

    def from_note(outputs, value):
        """A reveal-shaped offer: the note's commit coin in, signed over the note's script."""
        inputs = [{"txid": secrets.token_hex(32), "vout": 0, "value": value, "address": me.address,
                   "redeem": note.hex(), "envelope": "00"}]
        raw = build_raw_tx([(inputs[0]["txid"], 0)], outputs)
        return {"offer": secrets.token_hex(8), "chain": NET, "raw": raw, "inputs": inputs,
                "sighashes": [funding.sighash(inputs, outputs, 0, note).hex()]}

    def steps(fund_outs, lock_outs):
        return {"/account/trade/lock20": [
            {**_offer(me, [(3_000_000, commit), (40 * COIN, _mine(me))]), "lock20": "L", "step": "commit", "lock": "LOCK"},
            {**from_note([(100_000, _mine(me))], 3_000_000), "step": "reveal"},
            {**_offer(me, fund_outs, n_in=2, first_value=100_000), "step": "fund"},
            {**from_note(lock_outs, 4_000_000), "step": "lock"},
            {"step": "done", "lock_txid": "ef" * 32}]}

    good_fund = [(100_000, lock), (4_000_000, commit), (40 * COIN, _mine(me))]
    good_lock = [(100_000, lock), (reserve, lock)]
    cases = {
        "right": steps(good_fund, good_lock),
        "note to a stranger": steps([(100_000, stranger), (4_000_000, commit), (40 * COIN, _mine(me))], good_lock),
        "commit to a stranger": steps([(100_000, lock), (4_000_000, stranger), (40 * COIN, _mine(me))], good_lock),
        "short reserve": steps(good_fund, [(100_000, lock), (reserve - 1, lock)]),
        "lock skims": steps(good_fund, [(100_000, lock), (reserve - 500_000, lock), (500_000, stranger)]),
    }
    got = _fund(tmp_path, data, trade, redeem, list(cases.values()))
    results = dict(zip(cases, got))
    assert results["right"]["error"] == "" and len(results["right"]["signed"]) == 4, results["right"]
    assert len(results["note to a stranger"]["signed"]) == 2 and results["note to a stranger"]["error"]
    assert len(results["commit to a stranger"]["signed"]) == 2 and results["commit to a stranger"]["error"]
    assert len(results["short reserve"]["signed"]) == 3 and "Nothing was signed" in results["short reserve"]["error"]
    assert len(results["lock skims"]["signed"]) == 3 and "Nothing was signed" in results["lock skims"]["error"]
