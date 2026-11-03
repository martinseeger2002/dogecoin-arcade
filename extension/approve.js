import {drawTx} from "./render.js";

const $ = (id) => document.getElementById(id);
const id = new URLSearchParams(location.search).get("id");
const ask = (msg) => chrome.runtime.sendMessage(msg).then((r) => {
  if (!r || !r.ok) throw new Error((r && r.error) || "the wallet did not answer");
  return r.result;
});

const WORDS = {
  connect: ["Connect to this site?",
            "It will see your account's public key and can then ask you to sign. It can never sign on its own."],
  signIn: ["Sign in to this site?",
           "This signs the message below with your account key. It proves to this site, and only this site, that it is you."],
  signMessage: ["Sign this message?",
                "It is signed with your address key below, exactly as written. Anyone can check that you signed it. "
                + "Sign only words you mean: a signature cannot be taken back."],
  signTransaction: ["Sign this transaction?",
                    "The site gets it back signed and may broadcast it. Read every output below: once signed, it can be sent."],
  sendTransaction: ["Send this transaction?",
                    "Signed here, then broadcast through your linked arcade. Read every output below."],
};

ask({type: "pending", id}).then((p) => {
  $("origin").textContent = p.origin;
  const [what, why] = WORDS[p.kind] || ["Approve this request?", ""];
  $("what").textContent = what;
  $("why").textContent = why;
  if (p.detail && p.detail.message) { $("message").textContent = p.detail.message; $("message").hidden = false; }
  if (p.detail && p.detail.view) {
    $("why").textContent += ` Each signature ${p.detail.view.sighash}. Your coins are read from ${p.detail.arcade}.`;
    drawTx($("tx"), p.detail.view, p.detail.tokens || {});
    $("tx").hidden = false;
  }
  if (p.detail && p.detail.chain) {
    $("why").textContent += ` On ${p.detail.chain}${p.detail.address ? `, as ${p.detail.address}` : ""}.`;
  }
  $("pubkey").textContent = p.pubkey || "locked: type your password to approve";
  $("password").hidden = p.open;
  if (!p.open) $("password").focus();
}).catch((e) => { $("err").textContent = e.message; $("yes").disabled = true; });

$("yes").onclick = () => ask({type: "answer", id, yes: true, password: $("password").value || undefined})
  .catch((e) => { $("err").textContent = e.message; });
$("no").onclick = () => ask({type: "answer", id, yes: false}).catch(() => window.close());
$("password").addEventListener("keydown", (e) => { if (e.key === "Enter") $("yes").click(); });
