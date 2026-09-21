/* Coin keys, in the browser, where the only copy belongs.
 *
 * `signin.js` is the half of the wallet that WebCrypto can do on its own:
 * the words, the seed, the hardened tree, the Ed25519 key that opens a
 * seat. This is the half it cannot. Holding coins needs secp256k1, which
 * WebCrypto has not got, and an address needs RIPEMD-160, which it has not
 * got either -- so two audited libraries are vendored beside this file
 * (`vendor/PROVENANCE.md` says exactly which, and how they were checked).
 *
 * What lives here:
 *
 *   BIP32, both halves   hardened AND normal children, which is what the
 *                        coin path needs and what needs the curve
 *   addresses            hash160 of the public key, base58check, in this
 *                        chain's own version byte
 *   signing              one input at a time, over bytes the node hands us
 *                        and we check before we touch them
 *
 * **The node never sees a private key and never gets asked for one.** It
 * builds a transaction, says exactly what it does, and asks for
 * signatures. Everything in this file that could sign takes the sighash as
 * an argument, so there is no path from "the node said so" to "the key was
 * used" that does not pass through code the reader can see.
 */

import * as secp from "/vendor/noble-secp256k1.js";
import { ripemd160 } from "/vendor/hashes/ripemd160.js";

const enc = new TextEncoder();
export const hex = (bytes) => [...new Uint8Array(bytes)]
  .map((b) => b.toString(16).padStart(2, "0")).join("");
export const unhex = (text) => new Uint8Array(
  (text.match(/../g) || []).map((pair) => parseInt(pair, 16)));

async function sha256(bytes) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
}

/** The double SHA-256 that bitcoin-family chains use nearly everywhere. */
export async function hash256(bytes) {
  return sha256(await sha256(bytes));
}

/** hash160: RIPEMD-160 of SHA-256. What an address is made of. */
export async function hash160(bytes) {
  return ripemd160(await sha256(bytes));
}

async function hmac512(key, data) {
  const k = await crypto.subtle.importKey(
    "raw", key, {name: "HMAC", hash: "SHA-512"}, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", k, data));
}

/* --- BIP32, with the half signin.js refuses ------------------------------
 *
 * `signin.js` does hardened children only and says so: a hardened step is
 * HMAC over the private key and needs no curve. A NORMAL child needs the
 * parent's public POINT, and the addition at the end is modulo the curve
 * order -- which is why that file will not do it and this one can.
 */

const N = secp.CURVE.n;
const HARDENED = 0x80000000;

function beBytes(n, length) {
  const out = new Uint8Array(length);
  for (let i = length - 1; i >= 0; i--) { out[i] = Number(n & 255n); n >>= 8n; }
  return out;
}

function toBig(bytes) {
  let n = 0n;
  for (const b of bytes) n = (n << 8n) | BigInt(b);
  return n;
}

export function publicKey(priv) {
  return secp.getPublicKey(priv, true);          // compressed, 33 bytes
}

export async function master(seed) {
  const digest = await hmac512(enc.encode("Bitcoin seed"), seed);
  return {key: digest.slice(0, 32), chain: digest.slice(32), depth: 0};
}

export async function child(node, index) {
  const data = new Uint8Array(37);
  if (index >= HARDENED) {
    data[0] = 0;
    data.set(node.key, 1);
  } else {
    data.set(publicKey(node.key), 0);
  }
  new DataView(data.buffer).setUint32(33, index >>> 0);
  const digest = await hmac512(node.chain, data);

  // The real BIP32 step, which is the whole reason this file exists: the
  // left half is a scalar ADDED to the parent key, modulo the curve order.
  const tweak = toBig(digest.slice(0, 32));
  if (tweak >= N) throw new Error("that child is not on the curve; use the next");
  const key = (tweak + toBig(node.key)) % N;
  if (key === 0n) throw new Error("that child is zero; use the next");
  return {key: beBytes(key, 32), chain: digest.slice(32),
          depth: (node.depth || 0) + 1};
}

/** Walk a path like [44 + HARDENED, 1 + HARDENED, 0 + HARDENED, 0, 5]. */
export async function derive(seed, path) {
  let node = await master(seed);
  for (const index of path) node = await child(node, index);
  return node;
}

export {HARDENED};

/* --- addresses -----------------------------------------------------------
 *
 * Derived HERE rather than asked of the node, deliberately. A node that
 * answered "your address is..." could answer with somebody else's: coins
 * would arrive at an address the owner cannot spend from, and nothing on
 * the page would look wrong. It costs a vendored hash to never have to
 * trust that answer.
 */

const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

export function base58(bytes) {
  let n = toBig(bytes), out = "";
  while (n > 0n) { out = B58[Number(n % 58n)] + out; n /= 58n; }
  for (const b of bytes) { if (b === 0) out = "1" + out; else break; }
  return out;
}

export function unbase58(text) {
  let n = 0n;
  for (const ch of text) {
    const at = B58.indexOf(ch);
    if (at < 0) throw new Error("that is not an address");
    n = n * 58n + BigInt(at);
  }
  let out = [];
  while (n > 0n) { out.unshift(Number(n & 255n)); n >>= 8n; }
  for (const ch of text) { if (ch === "1") out.unshift(0); else break; }
  return new Uint8Array(out);
}

export async function base58check(version, payload) {
  const body = new Uint8Array(1 + payload.length);
  body[0] = version;
  body.set(payload, 1);
  const sum = (await hash256(body)).slice(0, 4);
  const whole = new Uint8Array(body.length + 4);
  whole.set(body);
  whole.set(sum, body.length);
  return base58(whole);
}

/** The P2PKH address for a public key, in one chain's version byte. */
export async function address(pubkey, version) {
  return base58check(version, await hash160(pubkey));
}

/* --- the coin path -------------------------------------------------------
 * m/44'/<coin>'/0'/0/<i>, the ordinary BIP44 path, so these keys are found
 * by any other wallet given the same words. Unlike the arcade's own branch
 * (seed.py: purpose 24946'), this one is somebody else's convention and is
 * followed exactly.
 */

//: SLIP-44 registers 3 for Dogecoin and 1 for every test chain. Pepecoin
//: has no registration of its own, so it follows the family it forked.
export const COIN_TYPE = {main: 3, test: 1, regtest: 1};

export function coinPath(network, index = 0) {
  const coin = COIN_TYPE[network] ?? 1;
  return [44 + HARDENED, coin + HARDENED, 0 + HARDENED, 0, index];
}

export async function coinKey(seed, network, index = 0) {
  const node = await derive(seed, coinPath(network, index));
  return {key: node.key, pubkey: publicKey(node.key)};
}

/* --- signing -------------------------------------------------------------
 *
 * The node builds a transaction and hands over, for each input, the exact
 * 32 bytes to sign. This signs them and nothing else: no transaction is
 * parsed here, and no amount is read here, because a signer that reads the
 * thing it signs is a signer that can be talked into reading it wrongly.
 *
 * What makes that safe is not this function -- it is that the page SHOWS
 * what the node said it was doing and the node verifies the returned
 * transaction is what it offered before broadcasting it. Both halves check;
 * neither is trusted alone.
 */

/* DER, written here because noble v2 does not encode it -- it hands back
 * the compact r||s, and a scriptSig wants DER. This is serialisation and
 * not arithmetic: no secret is read, the shape is fixed by the standard,
 * and a mistake is a transaction the network refuses rather than a key
 * that leaks. The two rules that are always got wrong are both here: a
 * minimal big-endian integer, and a leading zero byte whenever the top bit
 * is set so the value is not read as negative.
 */
function derInteger(value) {
  let bytes = [...value];
  while (bytes.length > 1 && bytes[0] === 0) bytes.shift();
  if (bytes[0] & 0x80) bytes.unshift(0);
  return [0x02, bytes.length, ...bytes];
}

export function toDER(compact) {
  const body = [...derInteger(compact.slice(0, 32)),
                ...derInteger(compact.slice(32, 64))];
  return new Uint8Array([0x30, body.length, ...body]);
}

export async function signHash(priv, sighash) {
  // lowS because the network refuses the other half of every pair, and a
  // transaction refused after broadcast is a fee spent for nothing.
  const signature = await secp.signAsync(sighash, priv, {lowS: true});
  return toDER(signature.toCompactRawBytes());
}

/** A DER signature with its sighash byte, as a scriptSig wants it. */
export async function signInput(priv, sighash, sighashType = 1) {
  const der = await signHash(priv, sighash);
  const out = new Uint8Array(der.length + 1);
  out.set(der);
  out[der.length] = sighashType;
  return out;
}
