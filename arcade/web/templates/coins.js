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
 *   reading              an unsigned transaction, parsed into its inputs
 *                        and outputs, and the hash each input has to sign
 *   signing              one input at a time, over bytes recomputed here
 *                        from the transaction rather than handed to us
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

/* --- reading a transaction ----------------------------------------------
 *
 * A signer that cannot read signs whatever it is handed. The node has
 * always sent the whole unsigned transaction alongside the list of hashes
 * (`funding.Unsigned.as_json`), so the material was here and unused; this
 * is the reading.
 *
 * The serialisation is the published legacy one, the same bytes
 * `arcade/funding.py:sighash` builds -- varints, little-endian, empty
 * scriptSigs except the input being signed, four bytes of sighash type,
 * SHA256d. Nothing here is new cryptography: it is parsing and
 * concatenation, and a mistake produces a signature the network refuses
 * rather than a key that leaks. `tests/test_coins_browser.py` compares
 * these hashes against Python's, byte for byte, for the same transaction.
 */

function readLE(bytes, at, n) {
  let value = 0n;
  for (let i = n - 1; i >= 0; i--) value = (value << 8n) | BigInt(bytes[at + i]);
  return value;
}

function writeLE(value, n) {
  const out = new Uint8Array(n);
  let rest = BigInt(value);
  for (let i = 0; i < n; i++) { out[i] = Number(rest & 255n); rest >>= 8n; }
  return out;
}

function join(parts) {
  const out = new Uint8Array(parts.reduce((n, part) => n + part.length, 0));
  let at = 0;
  for (const part of parts) { out.set(part, at); at += part.length; }
  return out;
}

/** A compact size: the value itself under 253, else a flag and the bytes. */
function sizeOf(n) {
  const value = BigInt(n);
  if (value < 0xfdn) return new Uint8Array([Number(value)]);
  if (value <= 0xffffn) return new Uint8Array([0xfd, ...writeLE(value, 2)]);
  if (value <= 0xffffffffn) return new Uint8Array([0xfe, ...writeLE(value, 4)]);
  return new Uint8Array([0xff, ...writeLE(value, 8)]);
}

/** `[value, where-the-bytes-resume]`, or a refusal for a truncated tx. */
export function varint(bytes, at) {
  const flag = bytes[at];
  if (flag === undefined) throw new Error("that transaction ends early");
  if (flag < 0xfd) return [Number(flag), at + 1];
  const width = {0xfd: 2, 0xfe: 4, 0xff: 8}[flag];
  if (at + 1 + width > bytes.length) throw new Error("that transaction ends early");
  return [Number(readLE(bytes, at + 1, width)), at + 1 + width];
}

/** The script that pays a hash160 -- the one shape this wallet spends from. */
export function p2pkh(hash) {
  return new Uint8Array([0x76, 0xa9, ...sizeOf(hash.length), ...hash, 0x88, 0xac]);
}

const SAME = (a, b) => a.length === b.length && a.every((b2, n) => b2 === b[n]);

/** Split an unsigned transaction into what it spends and what it pays. */
export function parseTx(raw) {
  const bytes = unhex(String(raw || ""));
  let at = 4;
  if (bytes.length < at + 1) throw new Error("that is not a transaction");
  const version = Number(readLE(bytes, 0, 4));
  const inputs = [];
  let [count, next] = varint(bytes, at);
  at = next;
  if (count === 0) {
    // A segwit marker, and this wallet's transactions are not segwit. The
    // hash below would be computed over the wrong bytes, so say so.
    throw new Error("that is not a plain transaction, and this browser will "
      + "not sign what it cannot read");
  }
  for (let i = 0; i < count; i++) {
    if (at + 36 > bytes.length) throw new Error("that transaction ends early");
    const txid = hex(bytes.slice(at, at + 32).reverse());   // display order
    const vout = Number(readLE(bytes, at + 32, 4));
    at += 36;
    let length; [length, at] = varint(bytes, at);
    if (at + length > bytes.length) throw new Error("that transaction ends early");
    const scriptSig = bytes.slice(at, at + length);
    at += length;
    const sequence = Number(readLE(bytes, at, 4));
    at += 4;
    inputs.push({txid, vout, scriptSig, sequence});
  }
  [count, at] = varint(bytes, at);
  const outputs = [];
  for (let i = 0; i < count; i++) {
    if (at + 8 > bytes.length) throw new Error("that transaction ends early");
    const value = readLE(bytes, at, 8);
    at += 8;
    let length; [length, at] = varint(bytes, at);
    if (at + length > bytes.length) throw new Error("that transaction ends early");
    outputs.push({value, script: bytes.slice(at, at + length)});
    at += length;
  }
  if (at + 4 > bytes.length) throw new Error("that transaction ends early");
  const locktime = Number(readLE(bytes, at, 4));
  at += 4;
  if (at !== bytes.length) {
    throw new Error("that transaction has bytes left over that this browser "
      + "cannot account for");
  }
  return {version, locktime, inputs, outputs};
}

/** The 32 bytes input `index` signs under SIGHASH_ALL, from the transaction. */
export async function sighashAll(tx, index, scriptPubKey) {
  const parts = [writeLE(tx.version, 4), sizeOf(BigInt(tx.inputs.length))];
  tx.inputs.forEach((input, n) => {
    const script = n === index ? scriptPubKey : new Uint8Array(0);
    parts.push(unhex(input.txid).reverse(), writeLE(input.vout, 4),
               sizeOf(BigInt(script.length)), script, writeLE(0xffffffffn, 4));
  });
  parts.push(sizeOf(BigInt(tx.outputs.length)));
  for (const out of tx.outputs)
    parts.push(writeLE(out.value, 8), sizeOf(BigInt(out.script.length)), out.script);
  parts.push(writeLE(tx.locktime, 4), writeLE(1n, 4));      // SIGHASH_ALL
  return hash256(join(parts));
}

/** The bytes a payload output actually carries, ignoring its opcodes. */
function payloadBytes(script) {
  let at = 0, total = 0;
  while (at < script.length) {
    const op = script[at];
    if (op > 0 && op < 0x4c && at + 1 + op <= script.length) {
      total += op; at += 1 + op; continue;
    }
    if (op === 0x4c && at + 2 <= script.length) {
      const n = script[at + 1];
      if (at + 2 + n > script.length) break;
      total += n; at += 2 + n; continue;
    }
    at += 1;
  }
  return total;
}

/* --- what this key may sign ---------------------------------------------
 *
 * One rule underneath this: a signature goes over bytes this browser
 * worked out for itself, over a transaction whose every coin it could
 * recognise. The node's `sighashes` are treated as a claim about the
 * transaction and checked against it, which is the only direction that
 * matters -- a node that can choose what you sign does not need your key,
 * and "the node never holds your key" is worth nothing on its own.
 *
 * What is checked, and what is still taken on trust:
 *
 *   the outputs          entirely, from the bytes. Where the money goes and
 *                        how much, the payload written to the chain, the
 *                        change. Every one of these is inside the hash, so
 *                        they are what the signature is really for.
 *   the coins it spends  their outpoints, against the offer's own list.
 *                        Their VALUES are still the node's numbers: the
 *                        sighash does not bind an amount, so a fee is
 *                        derived from what the index says the inputs were
 *                        worth rather than from the coins themselves. The
 *                        fix is the previous transactions, checked against
 *                        their own txids -- that belongs with building in
 *                        the browser, not here.
 *   coins that are NOT   refused. Every input this key signs has to be
 *     this key's         spendable by the key doing the signing, so an
 *                        offer cannot include a coin by this address that
 *                        the offer did not say it was spending.
 *   a counterparty's     `signed_from`: the inputs before it belong to the
 *     coins              other half of a trade, are left unsigned here, and
 *                        are not this key's to check or to sign.
 */
export async function verifyOffer(offer, keys) {
  if (!offer || !offer.raw) {
    throw new Error("that offer has no transaction in it, so there is nothing "
      + "to check against. Nothing was signed.");
  }
  const tx = parseTx(offer.raw);
  const named = offer.inputs || [];
  if (named.length !== tx.inputs.length) {
    throw new Error(`that offer lists ${named.length} coins and its `
      + `transaction spends ${tx.inputs.length}. Nothing was signed.`);
  }
  for (let i = 0; i < tx.inputs.length; i++) {
    if (tx.inputs[i].txid !== String(named[i].txid || "").toLowerCase()
        || tx.inputs[i].vout !== Number(named[i].vout)) {
      throw new Error(`input ${i} of that transaction is not the coin the `
        + "offer names. Nothing was signed.");
    }
  }

  const from = Number(offer.signed_from || 0);
  const asked = (offer.sighashes || []).map((h) => String(h).toLowerCase());
  if (tx.inputs.length - from !== asked.length) {
    throw new Error(`that transaction has ${tx.inputs.length - from} coins `
      + `for this key to sign and asks for ${asked.length} signatures. `
      + "Nothing was signed.");
  }

  const mine = p2pkh(await hash160(keys.pubkey));
  const own = keys.address || await address(keys.pubkey, keys.version);
  const hashes = [];
  for (let n = from; n < tx.inputs.length; n++) {
    if (String(named[n].address || "").toLowerCase() !== own.toLowerCase()) {
      throw new Error(`that transaction spends a coin at input ${n} that this `
        + "key does not hold. Nothing was signed.");
    }
    const derived = hex(await sighashAll(tx, n, mine));
    if (derived !== asked[n - from]) {
      throw new Error("those signatures were asked for over different bytes "
        + `than this transaction -- what this browser worked out for input ${n} `
        + "is not what the offer carries. Nothing was signed.");
    }
    hashes.push(derived);
  }

  // Everything below is what to SAY about it, and it is said from the
  // parsed transaction. `offer.fee` is never read: a confirmation that
  // repeats the node's numbers proves nothing.
  const version = unbase58(own)[0];
  let paid = 0n, change = 0n;
  const pays = [];
  for (const out of tx.outputs) {
    paid += out.value;
    const script = out.script;
    if (SAME(script, mine)) {
      change += out.value;
      pays.push({to: own, value: Number(out.value), mine: true});
    } else if (script.length === 25 && script[0] === 0x76 && script[1] === 0xa9
               && script[23] === 0x88 && script[24] === 0xac) {
      pays.push({to: await base58check(version, script.slice(3, 23)),
                 value: Number(out.value), mine: false});
    } else {
      pays.push({bytes: payloadBytes(script), value: Number(out.value),
                 mine: false});
    }
  }
  let taken = 0n;
  for (const coin of named) taken += BigInt(coin.value || 0);
  if (paid > taken) {
    throw new Error("that transaction pays out more than it takes in, which "
      + "no chain would accept. Nothing was signed.");
  }
  const fee = taken - paid;
  const coinsOf = (sats) => (Number(sats) / 100000000).toFixed(8);
  const says = pays.map((out) => out.bytes !== undefined
    ? `${out.bytes} bytes written to the chain`
    : `${coinsOf(out.value)} to ${out.to}${out.mine ? " (back to you)" : ""}`
  ).join(", ") + `; ${coinsOf(fee)} in fees`
    + (change > 0n ? `, ${coinsOf(change)} of it coming back to you` : "");
  return {tx, hashes, pays, fee: Number(fee), change: Number(change),
          what: offer.what || "", signs: {from, of: tx.inputs.length},
          coinsOf, says};
}

/* --- signing -------------------------------------------------------------
 *
 * This signs the 32 bytes it is given and nothing else. What makes that
 * safe is no longer that this file refuses to look: it is that the bytes
 * reaching here are the ones `verifyOffer` derived from the transaction's
 * own serialized bytes, having refused anything else. The node still
 * decodes what comes back and compares it against what it offered. Both
 * halves check, and neither is trusted alone.
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
