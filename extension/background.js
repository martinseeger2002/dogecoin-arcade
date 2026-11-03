/* The Arcade Wallet's service worker: the vault, the lock, the sites a person connected, and every request a page
 * makes through window.arcade.
 *
 * Who is asking is the browser's word (port.sender.origin), never the page's. Nothing is revealed or signed
 * without the extension's own window showing that origin, except `arcade_accounts` for a site already connected
 * while the wallet is open.
 */
import * as keys from "./keys.js";
import * as chains from "./chains.js";
import * as arcade from "./arcade.js";

const LOCK_MINUTES = 30;
const pending = new Map();          // request id -> {origin, method, params, resolve, window}
let nextId = 1;

/* --- the vault ------------------------------------------------------------------------------------------------ */

async function vault() { return (await chrome.storage.local.get("vault")).vault || null; }
async function phrase() { return (await chrome.storage.session.get("phrase")).phrase || null; }
async function sites() { return (await chrome.storage.local.get("sites")).sites || {}; }

async function arcades() { return (await chrome.storage.local.get("arcades")).arcades || []; }

async function seed() {
  const words = await phrase();
  return words ? keys.toSeed(words) : null;
}

async function account() {
  const s = await seed();
  return s ? keys.accountKey(s) : null;
}

/** The open wallet's key and seed, or a refusal: nothing is read or signed for a locked wallet. */
async function opened() {
  const s = await seed();
  if (!s) throw new Error("unlock the Arcade Wallet first");
  touch();
  return {seed: s, account: await keys.accountKey(s)};
}

/** Link an arcade: sign in there, tell it this wallet's addresses, and keep it (the first one linked is read first). */
async function link(text) {
  const origin = arcade.arcadeOrigin(text);
  const {seed: s, account: key} = await opened();
  await arcade.signIn(origin, key);
  const mine = await chains.addresses(s);
  await arcade.tellAddresses(origin, key, mine, await arcade.holdings(origin, key, mine));
  const list = (await arcades()).filter((o) => o !== origin);
  await chrome.storage.local.set({arcades: [...list, origin]});
  return origin;
}

/** Every chain's balance, Omni tokens and inscription count, as the first linked arcade reports them. */
async function balances() {
  const {seed: s, account: key} = await opened();
  const [origin] = await arcades();
  const mine = await chains.addresses(s);
  if (!origin) return {origin: null, mine, rows: []};
  const rows = await arcade.holdings(origin, key, mine);
  for (const row of rows) {
    if (row.ours && row.address) Object.assign(row, await arcade.assets(origin, row.address));
    if (row.balance != null) row.coins = chains.coinsOf(row.balance);
  }
  return {origin, mine, rows};
}

async function unlock(password) {
  const blob = await vault();
  if (!blob) throw new Error("there is no wallet in this browser yet");
  const words = await keys.open(blob, password);
  await chrome.storage.session.set({phrase: words});
  touch();
}

async function lock() {
  await chrome.storage.session.remove("phrase");
  tell("locked", {});
}

function touch() { chrome.alarms.create("lock", {delayInMinutes: LOCK_MINUTES}); }
chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === "lock") lock(); });

/** Tell every connected page's tabs something happened (accountChanged, locked, disconnect). */
async function tell(event, data, onlyOrigin) {
  const connected = await sites();
  for (const tab of await chrome.tabs.query({})) {
    let origin = "";
    try { origin = new URL(tab.url || "").origin; } catch (e) { continue; }
    if (!connected[origin] || (onlyOrigin && origin !== onlyOrigin)) continue;
    chrome.tabs.sendMessage(tab.id, {event, data}).catch(() => {});
  }
}

/* --- what a page may ask -------------------------------------------------------------------------------------- */

function secureOrigin(origin) {
  try {
    const u = new URL(origin);
    return u.protocol === "https:" || ["localhost", "127.0.0.1", "[::1]"].includes(u.hostname);
  } catch (e) { return false; }
}

const METHODS = {
  async arcade_accounts(origin) {
    const connected = (await sites())[origin];
    const key = connected ? await account() : null;
    return key ? [{pubkey: key.pubkey}] : [];
  },
  async arcade_connect(origin) {
    await approve(origin, "connect", {});
    const key = await account();
    await chrome.storage.local.set({sites: {...await sites(), [origin]: {since: Date.now()}}});
    return {pubkey: key.pubkey};
  },
  async arcade_signIn(origin, params) {
    const nonce = String((params || {}).nonce || "");
    if (!keys.NONCE.test(nonce)) throw new Error("a sign-in needs the arcade's nonce: 32 to 128 hex characters");
    const message = new TextDecoder().decode(keys.loginMessage(origin, nonce));
    await approve(origin, "signIn", {message});
    const key = await account();
    const signature = await key.sign(keys.loginMessage(origin, nonce));
    await chrome.storage.local.set({sites: {...await sites(), [origin]: {since: Date.now()}}});
    touch();
    return {pubkey: key.pubkey, origin, nonce, signature: keys.hex(signature)};
  },
  async arcade_disconnect(origin) {
    const all = await sites();
    delete all[origin];
    await chrome.storage.local.set({sites: all});
    tell("disconnect", {}, origin);
    return true;
  },
};

/** Open the approval window and wait for the person's answer; throws if they refuse or close it. */
function approve(origin, kind, detail) {
  return new Promise(async (resolve, reject) => {
    const id = String(nextId++);
    const win = await chrome.windows.create({url: `approve.html?id=${id}`, type: "popup", width: 420, height: 600});
    pending.set(id, {origin, kind, detail, resolve, reject, window: win.id});
  });
}

chrome.windows.onRemoved.addListener((windowId) => {
  for (const [id, p] of pending) {
    if (p.window === windowId) { pending.delete(id); p.reject(new Error("the request was refused")); }
  }
});

/* --- the wires: pages through content.js, and the extension's own pages ---------------------------------------- */

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "request") return;
  port.onMessage.addListener(async ({method, params}) => {
    const origin = port.sender && port.sender.origin;
    try {
      if (!origin || !secureOrigin(origin)) throw new Error("the Arcade Wallet works only on https pages");
      const fn = Object.prototype.hasOwnProperty.call(METHODS, method) ? METHODS[method] : null;
      if (!fn) throw new Error(`the Arcade Wallet has no method ${JSON.stringify(String(method))}`);
      port.postMessage({ok: true, result: await fn(origin, params)});
    } catch (e) {
      port.postMessage({ok: false, error: String(e.message || e)});
    }
  });
});

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  // Only the extension's own pages (popup, approve) talk this way; a content script never sends these.
  if (!sender.url || !sender.url.startsWith(chrome.runtime.getURL(""))) return false;
  (async () => {
    switch (msg.type) {
      case "status": {
        const key = await account().catch(() => null);
        return {vault: !!(await vault()), open: !!key, pubkey: key ? key.pubkey : null, sites: await sites()};
      }
      case "create": {
        const said = await keys.complaint(msg.phrase);
        if (said) throw new Error(said);
        await chrome.storage.local.set({vault: await keys.seal(msg.phrase, msg.password)});
        await chrome.storage.session.set({phrase: keys.normalise(msg.phrase)});
        touch();
        tell("accountChanged", {});
        return true;
      }
      case "unlock": await unlock(msg.password); return true;
      case "lock": await lock(); return true;
      case "arcades": return arcades();
      case "link": return link(msg.origin);
      case "unlink":
        await chrome.storage.local.set({arcades: (await arcades()).filter((o) => o !== msg.origin)});
        return true;
      case "balances": return balances();
      case "remove":
        await chrome.storage.local.remove(["vault", "sites", "arcades"]);
        await lock();
        return true;
      case "forget": return METHODS.arcade_disconnect(msg.origin);
      case "pending": {
        const p = pending.get(msg.id);
        if (!p) throw new Error("this request is gone");
        const key = await account().catch(() => null);
        return {origin: p.origin, kind: p.kind, detail: p.detail, open: !!key, pubkey: key ? key.pubkey : null};
      }
      case "answer": {
        const p = pending.get(msg.id);
        if (!p) throw new Error("this request is gone");
        if (msg.yes && msg.password) await unlock(msg.password);
        if (msg.yes && !(await phrase())) throw new Error("unlock the wallet first");
        pending.delete(msg.id);
        if (msg.yes) p.resolve(); else p.reject(new Error("the request was refused"));
        chrome.windows.remove(p.window).catch(() => {});
        return true;
      }
      default: throw new Error("unknown request");
    }
  })().then((result) => reply({ok: true, result}), (e) => reply({ok: false, error: String(e.message || e)}));
  return true;
});
