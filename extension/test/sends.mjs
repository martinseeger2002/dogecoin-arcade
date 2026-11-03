// For tests/test_extension.py: what sends.js makes of arcade offers, as JSON on stdout.
//   node --import ./extension/test/register.mjs extension/test/sends.mjs '{"phrase": ..., "cases": [{offer, ask, marks}]}'
import {readFileSync} from "node:fs";
import * as keys from "../keys.js";
import * as chains from "../chains.js";
import * as sends from "../sends.js";

const asked = JSON.parse(readFileSync(process.argv[2], "utf8"));
keys.setWords(readFileSync(new URL("../bip39-english.txt", import.meta.url), "utf8").split(/\s+/).filter(Boolean));
const seed = await keys.toSeed(asked.phrase);
const out = {cases: [], sats: ["1.5", "0.00000001", "7"].map((a) => sends.satsOf(a))};
for (const c of asked.cases) {
  try {
    const got = await sends.check(c.offer, await chains.keysOn(seed, c.ask.chain), c.ask, c.marks);
    out.cases.push({hashes: got.hashes, view: got.view});
  } catch (e) { out.cases.push({error: e.message}); }
}
console.log(JSON.stringify(out));
