/* The node's own wallet, for the Wallet page the accounts use (my_wallet.html with node_side; 2026-10-19:
 * "Everything should look the same as it looks on the client side on the operator side").
 *
 * The same calls the page makes of wallet.js, answered by the node: its coins are in its Core wallets (or, on a
 * borrowed chain, at its identity key's address, keywallet.py), so the node builds and signs a send and holds it
 * under a ticket; nothing leaves until confirm(). From outside the node machine the admin password is asked, as
 * Admin asks it. */

const coinsOf = (n) => (Number(n || 0) / 100000000).toLocaleString(undefined,
  {minimumFractionDigits: 0, maximumFractionDigits: 8});

async function call(path, body) {
  const r = await fetch(path, {method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-Arcade-Admin": "1"}, body: JSON.stringify(body || {})});
  const said = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(said.detail || `refused (${r.status})`); e.status = r.status; throw e; }
  return said;
}

export async function state() {
  return (await fetch("/node/wallet/state", {cache: "no-store"})).json();
}

/** Always open: the node's keys are its own, never in this browser. */
export async function opened() { return {node: true}; }

export async function offerSend(to, amount, chain) {
  return call("/node/wallet/send", {to, amount, chain: chain || ""});
}

/** What the review shows: the node's own figures for the transaction it built. */
export async function checked(offer) {
  return {fee: Number(offer.fee || 0), change: 0, coinsOf};
}

export async function confirm(keys, offer) {
  try {
    return await call("/admin/api/send/confirm", {ticket: offer.ticket});
  } catch (e) {
    if (e.status !== 403) throw e;
    const password = window.prompt("The admin password, to send the node's coins from outside its machine:");
    if (!password) throw new Error("not sent: the admin password is needed from outside the node machine");
    return call("/admin/api/send/confirm", {ticket: offer.ticket, password});
  }
}

/** Claim the @name typed on the page (me.html's claim panel), with the key, in one announcement. */
export async function claim() {
  // The setup wizard's one button (2026-11-02): the name and the key together, and the REAL outcome -- /publish
  // answered a refusal ("no coins") as an ordinary page, and this said "on its way" for a claim that never went.
  const box = document.getElementById("claim-name");
  const tag = ((box && box.value) || "").trim().replace(/^@/, "");
  if (!tag) throw new Error("type the name first");
  const r = await fetch("/admin/api/setup/claim", {method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-Arcade-Admin": "1"}, body: JSON.stringify({tag})});
  const said = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(said.detail || `refused (${r.status})`);
  return {txid: said.claimed || said.published || "", ...said};
}

export async function tellTheNode() {}
