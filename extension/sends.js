/* Simple sends from the wallet: coins, an Omni token, an arcade NFT, an inscription.
 *
 * The linked arcade builds the transaction, from its own index of this wallet's coins and by its own rules (coin
 * choice, the inscription guard, fees, the coins a send in flight already took), exactly as for its own Wallet page:
 * /account/send, /account/token/send, /account/nft/send, /account/doginal/send. Nothing is signed until this file has
 * checked the offer against what the person asked for and against the arcade's ord index, and coins.js verifyOffer
 * has read the transaction and worked out every hash it signs from the bytes.
 */
import * as coins from "./coins.js";
import * as txview from "./txview.js";

const refuse = (why) => { throw new Error(`${why} Nothing was signed.`); };

/** "1.5" as satoshis (a string), or a refusal. */
export function satsOf(amount, divisible = true) {
  const m = /^(\d{1,12})(?:\.(\d{1,8}))?$/.exec(String(amount || "").trim());
  if (!m || (!divisible && m[2])) refuse(`"${amount}" is not an amount this wallet reads.`);
  return divisible ? (BigInt(m[1]) * 100000000n + BigInt((m[2] || "").padEnd(8, "0"))).toString() : BigInt(m[1]).toString();
}

/** The arcade route and body for what the person asked: {kind, chain, to, amount?, propertyid?, piece?, txid?, vout?,
 *  inscription?}. */
export function request(ask) {
  const to = String(ask.to || "").trim(), chain = String(ask.chain || "");
  if (!to) refuse("Say who it goes to.");
  switch (ask.kind) {
    case "coin": return ["/account/send", {to, amount: String(ask.amount), chain}];
    case "token": return ["/account/token/send", {property_id: Number(ask.propertyid), to, amount: String(ask.amount), chain}];
    case "nft": return ["/account/nft/send", {piece: String(ask.piece), to, chain}];
    case "inscription": return ["/account/doginal/send", {txid: String(ask.txid), vout: Number(ask.vout),
                                                          inscription: String(ask.inscription), to, chain}];
    default: return refuse("The wallet sends coins, tokens, NFTs and inscriptions only.");
  }
}

/** Check `offer` (the arcade's answer to `request(ask)`) before signing with `keys` (chains.keysOn). `marks` is
 *  arcade.inscriptionsOn for its inputs. Returns {hashes, view}: the digests to sign and what the person is shown. */
export async function check(offer, keys, ask, marks) {
  if (!offer || offer.chain !== ask.chain) refuse("The arcade answered for another chain.");
  if (Number(offer.signed_from || 0) !== 0 || offer.signed_to != null) refuse("That is not a plain send.");
  for (const i of offer.inputs || []) {
    if (i.redeem || i.script || i.envelope || i.segwit) refuse("A send from this wallet spends its own address's coins only.");
  }
  const shown = await coins.verifyOffer(offer, keys);         // every input this key's; every digest from the bytes
  const tx = coins.parseTx(offer.raw, coins.TX_TIME.has(ask.chain));
  const ords = txview.ORD_CHAINS.has(ask.chain);
  const named = offer.inputs.map((i, n) => {
    const mark = marks[`${String(i.txid).toLowerCase()}:${Number(i.vout)}`];
    if (ords && (!mark || mark.state === "unknown")) {
      refuse(`The arcade's ord index cannot say yet whether input ${n} carries an inscription, so it is not spent.`);
    }
    const inscriptions = mark ? mark.inscriptions : [];
    if (inscriptions.length) {
      const one = inscriptions.length === 1 && inscriptions[0].id === ask.inscription && inscriptions[0].offset === 0;
      if (ask.kind !== "inscription" || n !== 0 || !one) {
        refuse(`Input ${n} carries ${inscriptions.map((x) => x.id).join(", ")}: a ${ask.kind} send never spends it.`);
      }
    }
    return {value: String(i.value), address: i.address, mine: true, inscriptions};
  });
  const hidden = txview.hiddenWrite(tx);
  if (hidden) refuse(hidden);
  const view = await txview.describe(tx, ask.chain, named, keys.address);
  const payloads = view.outputs.filter((o) => o.data);
  const toOk = (address) => address && (String(ask.to).startsWith("@") ? address === offer.to : address === String(ask.to).trim());
  if (ask.kind === "coin") {
    if (payloads.length) refuse("A coin send writes nothing to the chain, and this one does.");
    if (!view.outputs.some((o) => toOk(o.address) && o.value === satsOf(ask.amount))) refuse("No output pays them that amount.");
  } else if (ask.kind === "token" || ask.kind === "nft") {
    const p = payloads.length === 1 ? payloads[0].data : null;
    const right = ask.kind === "token"
      ? p && p.kind === "token" && p.propertyid === Number(ask.propertyid) && p.units === satsOf(ask.amount, !!offer.divisible)
      : p && p.kind === "nft" && p.piece === String(ask.piece).toLowerCase();
    if (!right) refuse(`That transaction does not move the ${ask.kind} asked for, or not that much of it.`);
    if (!toOk(p.to)) refuse(`The ${ask.kind} would go to ${p.to || "nobody"}, not to them.`);
  } else {
    const at = view.lands.find((l) => l.id === ask.inscription);
    if (!at || at.output !== 0 || !toOk(view.outputs[0].address) || view.outputs[0].value !== named[0].value) {
      refuse("The inscription does not land whole on their output.");
    }
    if (payloads.length) refuse("An inscription send writes nothing else to the chain, and this one does.");
  }
  view.to = offer.to_tag ? `@${offer.to_tag} (${offer.to})` : offer.to || ask.to;
  return {hashes: shown.hashes, view};
}
