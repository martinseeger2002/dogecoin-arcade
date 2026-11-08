/* Restoring without an arcade, drawn (2026-11-07, SadFrog's side).
 *
 * One chain's kit (coins.js `restoreKit`: path, address, key, the Core's own commands) as a panel with a Copy button on
 * each command. The backup page draws it after its unlock, from the account's words; the offline /recover page draws
 * it from words typed into a file opened with no network at all (arcade/restore_kit.py inlines this module there). One
 * renderer, so the two can never say different things. The chains' table comes from arcade/restore_kit.py.
 *
 * Every line that holds a key is the key: shown, never sent. Nothing in this file fetches.
 */
import * as coins from "/coins.js";

const enc = new TextEncoder();

/** BIP39 for the offline page, which has no node to fetch signin.js's word list from: the list is handed in. The same
 *  checks and the same seed as signin.js `complaint`/`toSeed` (PBKDF2-HMAC-SHA512, 2048 rounds, salt "mnemonic" and
 *  the passphrase); tests/test_restore_kit.py holds the addresses it makes to arcade/seed.py's. */
export async function seedOf(phrase, passphrase, list) {
  const said = String(phrase || "").normalize("NFKD").toLowerCase().split(/\s+/).filter(Boolean);
  if (![12, 15, 18, 21, 24].includes(said.length))
    throw new Error(`A seed phrase is 12, 15, 18, 21 or 24 words; this is ${said.length}.`);
  const unknown = said.filter((word) => list.indexOf(word) < 0);
  if (unknown.length) throw new Error(`Not a word from the list: ${unknown.slice(0, 3).join(", ")}.`);
  let bits = "";
  for (const word of said) bits += list.indexOf(word).toString(2).padStart(11, "0");
  const sumBits = bits.length / 33, entropy = new Uint8Array((bits.length - sumBits) / 8);
  for (let i = 0; i < entropy.length; i++) entropy[i] = parseInt(bits.slice(i * 8, i * 8 + 8), 2);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", entropy));
  if (bits.slice(bits.length - sumBits) !== digest[0].toString(2).padStart(8, "0").slice(0, sumBits))
    throw new Error("Those are all real words, but not in an order this can be: one is mistyped or swapped.");
  const key = await crypto.subtle.importKey("raw", enc.encode(said.join(" ")), "PBKDF2", false, ["deriveBits"]);
  return new Uint8Array(await crypto.subtle.deriveBits(
    {name: "PBKDF2", salt: enc.encode("mnemonic" + String(passphrase || "").normalize("NFKD")),
     iterations: 2048, hash: "SHA-512"}, key, 512));
}

/** Every chain's kit for these words. `whole`: rescan from block 0 rather than from each chain's `since`. */
export async function kitsFor(seed, chains, whole = false) {
  const out = [];
  for (const chain of chains) out.push({chain, kit: await coins.restoreKit(seed, chain, whole ? 0 : chain.since)});
  return out;
}

const day = (t) => new Date(t * 1000).toISOString().slice(0, 10);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function para(parts, cls = "rk-note") {
  const p = el("p", cls);
  for (const part of parts) p.append(typeof part === "string" ? document.createTextNode(part) : part);
  return p;
}

const code = (text) => el("code", "", text);

async function copy(text, button) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {                                           // a file:// page or an old browser: copy the selection
    const area = el("textarea");
    area.value = text;
    area.setAttribute("autocomplete", "off");
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  button.textContent = "Copied";
  setTimeout(() => { button.textContent = "Copy"; }, 1500);
}

function command(text) {
  const box = el("div", "rk-cmd");
  const pre = el("pre", "rk-pre", text);
  const button = el("button", "btn ghost small rk-copy", "Copy");
  button.type = "button";
  button.onclick = () => copy(text, button);
  box.append(pre, button);
  return box;
}

const KIND_SAID = {"legacy": "Legacy", "p2sh-segwit": "SegWit (P2SH)", "bech32": "Native SegWit"};

/** One chain's panel. */
export function drawKit({chain, kit}) {
  const box = el("details", "rk-chain");
  const head = el("summary", "rk-summary");
  head.append(el("b", "", chain.label), document.createTextNode(" "), el("span", "rk-addr", kit.address));
  box.append(head);
  box.append(para(["Address ", code(kit.address), " on the path ", code(kit.path), ". Any BIP39 wallet that lets you set "
    + "the derivation path finds it from your twelve words with the path ", code(kit.account),
    chain.real ? " (coin type 3, the same on every mainnet here. A wallet left on the chain's own registered number"
      + " shows an empty wallet: your coins are not lost, the wallet is looking somewhere else)."
      : " (coin type 1, as on every test chain)."]));
  for (const one of kit.segwit)
    box.append(para(["SegWit, if you used it here: ", code(one.address), " on ", code(one.path), "."]));
  if (kit.keys.length)
    for (const one of kit.keys) box.append(para([`Private key (WIF), ${KIND_SAID[one.kind]}: `, code(one.wif)]));
  else box.append(para(["Private key (WIF): ", code(kit.wif)]));
  const cli = chain.cli + (chain.flags ? " " + chain.flags : "");
  if (kit.recipe === "descriptors") {
    box.append(para([`In ${chain.core} or newer: make an empty descriptor wallet named "arcade", then import the `
      + `descriptors (the legacy P2PKH address the arcade uses${kit.segwit.length ? ", both SegWit kinds" : ""}`
      + `${kit.taproot ? ", and Taproot" : ""}; the first 21 receiving and change addresses of each). The rescan starts `
      + (kit.since ? `on ${day(kit.since)} (timestamp ${kit.since}); coins sent here before that are not looked for `
          + "unless you pick the whole chain, or put 0 for the timestamp."
        : "at the chain's first block (timestamp 0), which finds everything and takes a while.")]));
  } else if (kit.recipe === "legacy-keys") {
    box.append(para([`In ${chain.core}: its releases have no descriptor wallets, so make a wallet named "arcade" (a `
      + "legacy one), import each address type's key with no rescan, then rescan once "
      + (chain.height ? `from block ${chain.height} (about ${day(kit.since)}); coins sent here before that are not `
          + "looked for unless you rescan from 0." : "from the chain's first block, which finds everything and takes a while.")]));
  } else if (kit.recipe === "privkey-wallet") {
    box.append(para([`In ${chain.core}: make a wallet named "arcade" (skip it if you already have one you want to `
      + "use), then import the key. The last word, true, rescans the whole chain for this address; it takes a while."]));
  } else {
    box.append(para([`In ${chain.core}: import the key into its wallet. The last word, true, rescans the whole chain `
      + "for this address (the one the arcade uses); it takes a while. If the wallet is encrypted, unlock it first: ",
      code(`${cli} walletpassphrase "your wallet password" 600`), "."]));
  }
  for (const line of kit.commands) box.append(command(line));
  if (kit.taproot) {
    box.append(para(["DigiDollars: your DigiDollar address ", code(kit.taproot.digidollar || "(none on this chain)"),
      " is the Taproot output ", code(kit.taproot.address), " of this same key (key path, no script), which the tr() "
      + "lines import. NOT CHECKED YET: whether DigiByte Core's DigiDollar balance lists the DigiDollars held there "
      + "after this import. The key that holds them is this one either way, so if it shows none, they are not lost: "
      + "keep your words."], "rk-note rk-warn"));
  }
  if (kit.recipe === "descriptors")
    box.append(para(["To only watch, with no key in the wallet: the account's public key ", code(kit.xpub),
      ` (${kit.account}), as `, code(coins.withChecksum(`pkh(${kit.xpub}/0/*)`)),
      " in a wallet made with private keys disabled."]));
  return box;
}

/** Every chain's panel into `into`, with the safety lines first. */
export function drawKits(into, kits) {
  into.replaceChildren();
  into.append(para(["Each line below holds a private key. Type it only into a Core you run yourself, on your own "
    + "computer. The safest place is the Core's own window (Window → Console; pick the arcade wallet at the top "
    + "and leave out the part before the command, such as ", code("digibyte-cli -rpcwallet=arcade"), "). In a "
    + "terminal the line stays in your shell history: clear it afterwards (", code("history -c"), ")."],
    "rk-note rk-warn"));
  for (const one of kits) into.append(drawKit(one));
}
