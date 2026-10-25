"""The mainnet review is decided in the browser, and covers buys, shop trades and trade legs (2026-10-25, security
review item 8).

`isMainnet` used to read the node's /account `mainnet` flag, so a node that called Dogecoin `mainnet: false` skipped
the review of every real-coin transaction. It is now the network's own name (config.NETWORKS `real_coins`), and a name
the wallet does not know counts as mainnet. `buy`, the shop's half (`shopDoor` accept) and `tradeLeg` signed without
any review at all; on mainnet each now goes through `arcadeMainnetReview` with what the transaction's bytes say. Test
chains are unchanged. Run in Node against the real wallet.js and coins.js.
"""

import pathlib
import secrets
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_htlc import Key                                                # noqa: E402
from test_trade_tab import run_wallet_js                                 # noqa: E402

from arcade import encoding, funding, inscriptions as I, payload as P  # noqa: E402
from arcade.config import NETWORKS                                      # noqa: E402
from arcade.script import b58check_encode                               # noqa: E402
from arcade.txbuild import build_raw_tx                                 # noqa: E402

COIN = 100_000_000
NODE_WORDS = "a gift of nothing at all, says the node"

#: A wallet with one key on `doge` and `test`; a node that answers `answers[url]` in order and says every chain is
#: `mainnet: false`; a review that records what it was shown and answers `data.yes`.
HARNESS = """
  globalThis.document = {body: {dataset: {}}};
  const key = c.unhex(data.priv), pubkey = c.unhex(data.pub);
  const on = {};
  for (const [net, k] of Object.entries(data.keys)) on[net] = {network: net, version: k.version, key, pubkey, address: k.address};
  const wallet = {pubkey: "tester", seed: new Uint8Array(64), on};
  const run = async (answers, yes, fn) => {
    const asked = [], reviews = [], left = JSON.parse(JSON.stringify(answers));
    globalThis.arcadeMainnetReview = async (costs) => { reviews.push(costs); return yes; };
    globalThis.fetch = async (url, opts) => {
      asked.push(url);
      const reply = (obj, status = 200) => ({ok: status < 400, status, json: async () => obj});
      if (url === "/account") return reply({chains: Object.keys(on).map((n) => ({network: n, mainnet: false}))});
      const next = (left[url] || []).shift();
      if (!next) return reply({ok: true, txid: "cd".repeat(32), seal_to: "00", stamp: 0, order: {}});
      return reply(next);
    };
    try { await fn(); return {asked, reviews, error: ""}; }
    catch (e) { return {asked, reviews, error: String(e.message || e)}; }
  };
"""


def _keys():
    key = Key()
    keys = {net: {"version": NETWORKS[net].pubkeyhash_version,
                  "address": b58check_encode(NETWORKS[net].pubkeyhash_version, funding.hash160(key.pub))}
            for net in ("doge", "test")}
    return key, {"priv": key.secret.to_bytes(32, "big").hex(), "pub": key.pub.hex(), "keys": keys}


def _p2pkh(pub: bytes) -> bytes:
    return bytes([0x76, 0xA9, 0x14]) + funding.hash160(pub) + bytes([0x88, 0xAC])


def _cosigned(key, data, net, theirs_value, outputs):
    """A transaction with the other side's coin first (theirs, not signed here) and this key's after it."""
    inputs = [{"txid": secrets.token_hex(32), "vout": 0, "value": theirs_value, "address": "somebody"},
              {"txid": secrets.token_hex(32), "vout": 1, "value": 20 * COIN, "address": data["keys"][net]["address"]}]
    raw = build_raw_tx([(c["txid"], c["vout"]) for c in inputs], outputs)
    return {"chain": net, "raw": raw, "inputs": inputs, "signed_from": 1, "what": NODE_WORDS,
            "sighashes": [funding.sighash(inputs, outputs, 1, _p2pkh(key.pub)).hex()]}


def test_the_mainnet_names_are_config_s_real_coin_chains(tmp_path):
    got = run_wallet_js(tmp_path, "return {main: [...w.MAINNETS], test: [...w.TESTNETS]};", {})
    assert set(got["main"]) == {n for n, p in NETWORKS.items() if p.real_coins}
    # Every chain this software runs is named one way or the other: a new one has to be put in a list.
    assert set(got["test"]) == {n for n, p in NETWORKS.items() if not p.real_coins}


def test_is_mainnet_is_the_name_not_the_nodes_flag(tmp_path):
    key, data = _keys()
    outputs = [(COIN, _p2pkh(Key().pub)), (18 * COIN, _p2pkh(key.pub))]
    inputs = [{"txid": secrets.token_hex(32), "vout": 0, "value": 20 * COIN, "address": data["keys"]["doge"]["address"]}]
    raw = build_raw_tx([(inputs[0]["txid"], 0)], outputs)
    send = {"offer": "o1", "chain": "doge", "raw": raw, "inputs": inputs, "what": NODE_WORDS,
            "sighashes": [funding.sighash(inputs, outputs, 0, _p2pkh(key.pub)).hex()]}
    got = run_wallet_js(tmp_path, HARNESS + """
      const names = {};
      for (const n of ["main", "doge", "ltc", "bel", "lky", "kot", "wjk", "test", "regtest", "ltctest", "zzz", ""])
        names[n] = w.isMainnet(n);
      const no = await run({}, false, () => w.confirm(wallet, data.send));
      const yes = await run({}, true, () => w.confirm(wallet, data.send));
      return {names, no, yes};""", {**data, "send": send})
    assert got["names"] == {"main": True, "doge": True, "ltc": True, "bel": True, "lky": True, "kot": True,
                            "wjk": True, "test": False, "regtest": False, "ltctest": False, "zzz": True, "": True}
    # The node said doge is mainnet: false; the review is shown all the same, and a no signs nothing.
    assert len(got["no"]["reviews"]) == 1 and "/account/sign" not in got["no"]["asked"]
    assert "not confirmed" in got["no"]["error"]
    assert "/account/sign" in got["yes"]["asked"] and got["yes"]["error"] == ""
    shown = got["yes"]["reviews"][0]
    assert shown["chain"] == "doge" and shown["total"] == 2 * COIN
    assert NODE_WORDS not in shown["what"]
    assert [p["value"] for p in shown["to"]] == [COIN]


def test_a_buy_on_mainnet_is_reviewed_from_its_bytes_and_on_a_test_chain_is_not(tmp_path):
    key, data = _keys()
    seller = Key()
    price = 3 * COIN
    cases = {}
    for net in ("doge", "test"):
        outputs = [(price + 4 * COIN, _p2pkh(seller.pub)), (20 * COIN - price - 1_000_000, _p2pkh(key.pub))]
        cases[net] = {**_cosigned(key, data, net, 4 * COIN, outputs), "listing": "L1"}
    got = run_wallet_js(tmp_path, HARNESS + """
      const out = {};
      out.doge_no = await run({}, false, () => w.buy(wallet, data.cases.doge));
      out.doge_yes = await run({}, true, () => w.buy(wallet, data.cases.doge));
      out.test = await run({}, false, () => w.buy(wallet, data.cases.test));
      return out;""", {**data, "cases": cases})
    assert len(got["doge_no"]["reviews"]) == 1 and "/account/buy/sign" not in got["doge_no"]["asked"]
    assert "/account/buy/sign" in got["doge_yes"]["asked"]
    shown = got["doge_yes"]["reviews"][0]
    # What leaves this wallet: its own coin less its change -- the price and the fee -- not the seller's coin too.
    assert shown["total"] == price + 1_000_000
    assert shown["to"] == [{"to": b58check_encode(NETWORKS["doge"].pubkeyhash_version, funding.hash160(seller.pub)),
                            "value": price + 4 * COIN}]
    assert NODE_WORDS not in shown["what"]
    # A test chain: as before, no review.
    assert got["test"]["reviews"] == [] and "/account/buy/sign" in got["test"]["asked"]


def test_a_shops_half_on_mainnet_is_reviewed_before_it_is_signed(tmp_path):
    key, data = _keys()
    shopkeeper = Key()
    halves = {net: _cosigned(key, data, net, 1_000_000, [(2 * COIN, _p2pkh(shopkeeper.pub)),
                                                          (18 * COIN - 1_000_000, _p2pkh(key.pub))])
              for net in ("doge", "test")}
    got = run_wallet_js(tmp_path, HARNESS + """
      const out = {};
      for (const [name, net, yes] of [["doge_no", "doge", false], ["doge_yes", "doge", true], ["test", "test", false]])
        out[name] = await run({"/account/shop": [data.halves[net]]}, yes,
                              () => w.shopDoor(wallet, "shop1", {op: "accept", offer: "x"}));
      return out;""", {**data, "halves": halves})
    assert len(got["doge_no"]["reviews"]) == 1 and "/account/shop/sign" not in got["doge_no"]["asked"]
    assert "not confirmed" in got["doge_no"]["error"]
    assert "/account/shop/sign" in got["doge_yes"]["asked"]
    assert got["doge_yes"]["reviews"][0]["to"][0]["value"] == 2 * COIN
    assert got["test"]["reviews"] == [] and "/account/shop/sign" in got["test"]["asked"]


def test_a_trade_leg_on_mainnet_is_reviewed_before_either_signature(tmp_path):
    key, data = _keys()
    legs = {}
    for net in ("doge", "test"):
        mine = data["keys"][net]["address"]
        held = [{"txid": "%064x" % 7, "vout": 0, "value": 4 * COIN, "address": mine},
                {"txid": "%064x" % 9, "vout": 1, "value": 1 * COIN, "address": mine}]
        body = I.Swap(give=I.Leg(I.LEG_INSCRIPTION, txid=bytes.fromhex("ab" * 32)),
                      take=I.Leg(I.LEG_COINS, amount=COIN)).encode()
        leg = funding.build_leg(NETWORKS[net], mine, held[0], coins=COIN, rate=100_000,
                                payload=encoding.encode_class_c(P.AnyData(data=body).encode()), coin=held[1],
                                what=NODE_WORDS)
        legs[net] = {"chain": net, **leg.as_json()}
    got = run_wallet_js(tmp_path, HARNESS + """
      const out = {};
      for (const [name, net, yes] of [["doge_no", "doge", false], ["doge_yes", "doge", true], ["test", "test", false]])
        out[name] = await run({"/account/trade/leg": [data.legs[net]]}, yes,
                              () => w.tradeLeg(wallet, {chain: net, give: {}, take: {}, buyer: "b"}));
      return out;""", {**data, "legs": legs})
    for name in ("doge_no", "doge_yes", "test"):
        assert "/account/trade/leg" in got[name]["asked"], (name, got[name])
    assert len(got["doge_no"]["reviews"]) == 1 and "/account/trade/leg/sign" not in got["doge_no"]["asked"]
    assert "Nothing was signed" in got["doge_no"]["error"]
    assert "/account/trade/leg/sign" in got["doge_yes"]["asked"], got["doge_yes"]
    shown = got["doge_yes"]["reviews"][0]
    assert shown["leg"] is True and "takes 1.00000000 coins" in shown["what"] and NODE_WORDS not in shown["what"]
    assert got["test"]["reviews"] == [] and "/account/trade/leg/sign" in got["test"]["asked"]
