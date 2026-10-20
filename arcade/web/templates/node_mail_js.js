/* The node's own identity, for the pages the accounts use (me.html with node_side; 2026-10-19). What messaging.js
 * does in an account's tab -- publish the key, the name, the profile -- the node does with its own wallet, through
 * the routes its old Overview and Address book posted to. */

async function form(path, fields) {
  const body = new URLSearchParams({csrf_token: (window.ARCADE_CSRF || ""), ...fields});
  const r = await fetch(path, {method: "POST", credentials: "same-origin", body, redirect: "follow"});
  if (!r.ok) throw new Error(`refused (${r.status})`);
  return r;
}

async function said() {
  return (await fetch("/node/wallet/state", {cache: "no-store"})).json();
}

export async function identity() {
  const s = await said();
  return {publicKey: Uint8Array.from((s.identity || "").match(/../g) || [], (h) => parseInt(h, 16)), node: true};
}

export async function contactCode() {
  return (await said()).contact || "—";
}

/** Publish the key (with the name it has, if any): /publish files both in one announcement. */
export async function announce() {
  const s = await said();
  await form("/publish", {tag: s.tag || s.name || ""});
  return {ok: true};
}

/** The node's profile: a picture goes by /profile/picture, a bio and link by /profile/about. */
export async function publishProfile(keys, me, profile) {
  if ("bio" in profile || "url" in profile) {
    await form("/profile/about", {bio: profile.bio || "", url: profile.url || ""});
  }
  if (profile.pfp !== undefined && !("bio" in profile)) {
    await form("/profile/picture", {piece: profile.pfp || ""});
  }
  return {ok: true};
}

/* The node's Address book (its contacts table), in the shape messaging.js keeps an account's book. */
async function call(path, body) {
  const r = await fetch(path, {method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-Arcade-Admin": "1"}, body: JSON.stringify(body || {})});
  const said = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(said.detail || `refused (${r.status})`);
  return said;
}
export async function book() {
  try { return ((await (await fetch("/node/contacts", {cache: "no-store"})).json()).book || [])
    .sort((a, b) => a.tag.localeCompare(b.tag)); } catch (e) { return []; }
}
export async function addToBook(tag) { return call("/node/contacts/add", {tag}); }
export async function updateBookEntry(tag, fields) { return call("/node/contacts/update", {tag, ...fields}); }
export async function removeFromBook(tag) { return call("/node/contacts/remove", {tag}); }
export async function findNames(text) {
  const answer = await fetch(`/account/find?q=${encodeURIComponent(text)}`);
  if (!answer.ok) return [];
  return (await answer.json()).matches;
}
export async function lookUp(name) {
  const answer = await fetch(`/account/who/${encodeURIComponent(name)}`);
  const said = await answer.json();
  if (!answer.ok) throw new Error(said.detail || "nobody by that name");
  return said;
}
export async function syncMailbox() { return false; }

/* --- the node's Messenger (my_messages.html with node_side) ---------------------------------------------------
 * The node opened its own messages already; these are both sides of every conversation, as letters in the shape
 * messaging.js keeps an account's (/node/mail/letters). Reading a body is messaging.js's own code. */
import {text as textOf, attachment as fileOf, profile as profileOf, hex as hexOf, rootOf as rootOfGroup}
  from "/messaging.js";
export const text = textOf, attachment = fileOf, profile = profileOf, hex = hexOf, rootOf = rootOfGroup;

let held = null;
async function letters(fresh = true) {
  if (fresh || !held) held = (await (await fetch("/node/mail/letters", {cache: "no-store"})).json()).letters || [];
  return held;
}
export async function inbox() { return letters(); }

export async function threads() {
  const all = await letters();
  const known = new Map((await book()).filter((e) => e.key).map((e) => [e.key, e]));
  const by = new Map();
  for (const l of all) {
    let t = by.get(l.peer);
    if (!t) {
      const entry = known.get(l.peer);
      t = {peer: l.peer, tag: entry ? entry.tag : "", address: entry ? entry.address : (l.from_address || l.to_address || ""),
           inBook: !!entry, last: 0, unread: 0, count: 0, preview: "", outgoing: false};
      by.set(l.peer, t);
    }
    t.count += 1;
    if (!l.mine && l.unread) t.unread += 1;
    if ((l.when || 0) >= t.last) { t.last = l.when || 0; t.preview = text(l); t.outgoing = !!l.mine; }
  }
  return [...by.values()].sort((a, b) => b.last - a.last);
}

export async function conversation(peer) {
  return (await letters(false)).filter((l) => l.peer === peer).sort((a, b) => (a.when || 0) - (b.when || 0));
}

export async function markRead(peer) {
  try { await call("/node/mail/read", {peer}); } catch (e) { /* drawn again next look */ }
}

export async function byKey(peer) {
  for (const e of await book()) if (e.key && e.key === peer) return e;
  return null;
}

export async function reach(name) {
  const wanted = String(name || "").trim().replace(/^@/, "");
  for (const e of await book()) if (e.tag === wanted && e.key) return e;
  return lookUp(wanted);
}

/** Send in a conversation: the node seals and sends it (/messages/<key>/send), as its own Messenger did. */
export async function write(keys, me, to, words) {
  const them = await lookUp(to);
  if (!them.key) throw new Error(them.detail || "they have no key published");
  const body = new URLSearchParams({csrf_token: (window.ARCADE_CSRF || ""), body: words, confirmed: "yes"});
  const r = await fetch(`/messages/${them.key}/send`, {method: "POST", credentials: "same-origin", body});
  if (!r.ok) throw new Error(`not sent (${r.status})`);
  return {ok: true};
}

/** Look for new messages: the node's own scan. */
export async function collect() {
  const before = (held || []).length;
  await fetch("/scan", {method: "POST", credentials: "same-origin",
                        body: new URLSearchParams({csrf_token: (window.ARCADE_CSRF || "")})}).catch(() => null);
  const now = (await letters()).length;
  return {looked: now, opened: Math.max(0, now - before)};
}

// Groups live in an account's browser; the node's Messenger has none yet.
export function groupOfPeer() { return null; }
export async function currentGroup() { return null; }
const noGroups = async () => { throw new Error("groups are not on the node's Messenger yet"); };
export const createGroup = noGroups, writeGroup = noGroups, removeFromGroup = noGroups;
export async function tendHolderChats() { return {joined: 0, left: 0}; }
