import * as keys from "./keys.js";

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
  }
}

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
    try { await keep(words, password); } catch (e) { needPassword(words); say(e.message + " (for this browser)"); }
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
