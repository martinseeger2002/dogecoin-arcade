import * as keys from "./keys.js";
import {drawTx} from "./render.js";

const $ = (id) => document.getElementById(id);
const say = (text) => { $("err").textContent = text || ""; };
const ask = (msg) => chrome.runtime.sendMessage(msg).then((r) => {
  if (!r || !r.ok) throw new Error((r && r.error) || "the wallet did not answer");
  return r.result;
});
let chosen = "";            // the words waiting for a password

async function draw() {
  const s = await ask({type: "status"});
  $("none").hidden = s.vault;
  $("locked").hidden = !s.vault || s.open;
  $("open").hidden = !s.open;
  if (s.open) {
    $("pubkey").textContent = s.pubkey;
    const list = $("sites");
    list.textContent = "";
    const origins = Object.keys(s.sites || {}).sort();
    origins.forEach((origin) => {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = origin;
      const forget = document.createElement("button");
      forget.textContent = "Forget";
      forget.onclick = () => ask({type: "forget", origin}).then(draw).catch((e) => say(e.message));
      li.append(name, forget);
      list.append(li);
    });
    $("no-sites").hidden = origins.length > 0;
    drawArcades().catch((e) => say(e.message));
  }
}

function item(list, text, button, onclick) {
  const li = document.createElement("li");
  const name = document.createElement("span");
  name.textContent = text;
  li.append(name);
  if (button) {
    const b = document.createElement("button");
    b.textContent = button;
    b.onclick = onclick;
    li.append(b);
  }
  list.append(li);
  return li;
}

async function drawArcades() {
  const list = $("arcades");
  list.textContent = "";
  for (const origin of await ask({type: "arcades"}))
    item(list, origin, "Unlink", () => ask({type: "unlink", origin}).then(draw).catch((e) => say(e.message)));
  const coins = $("coins");
  coins.textContent = "";
  const b = await ask({type: "balances"});
  $("no-arcade").hidden = !!b.origin;
  $("read-from").hidden = !b.origin;
  $("read-from").textContent = b.origin ? `As ${b.origin} reads the chains:` : "";
  const rows = b.origin ? b.rows : Object.entries(b.mine).map(([network, m]) => ({network, label: network, ...m}));
  for (const row of rows) {
    const li = document.createElement("li");
    const head = document.createElement("div");
    head.className = "coin-head";
    head.textContent = `${row.label || row.network}${row.coins != null ? `  ${row.coins} ${row.coin || ""}` : ""}`;
    const address = document.createElement("code");
    address.textContent = row.ours === false ? `this arcade has another address: ${row.address}` : (row.address || "");
    li.append(head, address);
    const extra = [];
    if (row.tokens && row.tokens.length) extra.push(row.tokens.map((t) => `${t.balance} ${t.name || "#" + t.propertyid}`).join(", "));
    if (row.inscriptions) extra.push(`${row.inscriptions} inscription${row.inscriptions === 1 ? "" : "s"}`);
    if (extra.length) {
      const more = document.createElement("div");
      more.className = "muted";
      more.textContent = extra.join(" · ");
      li.append(more);
    }
    coins.append(li);
  }
  held = Object.fromEntries((b.rows || []).filter((r) => r.ours).map((r) => [r.network, r]));
  const pick = $("send-chain"), was = pick.value;
  pick.textContent = "";
  for (const r of Object.values(held)) pick.append(new Option(`${r.label} (${r.coins} ${r.coin})`, r.network));
  if (was && held[was]) pick.value = was;
  $("send-box").hidden = !pick.options.length;
  sendFields().catch((e) => say(e.message));
}

/* --- send: the arcade builds it, the wallet checks it, this shows it, and only "Sign and send" signs ---------------- */
let held = {}, readied = null;

async function sendFields() {
  const kind = $("send-kind").value, row = held[$("send-chain").value] || {};
  $("send-token").hidden = kind !== "token";
  $("send-piece").hidden = kind !== "nft" && kind !== "inscription";
  $("send-amount").hidden = kind === "nft" || kind === "inscription";
  $("send-amount").placeholder = kind === "token" ? "how many" : `amount, in ${row.coin || "coins"}`;
  if (kind === "token") {
    $("send-token").textContent = "";
    for (const t of row.tokens || []) $("send-token").append(new Option(`${t.balance} ${t.name || ""} (#${t.propertyid})`, t.propertyid));
  }
  if (kind === "nft" || kind === "inscription") {
    $("send-piece").textContent = "";
    $("send-piece").append(new Option("reading what you hold...", ""));
    const got = await ask({type: "toSend", chain: $("send-chain").value});
    $("send-piece").textContent = "";
    const list = kind === "nft" ? got.nfts.map((n) => [`#${n.number} ${n.id.slice(0, 16)}...`, JSON.stringify(n)])
      : got.ords.map((o) => [`${o.id.slice(0, 16)}... on ${o.txid.slice(0, 8)}:${o.vout}`, JSON.stringify(o)]);
    for (const [text, value] of list) $("send-piece").append(new Option(text, value));
    if (!list.length) $("send-piece").append(new Option(kind === "nft" ? "no NFT here" : "no inscription here", ""));
  }
}

function asked() {
  const kind = $("send-kind").value, chain = $("send-chain").value, to = $("send-to").value.trim();
  const out = {kind, chain, to, amount: $("send-amount").value.trim()};
  if (kind === "token") out.propertyid = Number($("send-token").value);
  if (kind === "nft" || kind === "inscription") {
    const p = JSON.parse($("send-piece").value || "null");
    if (!p) throw new Error("Pick what to send.");
    if (kind === "nft") out.piece = p.id; else Object.assign(out, {inscription: p.id, txid: p.txid, vout: p.vout});
  }
  return out;
}

$("send-kind").onchange = $("send-chain").onchange = () => sendFields().catch((e) => say(e.message));
$("send-prepare").onclick = async () => {
  say(""); $("sent").textContent = ""; $("review").hidden = true; readied = null;
  try {
    const got = await ask({type: "prepare", ask: asked()});
    readied = got.id;
    $("review-who").textContent = `To ${got.view.to}. Built by ${got.arcade}; checked here against what you asked.`;
    drawTx($("review-tx"), got.view, got.tokens);
    $("review").hidden = false;
  } catch (e) { say(e.message); }
};
$("send-cancel").onclick = () => { readied = null; $("review").hidden = true; };
$("send-yes").onclick = async () => {
  if (!readied) return;
  const id = readied;
  readied = null;
  $("review").hidden = true;
  try {
    const done = await ask({type: "confirm", id});
    $("sent").textContent = `Sent: ${done.txid}`;
    setTimeout(() => drawArcades().catch(() => {}), 3000);
  } catch (e) { say(e.message); }
};

$("link").onclick = () => { say(""); ask({type: "link", origin: $("link-origin").value}).then(() => { $("link-origin").value = ""; return draw(); })
  .catch((e) => say(e.message)); };

function needPassword(words) {
  chosen = words;
  $("password-box").hidden = false;
  $("new-password").focus();
}

async function keep(words, password) {
  await ask({type: "create", phrase: words, password});
  chosen = "";
  await draw();
}

$("import-words").onclick = async () => {
  say("");
  const words = keys.normalise($("phrase").value);
  const wrong = await keys.complaint(words);
  if (wrong) { say(wrong); return; }
  $("new-words").hidden = true;
  needPassword(words);
};

$("make").onclick = async () => {
  say("");
  const words = await keys.generate();
  $("made-words").textContent = words;
  $("new-words").hidden = false;
  $("wrote").checked = false;
  needPassword(words);
};

$("keep").onclick = async () => {
  say("");
  if (!$("new-words").hidden && !$("wrote").checked) { say("Write the words down first, then tick the box."); return; }
  if ($("new-password").value !== $("new-password2").value) { say("The two passwords are not the same."); return; }
  try { await keep(chosen, $("new-password").value); } catch (e) { say(e.message); }
};

/* @name and password: the sealed wallet the arcade keeps for that name (GET /signin/<name>), opened here. */
$("import-name").onclick = async () => {
  say("");
  let origin;
  try { origin = new URL($("arcade").value.trim()).origin; } catch (e) { say("Type the arcade's address, like https://example.com"); return; }
  const tag = $("tag").value.trim().replace(/^@/, "");
  const password = $("tag-password").value;
  if (!tag || !password) { say("Your @name and your password, please."); return; }
  try {
    const answer = await fetch(`${origin}/signin/${encodeURIComponent(tag)}`, {credentials: "omit"});
    const said = await answer.json();
    if (!answer.ok) throw new Error(said.detail || "no wallet by that name there");
    if (said.elsewhere || !said.blob) throw new Error("that name's wallet lives on another arcade; bring it from there");
    const words = await keys.open(said.blob, password);
    const key = await keys.accountKey(await keys.toSeed(words));
    if (said.pubkey && key.pubkey !== said.pubkey) throw new Error("that wallet does not match its name. Nothing was kept.");
    try { await keep(words, password); } catch (e) { needPassword(words); say(e.message + " (for this browser)"); return; }
    await ask({type: "link", origin}).then(draw).catch((e) => say(`Kept, but not linked to ${origin}: ${e.message}`));
  } catch (e) { say(e.message); }
};

$("unlock").onclick = () => ask({type: "unlock", password: $("password").value}).then(draw).catch((e) => say(e.message));
$("password").addEventListener("keydown", (e) => { if (e.key === "Enter") $("unlock").click(); });
$("lock").onclick = () => ask({type: "lock"}).then(draw).catch((e) => say(e.message));
$("remove").onclick = async () => {
  if (!confirm("Remove this wallet from this browser? Without your words or your arcade password you cannot get it back here."))
    return;
  await ask({type: "remove"}).catch((e) => say(e.message));
  draw();
};

draw().catch((e) => say(e.message));
