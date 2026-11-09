// For tests/test_extension.py: the proof a wallet's address carries when an arcade files it (chains.claimProof).
//   node --import ./extension/test/register.mjs extension/test/claim.mjs '{"phrase": "...", "account": "<hex>"}'
import {readFileSync} from "node:fs";
import * as keys from "../keys.js";
import * as chains from "../chains.js";

const asked = JSON.parse(process.argv[2]);
keys.setWords(readFileSync(new URL("../bip39-english.txt", import.meta.url), "utf8").split(/\s+/).filter(Boolean));
const seed = await keys.toSeed(asked.phrase);
const mine = await chains.addresses(seed);
const out = {};
for (const network of asked.networks) out[network] = {...mine[network], signature: await chains.claimProof(seed, asked.account, network)};
console.log(JSON.stringify(out));
