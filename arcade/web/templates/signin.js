/* The half of the login that the node must never be able to do.
 *
 * Everything here runs in the browser and stays there: the twelve words,
 * the seed they make, the keys derived from it and the password
 * that encrypts them. What crosses the wire is a public key and a
 * signature over a nonce the node just issued -- neither of which is worth
 * stealing, which is the point of doing it this way (docs/multi-user.md
 * §2).
 *
 * No library is loaded. Every primitive is one `crypto.subtle` already
 * implements:
 *
 *   BIP39 checksum   SHA-256
 *   BIP39 seed       PBKDF2-HMAC-SHA512, 2048, salt "mnemonic"
 *   BIP32 hardened   HMAC-SHA512
 *   account key      Ed25519, raw public key, 64-byte signature
 *   at rest          AES-256-GCM under PBKDF2-HMAC-SHA512
 *
 * They match `arcade/seed.py` and `arcade/accounts.py` byte for byte, and
 * the tests check that they do by deriving on both sides from the same
 * words. A vendored crypto library would be a third thing to keep in step
 * and a fourth thing to audit; when one is needed -- Argon2id, or
 * secp256k1 for coin keys -- it goes behind this same interface.
 *
 * crypto.subtle EXISTS ONLY IN A SECURE CONTEXT. Served over plain http on
 * a LAN address, `crypto.subtle` is undefined and nothing here can run.
 * That is a browser rule, not ours, and it is checked and explained at the
 * top rather than discovered as "undefined is not an object" half way
 * through somebody's signup. https, or localhost, or the remote tunnel.
 */

export const usable = (typeof crypto !== "undefined" && !!crypto.subtle);
/* The explanation for `usable === false` is written once, on the server
 * (WHY_NOT_SECURE in arcade/web/app.py), and rendered into the page --
 * because the server knows which address the browser used and can say so.
 * Two copies of a sentence is two sentences that drift. */

const enc = new TextEncoder();
const hex = (bytes) => [...new Uint8Array(bytes)]
  .map((b) => b.toString(16).padStart(2, "0")).join("");
const unhex = (text) => new Uint8Array(
  text.match(/../g).map((pair) => parseInt(pair, 16)));

/* --- the word list -------------------------------------------------------
 * Fetched once and remembered. The same file the node vendors, served from
 * the node, so a phrase made here is a phrase every other wallet reads.
 */
let WORDS = null;
export async function words() {
  if (WORDS === null) {
    const text = await (await fetch("/bip39-english.txt")).text();
    WORDS = text.split(/\s+/).filter(Boolean);
    if (WORDS.length !== 2048) throw new Error("the word list is damaged");
  }
  return WORDS;
}

/* --- BIP39 --------------------------------------------------------------- */

//: Twelve words, not twenty-four. Twenty-four was the original default and
//: is stronger in a way nobody can use: 128 bits is already beyond any
//: attack that will exist, and the difference in what somebody actually
//: writes down and keeps is the difference between a backup and a good
//: intention.
export const DEFAULT_STRENGTH = 128;

export async function generate(strength = DEFAULT_STRENGTH) {
  const list = await words();
  const entropy = crypto.getRandomValues(new Uint8Array(strength / 8));
  const checksumBits = entropy.length * 8 / 32;
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", entropy));
  let bits = "";
  for (const byte of entropy) bits += byte.toString(2).padStart(8, "0");
  bits += digest[0].toString(2).padStart(8, "0").slice(0, checksumBits);
  const out = [];
  for (let i = 0; i < bits.length; i += 11)
    out.push(list[parseInt(bits.slice(i, i + 11), 2)]);
  return out.join(" ");
}

export function normalise(phrase) {
  return (phrase || "").normalize("NFKD").toLowerCase().split(/\s+/)
    .filter(Boolean).join(" ");
}

/* Returns "" when the phrase is good, and otherwise says what is wrong with
 * it in the words a person can act on. Somebody typing a wallet back in is
 * the worst possible moment to be terse. */
export async function complaint(phrase) {
  const list = await words();
  const said = normalise(phrase);
  if (!said) return "Write the words in, separated by spaces.";
  const parts = said.split(" ");
  if (![12, 15, 18, 21, 24].includes(parts.length))
    return `A seed phrase is 12, 15, 18, 21 or 24 words; this is ${parts.length}.`;
  const unknown = parts.filter((word) => list.indexOf(word) < 0);
  if (unknown.length)
    return `Not a word from the list: ${unknown.slice(0, 3).join(", ")}` +
      `${unknown.length > 3 ? "…" : ""}. Every word comes from the standard 2048.`;
  let bits = "";
  for (const word of parts)
    bits += list.indexOf(word).toString(2).padStart(11, "0");
  const checksumBits = bits.length / 33;
  const entropyBits = bits.length - checksumBits;
  const entropy = new Uint8Array(entropyBits / 8);
  for (let i = 0; i < entropy.length; i++)
    entropy[i] = parseInt(bits.slice(i * 8, i * 8 + 8), 2);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", entropy));
  const want = digest[0].toString(2).padStart(8, "0").slice(0, checksumBits);
  if (bits.slice(entropyBits) !== want)
    return "Those are all real words, but not in an order this can be. A " +
      "seed phrase carries a checksum, so one mistyped or swapped word is " +
      "caught here rather than opening an empty wallet.";
  return "";
}

export async function toSeed(phrase, passphrase = "") {
  const complaintText = await complaint(phrase);
  if (complaintText) throw new Error(complaintText);
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(normalise(phrase)), "PBKDF2", false, ["deriveBits"]);
  return new Uint8Array(await crypto.subtle.deriveBits(
    {name: "PBKDF2", salt: enc.encode("mnemonic" + (passphrase || "").normalize("NFKD")),
     iterations: 2048, hash: "SHA-512"}, key, 512));
}

/* --- the hardened half of BIP32 ------------------------------------------
 * Hardened only, the same restriction `arcade/seed.py` documents: a normal
 * child needs secp256k1 point arithmetic, and every path this application
 * uses is hardened.
 */
const HARDENED = 0x80000000;

async function hmac512(key, data) {
  const k = await crypto.subtle.importKey(
    "raw", key, {name: "HMAC", hash: "SHA-512"}, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", k, data));
}

export async function derive(seed, indexes) {
  let digest = await hmac512(enc.encode("Bitcoin seed"), seed);
  let key = digest.slice(0, 32), chain = digest.slice(32);
  for (const raw of indexes) {
    const index = raw >= HARDENED ? raw : raw + HARDENED;
    const data = new Uint8Array(37);
    data.set(key, 1);
    new DataView(data.buffer).setUint32(33, index >>> 0);
    digest = await hmac512(chain, data);
    key = digest.slice(0, 32);
    chain = digest.slice(32);
  }
  return key;
}

/* Must equal arcade/seed.py's ARCADE_PURPOSE and LOGIN_BRANCH. */
export const ARCADE_PURPOSE = 24946;
export const LOGIN_BRANCH = [ARCADE_PURPOSE, 1, 0];

//: The X25519 identity that reads somebody's messages. Its own branch, for
//: the reason arcade/seed.py gives: it should not be the key that spends
//: their coins wearing a hat.
export const MESSAGING_BRANCH = [ARCADE_PURPOSE, 0, 0];

/* --- the account key -----------------------------------------------------
 * A 32-byte Ed25519 seed is wrapped in the one fixed PKCS#8 header that
 * describes it, because that is the only private-key format `importKey`
 * accepts for Ed25519. The header is a constant: version 0, algorithm
 * 1.3.101.112, a 34-byte octet string holding a 32-byte octet string.
 */
const PKCS8_ED25519 = new Uint8Array([
  0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70,
  0x04, 0x22, 0x04, 0x20]);

export async function accountKey(seed) {
  const raw = await derive(seed, LOGIN_BRANCH);
  const pkcs8 = new Uint8Array(PKCS8_ED25519.length + 32);
  pkcs8.set(PKCS8_ED25519);
  pkcs8.set(raw, PKCS8_ED25519.length);
  // Extractable, because the public half cannot be read off a private key
  // any other way: the JWK for an Ed25519 private key carries both `d` and
  // `x`, and `x` is the account id. It costs nothing -- the seed those
  // bytes came from is in this page's memory either way.
  const priv = await crypto.subtle.importKey(
    "pkcs8", pkcs8, {name: "Ed25519"}, true, ["sign"]);
  const jwk = await crypto.subtle.exportKey("jwk", priv);
  return {sign: (data) => crypto.subtle.sign({name: "Ed25519"}, priv, data),
          pubkey: hex(b64urlBytes(jwk.x))};
}

function b64urlBytes(text) {
  const padded = text.replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded + "=".repeat((4 - padded.length % 4) % 4));
  return Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
}

/* --- signing in ----------------------------------------------------------
 * Ask for a nonce, sign exactly the bytes the node will check, hand back
 * the signature. The message is built here rather than taken from the
 * server's answer so that a server cannot ask this key to sign something
 * of its choosing.
 */
export function loginMessage(origin, nonce) {
  return enc.encode(`DogecoinArcade login\n${origin}\n${nonce}`);
}

export async function signIn(phrase, {join = false, passphrase = ""} = {}) {
  const seed = await toSeed(phrase, passphrase);
  const key = await accountKey(seed);
  const challenge = await (await fetch("/auth/challenge")).json();
  const signature = await key.sign(loginMessage(challenge.origin, challenge.nonce));
  const answer = await fetch("/auth/login", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({pubkey: key.pubkey, nonce: challenge.nonce,
                          signature: hex(signature), join: !!join}),
  });
  const said = await answer.json();
  if (!answer.ok) throw new Error(said.detail || "that did not open anything");
  return said;
}

/* --- keeping the seed on this machine ------------------------------------
 * AES-256-GCM under a key from PBKDF2-HMAC-SHA512 at 600,000 iterations --
 * the WebCrypto-only option, and named here as SECOND BEST rather than
 * presented as good: Argon2id is memory-hard and this is not, so a
 * stolen ciphertext with a weak password falls to a GPU. Argon2id needs a
 * WASM module, which is the vendored library this file otherwise avoids;
 * when it arrives, `params.kdf` is how an old blob still opens.
 *
 * The ciphertext goes in IndexedDB. The password goes nowhere: not to the
 * node, not into a cookie, not into localStorage. Forgetting it is the
 * same as forgetting the words, which is why the words are shown first and
 * confirmed before this is offered.
 */
export const KDF_ITERATIONS = 600000;
const DB = "arcade-wallet", SHELF = "seed", ONLY = "me";

function shelf(mode) {
  return new Promise((ok, no) => {
    const open = indexedDB.open(DB, 1);
    open.onupgradeneeded = () => open.result.createObjectStore(SHELF);
    open.onerror = () => no(open.error);
    open.onsuccess = () => {
      const tx = open.result.transaction(SHELF, mode);
      ok(tx.objectStore(SHELF));
    };
  });
}

function awaited(request) {
  return new Promise((ok, no) => {
    request.onsuccess = () => ok(request.result);
    request.onerror = () => no(request.error);
  });
}

async function passwordKey(password, salt, iterations) {
  const base = await crypto.subtle.importKey(
    "raw", enc.encode(password), "PBKDF2", false, ["deriveKey"]);
  return crypto.subtle.deriveKey(
    {name: "PBKDF2", salt, iterations, hash: "SHA-512"}, base,
    {name: "AES-GCM", length: 256}, false, ["encrypt", "decrypt"]);
}

export async function keep(phrase, password) {
  if (!password || password.length < 8)
    throw new Error("a password of at least eight characters, please");
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const key = await passwordKey(password, salt, KDF_ITERATIONS);
  const sealed = await crypto.subtle.encrypt(
    {name: "AES-GCM", iv: nonce}, key, enc.encode(normalise(phrase)));
  const store = await shelf("readwrite");
  await awaited(store.put({
    kdf: "PBKDF2-SHA512", iterations: KDF_ITERATIONS,
    salt: hex(salt), nonce: hex(nonce), sealed: hex(sealed),
  }, ONLY));
  return true;
}

export async function kept() {
  try {
    return await awaited((await shelf("readonly")).get(ONLY)) || null;
  } catch (e) { return null; }
}

export async function unlock(password) {
  const blob = await kept();
  if (!blob) throw new Error("there is no wallet kept on this machine");
  const key = await passwordKey(password, unhex(blob.salt), blob.iterations);
  let plain;
  try {
    plain = await crypto.subtle.decrypt(
      {name: "AES-GCM", iv: unhex(blob.nonce)}, key, unhex(blob.sealed));
  } catch (e) {
    throw new Error("that password does not open this wallet.");
  }
  return new TextDecoder().decode(plain);
}

export async function forget() {
  const store = await shelf("readwrite");
  await awaited(store.delete(ONLY));
}
