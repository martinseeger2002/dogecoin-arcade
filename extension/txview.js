/* A transaction as the approval window says it, read from its own bytes: who each output pays and how much, which
 * are this wallet's change, the Omni token or NFT a payload moves and to whom, which output each inscription lands
 * on, and the fee. Nothing here is taken from what a page or an arcade says the transaction does.
 */
import * as coins from "./coins.js";
import * as chains from "./chains.js";

//: Chains whose inscriptions an arcade reads (arcade/ord_chains.py WORDS). On these a coin is spent only once the
//: arcade's ord index has said what it carries; regtest and Kotia have no ords.
export const ORD_CHAINS = new Set(["doge", "main", "bel", "lky", "wjk", "dgb", "ltc", "ltctest", "test"]);

//: The sighash types this wallet signs, as arcade/web/docs/exchange-safety.md allows them; NONE never.
export const SIGHASH = {ALL: 0x01, ALL_ANYONECANPAY: 0x81, SINGLE_ANYONECANPAY: 0x83};

const SAME = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
const OMNI = [0x6f, 0x6d, 0x6e, 0x69], ARCD = [0x61, 0x72, 0x63, 0x64], INSC = [0x49, 0x4e, 0x53, 0x43];
const big = (bytes) => bytes.reduce((n, b) => (n << 8n) | BigInt(b), 0n);

/** The address an output pays on `network`, or null for a script that is not an address. */
export async function addressOf(script, network) {
  const c = chains.chain(network);
  if (script.length === 25 && script[0] === 0x76 && script[1] === 0xa9 && script[2] === 20 && script[23] === 0x88
      && script[24] === 0xac) return coins.base58check(c.pubkeyhash, script.slice(3, 23));
  if (script.length === 23 && script[0] === 0xa9 && script[1] === 20 && script[22] === 0x87)
    return coins.base58check(c.scripthash, script.slice(2, 22));
  if (c.hrp && script.length === 22 && script[0] === 0 && script[1] === 20) return coins.segwitEncode(c.hrp, 0, script.slice(2));
  return null;
}

/** What an OP_RETURN carries: an Omni Simple Send ({kind: "token", propertyid, units}), an arcade NFT transfer
 *  ({kind: "nft", piece}), other bytes ({kind: "data", bytes}); null when the output is not an OP_RETURN. */
export function payload(script) {
  if (!script.length || script[0] !== 0x6a) return null;
  let at = 1, length = script[1] || 0;
  if (length === 0x4c) { length = script[2]; at = 3; } else at = 2;
  const data = script.slice(at, at + length);
  const marked = SAME([...data.slice(0, 4)], ARCD) || SAME([...data.slice(0, 4)], OMNI);
  if (marked && data.length === 20 && data[4] === 0 && data[5] === 0 && data[6] === 0 && data[7] === 0)
    return {kind: "token", propertyid: Number(big(data.slice(8, 12))), units: big(data.slice(12, 20)).toString()};
  if (marked && data.length === 46 && data[6] === 0 && data[7] === 200 && SAME([...data.slice(8, 12)], INSC)
      && data[12] === 1 && data[13] === 2) return {kind: "nft", piece: coins.hex(data.slice(14))};
  return {kind: "data", bytes: data.length};
}

/** Why a transaction may carry an operation the window cannot show, or null when it shows all of it (2026-11-03,
 *  a test machine's review of #73: Omni's "Send All" is 9 bytes, which the window said only as "Writes 9 bytes", so a site's
 *  transaction spending one small coin could move every token). Refused: more than one OP_RETURN; an OP_RETURN with
 *  anything after its first push (Omni joins pushes); an Omni or arcade marker anywhere in one that does not decode
 *  as an operation the window shows; and a bare multisig output (Omni's Class B encoding). */
export function hiddenWrite(tx) {
  let writes = 0;
  const has = (data, mark) => { for (let i = 0; i + mark.length <= data.length; i++) if (SAME([...data.slice(i, i + mark.length)], mark)) return true; return false; };
  for (const [n, o] of tx.outputs.entries()) {
    const s = o.script;
    if (s.length && s[0] === 0x6a) {
      writes += 1;
      if (s.length === 1) continue;
      let at, length = s[1];
      if (length === 0x4c) { length = s[2]; at = 3; } else if (length <= 0x4b) at = 2;
      else return `Output ${n} writes data in a form this wallet cannot show.`;
      if (at + length !== s.length) return `Output ${n} writes more than one piece of data, which this wallet cannot show.`;
      const data = s.slice(at, at + length);
      if ((has(data, OMNI) || has(data, ARCD)) && payload(s).kind === "data")
        return `Output ${n} carries a token operation this wallet cannot show.`;
    } else if (s.length && s[s.length - 1] === 0xae) {
      return `Output ${n} is a bare multisig script, which can carry token data this wallet cannot show.`;
    }
  }
  if (writes > 1) return "It writes data to the chain more than once, which this wallet cannot show.";
  return null;
}

/** Omni's recipient (tx.determine_reference): the one output with an address, else the last of them after the
 *  first one back to the sender. Its index, or -1. */
export function reference(addresses, sender) {
  const at = addresses.map((a, n) => (a ? n : -1)).filter((n) => n >= 0);
  if (at.length <= 1) return at.length ? at[0] : -1;
  let found = -1, skipped = false;
  for (const n of at) {
    if (addresses[n] === sender && !skipped) { skipped = true; continue; }
    found = n;
  }
  return found;
}

/** Where each inscription goes, by the ordinal rule (first in, first out): `inputs` [{value, inscriptions:
 *  [{id, offset}]}], `outputs` [value]. [{id, input, output}]; output null means it would be burned to the fee, and
 *  undefined that it cannot be worked out (an input before it whose value is not known). */
export function landing(inputs, outputs) {
  const out = [];
  let before = 0n, known = true;
  inputs.forEach((input, n) => {
    for (const ins of input.inscriptions || []) {
      if (!known) { out.push({id: ins.id, input: n, output: undefined}); continue; }
      const sat = before + BigInt(ins.offset || 0);
      let edge = 0n, output = null;
      for (let k = 0; k < outputs.length; k++) {
        edge += BigInt(outputs[k]);
        if (sat < edge) { output = k; break; }
      }
      out.push({id: ins.id, input: n, output});
    }
    if (input.value == null) known = false; else before += BigInt(input.value);
  });
  return out;
}

/** Everything the window shows. `named`: per input {value?, address?, mine, inscriptions?, sighash?}; `own`: this
 *  wallet's address on the chain. */
export async function describe(tx, network, named, own) {
  const c = chains.chain(network);
  const addresses = [];
  for (const o of tx.outputs) addresses.push(await addressOf(o.script, network));
  const outputs = tx.outputs.map((o, n) => ({n, value: o.value.toString(), coins: chains.coinsOf(o.value),
                                            address: addresses[n], mine: !!own && addresses[n] === own,
                                            data: payload(o.script), receives: []}));
  for (const o of outputs) {
    if (!o.data || o.data.kind === "data") continue;
    const to = reference(addresses, own);
    o.data.to = to >= 0 ? addresses[to] : null;
    o.data.toOutput = to;
  }
  const lands = landing(named.map((i) => ({value: i.value, inscriptions: i.inscriptions})), tx.outputs.map((o) => o.value));
  for (const l of lands) if (l.output != null) outputs[l.output].receives.push(l.id);
  const into = named.every((i) => i.value != null) ? named.reduce((t, i) => t + BigInt(i.value), 0n) : null;
  const out = tx.outputs.reduce((t, o) => t + o.value, 0n);
  return {
    network, label: c.label, coin: c.coin, real: c.real,
    inputs: tx.inputs.map((i, n) => ({txid: i.txid, vout: i.vout, ...named[n],
                                      value: named[n].value == null ? null : String(named[n].value)})),
    outputs, lands,
    fee: into == null ? null : (into - out).toString(), feeCoins: into == null ? null : chains.coinsOf(into - out),
    sent: chains.coinsOf(outputs.filter((o) => !o.mine).reduce((t, o) => t + BigInt(o.value), 0n)),
  };
}

const le = (value, n) => { const b = new Uint8Array(n); let v = BigInt(value); for (let i = 0; i < n; i++) { b[i] = Number(v & 255n); v >>= 8n; } return b; };
const size = (n) => n < 0xfd ? [n] : n <= 0xffff ? [0xfd, ...le(n, 2)] : [0xfe, ...le(n, 4)];

/** A P2PKH input's scriptSig: the signature with its sighash byte, then the compressed key. */
export function scriptSig(signature, pubkey) {
  return new Uint8Array([signature.length, ...signature, pubkey.length, ...pubkey]);
}

/** The transaction's bytes again, with `sigs[n]` as input n's scriptSig where given (its own one otherwise). */
export function serialize(tx, sigs = []) {
  const parts = [...le(tx.version, 4), ...(tx.time == null ? [] : le(tx.time, 4)), ...size(tx.inputs.length)];
  tx.inputs.forEach((i, n) => {
    const s = sigs[n] || i.scriptSig || new Uint8Array(0);
    parts.push(...coins.unhex(i.txid).reverse(), ...le(i.vout, 4), ...size(s.length), ...s,
               ...le(i.sequence === undefined ? 0xffffffff : i.sequence, 4));
  });
  parts.push(...size(tx.outputs.length));
  for (const o of tx.outputs) parts.push(...le(o.value, 8), ...size(o.script.length), ...o.script);
  parts.push(...le(tx.locktime, 4));
  return coins.hex(new Uint8Array(parts));
}
