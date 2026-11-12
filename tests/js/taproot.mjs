// For tests/test_taproot_wallet.py: the wallet's Taproot (coins.js, 2026-11-07) against BIP-341's and BIP-86's vectors and
// against the arcade's own offers, from the real coins.js (extension/coins.js, the same bytes).
//   node --experimental-default-type=module --import ./extension/test/register.mjs tests/js/taproot.mjs '<json>'
// asked: {tweaks: [{internal, root}], spend: {raw, spent: [{script, value}], index, secret, aux},
//         bip86: {seed, paths: [[indexes]]}, offers: [{offer, keys: {key, pubkey, address, version, network}}]}
import * as coins from "../../extension/coins.js";

const asked = JSON.parse(process.argv[2]);
const out = {tweaks: [], bip86: [], offers: []};

for (const one of asked.tweaks) {
  const got = await coins.taprootTweak(coins.unhex(one.internal), coins.unhex(one.root || ""));
  out.tweaks.push(coins.hex(got.key));
}

const s = asked.spend;
const tx = coins.parseTx(s.raw);
const spent = s.spent.map((u) => ({value: BigInt(u.value), script: coins.unhex(u.script)}));
const digest = await coins.sighashTaproot(tx, s.index, spent);
out.spend = {digest: coins.hex(digest),
             sig: coins.hex(await coins.signSchnorr(coins.unhex(s.secret), digest, false, coins.unhex(s.aux)))};

const seed = coins.unhex(asked.bip86.seed);
for (const path of asked.bip86.paths) {
  const node = await coins.derive(seed, path);
  const pubkey = coins.publicKey(node.key);
  out.bip86.push({pubkey: coins.hex(pubkey), address: await coins.taprootAddress(pubkey, "bc")});
}
out.kinds = {};
for (const network of ["ltc", "ltctest", "dgb", "dgbtest"]) {
  const k = await coins.taprootKey(seed, network);
  out.kinds[network] = coins.hex(k.pubkey);
}

for (const {offer, keys: k} of asked.offers) {
  const keys = {...k, key: coins.unhex(k.key), pubkey: coins.unhex(k.pubkey)};
  try {
    const shown = await coins.verifyOffer(offer, keys);
    const sigs = [];
    for (const [n, h] of shown.hashes.entries()) {
      sigs.push(shown.signers[n] ? coins.hex(await coins.signSchnorr(keys.key, coins.unhex(h), shown.signers[n] === "key"))
                                 : null);
    }
    out.offers.push({hashes: shown.hashes, signers: shown.signers, sigs, change: shown.change, fee: shown.fee,
                     pays: shown.pays.filter((p) => !p.mine).map((p) => [p.to, p.value]), says: shown.says});
  } catch (e) {
    out.offers.push({error: String(e && e.message || e)});
  }
}
console.log(JSON.stringify(out));
