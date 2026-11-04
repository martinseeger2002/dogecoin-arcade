// For tests/test_extension.py: every chain's address for some words, as JSON on stdout.
//   node --import ./extension/test/register.mjs extension/test/coins.mjs '{"phrases": [...]}'
import {readFileSync} from "node:fs";
import * as keys from "../keys.js";
import * as chains from "../chains.js";

const asked = JSON.parse(process.argv[2]);
keys.setWords(readFileSync(new URL("../bip39-english.txt", import.meta.url), "utf8").split(/\s+/).filter(Boolean));
const out = [];
for (const phrase of asked.phrases) out.push(await chains.addresses(await keys.toSeed(phrase)));
console.log(JSON.stringify({addresses: out, chains: chains.CHAINS, coins: [chains.coinsOf(0), chains.coinsOf(123456789),
                            chains.coinsOf(100000000), chains.coinsOf(1)]}));
