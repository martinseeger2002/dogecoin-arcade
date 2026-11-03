/* The arcades this wallet is linked to: where it reads balances, coins, tokens and inscriptions, and where it hands
 * signed transactions. The wallet runs no node and keeps no chain data; an arcade says what the chains hold.
 *
 * It signs in to a linked arcade itself, the way any arcade page does (GET /auth/challenge, the login message signed
 * with the account key, POST /auth/login), and then uses the account routes the arcade's own Wallet page uses. Every
 * call names the arcade's own origin; a page never chooses where these go.
 */
import * as keys from "./keys.js";

/** An arcade's origin as it is kept, or a refusal: https only, or this machine for a test arcade. */
export function arcadeOrigin(text) {
  let u;
  try { u = new URL(String(text || "").trim()); } catch (e) { throw new Error("Type the arcade's address, like https://example.com"); }
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(u.hostname);
  if (u.protocol !== "https:" && !(local && u.protocol === "http:")) throw new Error("An arcade is linked over https only.");
  return u.origin;
}

async function said(answer) {
  const body = await answer.json().catch(() => ({}));
  if (!answer.ok) throw Object.assign(new Error(body.detail || `the arcade said ${answer.status}`), {status: answer.status});
  return body;
}

/** Sign in to `origin` with the account key (`account` from keys.accountKey). */
export async function signIn(origin, account) {
  const challenge = await said(await fetch(`${origin}/auth/challenge`, {credentials: "include"}));
  if (!keys.NONCE.test(String(challenge.nonce || ""))) throw new Error("that arcade sent a nonce this wallet does not sign");
  const signature = keys.hex(await account.sign(keys.loginMessage(origin, challenge.nonce)));
  return said(await fetch(`${origin}/auth/login`, {
    method: "POST", credentials: "include", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({pubkey: account.pubkey, nonce: challenge.nonce, origin, signature}),
  }));
}

/** One call to a linked arcade as the account: signed in again once if its session ended. */
export async function call(origin, path, {body, account} = {}) {
  const once = () => fetch(`${origin}${path}`, body === undefined ? {credentials: "include"} : {
    method: "POST", credentials: "include", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  });
  let answer = await once();
  if (answer.status === 403 && account) {
    await signIn(origin, account);
    answer = await once();
  }
  return said(answer);
}

/** What the account holds on every chain this arcade runs for it: [{network, label, coin, address, balance, ...}].
 *  `mine` is this wallet's own addresses ({network: {address}}): a row whose address is not this wallet's says so. */
export async function holdings(origin, account, mine) {
  const state = await call(origin, "/account?chains=all", {account});
  if (state.pubkey !== account.pubkey) {
    await signIn(origin, account);
    return holdings(origin, account, mine);
  }
  return (state.chains || []).map((row) => ({
    ...row, ours: !!mine[row.network] && row.address === mine[row.network].address,
  }));
}

/** Tell the arcade this wallet's address on each chain it runs, where it knows another or none (wallet_js
 *  `tellTheNode`): its sends are built from, and its coins watched at, the address it was told. */
export async function tellAddresses(origin, account, mine, rows) {
  for (const row of rows) {
    const own = mine[row.network];
    if (!own || row.address === own.address) continue;
    await call(origin, "/account/address", {account, body: {address: own.address, chain: row.network,
                                                            coin_pubkey: own.pubkey}});
  }
}

/** An address's Omni tokens and inscriptions, from the arcade's public reads. */
export async function assets(origin, address) {
  const get = async (path) => said(await fetch(`${origin}${path}`, {credentials: "omit"}));
  const [tokens, count] = await Promise.all([
    get(`/r/balances/${encodeURIComponent(address)}`).catch(() => []),
    get(`/r/inscriptions/count?owner=${encodeURIComponent(address)}`).catch(() => ({count: 0})),
  ]);
  return {tokens: Array.isArray(tokens) ? tokens : [], inscriptions: Number(count.count || 0)};
}

/** What the arcade's ord index says sits on each of `coins` ([{txid, vout}], this wallet's own on `network`):
 *  {"txid:vout": {state: "clean" | "inscribed" | "unknown", inscriptions: [{id, offset}]}}. "unknown" is anything the
 *  index could not answer for now, a coin it did not list, or a chain whose inscriptions it does not check. */
export async function inscriptionsOn(origin, account, network, address, list) {
  const out = {};
  for (const c of list) out[`${c.txid}:${c.vout}`] = {state: "unknown", inscriptions: []};
  const txids = [...new Set(list.map((c) => c.txid))];
  for (let at = 0; at < txids.length; at += 100) {
    let said;
    try {
      said = await call(origin, "/account/doginal-txs",
                        {account, body: {chain: network, address, txids: txids.slice(at, at + 100)}});
    } catch (e) {
      if (e.status === 404 || e.status === 400 || e.status === 503) return out;     // not checked: unknown, all of it
      throw e;
    }
    for (const c of list) {
      const rows = (said.txs || {})[c.txid];
      if (!Array.isArray(rows)) continue;                                            // null or not listed: unknown
      const here = rows.filter((r) => Number(r.vout) === Number(c.vout));
      if (here.some((r) => r.unknown || r.wait || !r.inscription_id)) continue;
      out[`${c.txid}:${c.vout}`] = {state: here.length ? "inscribed" : "clean",
                                    inscriptions: here.map((r) => ({id: r.inscription_id, offset: Number(r.sat_offset || 0)}))};
    }
  }
  return out;
}
