/* Checking what an Arcade Wallet signed, for any site. Nothing here asks a server.
 *
 *   import {verifySignIn, verifyMessage} from "./verify.js";
 *   verifySignIn({origin, nonce, pubkey, signature})        // arcade.request({method: "arcade_signIn", params: {nonce}})
 *   verifyMessage({chain, address, message, signature})     // arcade.request({method: "arcade_signMessage", ...})
 *
 * Copy this file with vendor/noble-secp256k1.js and vendor/hashes/ (or point the two imports at @noble/secp256k1 2.x
 * and @noble/hashes). verify.py is the same in Python. A sign-in is the arcade's login message, signed with the
 * account's Ed25519 key: `DogecoinArcade login\n<your origin>\n<your nonce>`. Issue the nonce yourself (32-128 hex),
 * use it once, and check the origin is yours.
 */
import * as secp from "./vendor/noble-secp256k1.js";
import {ripemd160} from "./vendor/hashes/ripemd160.js";

const enc = new TextEncoder();
const unhex = (t) => new Uint8Array((String(t).match(/../g) || []).map((p) => parseInt(p, 16)));

//: Each chain's P2PKH version byte and its Core's message words (chains.js; a test holds the two equal).
export const CHAINS = {
  main: [56, "Pepecoin Signed Message:\n"], test: [113, "Pepecoin Signed Message:\n"],
  regtest: [111, "Pepecoin Signed Message:\n"], doge: [30, "Dogecoin Signed Message:\n"],
  ltc: [48, "Litecoin Signed Message:\n"], ltctest: [111, "Litecoin Signed Message:\n"],
  bel: [25, "Bells Signed Message:\n"], lky: [47, "Luckycoin Signed Message:\n"],
};

/** Is `signature` (hex) the account `pubkey`'s (hex) sign-in to `origin` with `nonce`? */
export async function verifySignIn({origin, nonce, pubkey, signature}) {
  if (!/^[0-9a-f]{32,128}$/.test(String(nonce))) return false;
  try {
    const key = await crypto.subtle.importKey("raw", unhex(pubkey), {name: "Ed25519"}, false, ["verify"]);
    return await crypto.subtle.verify({name: "Ed25519"}, key, unhex(signature),
                                      enc.encode(`DogecoinArcade login\n${origin}\n${nonce}`));
  } catch (e) { return false; }
}

const sha256 = async (b) => new Uint8Array(await crypto.subtle.digest("SHA-256", b));
const size = (n) => n < 0xfd ? [n] : [0xfd, n & 0xff, n >> 8];

async function digest(message, magic) {
  const m = enc.encode(magic), b = enc.encode(message);
  return sha256(await sha256(new Uint8Array([...size(m.length), ...m, ...size(b.length), ...b])));
}

const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
function unbase58(text) {
  let n = 0n;
  for (const ch of text) {
    const at = B58.indexOf(ch);
    if (at < 0) return null;
    n = n * 58n + BigInt(at);
  }
  const out = [];
  while (n > 0n) { out.unshift(Number(n & 255n)); n >>= 8n; }
  for (const ch of text) { if (ch === "1") out.unshift(0); else break; }
  return new Uint8Array(out);
}

/** Did the key of `address` (a P2PKH address on `chain`) sign `message`? `signature` is the base64 Core gives. */
export async function verifyMessage({chain, address, message, signature}) {
  const known = CHAINS[chain];
  const whole = unbase58(String(address || ""));
  if (!known || !whole || whole.length !== 25 || whole[0] !== known[0]) return false;
  const check = await sha256(await sha256(whole.slice(0, 21)));
  if (check.slice(0, 4).some((b, i) => b !== whole[21 + i])) return false;
  try {
    const raw = Uint8Array.from(atob(String(signature)), (c) => c.charCodeAt(0));
    if (raw.length !== 65 || raw[0] < 27 || raw[0] > 34) return false;
    const flag = raw[0] - 27;
    const point = secp.Signature.fromCompact(raw.slice(1)).addRecoveryBit(flag & 3)
      .recoverPublicKey(await digest(String(message), known[1]));
    const hash = ripemd160(await sha256(point.toRawBytes(flag >= 4)));
    return hash.every((b, i) => b === whole[1 + i]);
  } catch (e) { return false; }
}
