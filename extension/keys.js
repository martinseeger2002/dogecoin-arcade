/* The Arcade Wallet's keys: the same words, seed and account key as the arcade's own web wallet.
 *
 * This is signin.js's derivation (arcade/web/templates/signin.js) and wallet_js.js's sealed blob, kept to the same
 * bytes: tests/test_extension.py derives from the same words here and in arcade/seed.py and compares. Only
 * crypto.subtle is used, so it runs in the extension's service worker, its pages, and Node for the tests.
 */

const enc = new TextEncoder();
export const hex = (bytes) => [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join("");
export const unhex = (text) => new Uint8Array((String(text).match(/../g) || []).map((p) => parseInt(p, 16)));

/* --- the word list ------------------------------------------------------------------------------------------- */

let WORDS = null;
export function setWords(list) {
  if (list.length !== 2048) throw new Error("the word list is damaged");
  WORDS = list;
}
export async function words() {
  if (WORDS === null) {
    const text = await (await fetch(new URL("./bip39-english.txt", import.meta.url))).text();
    setWords(text.split(/\s+/).filter(Boolean));
  }
  return WORDS;
}

/* --- BIP39 ---------------------------------------------------------------------------------------------------- */

export async function generate(strength = 128) {
  const list = await words();
  const entropy = crypto.getRandomValues(new Uint8Array(strength / 8));
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", entropy));
  let bits = "";
  for (const byte of entropy) bits += byte.toString(2).padStart(8, "0");
  bits += digest[0].toString(2).padStart(8, "0").slice(0, entropy.length * 8 / 32);
  const out = [];
  for (let i = 0; i < bits.length; i += 11) out.push(list[parseInt(bits.slice(i, i + 11), 2)]);
  return out.join(" ");
}

export function normalise(phrase) {
  return (phrase || "").normalize("NFKD").toLowerCase().split(/\s+/).filter(Boolean).join(" ");
}

/** "" when the phrase is good; otherwise what is wrong with it. */
export async function complaint(phrase) {
  const list = await words();
  const parts = normalise(phrase).split(" ").filter(Boolean);
  if (!parts.length) return "Write the words in, separated by spaces.";
  if (![12, 15, 18, 21, 24].includes(parts.length)) return `A seed phrase is 12 to 24 words; this is ${parts.length}.`;
  const unknown = parts.filter((w) => list.indexOf(w) < 0);
  if (unknown.length) return `Not a word from the list: ${unknown.slice(0, 3).join(", ")}.`;
  let bits = "";
  for (const w of parts) bits += list.indexOf(w).toString(2).padStart(11, "0");
  const checksumBits = bits.length / 33, entropyBits = bits.length - checksumBits;
  const entropy = new Uint8Array(entropyBits / 8);
  for (let i = 0; i < entropy.length; i++) entropy[i] = parseInt(bits.slice(i * 8, i * 8 + 8), 2);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", entropy));
  if (bits.slice(entropyBits) !== digest[0].toString(2).padStart(8, "0").slice(0, checksumBits))
    return "Those are real words, but not in an order a seed phrase can have. Check each one.";
  return "";
}

export async function toSeed(phrase, passphrase = "") {
  const said = await complaint(phrase);
  if (said) throw new Error(said);
  const key = await crypto.subtle.importKey("raw", enc.encode(normalise(phrase)), "PBKDF2", false, ["deriveBits"]);
  return new Uint8Array(await crypto.subtle.deriveBits(
    {name: "PBKDF2", salt: enc.encode("mnemonic" + (passphrase || "").normalize("NFKD")), iterations: 2048,
     hash: "SHA-512"}, key, 512));
}

/* --- the hardened half of BIP32, and the account key --------------------------------------------------------- */

const HARDENED = 0x80000000;
export const ARCADE_PURPOSE = 24946;
export const LOGIN_BRANCH = [ARCADE_PURPOSE, 1, 0];

async function hmac512(key, data) {
  const k = await crypto.subtle.importKey("raw", key, {name: "HMAC", hash: "SHA-512"}, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", k, data));
}

export async function derive(seed, indexes) {
  let digest = await hmac512(enc.encode("Bitcoin seed"), seed);
  let key = digest.slice(0, 32), chain = digest.slice(32);
  for (const raw of indexes) {
    const data = new Uint8Array(37);
    data.set(key, 1);
    new DataView(data.buffer).setUint32(33, (raw >= HARDENED ? raw : raw + HARDENED) >>> 0);
    digest = await hmac512(chain, data);
    key = digest.slice(0, 32);
    chain = digest.slice(32);
  }
  return key;
}

const PKCS8_ED25519 = new Uint8Array([0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70,
                                      0x04, 0x22, 0x04, 0x20]);

function b64urlBytes(text) {
  const padded = text.replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded + "=".repeat((4 - padded.length % 4) % 4));
  return Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
}

/** The account key: {pubkey (hex), sign(bytes) -> signature bytes}. */
export async function accountKey(seed) {
  const raw = await derive(seed, LOGIN_BRANCH);
  const pkcs8 = new Uint8Array(PKCS8_ED25519.length + 32);
  pkcs8.set(PKCS8_ED25519);
  pkcs8.set(raw, PKCS8_ED25519.length);
  const priv = await crypto.subtle.importKey("pkcs8", pkcs8, {name: "Ed25519"}, true, ["sign"]);
  const jwk = await crypto.subtle.exportKey("jwk", priv);
  return {sign: async (data) => new Uint8Array(await crypto.subtle.sign({name: "Ed25519"}, priv, data)),
          pubkey: hex(b64urlBytes(jwk.x))};
}

/* --- signing in ----------------------------------------------------------------------------------------------- */

/** Exactly the bytes an arcade checks (arcade/accounts.py login_message). */
export function loginMessage(origin, nonce) {
  return enc.encode(`DogecoinArcade login\n${origin}\n${nonce}`);
}

//: An arcade's nonce is 64 hex characters; anything else could carry a second line into the message.
export const NONCE = /^[0-9a-f]{32,128}$/;

/* --- the sealed words (the arcade's blob: AES-256-GCM under PBKDF2-HMAC-SHA512) ------------------------------ */

export const KDF_ITERATIONS = 600000;
export const MIN_PASSWORD = 12;

async function keyFrom(password, salt, iterations) {
  const base = await crypto.subtle.importKey("raw", enc.encode(password), "PBKDF2", false, ["deriveKey"]);
  return crypto.subtle.deriveKey({name: "PBKDF2", salt, iterations, hash: "SHA-512"}, base,
                                 {name: "AES-GCM", length: 256}, false, ["encrypt", "decrypt"]);
}

export async function seal(phrase, password) {
  if ([...String(password || "")].length < MIN_PASSWORD)
    throw new Error(`a password of at least ${MIN_PASSWORD} characters, please`);
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const key = await keyFrom(password, salt, KDF_ITERATIONS);
  const sealed = await crypto.subtle.encrypt({name: "AES-GCM", iv: nonce}, key, enc.encode(normalise(phrase)));
  return {kdf: "PBKDF2-SHA512", iterations: KDF_ITERATIONS, salt: hex(salt), nonce: hex(nonce), sealed: hex(sealed)};
}

export async function open(blob, password) {
  if (!blob || blob.kdf !== "PBKDF2-SHA512") throw new Error("this wallet is sealed in a way this does not know");
  const key = await keyFrom(password, unhex(blob.salt), blob.iterations);
  try {
    return new TextDecoder().decode(
      await crypto.subtle.decrypt({name: "AES-GCM", iv: unhex(blob.nonce)}, key, unhex(blob.sealed)));
  } catch (e) {
    throw new Error("that password does not open this wallet.");
  }
}
