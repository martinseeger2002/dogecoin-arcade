// For tests/test_restore_kit.py: the restore kit, from the real coins.js AND from the offline page's inlined bundle.
//   node --experimental-default-type=module --import ./extension/test/register.mjs tests/js/restore_kit.mjs '<json>'
// asked: {bundle: path of arcade.restore_kit.bundle()'s output, words: the BIP39 list's path, phrase, chains,
//         descriptors: [strings to checksum], bip32: [{seed: hex, path: [indexes]}], bad: a phrase with a bad checksum}
import {readFileSync} from "node:fs";
import {pbkdf2Sync} from "node:crypto";
import * as coins from "../../extension/coins.js";

const asked = JSON.parse(process.argv[2]);
const M = {};
const AsyncFunction = (async () => {}).constructor;
await new AsyncFunction("M", readFileSync(asked.bundle, "utf8"))(M);
const bundled = M["/coins.js"], kit = M["/restore-kit.js"];
const words = readFileSync(asked.words, "utf8").split(/\s+/).filter(Boolean);
const hex = (b) => Buffer.from(b).toString("hex");

const seed = await kit.seedOf(asked.phrase, "", words);
const plain = new Uint8Array(pbkdf2Sync(asked.phrase, "mnemonic", 2048, 64, "sha512"));
let refused = "";
try { await kit.seedOf(asked.bad, "", words); } catch (e) { refused = String(e.message); }

const out = {seed: hex(seed), seedPlain: hex(plain), refused,
             checksums: asked.descriptors.map((d) => coins.descriptorChecksum(d)),
             bundledChecksums: asked.descriptors.map((d) => bundled.descriptorChecksum(d)),
             bip32: [], kits: [], bundledKits: [], drawn: typeof kit.drawKits};
for (const one of asked.bip32) {
  const node = await coins.xnode(coins.unhex(one.seed), one.path);
  out.bip32.push({xprv: await coins.extendedKey(node, true), xpub: await coins.extendedKey(node, false)});
}
for (const chain of asked.chains) {
  out.kits.push(await coins.restoreKit(plain, chain, chain.since));
  out.bundledKits.push(await bundled.restoreKit(seed, chain, chain.since));
}
out.whole = (await kit.kitsFor(seed, asked.chains.slice(0, 1), true))[0].kit.since;
console.log(JSON.stringify(out));
