// For tests/test_extension.py: messages signed and checked here, as JSON on stdout.
//   node --import ./extension/test/register.mjs extension/test/messages.mjs '{"key": hex, "text": ..., "phrase": ...}'
import {readFileSync} from "node:fs";
import * as keys from "../keys.js";
import * as coins from "../coins.js";
import * as chains from "../chains.js";
import * as messages from "../messages.js";
import * as verify from "../verify.js";

const asked = JSON.parse(process.argv[2]);
keys.setWords(readFileSync(new URL("../bip39-english.txt", import.meta.url), "utf8").split(/\s+/).filter(Boolean));
const out = {probe: {}, mine: {}, refused: {}, verified: {}, tables: {chains: chains.CHAINS, verify: verify.CHAINS}};
for (const network of Object.keys(verify.CHAINS))
  out.probe[network] = await messages.signText(coins.unhex(asked.key), asked.text, network);
const s = await keys.toSeed(asked.phrase);
for (const network of Object.keys(chains.CHAINS)) {
  const k = await chains.keysOn(s, network);
  try {
    const signature = await messages.signText(k.key, asked.text, network);
    out.mine[network] = {address: k.address, signature};
    out.verified[network] = {
      right: await verify.verifyMessage({chain: network, address: k.address, message: asked.text, signature}),
      otherText: await verify.verifyMessage({chain: network, address: k.address, message: asked.text + ".", signature}),
      otherChain: await verify.verifyMessage({chain: network === "doge" ? "main" : "doge", address: k.address,
                                              message: asked.text, signature}),
    };
  } catch (e) { out.refused[network] = e.message; }
}
for (const text of asked.arcadeWords) out.refused[text] = messages.refusal(text, "main");
const account = await keys.accountKey(s);
const nonce = "ab".repeat(32), origin = "https://market.example";
const signature = keys.hex(await account.sign(keys.loginMessage(origin, nonce)));
out.signIn = {pubkey: account.pubkey, signature, nonce, origin,
              right: await verify.verifySignIn({origin, nonce, pubkey: account.pubkey, signature}),
              otherOrigin: await verify.verifySignIn({origin: "https://evil.example", nonce, pubkey: account.pubkey, signature})};
console.log(JSON.stringify(out));
