/* A transaction a site built, signed by this wallet (arcade_signTransaction), or signed and handed to the linked
 * arcade to broadcast (arcade_sendTransaction).
 *
 * The site gives the unsigned bytes and nothing else is believed: which inputs are this wallet's, and what they are
 * worth, comes from the linked arcade's list of the account's own coins; what each carries, from its ord index.
 * Signatures are only the kinds arcade/web/docs/exchange-safety.md allows (ALL, ALL|ANYONECANPAY, and
 * SINGLE|ANYONECANPAY over an output that exists), never NONE.
 */
import * as coins from "./coins.js";
import * as txview from "./txview.js";

const refuse = (why) => { throw new Error(`${why} Nothing was signed.`); };
const DIGEST = {[txview.SIGHASH.ALL]: coins.sighashAll, [txview.SIGHASH.ALL_ANYONECANPAY]: coins.sighashAllAcp,
                [txview.SIGHASH.SINGLE_ANYONECANPAY]: coins.sighashLeg};
export const SAID = {[txview.SIGHASH.ALL]: "covers every input and output",
                     [txview.SIGHASH.ALL_ANYONECANPAY]: "covers every output; others may add their own coins",
                     [txview.SIGHASH.SINGLE_ANYONECANPAY]: "covers only the output beside it; anyone may complete the rest"};

/** What signing `params` ({chain, raw, sign?: [input indexes], sighash?: 1|0x81|0x83, inscriptions?: [ids]}) means.
 *  `own`: {"txid:vout": {value, address}} the account's coins on the chain; `marks`: arcade.inscriptionsOn for them;
 *  `keys`: chains.keysOn. `send`: for broadcast, which takes ALL and plain coins only. Returns {tx, sign, view}. */
export async function plan(params, own, marks, keys, {send = false} = {}) {
  const chain = String(params.chain || "");
  const timed = coins.TX_TIME.has(chain);
  let tx;
  try { tx = coins.parseTx(String(params.raw || "").toLowerCase(), timed); } catch (e) { refuse(`That is not a transaction this wallet reads: ${e.message}.`); }
  const type = params.sighash == null ? txview.SIGHASH.ALL : Number(params.sighash);
  if (!DIGEST[type]) refuse("This wallet signs ALL, ALL|ANYONECANPAY or SINGLE|ANYONECANPAY only, never NONE.");
  if ((send || timed) && type !== txview.SIGHASH.ALL) refuse("Only SIGHASH_ALL here.");
  const mine = tx.inputs.map((i) => own[`${i.txid}:${i.vout}`] || null);
  const sign = params.sign == null ? mine.map((m, n) => (m ? n : -1)).filter((n) => n >= 0) : params.sign.map(Number);
  if (!sign.length) refuse("It spends none of this wallet's coins.");
  if (send && mine.some((m) => !m)) refuse("A transaction this wallet sends spends its own coins only.");
  const declared = new Set((params.inscriptions || []).map(String));
  const named = tx.inputs.map((i, n) => ({mine: !!mine[n], value: mine[n] ? String(mine[n].value) : null,
                                          address: mine[n] ? mine[n].address : null, inscriptions: []}));
  for (const n of new Set(sign)) {
    const input = tx.inputs[n];
    if (!input || !mine[n]) refuse(`Input ${n} is not a coin of this wallet's.`);
    if (input.scriptSig.length) refuse(`Input ${n} is signed already.`);
    if (type !== txview.SIGHASH.ALL && input.sequence !== 0xffffffff) refuse(`Input ${n} has a sequence this wallet does not sign that way.`);
    if (type === txview.SIGHASH.SINGLE_ANYONECANPAY && n >= tx.outputs.length) refuse(`Input ${n} has no output beside it.`);
    const mark = marks[`${input.txid}:${input.vout}`];
    if (txview.ORD_CHAINS.has(chain) && (!mark || mark.state === "unknown")) {
      refuse(`The arcade's ord index cannot say yet whether input ${n} carries an inscription.`);
    }
    named[n].inscriptions = mark ? mark.inscriptions : [];
    named[n].sighash = type;
    const undeclared = named[n].inscriptions.filter((i) => !declared.has(i.id));
    if (send && named[n].inscriptions.length) refuse(`Input ${n} carries an inscription; a send never spends it.`);
    if (undeclared.length) refuse(`Input ${n} carries ${undeclared.map((i) => i.id).join(", ")}, which the site did not say it moves.`);
  }
  const hidden = txview.hiddenWrite(tx);
  if (hidden) refuse(hidden);
  const view = await txview.describe(tx, chain, named, keys.address);
  if (type === txview.SIGHASH.ALL) {
    for (const l of view.lands) {
      if (sign.includes(l.input) && l.output == null) refuse(`Inscription ${l.id} would be burned, or its landing cannot be worked out.`);
    }
  }
  const script = coins.p2pkh(await coins.hash160(keys.pubkey));
  const out = [];
  for (const n of new Set(sign)) out.push({n, type, digest: await DIGEST[type](tx, n, script)});
  view.sighash = SAID[type];
  return {tx, sign: out, view};
}

/** The transaction with this wallet's signatures in, as hex. */
export async function finish(planned, keys) {
  const sigs = [];
  for (const s of planned.sign) sigs[s.n] = txview.scriptSig(await coins.signInput(keys.key, s.digest, s.type), keys.pubkey);
  return txview.serialize(planned.tx, sigs);
}
