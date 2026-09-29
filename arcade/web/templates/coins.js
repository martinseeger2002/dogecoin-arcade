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

//: SINGLE|ANYONECANPAY, the type a listing signs with. ANYONECANPAY because a
//: listing is signed while the buyer is a stranger, so it must not commit to
//: any coin but the one signing. SINGLE because what is left is one output --
//: the one standing under the input that signs it. `arcade/funding.py:sighash`
//: is the authority for the shape; `arcade/listings.py` is what refuses a leg
//: whose digests do not match these bytes.
export const SINGLE_ANYONECANPAY = 0x83;

/** The 32 bytes input `index` signs under SINGLE|ANYONECANPAY.
 *
 * Narrower than `sighashAll` in two ways, and the second is the one Python got
 * wrong first: the preimage carries this input ALONE, and an output list
 * `index + 1` long -- every slot before this one's serialised as
 * `CTxOut::SetNull()` (value -1, no scriptPubKey) and the output standing at
 * this input's own index for real. So the digest for input 1 is NOT the digest
 * for input 0 with one more output beside it, and a browser that assumed so
 * would sign a listing that no node will ever file.
 */
export async function sighashLeg(tx, index, scriptPubKey) {
  if (index >= tx.outputs.length) {
    throw new Error(`input ${index} has no output ${index} to stand over, and`
      + " what a SINGLE signature falls back to there is one fixed number --"
      + " a signature over it would authorise every transaction there is");
  }
  const input = tx.inputs[index];
  const parts = [writeLE(tx.version, 4), sizeOf(1n),
                 unhex(input.txid).reverse(), writeLE(input.vout, 4),
                 sizeOf(BigInt(scriptPubKey.length)), scriptPubKey,
                 writeLE(0xffffffffn, 4), sizeOf(BigInt(index + 1))];
  // -1n little-endian is eight 0xff bytes, which is SetNull on the wire:
  // BigInt's shift keeps the sign, so no masking is needed to write it.
  for (let n = 0; n < index; n++) parts.push(writeLE(-1n, 8), sizeOf(0n));
  const out = tx.outputs[index];
  parts.push(writeLE(out.value, 8), sizeOf(BigInt(out.script.length)), out.script,
             writeLE(tx.locktime, 4), writeLE(BigInt(SINGLE_ANYONECANPAY), 4));
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

/* --- what a listing's own bytes say --------------------------------------
 *
 * The payload at output 0 is the one part of a listing that both of its
 * signatures stand over, and `arcade/listings.py` files a row only after
 * reading these bytes back out of the transaction. So they are read here too,
 * and the sentence a person is shown comes from them rather than from the
 * words the node sent beside them.
 *
 * The shape, from `arcade/config.py`, `arcade/payload.py` and
 * `arcade/inscriptions.py`:
 *
 *   "arcd"            the marker every output of ours opens with
 *   0x0000 0x00c8     an Omni AnyData header: version 0, type 200
 *   "INSC" 0x01 0x05  an inscription, version 1, kind 5 -- a swap
 *   0x01 + txid(32)   what the seller gives: a piece, named by its txid
 *   0x03 + sats(8)    what the seller takes, paid in the same transaction
 *
 * Anything else is refused rather than described. A listing that says what
 * this browser cannot read is a listing whose words would be a guess, and the
 * words are the reason a signature is being asked for.
 */

const MARKER = [0x61, 0x72, 0x63, 0x64];              // "arcd"
const INSC = [0x49, 0x4e, 0x53, 0x43];                // "INSC"
const ANYDATA = 200;                                 // the inscription carrier
const INSCRIPTION_VERSION = 1;
const KIND_SWAP = 5;
const LEG_INSCRIPTION = 1;
const LEG_TOKEN = 2;
const LEG_COINS = 3;

/** The single push an OP_RETURN output carries, opcodes gone, or null.
 *
 * Stricter than `payloadBytes` on purpose: that one counts every push a script
 * holds, which is right for a Class B multisig output, while a Class C output
 * is one push after the OP_RETURN and anything else is not a listing this
 * repository wrote.
 */
function opreturnData(script) {
  if (!script.length || script[0] !== 0x6a) return null;
  let at = 1, length;
  const flag = script[at];
  if (flag > 0 && flag < 0x4c) { length = flag; at += 1; }
  else if (flag === 0x4c) { length = script[at + 1]; at += 2; }
  else return null;
  if (length === undefined || at + length !== script.length) return null;
  return script.slice(at, at + length);
}

/** What a listing's payload promises, read off those bytes:
 *  {txid, sats, token} -- `sats` is the price when it is paid in coins and 0
 *  when it is paid in a token, which is then in `token`.
 *
 * `gives` is an inscription, because that is the only half a pre-signed leg can
 * hand over -- the other half of a piece-for-a-piece swap would need a signature
 * that does not exist yet. What it `takes` can be coins or a token, and the two
 * are not alike: coins move inside the finished transaction, so the leg's own
 * payment states a coin price a second time and arithmetic can be checked
 * against it, while a token moves in the engine's ledger on the strength of
 * these bytes alone, which makes them the only record of that price there is.
 */
function listingPayload(data) {
  const refuse = (what) => {
    throw new Error(`that listing writes ${what}. Nothing was signed.`);
  };
  const eq = (bytes, want) => bytes.length === want.length
    && bytes.every((b, n) => b === want[n]);
  let at = 0;
  const field = (n, short) => {
    if (at + n > data.length) refuse(short);
    const out = data.subarray(at, at + n);
    at += n;
    return out;
  };

  if (!eq(field(MARKER.length, "shorter than its marker"), MARKER)) {
    refuse("no marker this repository writes");
  }
  field(2, "shorter than its header");                  // the message's version
  const type = field(2, "shorter than its header");
  if (eq(type, [0x00, 0x00])) {
    // A token send: a CLAIM LOT (2026-09-28), the tokens a game pays
    // out. What it takes is coins, stated by the payment output alone.
    const send = {propertyid: Number(toBig(field(4, "names no token"))),
                  units: toBig(field(8, "names no amount"))};
    if (at !== data.length) refuse("longer than the send it states");
    if (send.units <= 0n) refuse("a send of nothing");
    return {txid: null, sats: 0n, token: null, send};
  }
  if (!eq(type, [0x00, ANYDATA])) {
    refuse("not an inscription carrier");
  }
  if (!eq(field(INSC.length, "shorter than its magic"), INSC)) {
    refuse("not an inscription");
  }
  if (field(1, "names no inscription version")[0] !== INSCRIPTION_VERSION) {
    refuse("an inscription version this browser cannot read");
  }
  if (field(1, "names no kind")[0] !== KIND_SWAP) {
    refuse("not a swap, and a leg is a swap waiting for its buyer");
  }
  if (field(1, "has no first leg")[0] !== LEG_INSCRIPTION) {
    refuse("gives something besides a piece, which no signature here covers");
  }
  const txid = hex(field(32, "names no piece"));
  // Big-endian, because the payload is the protocol's own encoding and not the
  // transaction's: every number in a leg is written the other way round from
  // every number in the serialisation above it.
  const kind = field(1, "has no second leg")[0];
  let sats = 0n, token = null;
  if (kind === LEG_COINS) {
    sats = toBig(field(8, "names no price"));
  } else if (kind === LEG_TOKEN) {
    // Read, and read only here: a token is not paid inside this transaction,
    // so nothing else states this price and nothing else can contradict it.
    token = {propertyid: Number(toBig(field(4, "names no token"))),
             units: toBig(field(8, "names no amount"))};
  } else {
    refuse("takes something besides coins or tokens, which nobody has signed");
  }
  if (at !== data.length) refuse("longer than the trade it states");
  return {txid, sats, token};
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
/** Whether `script` is a bare 1-of-n multisig (OP_1 <keys> OP_n
 *  OP_CHECKMULTISIG) with `pubkey` among its keys: a Class B payload output
 *  this key alone can spend. */
function ownMultisig(script, pubkey) {
  const s = script, n = s.length;
  if (n < 37 || s[0] !== 0x51 || s[n - 1] !== 0xae) return false;
  let at = 1, found = false, keys = 0;
  while (at < n - 2) {
    const len = s[at];
    if (len !== 33 && len !== 65) return false;
    if (SAME(s.slice(at + 1, at + 1 + len), pubkey)) found = true;
    at += 1 + len; keys++;
  }
  return found && at === n - 2 && s[n - 2] === 0x50 + keys;
}

/** One buy-order lot (arcade/standing.py): a transaction of one input, this key's
 *  coin, and one output, this key's own change. The digest is worked out here;
 *  what comes back is what to sign. */
export async function checkLot(lot, keys) {
  const tx = parseTx(lot.raw);
  if (tx.inputs.length !== 1 || tx.outputs.length !== 1) {
    throw new Error("a lot is one coin and one output. Nothing was signed.");
  }
  if (tx.inputs[0].txid !== String(lot.txid).toLowerCase() || tx.inputs[0].vout !== Number(lot.vout)) {
    throw new Error("that lot is not the coin it says. Nothing was signed.");
  }
  const mine = p2pkh(await hash160(keys.pubkey));
  if (!SAME(tx.outputs[0].script, mine) || tx.outputs[0].value !== BigInt(lot.change)) {
    throw new Error("a lot's only output must be your own change. Nothing was signed.");
  }
  const digest = await sighashLeg(tx, 0, mine);
  if (hex(digest) !== String(lot.digest).toLowerCase()) {
    throw new Error("that lot asks for a signature over different bytes. Nothing was signed.");
  }
  return digest;
}

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
    // A Class B payload output being swept back (2026-09-28) is signed
    // over its own bare 1-of-n multisig script -- and only if this key is in it.
    let spent = mine;
    if (named[n].script) {
      spent = unhex(String(named[n].script));
      if (!ownMultisig(spent, keys.pubkey)) {
        throw new Error(`input ${n} is not a payload output this key can sweep. `
          + "Nothing was signed.");
      }
    }
    const derived = hex(await sighashAll(tx, n, spent));
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
  // A token lot's tokens go to the LAST output that is not the seller's, so a
  // claim that did not end on an output of this key's would pay the seller and
  // send the tokens back to them (2026-09-28: token claims).
  const first = tx.outputs.length ? opreturnData(tx.outputs[0].script) : null;
  if (from > 0 && first && first.length === 20
      && MARKER.every((b, n) => first[n] === b) && first[6] === 0 && first[7] === 0
      && !SAME(tx.outputs[tx.outputs.length - 1].script, mine)) {
    throw new Error("that transaction sends tokens and does not end on an output "
      + "of yours, so they would go to somebody else. Nothing was signed.");
  }
  let taken = 0n;
  for (const coin of named) taken += BigInt(coin.value || 0);
  if (paid > taken) {
    throw new Error("that transaction pays out more than it takes in, which "
      + "no chain would accept. Nothing was signed.");
  }
  const fee = taken - paid;
  const coinsOf = (sats) => (Number(sats) / 100000000).toFixed(8);
  // The data outputs said once, counted and summed: three "99 bytes written to
  // the chain" in a row read as a stutter (filming the token tutorial, 2026-09-25).
  const data = pays.filter((out) => out.bytes !== undefined);
  const bytes = data.reduce((n, out) => n + out.bytes, 0);
  const says = [
    ...(data.length ? [data.length === 1 ? `${bytes} bytes written to the chain`
                       : `${bytes} bytes written to the chain in ${data.length} outputs`] : []),
    ...pays.filter((out) => out.bytes === undefined).map((out) =>
      `${coinsOf(out.value)} to ${out.to}${out.mine ? " (back to you)" : ""}`),
  ].join(", ") + `; ${coinsOf(fee)} in fees`
    + (change > 0n ? `, ${coinsOf(change)} of it coming back to you` : "");
  return {tx, hashes, pays, fee: Number(fee), change: Number(change),
          what: offer.what || "", signs: {from, of: tx.inputs.length},
          coinsOf, says};
}

/** What this browser makes of a listing, before either signature is made.
 *
 * A leg is not an offer and `verifyOffer` cannot read it, which is worth
 * saying out loud because the two look alike. An offer is broadcast the moment
 * it is signed and wants SIGHASH_ALL over every input; a leg is a signature
 * the node holds and never broadcasts, it names no buyer, and it wants to be
 * signed TWICE -- once standing over the bytes that name the piece, once over
 * the payment. So four questions, and the order they are asked in is the order
 * that fails loudest:
 *
 *   the type. SINGLE|ANYONECANPAY or nothing, because that pairing is the only
 *     one that promises a piece without committing to coins never seen before.
 *   every coin is this key's. A leg has no counterparty in it, so an input
 *     this address does not hold is not somebody else's half of a trade-- it
 *     is a coin being promised that nobody agreed to promise.
 *   two digests, one per input, each the preimage THIS browser builds for that
 *     index. One digest is a listing whose price is signed by nobody.
 *   output 0 reads as a listing and output 1 pays this address. Those are what
 *     the two signatures stand over; what is not stood over is not promised,
 *     and the buyer's half does not exist yet.
 *
 * What stays the node's numbers: the VALUES of the coins, exactly as in
 * `verifyOffer` -- they are in neither digest. The outpoints are checked, and
 * the outpoints are the part both signatures cover.
 */
export async function verifyLeg(leg, keys) {
  if (!leg || !leg.raw) {
    throw new Error("that listing has no transaction in it, so there is nothing "
      + "to check against. Nothing was signed.");
  }
  const type = Number(leg.sighash_type === undefined ? 1 : leg.sighash_type);
  if (type !== SINGLE_ANYONECANPAY) {
    throw new Error("a listing is signed with SINGLE|ANYONECANPAY, which is what "
      + "keeps it from committing to coins you have never seen. This one asks "
      + `for sighash type ${type}. Nothing was signed.`);
  }
  const tx = parseTx(leg.raw);
  if (tx.outputs.length < tx.inputs.length) {
    throw new Error(`that transaction spends ${tx.inputs.length} coins and has `
      + `${tx.outputs.length} outputs, so an input would stand over no output `
      + "at all -- and what a SINGLE signature falls back to there is one fixed "
      + "number, good for every transaction there is. Nothing was signed.");
  }
  const named = leg.inputs || [];
  if (named.length !== tx.inputs.length) {
    throw new Error(`that listing names ${named.length} coins and its `
      + `transaction spends ${tx.inputs.length}. Nothing was signed.`);
  }
  const asked = (leg.sighashes || []).map((h) => String(h).toLowerCase());
  if (asked.length !== tx.inputs.length) {
    throw new Error(`that transaction spends ${tx.inputs.length} coins and asks `
      + `for ${asked.length} signatures. One signature stands over one output, `
      + "so a listing that says what it sells is signed twice -- over the bytes "
      + "naming the piece, and over the price. Nothing was signed.");
  }

  const mine = p2pkh(await hash160(keys.pubkey));
  const own = keys.address || await address(keys.pubkey, keys.version);
  const hashes = [];
  for (let n = 0; n < tx.inputs.length; n++) {
    if (tx.inputs[n].txid !== String(named[n].txid || "").toLowerCase()
        || tx.inputs[n].vout !== Number(named[n].vout)) {
      throw new Error(`input ${n} of that transaction is not the coin the `
        + "listing names. Nothing was signed.");
    }
    if (String(named[n].address || "").toLowerCase() !== own.toLowerCase()) {
      throw new Error(`that listing promises a coin at input ${n} that this key `
        + "does not hold. Nothing was signed.");
    }
    const derived = hex(await sighashLeg(tx, n, mine));
    if (derived !== asked[n]) {
      throw new Error("those signatures were asked for over different bytes "
        + `than this transaction -- what this browser worked out for input ${n} `
        + "is not what the listing carries. Nothing was signed.");
    }
    hashes.push(derived);
  }

  const data = opreturnData(tx.outputs[0].script);
  if (tx.outputs[0].value !== 0n || data === null) {
    throw new Error("output 0 of that listing is not bytes written to the chain, "
      + "and output 0 is what the first signature stands over. Nothing was "
      + "signed.");
  }
  const listing = listingPayload(data);
  if (listing.send) {
    // A lot's price is in no payload: it is the coins the payment output adds.
    // The node states it and register() re-derives it from these bytes, and the
    // number that matters to the seller -- what comes back -- is read below.
    const told = leg.send || {};
    if (!/^\d+$/.test(String(leg.price || "")) || BigInt(String(leg.price)) <= 0n) {
      throw new Error("that lot names no price in coins. Nothing was signed.");
    }
    if (Number(told.propertyid) !== listing.send.propertyid
        || String(told.units) !== String(listing.send.units)) {
      throw new Error("the lot says it gives one thing and its own bytes send "
        + "another. Nothing was signed.");
    }
    listing.sats = BigInt(String(leg.price));
    listing.send.name = String(told.name || "");
    listing.send.text = String(told.amount || listing.send.units);
  }
  if (!SAME(tx.outputs[1].script, mine)) {
    throw new Error("output 1 of that listing -- the output the signature over "
      + "the price stands over -- does not pay this address. The price would "
      + "come back to somebody else. Nothing was signed.");
  }
  // The node's words about the price, when it bothered to send any. The numbers
  // come from the bytes and never from them; but where a listing speaks of a
  // token it has to mean the one its own bytes name, because a token price is
  // written nowhere else to be checked against. What it adds that the bytes
  // cannot is a name, and a name is only for reading aloud.
  const told = (leg.take && leg.take.kind === "token") ? leg.take : null;
  if (told) {
    const units = /^\d+$/.test(String(told.units)) ? BigInt(String(told.units))
                                                   : null;
    if (!listing.token || !units || units !== listing.token.units
        || Number(told.propertyid) !== listing.token.propertyid) {
      throw new Error("the listing says it takes a token and its own bytes take "
        + "something else. A price paid in tokens is written nowhere but those "
        + "bytes. Nothing was signed.");
    }
    listing.token.name = String(told.name || "");
    // `amount` is a rendering of `units`, not a second fact, and a card that
    // reads a price out loud has to be reading the number the bytes take. So it
    // is kept only when it reconciles with them -- the same units read as a
    // divisible token, eight decimals, or as whole units, which is every way
    // this repository writes one. A figure that reconciles neither way is
    // dropped and the page says the units it checked instead, so a node cannot
    // pick the sentence a person decides on by answering with a number that
    // means something else.
    listing.token.text = null;
    const said = /^(\d+)(?:\.(\d{1,8}))?$/.exec(String(told.amount || "").trim());
    if (said) {
      const asUnits = BigInt(said[1]) * 100000000n
                    + BigInt((said[2] || "").padEnd(8, "0"));
      if (asUnits === units || (!said[2] && BigInt(said[1]) === units)) {
        listing.token.text = String(told.amount);
      }
    }
  }
  // The other direction, and the one nothing else catches: bytes that take a token
  // with no words about it at all. A coin price is in the finished transaction as
  // well as in the payload, so a silent coin leg can still be checked by
  // arithmetic; a token moves in the engine's ledger on the strength of those bytes
  // and nowhere else, so a missing `take` is not a prettier sentence missing — it
  // is a price with no name and no amount, and the card a person decides on would
  // have to invent one to be printed at all. Every route that hands this function a
  // leg states `take` when its payload takes a token; this is what one that forgets
  // costs the tab, which is nothing.
  if (listing.token && !told) {
    throw new Error("that listing takes a token and says nothing about which one "
      + "or how much, so there is no price here to read aloud. A card that named "
      + "one would be making it up. Nothing was signed.");
  }

  // The arithmetic of a leg is not an offer's: its payment output is the
  // seller's own coins PLUS the price minus what it reserved for the fee, so
  // paying out more than it takes in is the correct shape here rather than a
  // fraud sign. What must not go negative is the reservation. A token price
  // pays no coin at all, so it adds nothing here and only the fee comes out of
  // the coins -- which is why the reservation is still the number to check.
  let taken = 0n;
  for (const coin of named) taken += BigInt(coin.value || 0);
  const back = tx.outputs[1].value;
  const reserved = taken + listing.sats - back;
  if (reserved < 0n) {
    throw new Error("that listing pays out more than its coins plus the price "
      + "it names, so nothing is left for a block, and it would sit in the "
      + "mempool until it fell out. Nothing was signed.");
  }
  const coinsOf = (sats) => (Number(sats) / 100000000).toFixed(8);
  const price = listing.token
    ? `${listing.token.text || listing.token.units} `
      + `${listing.token.name || `token ${listing.token.propertyid}`}`
    : `${coinsOf(listing.sats)} coins`;
  const gives = listing.send
    ? `${listing.send.text} ${listing.send.name || `of token #${listing.send.propertyid}`}`
    : `inscription ${listing.txid.slice(0, 16)}…`;
  const says = `gives ${gives} and takes `
    + `${price}; ${coinsOf(back)} comes back to you when `
    + `it sells, ${coinsOf(reserved)} of it reserved for the fee`;
  return {tx, hashes, listing, reserved: Number(reserved), back: Number(back),
          what: leg.what || "", signs: {from: 0, of: tx.inputs.length},
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

/* --- pre-signed offers (2026-09-27) --------------------------------
 *
 * An offer now carries the buyer's half of the trade, signed when it is made,
 * so the seller's Accept completes it on the spot. The buyer's inputs sign
 * ALL|ANYONECANPAY: every output is fixed -- the swap's bytes, the seller's
 * payment, the buyer's change -- and the seller may add the one input the
 * trade still needs. `arcade/funding.py:build_bid` builds it; these check it.
 */
export const ALL_ANYONECANPAY = 0x81;

/** The digest input `index` signs under ALL|ANYONECANPAY: this input alone,
 *  and every output. */
export async function sighashAllAcp(tx, index, scriptPubKey) {
  const input = tx.inputs[index];
  const parts = [writeLE(tx.version, 4), sizeOf(1n),
                 unhex(input.txid).reverse(), writeLE(input.vout, 4),
                 sizeOf(BigInt(scriptPubKey.length)), scriptPubKey,
                 writeLE(0xffffffffn, 4), sizeOf(BigInt(tx.outputs.length))];
  for (const out of tx.outputs)
    parts.push(writeLE(out.value, 8), sizeOf(BigInt(out.script.length)), out.script);
  parts.push(writeLE(tx.locktime, 4), writeLE(BigInt(ALL_ANYONECANPAY), 4));
  return hash256(join(parts));
}

/** Check the buyer's half before signing it: every input this key's, output 0
 *  the swap for this piece at this price, output 1 the seller paid the price
 *  plus their own coin back, everything else change to this key. */
export async function verifyBid(bid, keys, expect) {
  const refuse = (why) => { throw new Error(`${why} Nothing was signed.`); };
  if (!bid || !bid.raw) refuse("that offer carries no trade to sign.");
  const tx = parseTx(bid.raw);
  const own = keys.address || await address(keys.pubkey, keys.version);
  const mine = p2pkh(await hash160(keys.pubkey));
  const named = bid.inputs || [];
  if (named.length !== tx.inputs.length
      || named.length !== (bid.sighashes || []).length) {
    refuse("that trade's coins do not match what it asks to be signed.");
  }
  for (let i = 0; i < tx.inputs.length; i++) {
    if (tx.inputs[i].txid !== String(named[i].txid).toLowerCase()
        || tx.inputs[i].vout !== Number(named[i].vout)
        || String(named[i].address || "") !== own) {
      refuse(`input ${i} is not a coin of this key's.`);
    }
  }
  if (tx.outputs.length < 2 || tx.outputs.length > 3) refuse("that trade has the wrong shape.");
  const said = listingPayload(opreturnData(tx.outputs[0].script) || refuse("output 0 is not the trade's bytes."));
  if (said.txid !== String(expect.piece).toLowerCase()) refuse("that trade is for a different piece.");
  const sats = BigInt(expect.sats || 0);
  if (expect.token) {
    if (!said.token || said.token.propertyid !== Number(expect.token.propertyid)
        || said.token.units !== BigInt(expect.token.units)) {
      refuse("that trade names a different token price.");
    }
  } else if (said.sats !== sats) {
    refuse("that trade names a different price.");
  }
  const exact = BigInt(bid.exact);
  const seller = tx.outputs[1];
  const sellerScript = p2pkh(unbase58(expect.seller).slice(1, 21));
  if (!SAME(seller.script, sellerScript) || seller.value !== sats + exact) {
    refuse("that trade does not pay the piece's holder the price.");
  }
  let change = 0n;
  for (const out of tx.outputs.slice(2)) {
    if (!SAME(out.script, mine)) refuse("that trade pays somebody besides the seller and you.");
    change += out.value;
  }
  const hashes = [];
  for (let n = 0; n < tx.inputs.length; n++) {
    const derived = hex(await sighashAllAcp(tx, n, mine));
    if (derived !== String(bid.sighashes[n]).toLowerCase()) {
      refuse(`what this browser worked out for input ${n} is not what the offer asks.`);
    }
    hashes.push(derived);
  }
  const into = named.reduce((t, c) => t + BigInt(c.value || 0), 0n);
  return {hashes, fee: Number(into + exact - tx.outputs.reduce((t, o) => t + o.value, 0n)),
          change: Number(change)};
}

/** Check the finished trade before the seller signs input 0 (SIGHASH_ALL):
 *  input 0 this key's coin, and this key paid its coin back plus the price the
 *  bytes name. The buyer's inputs are already signed and not this key's. */
export async function verifyFill(offer, keys) {
  const refuse = (why) => { throw new Error(`${why} Nothing was signed.`); };
  const tx = parseTx(offer.raw);
  const own = keys.address || await address(keys.pubkey, keys.version);
  const mine = p2pkh(await hash160(keys.pubkey));
  const first = (offer.inputs || [])[0] || {};
  if (tx.inputs[0].txid !== String(first.txid).toLowerCase() || first.address !== own) {
    refuse("input 0 is not this key's coin.");
  }
  const said = listingPayload(opreturnData(tx.outputs[0].script) || refuse("output 0 is not the trade's bytes."));
  const exact = BigInt(offer.exact);
  const paid = tx.outputs.filter((o) => SAME(o.script, mine)).reduce((t, o) => t + o.value, 0n);
  if (paid < exact + said.sats) refuse("that trade does not pay you the price.");
  const derived = hex(await sighashAll(tx, 0, mine));
  if (derived !== String((offer.sighashes || [])[0]).toLowerCase()) {
    refuse("what this browser worked out is not what the node asks.");
  }
  return {hash: derived, sats: Number(said.sats), token: said.token};
}
