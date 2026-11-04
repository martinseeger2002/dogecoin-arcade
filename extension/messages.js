/* Signing a site's text with one chain's address key, the way that chain's Core does (`signmessage`), so any node
 * checks it with `verifymessage` and any site with verify.js or verify.py, with no arcade in between.
 *
 * coins.js keeps its own signMessage unexported on purpose (issue #5, blind signing): an arcade key signs only words
 * the arcade's templates build. Those words stay the arcade's. A realtime certificate or an LLM grant signed for a
 * site would let that site be the person on the mesh or spend their credits, so text in any of those shapes, or
 * starting the way the arcade's own words start, is refused here whatever the site says it is for.
 */
import * as secp from "./vendor/noble-secp256k1.js";
import * as coins from "./coins.js";
import * as chains from "./chains.js";

export const MOST = 4000;                 // characters: what the approval window can show in full

/** "" when a site may ask for `text` to be signed on `network`; otherwise why not. */
export function refusal(text, network) {
  if (typeof text !== "string" || !text) return "there is no text to sign";
  if ([...text].length > MOST) return `a message is at most ${MOST} characters`;
  if (!chains.chain(network).magic) return `${chains.chain(network).label} messages are not signed here yet`;
  if (coins.arcadeWords(text) !== null || /^(arcade-|DogecoinArcade)/.test(text))
    return "those are words an arcade signs for itself; no site can ask for them";
  return "";
}

/** Core's compact signature over `text`, base64: 65 bytes, the first saying which key to recover (compressed). */
export async function signText(key, text, network) {
  const said = refusal(text, network);
  if (said) throw new Error(`Nothing was signed: ${said}.`);
  const sig = await secp.signAsync(await coins.messageHash(text, chains.chain(network).magic), key, {lowS: true});
  const out = new Uint8Array(65);
  out[0] = 27 + sig.recovery + 4;
  out.set(sig.toCompactRawBytes(), 1);
  return btoa(String.fromCharCode(...out));
}
