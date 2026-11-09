// For tests/test_dgb_staging_regtest.py (2026-11-09, security review): the browser's half of the DigiByte staging
// checks, from the real coins.js, so a real DigiByte Core can be asked the same questions.
//   node --experimental-default-type=module --import ./extension/test/register.mjs tests/js/dgb_staging.mjs '<json>'
// asked.mode "derive": {seed: hex, networks: [...], v: {pubkeyhash, scripthash, bech32Hrp, wif}, indexes: n,
//                       chains: [restore_kit.chains() rows]}
//   -> {wallets: {network: [electrumWallet(i) with each kind's key and pubkey]}, kits: [restoreKit(row)]}
// asked.mode "sign": {offers: [{offer, keys: {key, pubkey, address, network, version, nested}}]}
//   -> [{pays, hashes, fee, change, signatures}]: verifyOffer, then signInput over each hash, as wallet_js signOffer does.
import * as coins from "../../extension/coins.js";

const asked = JSON.parse(process.argv[2]);
const hex = (b) => Buffer.from(b).toString("hex");
const plain = (value) => JSON.parse(JSON.stringify(value, (k, v) => typeof v === "bigint" ? v.toString() : v));

if (asked.mode === "derive") {
  const seed = coins.unhex(asked.seed);
  const wallets = {};
  for (const network of asked.networks) {
    wallets[network] = [];
    for (let i = 0; i < asked.indexes; i++) {
      const w = await coins.electrumWallet(seed, network, asked.v, i);
      const keys = [await coins.legacyKey(seed, network, i), await coins.p2shSegwitKey(seed, network, i),
                    await coins.nativeSegwitKey(seed, network, i)];
      const own = await coins.coinKey(seed, network, i);
      w.coinKey = {key: hex(own.key), pubkey: hex(own.pubkey)};
      for (const [kind, k] of [["legacy", keys[0]], ["p2shSegwit", keys[1]], ["nativeSegwit", keys[2]]]) {
        w[kind].key = hex(k.key);
        w[kind].pubkey = hex(k.pubkey);
      }
      wallets[network].push(w);
    }
  }
  const kits = [];
  for (const chain of asked.chains || []) kits.push(await coins.restoreKit(seed, chain, chain.since));
  console.log(JSON.stringify(plain({wallets, kits})));
} else if (asked.mode === "sign") {
  const out = [];
  for (const one of asked.offers) {
    const keys = {...one.keys, key: coins.unhex(one.keys.key), pubkey: coins.unhex(one.keys.pubkey)};
    try {
      const shown = await coins.verifyOffer(one.offer, keys);
      const signatures = [];
      for (const sighash of shown.hashes) signatures.push(hex(await coins.signInput(keys.key, coins.unhex(sighash))));
      out.push(plain({pays: shown.pays, hashes: shown.hashes, fee: shown.fee, change: shown.change, signatures}));
    } catch (e) {
      out.push({refused: String(e.message)});
    }
  }
  console.log(JSON.stringify(out));
}
