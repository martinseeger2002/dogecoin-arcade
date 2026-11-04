// For tests/test_extension.py: what sitetx.js makes of sites' transactions, as JSON on stdout.
//   node --import ./extension/test/register.mjs extension/test/sitetx.mjs cases.json
import {readFileSync} from "node:fs";
import * as keys from "../keys.js";
import * as coins from "../coins.js";
import * as chains from "../chains.js";
import * as sitetx from "../sitetx.js";

const asked = JSON.parse(readFileSync(process.argv[2], "utf8"));
keys.setWords(readFileSync(new URL("../bip39-english.txt", import.meta.url), "utf8").split(/\s+/).filter(Boolean));
const seed = await keys.toSeed(asked.phrase);
const out = [];
for (const c of asked.cases) {
  try {
    const k = await chains.keysOn(seed, c.params.chain);
    const planned = await sitetx.plan(c.params, c.own, c.marks, k, {send: !!c.send});
    out.push({raw: await sitetx.finish(planned, k), digests: planned.sign.map((s) => coins.hex(s.digest)),
              view: planned.view});
  } catch (e) { out.push({error: e.message}); }
}
console.log(JSON.stringify(out));
