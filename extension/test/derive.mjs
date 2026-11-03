// For tests/test_extension.py: what keys.js makes of some words, as JSON on stdout.
//   node --experimental-default-type=module extension/test/derive.mjs '{"phrases": [...], "origin": "...", "nonce": "..."}'
import {readFileSync} from "node:fs";
import * as keys from "../keys.js";

const asked = JSON.parse(process.argv[2]);
keys.setWords(readFileSync(new URL("../bip39-english.txt", import.meta.url), "utf8").split(/\s+/).filter(Boolean));
const out = [];
for (const phrase of asked.phrases) {
  const key = await keys.accountKey(await keys.toSeed(phrase));
  const signature = keys.hex(await key.sign(keys.loginMessage(asked.origin, asked.nonce)));
  const blob = await keys.seal(phrase, "a long enough password");
  out.push({pubkey: key.pubkey, signature, reopened: await keys.open(blob, "a long enough password"),
            complaint: await keys.complaint(phrase)});
}
out.push({badChecksum: await keys.complaint(asked.phrases[0].split(" ").reverse().join(" "))});
console.log(JSON.stringify(out));
