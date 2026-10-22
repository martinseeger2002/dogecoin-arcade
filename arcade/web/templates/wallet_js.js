/* Signing up and signing in with a @tag and a password.
 *
 * The whole of what a person does: type a name, type a password, and have
 * a wallet. Everything that makes that non-custodial happens here, in
 * their browser, and none of it is their problem:
 *
 *   signing up   twelve words from `crypto.getRandomValues`, the keys
 *                derived from them, the seed encrypted under the password,
 *                and the node handed a name, a public key, an address and
 *                a blob it cannot read.
 *   signing in   fetch the blob for that name, decrypt it here, derive the
 *                same keys, sign the node's challenge.
 *
 * **The password never leaves this file.** Not to the node, not into a
 * cookie, not into storage. Which is also why nobody can reset it: the
 * twelve words are the only way back, and they are shown once and asked
 * for once so that they are actually written down.
 */

import * as signer from "/signin.js";
import * as coins from "/coins.js";

/* No minimum (D-157). A floor stops the person who would have chosen
 * something short and stops nobody else -- an attacker does not type
 * passwords into a form, they take the blob and try it offline. What the
 * page owes somebody is the truth about what they have chosen, not a
 * refusal; `strength` below is what it says. */
export const MIN_PASSWORD = 1;

/* A password that has already leaked is refused (2026-10-20, answering SadFrog's review: "check a leaked
 * password api and make sure whatever password they use is not on the leaked password list"). Not a floor: a
 * leaked password is on every list an attacker tries first, whatever its length. Have I Been Pwned's range API,
 * k-anonymity: only the first 5 hex characters of the password's SHA-1 leave this browser, with padding, and the
 * hundreds of hash endings that come back are compared HERE. Neither the password nor its whole hash is ever sent,
 * and nothing goes to this node. */
export const LEAKED_URL = "https://api.pwnedpasswords.com/range/";

/** How many times this password appears in the leaked-password lists: 0 for none, null when the list could not be
 *  asked (the page then lets the password through rather than blocking a sign-up on somebody else's server). */
export async function leaked(password) {
  if (!password) return 0;
  // The arcade's own word on where the list is (base.html, ARCADE_LEAKED_URL); "" turns the check off.
  const url = typeof globalThis.ARCADE_LEAKED_URL === "string" ? globalThis.ARCADE_LEAKED_URL : LEAKED_URL;
  if (!url) return null;
  try {
    const digest = hex(new Uint8Array(await crypto.subtle.digest("SHA-1", enc.encode(password)))).toUpperCase();
    const head = digest.slice(0, 5), tail = digest.slice(5);
    const answer = await fetch(url + head, {headers: {"Add-Padding": "true"}, cache: "no-store",
                                                   credentials: "omit", referrerPolicy: "no-referrer"});
    if (!answer.ok) return null;
    for (const line of (await answer.text()).split("\n")) {
      const [ending, count] = line.trim().split(":");
      if (ending === tail) return parseInt(count, 10) || 0;      // padding lines carry a count of 0
    }
    return 0;
  } catch (e) {
    return null;
  }
}

async function refuseLeaked(password) {
  const seen = await leaked(password);
  if (seen) {
    throw new Error(`That password has turned up ${seen.toLocaleString()} time${seen === 1 ? "" : "s"} in leaked-`
      + "password lists (haveibeenpwned.com), so it is one of the first an attacker tries. Choose another.");
  }
}

//: Roughly how long the blob would stand up to somebody who has it and a
//: graphics card. Deliberately pessimistic: 600,000 PBKDF2 iterations is
//: about 10^5 guesses a second on hardware somebody can rent, and the
//: number a person needs is the one that is true on the attacker's
//: machine rather than on theirs.
const GUESSES_A_SECOND = 100000;

export function strength(password) {
  const text = password || "";
  if (!text) return {ok: false, says: ""};
  let alphabet = 0;
  if (/[a-z]/.test(text)) alphabet += 26;
  if (/[A-Z]/.test(text)) alphabet += 26;
  if (/[0-9]/.test(text)) alphabet += 10;
  if (/[^a-zA-Z0-9]/.test(text)) alphabet += 33;
  const seconds = Math.pow(alphabet, text.length) / 2 / GUESSES_A_SECOND;
  if (seconds < 60) {
    return {ok: false, level: "instantly", says:
      "Somebody who knows your name can take your encrypted wallet from "
      + "this node and open it in seconds. Your coins and your messages "
      + "with it. Use it if you mean to \u2014 nothing here will stop you."};
  }
  if (seconds < 86400 * 365) {
    return {ok: false, level: "in a while", says:
      "This would take somebody with your encrypted wallet and a graphics "
      + "card somewhere between minutes and months. That is shorter than "
      + "you will want to keep this account."};
  }
  return {ok: true, level: "a long time", says:
    "Long enough that taking your encrypted wallet and breaking it is not "
    + "worth anybody's time."};
}

const enc = new TextEncoder();
const hex = (bytes) => [...new Uint8Array(bytes)]
  .map((b) => b.toString(16).padStart(2, "0")).join("");
const unhex = (text) => new Uint8Array(
  (text.match(/../g) || []).map((pair) => parseInt(pair, 16)));

/* --- the blob ------------------------------------------------------------
 * AES-256-GCM under PBKDF2-HMAC-SHA512. The parameters travel with it, so
 * raising them later does not lock anybody out of a wallet they still know
 * the password to -- and so a reader can see what it was made with rather
 * than having to trust that it was made well.
 */
export const KDF_ITERATIONS = 600000;

async function keyFrom(password, salt, iterations) {
  const base = await crypto.subtle.importKey(
    "raw", enc.encode(password), "PBKDF2", false, ["deriveKey"]);
  return crypto.subtle.deriveKey(
    {name: "PBKDF2", salt, iterations, hash: "SHA-512"}, base,
    {name: "AES-GCM", length: 256}, false, ["encrypt", "decrypt"]);
}

export async function seal(phrase, password) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const key = await keyFrom(password, salt, KDF_ITERATIONS);
  const sealed = await crypto.subtle.encrypt(
    {name: "AES-GCM", iv: nonce}, key, enc.encode(signer.normalise(phrase)));
  return {kdf: "PBKDF2-SHA512", iterations: KDF_ITERATIONS,
          salt: hex(salt), nonce: hex(nonce), sealed: hex(sealed)};
}

export async function open(blob, password) {
  const key = await keyFrom(password, unhex(blob.salt), blob.iterations);
  let plain;
  try {
    plain = await crypto.subtle.decrypt(
      {name: "AES-GCM", iv: unhex(blob.nonce)}, key, unhex(blob.sealed));
  } catch (e) {
    // The only thing that can tell a wrong password from a right one is
    // whether what comes out is a seed phrase. The node cannot, and does
    // not have to.
    throw new Error("that password does not open this wallet.");
  }
  return new TextDecoder().decode(plain);
}

/* --- what a wallet is, once there is one --------------------------------- */

export async function walletFrom(phrase, network, version) {
  const seed = await signer.toSeed(phrase);
  const account = await signer.accountKey(seed);      // signs into the node
  const coin = await coins.coinKey(seed, network, 0); // holds the money
  const wallet = {
    phrase,
    seed,
    pubkey: account.pubkey,
    sign: account.sign,
    coinKey: coin.key,
    coinPubkey: coin.pubkey,
    address: await coins.address(coin.pubkey, version),
    // Every chain, by network name. The tag chain is in here as well as
    // in the fields above, so a caller that knows which chain it means
    // never has to work out whether it is the special one.
    on: {},
  };
  wallet.on[network] = {network, version, key: coin.key,
                        pubkey: coin.pubkey, address: wallet.address};
  return wallet;
}

/* --- more than one chain -------------------------------------------------
 *
 * The same twelve words hold coins on every chain this node runs, at that
 * chain's own coin type: m/44'/1'/0'/0/0 on testnet, m/44'/3'/0'/0/0 on
 * mainnet. Two keys, one secret, and nothing extra to write down -- which
 * is the whole reason an account can be given a mainnet wallet at all.
 *
 * The version byte is the other half. An address is a hash with a chain
 * stamped on the front, and the stamp is what stops mainnet coins being
 * sent to a testnet address that happens to hash the same way.
 */

export async function everyChain(wallet, chains) {
  for (const chain of chains || []) {
    if (wallet.on[chain.network]) continue;
    const coin = await coins.coinKey(wallet.seed, chain.network, 0);
    wallet.on[chain.network] = {
      network: chain.network, version: chain.version,
      key: coin.key, pubkey: coin.pubkey,
      address: await coins.address(coin.pubkey, chain.version),
    };
  }
  return wallet;
}

/* --- address types --------------------------------------------------------
 *
 * One set of twelve words gives a chain more than one kind of address
 * (2026-10-20: a drop-down on the Wallet card to choose the type; more
 * types are coming from tbuuol). Each kind is a row here and nothing else needs
 * to change: the card lists them, Receive shows that kind's address, and a coin
 * send is built from it and signed with its key. The first row is the chain's
 * ordinary address. A chain with one kind shows no drop-down.
 * Segwit is test chains only until it has had a real-money review.
 * The order is the menu's (2026-10-20, tbuuol): Legacy, P2SH, Native SegWit.
 */
export const ADDRESS_KINDS = {
  ltctest: [
    {id: "legacy", label: "Legacy", path: "m/44'/1'/0'/0/0"},
    {id: "p2sh-p2wpkh", label: "P2SH (Nested SegWit)", scripthash: 58, path: "m/49'/1'/0'/0/0"},
    {id: "p2wpkh", label: "Native SegWit", hrp: "tltc", path: "m/84'/1'/0'/0/0"},
  ],
};
export const addressKinds = (network) => ADDRESS_KINDS[network] || [];

/** That kind's key and address on `network`, from the wallet's words. */
async function kindKey(wallet, network, kind) {
  const base = keysOn(wallet, network);
  if (kind.id === "p2wpkh") {
    const seg = await coins.nativeSegwitKey(wallet.seed, network, 0);
    return {...base, key: seg.key, pubkey: seg.pubkey,
            address: await coins.p2wpkhAddress(seg.pubkey, kind.hrp)};
  }
  if (kind.id === "p2sh-p2wpkh") {
    // BIP49: the key at m/49', its P2WPKH program wrapped in P2SH. `version`
    // stays the chain's pubkeyhash, for reading who a send pays (verifyOffer).
    const seg = await coins.p2shSegwitKey(wallet.seed, network, 0);
    return {...base, key: seg.key, pubkey: seg.pubkey, nested: true,
            address: await coins.p2shSegwitAddress(seg.pubkey, kind.scripthash)};
  }
  throw new Error(`this wallet cannot make a ${kind.label} address yet`);
}

/** The wallet as one address kind sees it: its key on `network` is that kind's,
 *  so checked() and confirm() sign with it. The first kind is the wallet itself. */
export async function asKind(wallet, network, kindId) {
  const kinds = addressKinds(network);
  const kind = kinds.find((k) => k.id === kindId);
  if (!kind || kind === kinds[0]) return wallet;
  if (!wallet || !wallet.seed) throw new Error(`Unlock your wallet to use its ${kind.label} address.`);
  return {...wallet, on: {...wallet.on, [network]: await kindKey(wallet, network, kind)}};
}

/** Register any address this node does not know yet. Signed-in only. */
export async function tellTheNode(wallet) {
  const said = await state();
  for (const chain of said.chains || []) {
    const keys = wallet.on && wallet.on[chain.network];
    if (!keys || chain.address === keys.address) continue;
    await fetch("/account/address", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({address: keys.address, chain: chain.network,
                            coin_pubkey: coinsHex(keys.pubkey)}),
    });
  }
}

/** What chains this node runs for accounts, whether or not anybody is in. */
//: The chain list, kept a minute (2026-10-17: every `opened()` asked /account for it, and the
//: swap runner opens the wallet every 1.5 s -- 160 /account a minute, a quarter of the node's time).
let chainsKept = null, chainsAt = 0, chainsAsking = null;
export async function chains() {
  if (chainsKept && Date.now() - chainsAt < 60000) return chainsKept;
  if (!chainsAsking) {
    chainsAsking = (async () => {
      try {
        const got = (await (await fetch("/account")).json()).chains || [];
        if (got.length) { chainsKept = got; chainsAt = Date.now(); }
        return got;
      } catch (e) { return chainsKept || []; }
      finally { chainsAsking = null; }
    })();
  }
  return chainsAsking;
}

/** The keys for one chain, by network name. Throws rather than guessing:
 *  signing a mainnet transaction with a testnet key produces a signature
 *  that verifies against nothing, which is a silent failure. */
export function keysOn(wallet, network) {
  const found = wallet.on && wallet.on[network];
  if (!found) {
    throw new Error(`this wallet has no key for ${network} in this tab`);
  }
  return found;
}

/* --- the two things a person does ---------------------------------------- */

export async function available(tag) {
  return (await fetch(`/signup/${encodeURIComponent(tag)}`)).json();
}

export async function signUp(tag, password, options) {
  return working(() => _signUp(tag, password, options));
}

async function _signUp(tag, password, {network, version}) {
  if (!(password || "")) {
    throw new Error("a password, please -- even a short one. It is what "
      + "opens your wallet from anywhere, and nobody can reset it.");
  }
  const phrase = await signer.generate();
  const wallet = await walletFrom(phrase, network, version);
  // The same words on every chain this node runs, derived before signing
  // up rather than on some later visit: an account should be payable on
  // both from the minute it exists, and the node cannot derive either of
  // these addresses for itself.
  const running = await chains();
  await everyChain(wallet, running);
  await refuseLeaked(password);
  const blob = await seal(phrase, password);
  const alsoOn = {};
  for (const chain of running) {
    if (chain.network === network) continue;
    const keys = wallet.on[chain.network];
    if (!keys) continue;
    alsoOn[`address_${chain.network}`] = keys.address;
    alsoOn[`coin_pubkey_${chain.network}`] = coinsHex(keys.pubkey);
  }
  const answer = await fetch("/signup", {
    method: "POST", headers: {"Content-Type": "application/json"},
    // The coin public key travels too: a Class B payload puts it in every
    // output so the dust stays spendable by whoever sent it, and the node
    // cannot derive it from anything it holds.
    body: JSON.stringify({tag, pubkey: wallet.pubkey,
                          address: wallet.address,
                          coin_pubkey: coinsHex(wallet.coinPubkey),
                          ...alsoOn, blob}),
  });
  const said = await answer.json();
  if (!answer.ok) throw new Error(said.detail || "that did not work");
  // Open from here on. Somebody who has just chosen a password should not
  // be asked for it again to use the thing they made.
  remember(wallet.phrase);
  return {...said, wallet};
}

export async function signIn(tag, password, options) {
  return working(() => _signIn(tag, password, options));
}

async function _signIn(tag, password, {network, version}) {
  const found = await fetch(`/signin/${encodeURIComponent(tag)}`);
  const said = await found.json();
  if (!found.ok) throw new Error(said.detail || "no wallet by that name");
  // Made on another arcade: this one found it there by itself (2026-10-19: "It should just know
  // automatically"), and it is brought here the way an import is, so the same name and password open it here next.
  if (said.elsewhere) return bringHere(said, password, {network, version});
  const phrase = await open(said.blob, password);
  const wallet = await walletFrom(phrase, network, version);
  await everyChain(wallet, await chains());
  if (wallet.pubkey !== said.pubkey) {
    // The blob opened, and produced a different key than the node has on
    // file. That is not a wrong password -- it is a wallet that does not
    // match its own name, and going on would sign in as somebody else.
    throw new Error("that wallet does not match this name. Nothing was opened.");
  }
  const result = await seatWith(wallet);
  // A newer record of its imported wallets from the arcade it came from, when this one has not heard (the mesh
  // carries them too); then they open with the same password (plan item 18), in the second sealed record.
  await bringImports("");
  await openImportsWith(wallet, password);
  // Any chain whose coins were never read back here (a read-back cut short by a restart): finished now. A chain
  // already done costs the node nothing (2026-10-19).
  fetch("/account/coins/backfill", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"})
    .catch(() => {});
  return {...result, tag: said.tag, address: said.address, wallet};
}

/** Bring an account from another arcade (2026-09-30): its encrypted wallet,
 *  fetched there by name, opened here with its password, and filed with this
 *  node the way a sign-up is -- so the same name and password open it here
 *  from now on. The words and the password stay in this tab. */
export async function importFrom(node, tag, password, options) {
  return working(() => _importFrom(node, tag, password, options));
}

async function _importFrom(node, tag, password, {network, version}) {
  const got = await fetch(`/signin-import?node=${encodeURIComponent(node)}`
                          + `&tag=${encodeURIComponent(String(tag).replace(/^@/, ""))}`);
  const said = await got.json();
  if (!got.ok) throw new Error(said.detail || "that arcade did not send the wallet");
  return bringHere(said, password, {network, version});
}

/** A sealed wallet from another arcade, opened in this tab and filed with this node like a sign-up. */
async function bringHere(said, password, {network, version}) {
  const phrase = await open(said.blob, password);
  const wallet = await walletFrom(phrase, network, version);
  const running = await chains();
  await everyChain(wallet, running);
  if (said.pubkey && wallet.pubkey !== String(said.pubkey).toLowerCase()) {
    throw new Error("that wallet does not match its name. Nothing was opened.");
  }
  const alsoOn = {};
  for (const chain of running) {
    if (chain.network === network) continue;
    const keys = wallet.on[chain.network];
    if (!keys) continue;
    alsoOn[`address_${chain.network}`] = keys.address;
    alsoOn[`coin_pubkey_${chain.network}`] = coinsHex(keys.pubkey);
  }
  const filed = await fetch("/signup", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({tag: said.tag, pubkey: wallet.pubkey, address: wallet.address,
                          coin_pubkey: coinsHex(wallet.coinPubkey), ...alsoOn,
                          blob: said.blob}),
  });
  let result;
  if (filed.ok) {
    result = await filed.json();
  } else {
    // Already here (brought over before): sign in the ordinary way.
    result = await seatWith(wallet);
  }
  remember(wallet.phrase);
  // Its coins arrived while this node was not watching it: as the arcade it came from knows them, each checked here
  // (2026-10-19), or read back from the blocks when that arcade cannot say.
  fetch("/account/coins/backfill", {method: "POST", headers: {"Content-Type": "application/json"},
                                    body: JSON.stringify({from: said.from || ""})}).catch(() => {});
  // Its imported wallets too (2026-10-21): the signed record from the arcade it came from, checked here
  // against this account's key, then opened with the same password.
  await bringImports(said.from || "");
  await openImportsWith(wallet, password);
  return {...result, tag: said.tag, wallet};
}

/** Open a wallet this node has never held, from the file the Backup page
 *  saves, and take the seat the words entitle.
 *
 *  The file is read in this tab and thrown away in this tab. Nothing is
 *  asked of the node until a key has to prove itself with a signature, so
 *  an account can come to a node that does not know it. The cost is stated
 *  on the page rather than here: a node that was shown a wallet rather than
 *  given one keeps no copy, so it cannot send it back afterwards.
 */
export async function openFile(said, password, options) {
  return working(() => _openFile(said, password, options));
}

async function _openFile(said, password, {network, version}) {
  // The Backup page saves this node's whole `/signin/{tag}` answer, so the
  // file may be an envelope or a bare blob. Both are the same wallet.
  const blob = (said && said.blob) ? said.blob : said;
  if (!blob || !blob.sealed || !blob.salt || !blob.iterations) {
    throw new Error("that file is not a wallet backup. Nothing was opened.");
  }
  const phrase = await open(blob, password);
  const wallet = await walletFrom(phrase, network, version);
  await everyChain(wallet, await chains());
  if (said && said.pubkey && wallet.pubkey !== String(said.pubkey).toLowerCase()) {
    // The file's two halves disagree -- an envelope that opens to a key
    // other than the name it carries. Going on would sign in as somebody
    // else, and it is the file that is at fault, not this node.
    throw new Error("that file's wallet does not match the name it carries. "
      + "Nothing was opened.");
  }
  const result = await seatWith(wallet);
  await openImportsWith(wallet, password);
  fetch("/account/coins/backfill", {method: "POST"}).catch(() => {});
  return {...result, tag: (said && said.tag) || "", wallet};
}

/** Forgot the password: the twelve words open the account, and the wallet is
 *  sealed again under a new password -- all in this tab.
 *
 *  The words never leave it. They make the login key, which signs in (without
 *  taking a seat: words that are not an account here must not become a new,
 *  empty one); the wallet is sealed with the new password here; the node is
 *  handed only the new encrypted blob, in place of the old one, and the
 *  account's @tag opens with the new password from then on, anywhere.
 */
export async function restore(phrase, password, options) {
  return working(() => _restore(phrase, password, options));
}

async function _restore(phrase, password, {network, version}) {
  const wrong = await signer.complaint(phrase);
  if (wrong) throw new Error(wrong);
  if (!(password || "")) throw new Error("a new password, please.");
  const wallet = await walletFrom(signer.normalise(phrase), network, version);
  await everyChain(wallet, await chains());
  let result;
  try {
    result = await seatWith(wallet, {join: false});
  } catch (e) {
    throw new Error("those words are not an account on this node. Check each word; "
      + "if you signed up on another node, restore there. Nothing was changed.");
  }
  await refuseLeaked(password);
  const blob = await seal(wallet.phrase, password);
  const stored = await fetch("/account/vault", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({blob}),
  });
  const said = await stored.json();
  if (!stored.ok) throw new Error(said.detail || "the new password was not saved");
  return {...result, wallet};
}

/** Prove a wallet to the node the only way a node believes, and open it
 *  from here -- the same two requests whichever way the words arrived.
 */
async function seatWith(wallet, {join = true} = {}) {
  const challenge = await (await fetch("/auth/challenge")).json();
  const signature = await wallet.sign(
    signer.loginMessage(challenge.origin, challenge.nonce));
  const opened = await fetch("/auth/login", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({pubkey: wallet.pubkey, nonce: challenge.nonce,
                          signature: hex(signature), join}),
  });
  const result = await opened.json();
  if (!opened.ok) throw new Error(result.detail || "that did not open anything");
  remember(wallet.phrase);
  // An account made before this node ran a second chain has no address
  // registered there, and nothing else will ever notice: told here, once,
  // on the first sign-in that can derive it.
  try { await tellTheNode(wallet); } catch (e) { /* not worth refusing a login */ }
  return result;
}

/* --- claiming the name on the chain --------------------------------------
 *
 * Signing up puts a name in this node's table. That is enough to sign in
 * and not enough to be anybody: a @tag belongs to whoever claimed it on
 * the chain, first claim wins, and until that transaction is in a block
 * the name is only a local reservation.
 *
 * So this is the second half of signing up, and it is the first thing the
 * wallet does with its own coins.
 */

/* While any of this is happening the page must not be reloaded under it.
 *
 * base.html polls `/events` and reloads when something new appears, and a
 * claim bumps that generation itself -- so the page reloaded in the middle
 * of signing, and the keys went with the document. It was found by a test
 * doing two claims in a row; the first was already broadcast, so nothing
 * was lost, and the second would have been broadcast and unfinished.
 */
async function working(what) {
  document.body.dataset.working = "yes";
  try {
    return await what();
  } finally {
    delete document.body.dataset.working;
  }
}

export async function claim(wallet, tag) {
  return working(() => _claim(wallet, tag));
}

async function _claim(wallet, tag) {
  const offered = await fetch("/account/claim", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({tag: tag || ""}),
  });
  const offer = await offered.json();
  if (!offered.ok) throw new Error(offer.detail || "that cannot be claimed");

  const said = await signOffer(wallet, offer);
  return {...said, what: offer.what};
}

const coinsHex = (bytes) => [...new Uint8Array(bytes)]
  .map((b) => b.toString(16).padStart(2, "0")).join("");

/** What the node knows about this account: address, coins, name. */
export async function state() {
  return (await fetch("/account")).json();
}

/** Wait for coins, counting what this node has already sent.
 *
 * `incoming` is what the node broadcast and has not yet read back out of a
 * block -- the faucet's payment, usually. It is spendable: the node made
 * it and is holding it in its own pool. Waiting for the block instead
 * would be waiting on this machine for this machine.
 */
export async function waitForCoins(seconds = 120) {
  const until = Date.now() + seconds * 1000;
  for (;;) {
    const said = await state();
    if ((said.balance || 0) + (said.incoming || 0) > 0) return said;
    if (Date.now() >= until) return said;
    await new Promise((r) => setTimeout(r, 2000));
  }
}

/* --- sending coins -------------------------------------------------------
 *
 * The same handshake as a claim, carrying money. Two calls rather than
 * one, and deliberately: `offer` comes back with who is being paid, how
 * much and what it costs, and nothing is signed until somebody has been
 * shown that and said yes. A one-call send would be a page that spends
 * without asking.
 */

export async function offerSend(to, amount, chain, from = "") {
  return working(async () => {
    const asked = await fetch("/account/send", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(from ? {to, amount, chain: chain || "", from} : {to, amount, chain: chain || ""}),
    });
    const offer = await asked.json();
    if (!asked.ok) throw new Error(offer.detail || "that cannot be sent");
    return offer;
  });
}

/* Signing an offer, on whichever chain it was built for.
 *
 * The key is chosen by the offer's own `chain`, not by whatever the page
 * happened to have to hand. Signing a mainnet transaction with a testnet
 * key does not fail loudly: it produces a signature that verifies against
 * nothing, and the node's refusal arrives with no clue why.
 *
 * Nothing is signed until `coins.verifyOffer` has read the transaction the
 * offer carries and worked out its own hashes from it. The offer's
 * `sighashes` are the node's claim about that transaction, and a claim
 * that does not match the bytes is refused here, in the tab, before the
 * key is used -- which is the only place the refusal can be effective. A
 * node that can choose what you sign does not need your key.
 */
/* --- real coins: every mainnet transaction is said before it is signed -------
 *
 * 2026-10-09: "make sure any main net transactions are verified by the
 * user ... Just a prompt with what their fees and costs are going to be being
 * very honest". Every path that broadcasts goes through `signOffer`, so it is
 * here: on mainnet, the costs are read off the transaction's own bytes (the
 * same `verifyOffer` the signature covers) -- the network fee, the coins locked
 * into data outputs, anything paid to somebody else, the operator's fee -- and
 * `arcadeMainnetReview` (base.html) shows them with one button. Nothing is
 * signed without a yes.
 *
 * A collection is asked once for the whole run ("Show total confirm once"):
 * `approveMainnet` leaves an allowance in this tab that each of its pieces
 * draws down without asking; a piece that would go past it asks again.
 */
let mainnetNets = null;
async function isMainnet(network) {
  if (!network) return false;
  if (!mainnetNets) {
    mainnetNets = new Set((await chains()).filter((c) => c.mainnet).map((c) => c.network));
  }
  return mainnetNets.has(network);
}
const ALLOWANCE = "arcade.mainnet.allowance";
/** Let up to `sats` of mainnet spending through without asking, for `minutes`. */
export function approveMainnet(sats, label = "", minutes = 180) {
  try {
    sessionStorage.setItem(ALLOWANCE, JSON.stringify(
      {left: Math.max(0, Math.round(Number(sats) || 0)), label, until: Date.now() + minutes * 60e3}));
  } catch (e) { /* without storage every transaction asks, which is the safe way */ }
}
export function endMainnetApproval() {
  try { sessionStorage.removeItem(ALLOWANCE); } catch (e) {}
}
/** What a transaction costs, by kind, from its own bytes. */
export function mainnetCosts(offer, shown) {
  const opAddr = offer.operator_fee && offer.operator_fee.address;
  // On a borrowed chain a tenth of the fee goes to the arcade whose Core it is (2026-10-18): the same fee line.
  const shareAddr = offer.operator_fee && offer.operator_fee.sharer && offer.operator_fee.sharer.address;
  let operator = 0, data = 0, others = 0;
  for (const p of shown.pays || []) {
    if (p.mine) continue;
    if (p.bytes !== undefined) data += Number(p.value);
    else if ((opAddr && p.to === opAddr) || (shareAddr && p.to === shareAddr)) operator += Number(p.value);
    else others += Number(p.value);
  }
  const network = Number(shown.fee || 0);
  return {network, data, others, operator, total: network + data + others + operator,
          fee: offer.operator_fee || null, what: shown.what || offer.what || "",
          chain: offer.chain || ""};
}
async function mainnetOk(offer, shown, network, options) {
  if (!(await isMainnet(network))) return;
  const costs = mainnetCosts(offer, shown);
  if (options && options.reviewed) return;     // the page's own review listed these costs
  let allowed = null;
  try { allowed = JSON.parse(sessionStorage.getItem(ALLOWANCE) || "null"); } catch (e) {}
  if (allowed && allowed.until > Date.now() && costs.total <= allowed.left) {
    allowed.left -= costs.total;
    try { sessionStorage.setItem(ALLOWANCE, JSON.stringify(allowed)); } catch (e) {}
    return;
  }
  const ask = globalThis.arcadeMainnetReview;
  if (typeof ask !== "function" || !(await ask(costs))) {
    throw new Error("Not sent: this mainnet transaction was not confirmed. Nothing was signed.");
  }
}

async function signOffer(wallet, offer, given = null, options = null) {
  const network = offer.chain || (wallet.on && Object.keys(wallet.on)[0]);
  const keys = given || keysOn(wallet, network);
  const shown = await coins.verifyOffer(offer, keys);
  await mainnetOk(offer, shown, network, options);
  const signatures = [];
  for (const sighash of shown.hashes) {
    signatures.push(coinsHex(await coins.signInput(keys.key, unhex(sighash))));
  }
  const done = await fetch("/account/sign", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({offer: offer.offer, signatures,
                          pubkey: coinsHex(keys.pubkey)}),
  });
  // A node restarting answers through its proxy with an HTML page, not JSON:
  // say that, rather than a JSON parse error nobody can act on.
  const said = await done.json().catch(() => {
    throw new Error(`the node did not answer (HTTP ${done.status}); it may be restarting`);
  });
  if (!done.ok) throw new Error(said.detail || "the node would not take it");
  return {...said, fee: shown.fee, change: shown.change, says: shown.says};
}

/** What this browser makes of an offer, before anything is signed.
 *
 * Every page that asks somebody to confirm a transaction calls this and
 * prints what it returns, rather than printing `offer.fee` and
 * `offer.what` -- the node's own summary of what it built. The check is
 * the same one `confirm` runs a moment later, so what a person reads is
 * what the signature covers, and a transaction that does not match its
 * own hashes is refused before a confirmation is even offered.
 */
export async function checked(offer, wallet) {
  const keys = keysOn(wallet, offer.chain
                      || (wallet.on && Object.keys(wallet.on)[0]));
  return coins.verifyOffer(offer, keys);
}

/** A DRC-20 / PRC-20 send in one press (2026-10-22; arcade/token20_send.py), in two calls so the page can
 *  review it first. `startToken20(keys, ask)` -- {chain, tick, amount, to} -- has the node check the balance and offer the
 *  commit: what comes back carries the exact `note`, the recipient and `fees` (commit, reveal, send). */
async function token20Post(body, route = "/account/token20/send") {
  const r = await fetch(route, {method: "POST", headers: {"Content-Type": "application/json"},
                                                  body: JSON.stringify(body)});
  const said = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(said.detail || `the node would not take it (HTTP ${r.status})`);
  return said;
}
export async function startToken20(wallet, ask) {
  // The address's own key (not the login key): the note's script is its, so only it can reveal the note.
  const k = keysOn(wallet, ask.chain);
  return token20Post({...ask, pubkey: coinsHex(k.pubkey)});
}
/** Then `sendToken20(keys, first, onStep)`: the commit, the note's reveal and the send, each checked here
 *  (coins.verifyOffer: the reveal only for this key's own note script) and broadcast before the next is asked for.
 *  The page's review listed the note and the fees, so each signature is `reviewed`. The three txids. */
export async function sendToken20(keys, first, onStep = null) {
  let offer = first;
  const sid = first.send, txids = {};
  for (let n = 0; n < 3 && offer.step !== "done"; n++) {
    if (onStep) onStep(offer.step, offer);
    const sent = await working(() => signOffer(keys, offer, null, {reviewed: true}));
    txids[offer.step] = sent.txid;
    offer = await token20Post({send: sid});
  }
  return txids;
}

/** Sign and send. `{reviewed: true}` only from a page whose own review already
 *  listed every cost of this mainnet transaction from `checked` (the Wallet's). */
export async function confirm(wallet, offer, options = null) {
  return working(() => signOffer(wallet, offer, null, options));
}

/* --- listing a piece ----------------------------------------------------
 *
 * Two calls, like a send, and only the second of them costs anything.
 *
 * `/account/list` builds the leg and spends nothing doing it: an account that
 * holds the key could compute the same bytes for itself, so charging for the
 * reading would be charging for arithmetic. `/account/list/sign` is the request
 * that puts a row on a public page, and that is where the allowance goes.
 * Neither one broadcasts -- a listing is a signature the node holds, not a
 * transaction it makes.
 *
 * This is why the signing is written out here rather than handed to
 * `signOffer`, which is where every other path in this file ends: `signOffer`
 * signs EVERY input with SIGHASH_ALL and posts to `/account/sign`, which
 * broadcasts what comes back. A leg wants two signatures of a different type,
 * and it wants neither of them broadcast. So this is the one place in this file
 * where a key is used with no `/account/sign` anywhere near it, and what keeps
 * it safe is `coins.verifyLeg`, which refuses a leg whose two digests are not
 * the ones this browser worked out for itself.
 */

export async function offerListing(piece, amount, chain, token = 0) {
  return working(async () => {
    const asked = await fetch("/account/list", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({piece, amount, chain: chain || "", token: Number(token) || 0}),
    });
    const leg = await asked.json();
    if (!asked.ok) throw new Error(leg.detail || "that cannot be listed");
    return leg;
  });
}

/** What this browser makes of a leg, before anything is signed. */
export async function checkedListing(leg, wallet) {
  const keys = keysOn(wallet, leg.chain
                      || (wallet.on && Object.keys(wallet.on)[0]));
  return coins.verifyLeg(leg, keys);
}

/** Sign a leg twice and hand it in. Nothing here is broadcast: it could not be.
 *
 * The price sent with it is read off the payload the check above just parsed,
 * not off `leg.price` -- the payload is what the signatures stand over, and a
 * row priced from anywhere else could carry a number no signature covers.
 */
export async function list(wallet, leg, {days = 0, claim = "", bound = ""} = {}) {
  return working(() => _list(wallet, leg, days, claim, bound));
}

/** A prize pool of token lots, each claimed with `phrase` (2026-09-28:
 *  games that pay out a token). `ask` is {property_id, lot, count, price, chain,
 *  days}. Splits a coin first when there are too few to stand the lots on, then
 *  signs and files every lot; returns {ids, what} -- the first id is the one a
 *  page is given, and the node hands out whichever lot is still open. */
export async function makeClaimLots(wallet, ask, phrase, onStep = () => {}) {
  return working(async () => {
    if (!String(phrase || "").trim()) throw new Error("a claim needs its phrase");
    const build = async () => {
      const asked = await fetch("/account/claimlots", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(ask)});
      // Not every refusal is JSON: a route the door does not know answers with
      // a page, and "JSON.parse: unexpected character" helps nobody.
      const said = await asked.json().catch(() => ({}));
      if (!asked.ok) throw new Error(said.detail
        || `this node would not make those lots (it answered ${asked.status})`);
      return said;
    };
    let said = await build();
    if (said.needs_split) {
      onStep("Splitting a coin so every lot has two to stand on\u2026");
      await signOffer(wallet, said);
      said = await build();
      if (said.needs_split) throw new Error("the split is still on its way; try again in a minute");
    }
    const ids = [];
    for (const leg of said.legs || []) {
      onStep(`Signing lot ${ids.length + 1} of ${said.legs.length}\u2026`);
      const done = await _list(wallet, leg, Number(ask.days || 0), phrase, ask.bound || "");
      ids.push(done.listed);
    }
    return {ids, what: said.what || ""};
  });
}

/* --- prize pools at their own address (2026-09-29) --------------------------
 *
 * A token is a balance of an address, so a pool whose tokens sat at the
 * account's own address could be spent from under its lots. A pool gets an
 * address of its own, from these same words at index 1, 2, ...: nothing to
 * write down, and nothing but a claim or closing the pool ever sends from it.
 */
async function poolKeys(wallet, network, index) {
  const on = keysOn(wallet, network);
  const coin = await coins.coinKey(wallet.seed, network, index);
  return {network, version: on.version, key: coin.key, pubkey: coin.pubkey,
          address: await coins.address(coin.pubkey, on.version)};
}

async function askJson(url, body) {
  const asked = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"},
                                  body: JSON.stringify(body)});
  const said = await asked.json().catch(() => ({}));
  return {ok: asked.ok, status: asked.status, said};
}

/** Sign and file a funded pool's lots, waiting for its funding block first. */
async function _finishPool(wallet, chain, address, index, onStep) {
  const keys = await poolKeys(wallet, chain, index);
  let built = null;
  for (let tries = 0; tries < 90; tries++) {             // up to ~20 minutes
    const r = await askJson("/account/claimlots", {chain, pool: address});
    if (r.ok) { built = r.said; break; }
    if (!r.said.waiting) throw new Error(r.said.detail || "the pool's lots could not be made");
    onStep("Waiting for the pool's tokens and coins to land in a block\u2026");
    await new Promise((ok) => setTimeout(ok, 15000));
  }
  if (!built) throw new Error("the pool's funding has not landed yet; finish it later from Your prize pools");
  const ids = [];
  for (const leg of built.legs || []) {
    onStep(`Signing lot ${ids.length + 1} of ${built.legs.length}\u2026`);
    const done = await _list(wallet, leg, Number(leg.days || 0), "", leg.bound || "",
                             keys, leg.claim_hash);
    ids.push(done.listed);
  }
  return {ids, what: built.what || "", address};
}

/** A new prize pool at its own address: register it, fund it from this
 *  account in one send, then sign its lots when that send is in a block.
 *  `ask` is {property_id, lot, count, price, days, bound, chain}. */
export async function makePool(wallet, ask, phrase, onStep = () => {}) {
  return working(async () => {
    if (!String(phrase || "").trim()) throw new Error("a claim needs its phrase");
    const chain = ask.chain;
    const list = await (await fetch("/account/claimpools?chain=" + chain)).json();
    const index = Number(list.next_index || 1);
    const keys = await poolKeys(wallet, chain, index);
    const opened = await askJson("/account/pools/open",
                                 {chain, index, pubkey: coinsHex(keys.pubkey)});
    if (!opened.ok) throw new Error(opened.said.detail || "the pool could not be opened");
    if (opened.said.address !== keys.address) throw new Error("the node named a different pool address");
    const funding = await askJson("/account/pools/fund", {...ask, pool: keys.address,
                                  claim_hash: await claimHash(phrase)});
    if (!funding.ok) throw new Error(funding.said.detail || "the pool could not be funded");
    onStep("Moving the tokens and coins into the pool\u2026");
    await signOffer(wallet, funding.said);
    return _finishPool(wallet, chain, keys.address, index, onStep);
  });
}

/** Finish a pool whose funding landed while the page was closed. */
export async function finishPool(wallet, chain, address, index, onStep = () => {}) {
  return working(() => _finishPool(wallet, chain, address, Number(index), onStep));
}

/* --- prize pools ON THE CHAIN (2026-09-29) --------------------------------
 *
 * A pool is an inscription any node can read: its terms in the JSON, and its
 * lots' signatures sealed with the phrase (AES-GCM, key = SHA-256 of
 * "arcade prize pool v1\n" + phrase). Whoever holds the phrase can open them,
 * which is exactly who can claim; nobody else learns anything.
 */
const POOL_TYPE = "application/vnd.arcade.prizepool";

async function poolCipher(phrase) {
  const bytes = new TextEncoder().encode("arcade prize pool v1\n" + String(phrase || "").trim());
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return crypto.subtle.importKey("raw", digest, "AES-GCM", false, ["encrypt", "decrypt"]);
}

async function sealLots(phrase, lots) {
  const parts = [];
  for (const sigs of lots) for (const sig of sigs) {
    const raw = unhex(sig);
    parts.push(raw.length, ...raw);
  }
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const sealed = new Uint8Array(await crypto.subtle.encrypt(
    {name: "AES-GCM", iv}, await poolCipher(phrase), new Uint8Array(parts)));
  const out = new Uint8Array(12 + sealed.length);
  out.set(iv); out.set(sealed, 12);
  return out;
}

/** The lots' signatures of pool inscription `pool`, opened with `phrase`. */
export async function openPrize(pool, phrase) {
  const bytes = new Uint8Array(await (await fetch("/content/" + pool)).arrayBuffer());
  let plain;
  try {
    plain = new Uint8Array(await crypto.subtle.decrypt(
      {name: "AES-GCM", iv: bytes.slice(0, 12)}, await poolCipher(phrase), bytes.slice(12)));
  } catch (e) { throw new Error("that is not this prize's phrase"); }
  const sigs = [];
  for (let at = 0; at < plain.length;) {
    const n = plain[at];
    sigs.push(coinsHex(plain.slice(at + 1, at + 1 + n)));
    at += 1 + n;
  }
  const lots = [];
  for (let i = 0; i + 1 < sigs.length; i += 2) lots.push([sigs[i], sigs[i + 1]]);
  return lots;
}

/** Everything a prize pool inscription needs, from the JSON a person wrote:
 *  {token, lot, lots, price, game, phrase, days, name}. Funds the pool's own
 *  address from this account (one send, confirmed on its card), signs every
 *  lot with the pool's key, and seals the signatures with the phrase. Returns
 *  {content, json, type} for the inscription; nothing is inscribed here. */
export async function makeChainPool(wallet, spec, chain, onStep = () => {}) {
  return working(async () => {
    const phrase = String(spec.phrase || "").trim();
    if (!phrase) throw new Error("a prize pool needs a \"phrase\"");
    if (spec.fee === "pool" && !spec.referee) {
      throw new Error("a pool that pays its own claims (\"fee\": \"pool\") needs a \"referee\"");
    }
    const nft = spec.kind === "nft";
    let pieces = [];
    if (nft) {
      // The pieces it hands out: named ("#12"), or every piece of a collection
      // this wallet holds (2026-09-29, NFT prize pools).
      const mine = keysOn(wallet, chain).address;
      if (Array.isArray(spec.pieces) && spec.pieces.length) {
        for (const key of spec.pieces) {
          const r = await fetch("/r/inscription/" + encodeURIComponent(String(key).replace(/^#/, "")));
          const row = await r.json().catch(() => ({}));
          if (!r.ok || !row.id) throw new Error(`there is no inscription ${key}`);
          pieces.push(row.id);
        }
      } else if (spec.collection) {
        // A collection is its creator's: only the wallet that made it mints it,
        // so a set that never seals (supply 0, "of \u221e") is pooled from the pieces
        // its creator has minted so far, and topped up with another pool later.
        // Two creators can use one name, so the maker is part of the match.
        const maker = String(spec.creator || mine);
        const held = await (await fetch(`/r/inscriptions/${mine}?limit=500`)).json();
        pieces = (Array.isArray(held) ? held : [])
          .filter((i) => i.collection === spec.collection && i.creator === maker)
          .map((i) => i.id).slice(0, 25);
        if (!pieces.length) throw new Error(`this wallet holds nothing of ${spec.collection}`
          + (spec.creator ? ` made by ${spec.creator}` : " that it made itself: mint some first"));
      } else throw new Error("an NFT pool names its \"pieces\" or its \"collection\"");
    }
    const index = 1 + (crypto.getRandomValues(new Uint32Array(1))[0] % 0x7ffffffe);
    const keys = await poolKeys(wallet, chain, index);
    // A refereed pool (2026-09-29) lives at a two-key address the node works
    // out from the pool key, the referee's and this wallet's own: the one that
    // closes it. So the address is the node's answer, checked below by the lots.
    const opened = await askJson("/account/pools/open", {
      chain, index, pubkey: coinsHex(keys.pubkey),
      ...(spec.referee ? {referee: spec.referee,
                          creator_pubkey: coinsHex(keysOn(wallet, chain).pubkey)} : {})});
    if (!opened.ok) throw new Error(opened.said.detail || "the pool could not be opened");
    if (!spec.referee && opened.said.address !== keys.address) {
      throw new Error("the node named a different pool address");
    }
    const home = opened.said.address;
    const funding = await askJson("/account/pools/fund", nft ? {
      chain, pool: home, kind: "nft", pieces, price: String(spec.price),
      days: spec.days || 0, bound: spec.game ? String(spec.game) : "", once: !!spec.once,
      fee: spec.fee === "pool" ? "pool" : "",
      claim_hash: await claimHash(phrase)} : {
      chain, pool: home, property_id: spec.token, lot: String(spec.lot),
      count: Number(spec.lots), price: String(spec.price), days: spec.days || 0,
      once: !!spec.once, fee: spec.fee === "pool" ? "pool" : "",
      bound: spec.game ? String(spec.game) : "", claim_hash: await claimHash(phrase)});
    if (!funding.ok) throw new Error(funding.said.detail || "the pool could not be funded");
    onStep(nft ? "Putting coins into the pool\u2026" : "Moving the tokens and coins into the pool\u2026");
    const sent = await signOffer(wallet, funding.said);
    for (let n = 0; n < pieces.length; n++) {
      onStep(`Moving piece ${n + 1} of ${pieces.length} into the pool\u2026`);
      const moved = await askJson("/account/nft/send", {piece: pieces[n], to: home, chain});
      if (!moved.ok) throw new Error(moved.said.detail || "a piece could not be moved");
      await signOffer(wallet, moved.said);
    }
    const built = await askJson("/account/pools/legs", {chain, pool: home, fund: sent.txid});
    if (!built.ok) throw new Error(built.said.detail || "the pool's lots could not be built");
    const lots = [];
    for (const leg of built.said.legs) {
      onStep(`Signing lot ${lots.length + 1} of ${built.said.legs.length}\u2026`);
      const shown = await coins.verifyLeg(leg, keys);
      const sigs = [];
      for (const sighash of shown.hashes) {
        sigs.push(coinsHex(await coins.signInput(keys.key, unhex(sighash), coins.SINGLE_ANYONECANPAY)));
      }
      lots.push(sigs);
    }
    const content = await sealLots(phrase, lots);
    const json = JSON.stringify({name: String(spec.name || "Prize pool").slice(0, 60),
                                 prizepool: {...built.said.prizepool,
                                             pubkey: coinsHex(keys.pubkey)}});
    return {content, json, type: POOL_TYPE};
  });
}

/** The transaction that claims one lot of a chain pool, from any node. */
export async function offerPrize(pool, lot, sigs, secret, page, chain, replay = null) {
  return working(async () => {
    const r = await askJson("/account/prize", {pool, lot, signatures: sigs, secret, page,
                                               chain: chain || ""});
    if (!r.ok) throw new Error(r.said.detail || "that prize cannot be claimed");
    return {...r.said, _ask: {pool, lot, signatures: sigs, secret, page, chain: chain || "",
                              ...(replay ? {replay} : {})}};
  });
}

/** Claim a lot of a pool that pays its own claims ("fee": "pool", 2026-10-04):
 *  no key, no coins, nothing to sign -- the referee signs the whole claim once
 *  the pool's judge passes the replay, and the node sends it. */
export async function takePrize(pool, lot, sigs, secret, page, chain, replay = null) {
  return working(async () => {
    const r = await askJson("/account/prize/take", {pool, lot, signatures: sigs, secret, page,
                                                    chain: chain || "", ...(replay ? {replay} : {})});
    if (!r.ok) throw new Error(r.said.detail || "that prize cannot be claimed");
    return r.said;
  });
}

/** A seed for one run at a refereed pool, from its referee (2026-09-29). */
export async function refereeSeed(pool, chain) {
  const r = await askJson("/account/referee/seed", {pool, chain: chain || ""});
  if (!r.ok) throw new Error(r.said.detail || "no seed");
  return r.said;
}

export async function claimPrize(wallet, offer) {
  return working(async () => {
    const keys = keysOn(wallet, offer.chain || (wallet.on && Object.keys(wallet.on)[0]));
    const shown = await coins.verifyOffer(offer, keys);
    const claimer = [];
    for (const sighash of shown.hashes) claimer.push(coinsHex(await coins.signInput(keys.key, unhex(sighash))));
    const r = await askJson("/account/prize/sign", {...offer._ask, raw: offer.raw, claimer,
                                                    pubkey: coinsHex(keys.pubkey)});
    if (!r.ok) throw new Error(r.said.detail || "the node would not take it");
    return {...r.said, fee: shown.fee, says: shown.says};
  });
}

/** Close a chain pool this account made: returns what closePool returns,
 *  for sendClose once the person has said yes. */
export async function closeChainPool(wallet, key, chain) {
  return working(async () => {
    const info = await (await fetch("/r/prizepool/" + encodeURIComponent(key))).json();
    if (!info || !info.txid) throw new Error("there is no such prize pool");
    const r = await askJson("/account/pools/close", {chain, prize: info.txid});
    if (!r.ok) throw new Error(r.said.detail || "that pool could not be closed");
    const keys = r.said.creator_signs ? keysOn(wallet, chain)
                                      : await poolKeys(wallet, chain, Number(info.index));
    if (Array.isArray(r.said.offers)) {
      const offers = [];
      for (const offer of r.said.offers) offers.push({offer, shown: await coins.verifyOffer(offer, keys)});
      const says = `${offers.length} transaction${offers.length === 1 ? "" : "s"}: `
        + offers.map((o) => o.offer.what || "").filter(Boolean).join("; ") + ".";
      return {offers, shown: {says}, keys, pool: info.txid};
    }
    const shown = await coins.verifyOffer(r.said, keys);
    return {offer: r.said, shown, keys, pool: info.txid};
  });
}

/** Cancel and close a pool: its tokens and every coin back to this account,
 *  signed with the pool's own key. Returns what was sent. */
export async function closePool(wallet, chain, address, index) {
  return working(async () => {
    const r = await askJson("/account/pools/close", {chain, pool: address});
    if (!r.ok) throw new Error(r.said.detail || "the pool could not be closed");
    const keys = await poolKeys(wallet, chain, Number(index));
    const shown = await coins.verifyOffer(r.said, keys);
    return {offer: r.said, shown, keys};
  });
}

/** Send what closePool built, once the person has said yes. */
export async function sendClose(wallet, closing) {
  return working(async () => {
    if (closing.offers) {
      let last = null;
      for (const o of closing.offers) last = await signOffer(wallet, o.offer, closing.keys);
      return last;
    }
    return signOffer(wallet, closing.offer, closing.keys);
  });
}

/** Offer one payment to this account's own address in `count` coins -- what a
 *  mintpad needs to stand one listing per piece on (two coins each). */
export async function offerSplit(count, chain) {
  return working(async () => {
    const asked = await fetch("/account/split", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({count, chain: chain || ""}),
    });
    const offer = await asked.json();
    if (!asked.ok) throw new Error(offer.detail || "the coins could not be split");
    return offer;
  });
}

async function _list(wallet, leg, days = 0, claim = "", bound = "", given = null,
                     hashed = "") {
  const keys = given || keysOn(wallet, leg.chain
                      || (wallet.on && Object.keys(wallet.on)[0]));
  const shown = await coins.verifyLeg(leg, keys);
  const signatures = [];
  for (const sighash of shown.hashes) {
    signatures.push(coinsHex(await coins.signInput(
      keys.key, unhex(sighash), coins.SINGLE_ANYONECANPAY)));
  }
  const done = await fetch("/account/list/sign", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({raw: leg.raw, chain: leg.chain || "",
                          amount: shown.coinsOf(shown.listing.sats),
                          pubkey: coinsHex(keys.pubkey), signatures, days,
                          claim_hash: hashed || (claim ? await claimHash(claim) : ""),
                          bound: bound || "", owner: given ? given.address : ""}),
  });
  const said = await done.json();
  if (!done.ok) throw new Error(said.detail || "the node would not take it");
  return {...said, says: shown.says, reserved: shown.reserved};
}

/* --- answering an offer, with this account's own key --------------------
 *
 * `/exchange/offers/{txid}` is where the operator's wallet answers an offer,
 * and every step of that belongs to a wallet this node holds the key for: it
 * signs an OFFER back, and its own node countersigns the buyer's half against
 * it. An account holds a key that signs transactions and no key that signs an
 * offer, so its answer is the other thing a holder can hand over -- a LEG, the
 * same bytes `/account/list` builds and the same two signatures, at the
 * BUYER's price instead of its own -- and the answer is a letter carrying them
 * rather than a transaction. `/account/accept` builds it,
 * `/account/accept/sign` checks the signatures really say the offer that was
 * made, and `/account/talk` carries the sealed answer to whoever asked.
 *
 * Three signatures then, of two kinds. Two are asked of the coin key with
 * SINGLE|ANYONECANPAY and neither is broadcast: there is no transaction here
 * to broadcast, only a promise that the buyer's wallet finishes with its own
 * coins. The third is an ordinary send of one message, signed the usual way
 * and broadcast, and it is the only coin this account spends -- a fee, for a
 * letter. That is the whole cost of saying yes from a tab, and the reason the
 * price is never taken from the tab: a signature is over a number, and this is
 * the number that was asked.
 *
 * Refusing is the third signature alone. `refuse` asks the node for the words
 * to seal and never touches a coin key at all.
 */
/** Sign the buyer's half of a pre-signed offer, once the offer itself is sent.
 *  `offer` is what `/account/offer` answered; `sent` is the broadcast offer's
 *  result ({txid}); `expect` is what this page asked for: {piece, seller, sats}
 *  or {piece, seller, token: {propertyid, units}}. */
export async function signBid(wallet, offer, sent, expect) {
  if (!offer || !offer.presigned) return null;
  return working(async () => {
    const keys = keysOn(wallet, offer.chain || (wallet.on && Object.keys(wallet.on)[0]));
    // Built on the coin the offer just set aside, now that its txid is known.
    const built = await fetch("/account/offer/bid-build", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({offer: offer.offer, txid: sent.txid}),
    });
    const bid = await built.json();
    if (!built.ok) throw new Error(bid.detail || "the node could not build the trade");
    const take = bid.take || {};
    const shown = await coins.verifyBid(bid, keys, {
      piece: expect.piece, seller: expect.seller,
      sats: take.kind === "coins" ? expect.sats : 0,
      token: take.kind === "token" ? {propertyid: take.propertyid, units: take.units} : null});
    const signatures = [];
    for (const h of shown.hashes) {
      signatures.push(coinsHex(await coins.signInput(keys.key, unhex(h),
                                                      coins.ALL_ANYONECANPAY)));
    }
    const back = await fetch("/account/offer/bid", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({offer: offer.offer, txid: sent.txid, signatures,
                            pubkey: coinsHex(keys.pubkey)}),
    });
    const said = await back.json();
    if (!back.ok) throw new Error(said.detail || "the node would not keep it");
    return said;
  });
}

/** Take a pre-signed offer back: one small transaction spending its coins. */
/** Sign a buy order's lots (arcade/standing.py): for each, read back the one-input
 *  one-output transaction, check the coin is ours and the output is our own change
 *  of exactly what was said, work the SINGLE|ANYONECANPAY digest out here, and only
 *  then sign it. The node never gets anything it did not show. */
export async function signLots(wallet, lots) {
  return working(async () => {
    const keys = keysOn(wallet, (wallet.on && Object.keys(wallet.on)[0]));
    const mine = coins.p2pkhOf ? await coins.p2pkhOf(keys) : null;
    const out = [];
    for (const lot of lots) {
      const digest = await coins.checkLot(lot, keys);
      out.push(coinsHex(await coins.signInput(keys.key, digest, coins.SINGLE_ANYONECANPAY)));
    }
    return out;
  });
}

/** Offer the sweep of this account's own payload outputs on `chain`. */
export async function offerDustSweep(chain) {
  const asked = await fetch("/account/dust/sweep", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({chain: chain || ""}),
  });
  const offer = await asked.json();
  if (!asked.ok) throw new Error(offer.detail || "there is nothing to sweep");
  return offer;
}

/** Sweep this account's payload outputs back by itself (2026-10-17, of the Wallet's
 *  "Sweep back 400 of 2412" button: "Can this be automatically swept back?"). Runs from every
 *  arcade page while the wallet is open; never asks for it. Signed only when every output of the
 *  transaction comes back to this wallet -- nothing paid to anybody, the fee aside -- and the fee
 *  is under half of what comes back; anything else waits for the Wallet's own button. One sweep
 *  (up to 400 outputs) per chain per SWEEP_GAP: a sweep drops its outputs at once, so the next
 *  takes the next 400. */
const SWEEP_GAP = 120000, SWEEP_KEY = "arcade-autosweep";
const SWEEP_MIN = () => Number(globalThis.ARCADE_SWEEP_MIN || 10);   // a test lowers it
let sweeping = false;
export async function autoSweep() {
  if (sweeping) return [];
  let last = {};
  try { last = JSON.parse(localStorage.getItem(SWEEP_KEY) || "{}") || {}; } catch (e) { last = {}; }
  const due = (net) => Date.now() - Number(last[net] || 0) >= SWEEP_GAP;
  const done = [];
  // Asking what there is to sweep costs the node seconds (each payload transaction read): asked
  // once per SWEEP_GAP per browser, not on every page that opens.
  if (Date.now() - Number(last._asked || 0) < SWEEP_GAP) return done;
  last._asked = Date.now();
  try { localStorage.setItem(SWEEP_KEY, JSON.stringify(last)); } catch (e) {}
  sweeping = true;
  try {
    let said = null;
    try { said = await (await fetch("/account/dust", {cache: "no-store"})).json(); } catch (e) { return done; }
    for (const c of (said && said.chains) || []) {
      if (c.count < SWEEP_MIN() || !due(c.chain)) continue;
      const held = await opened({network: c.chain, version: c.version});
      if (!held) continue;                           // a locked wallet: nothing happens
      last[c.chain] = Date.now();
      try { localStorage.setItem(SWEEP_KEY, JSON.stringify(last)); } catch (e) {}
      try {
        const offer = await offerDustSweep(c.chain);
        const shown = await checked(offer, held);
        const back = Number(shown.change || 0), fee = Number(shown.fee || 0);
        if ((shown.pays || []).some((p) => !p.mine) || !(back > 0) || fee * 2 > back) continue;
        await confirm(held, offer, {reviewed: true});
        done.push({chain: c.chain, label: c.label, count: offer.count, coins: back});
      } catch (e) { /* tried again after SWEEP_GAP */ }
    }
  } finally { sweeping = false; }
  if (done.length && globalThis.arcadeToast) {
    arcadeToast(done.map((d) => `Swept ${(d.coins / 1e8).toLocaleString(undefined, {maximumFractionDigits: 8})} `
      + `${d.label} coins back from ${d.count} message outputs.`).join(" "));
  }
  return done;
}

export async function withdrawOffer(wallet, txid) {
  const asked = await fetch("/account/offer/withdraw", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({offer: txid}),
  });
  const offer = await asked.json();
  if (!asked.ok) throw new Error(offer.detail || "that offer cannot be withdrawn");
  return offer;
}

/** Accept a pre-signed offer: the trade completes now (2026-09-27).
 *  When this account has no coin of the exact size the trade takes, one is made
 *  first -- one small transaction -- and the trade spends it at once. */
async function _acceptPresigned(wallet, ask, said) {
  const chain = said.chain || ask.chain || "";
  const keys = keysOn(wallet, chain || (wallet.on && Object.keys(wallet.on)[0]));
  let got = said;
  if (got.needs_coin) {
    const split = await signOffer(wallet, got);
    const again = await fetch("/account/accept", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({piece: ask.piece, offer: ask.offer, chain,
                            coin: {txid: split.txid, vout: 0}}),
    });
    got = await again.json();
    if (!again.ok) throw new Error(got.detail || "that offer cannot be accepted");
  }
  const shown = await coins.verifyFill(got, keys);
  const signature = coinsHex(await coins.signInput(keys.key, unhex(shown.hash)));
  const done = await fetch("/account/accept/complete", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({bid: got.bid, raw: got.raw, signature, chain,
                          pubkey: coinsHex(keys.pubkey)}),
  });
  const out = await done.json();
  if (!done.ok) throw new Error(out.detail || "the node would not send it");
  return {...out, completed: true,
          says: `Sold for ${got.price} to ${got.buyer_tag ? "@" + got.buyer_tag : "the buyer"}.`};
}

export async function answerOffer(wallet, ask) {
  return working(() => _answerOffer(wallet, ask));
}

async function _answerOffer(wallet, ask) {
  const accept = () => fetch("/account/accept", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({piece: ask.piece, offer: ask.offer,
                          decision: ask.refuse ? "refuse" : "",
                          chain: ask.chain || ""}),
  });
  let asked = await accept();
  let said = await asked.json();
  if (!asked.ok) throw new Error(said.detail || "that offer cannot be answered");
  let splitNote = "";
  if (said.needs_split) {
    splitNote = " One coin was split first, a small send to yourself, so the answer had two to sign.";
    // One coin in the wallet and an answer signs two: a small send to yourself
    // first, then the answer is asked for again (a tester, 2026-09-27).
    await signOffer(wallet, said);
    asked = await accept();
    said = await asked.json();
    if (!asked.ok) throw new Error(said.detail || "that offer cannot be answered");
  }
  if (said.presigned && !ask.refuse) return _acceptPresigned(wallet, ask, said);
  const chain = said.chain || ask.chain || "";
  let answer = said.answer, says = said.what || "";
  if (!ask.refuse) {
    const keys = keysOn(wallet, chain
                        || (wallet.on && Object.keys(wallet.on)[0]));
    const shown = await coins.verifyLeg(said, keys);
    const signatures = [];
    for (const sighash of shown.hashes) {
      signatures.push(coinsHex(await coins.signInput(
        keys.key, unhex(sighash), coins.SINGLE_ANYONECANPAY)));
    }
    const back = await fetch("/account/accept/sign", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({piece: ask.piece, offer: ask.offer, chain,
                            raw: said.raw, signatures,
                            pubkey: coinsHex(keys.pubkey)}),
    });
    const checked = await back.json();
    if (!back.ok) throw new Error(checked.detail || "the node would not take it");
    answer = checked.answer;
    says = checked.what || says;
  }
  const {mail, me} = await messenger(wallet);
  const talked = await fetch("/account/talk", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({op: "send", to: said.seal_to,
                          sealed: mail.sealForProgram(me, said.seal_to,
                                                     said.stamp, answer)}),
  });
  const carrier = await talked.json();
  if (!talked.ok) throw new Error(carrier.detail || "the node could not carry it");
  const sent = await signOffer(wallet, carrier);
  return {...sent, says: says + splitNote, refused: !!ask.refuse};
}

/* --- finishing an offer somebody answered -------------------------------
 *
 * The other end of `answerOffer`. The seller's answer arrives as a sealed
 * program message carrying their signed leg; `/account/fill` builds the
 * transaction around it with this account's coins behind, and
 * `/account/fill/sign` pastes this key's signatures in and broadcasts. Nothing
 * is signed before `coins.verifyOffer` has checked that only this key's own
 * inputs (from `signed_from` on) are asked for. Until 2026-09-26 no page did
 * this, so an accepted offer could never be finished from a browser (found
 * filming the accept video).
 *
 * The answers are read with `programAnswers` and kept in this browser, with
 * the cursor, so each page load reads only what is new.
 */
const ANSWERS = (pubkey) => `arcade.answers.${pubkey}`;

export async function answersToMe(wallet) {
  const {mail, me} = await messenger(wallet);
  const slot = ANSWERS(coinsHex(me.publicKey));
  let kept = {cursor: 0, answers: {}};
  try { kept = JSON.parse(localStorage.getItem(slot) || "null") || kept; } catch (e) {}
  const read = await mail.programAnswers(me, kept.cursor);
  for (const a of read.answers) {
    const j = a.json || {};
    if (j.swap !== "bid" || !j.id) continue;
    kept.answers[j.id] = {id: j.id, ok: !!j.ok, leg: j.leg || null,
                          error: j.error || "", when: a.when, txid: a.txid};
  }
  kept.cursor = read.cursor;
  try { localStorage.setItem(slot, JSON.stringify(kept)); } catch (e) {}
  return kept.answers;                     // offer txid -> the answer to it
}

export async function offerFill(leg, chain) {
  return working(async () => {
    const asked = await fetch("/account/fill", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({leg, chain: chain || ""}),
    });
    const offer = await asked.json();
    if (!asked.ok) throw new Error(offer.detail || "that answer cannot be finished");
    return offer;
  });
}

export async function fill(wallet, leg, offer, expect) {
  return working(async () => {
    const keys = keysOn(wallet, offer.chain
                        || (wallet.on && Object.keys(wallet.on)[0]));
    const shown = await coins.verifyOffer(offer, keys);
    const signatures = [];
    for (const sighash of shown.hashes) {
      signatures.push(coinsHex(await coins.signInput(keys.key, unhex(sighash))));
    }
    const done = await fetch("/account/fill/sign", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({leg, chain: offer.chain || "", raw: offer.raw,
                            signatures, pubkey: coinsHex(keys.pubkey),
                            ...(expect ? {expect} : {})}),
    });
    const said = await done.json();
    if (!done.ok) throw new Error(said.detail || "the node would not take it");
    return {...said, fee: shown.fee, says: shown.says};
  });
}

/* --- taking an escrow back, after its unlock time --------------------------
 *
 * The node lists every move home (`/account/escrow/reclaim`), unsigned. Each is
 * read here before this key signs it: one coin in, spent from the escrow, and
 * every output an OP_RETURN, this owner, or the escrow itself; the digest is
 * worked out here from the escrow's own script (the redeem script, whose hash
 * the spent coin pays), never taken from the node.
 */
export async function reclaimEscrow(wallet, escrow, chain) {
  return working(async () => {
    const asked = await fetch("/account/escrow/reclaim", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({escrow, chain: chain || ""}),
    });
    const said = await asked.json();
    if (!asked.ok) throw new Error(said.detail || "that escrow cannot be taken back yet");
    const keys = keysOn(wallet, said.chain || chain || (wallet.on && Object.keys(wallet.on)[0]));
    const redeem = unhex(said.redeem);
    const mine = coins.p2pkh(await coins.hash160(keys.pubkey));
    const escrowScript = new Uint8Array([0xa9, 20, ...(await coins.hash160(redeem)), 0x87]);
    const same = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
    const txs = [];
    for (const t of said.txs) {
      const tx = coins.parseTx(t.raw);
      if (tx.inputs.length !== 1) throw new Error("a move home spends one coin. Nothing was signed.");
      for (const out of tx.outputs) {
        const ok = out.script[0] === 0x6a || same(out.script, mine) || same(out.script, escrowScript);
        if (!ok) throw new Error("a move home pays somebody else. Nothing was signed.");
      }
      const digest = coinsHex(await coins.sighashAll(tx, 0, redeem));
      if (digest !== String(t.sighash).toLowerCase()) {
        throw new Error("the node asked for a signature over different bytes. Nothing was signed.");
      }
      txs.push({raw: t.raw, signature: coinsHex(await coins.signInput(keys.key, unhex(digest)))});
    }
    const back = await fetch("/account/escrow/reclaim/sign", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({escrow, chain: said.chain, txs}),
    });
    const done = await back.json();
    if (!back.ok) throw new Error(done.detail || "the node would not send it");
    return done;                            // {ok, txids}
  });
}

/* --- a trade between two players, agreed inside a game -------------------
 *
 * The side giving an NFT signs a leg for ONE named player (`tradeLeg`), seals
 * it to that player's messaging key (`sealTrade`), and the viewer carries it
 * across the realtime room. The other side opens it (`openTrade`) and finishes
 * it with the terms it agreed to (`offerTradeFill`, then `fill(..., expect)`):
 * the node refuses a leg whose bytes say any other trade. A leg in clear is a
 * promise anybody holding it could take up, so it only ever travels sealed.
 */
export async function tradeLeg(wallet, t) {
  return working(async () => {
    const ask = () => fetch("/account/trade/leg", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(t),
    });
    let asked = await ask(), said = await asked.json();
    if (!asked.ok) throw new Error(said.detail || "that trade cannot be signed");
    if (said.needs_split) {
      await signOffer(wallet, said);       // one small send to yourself first
      asked = await ask(); said = await asked.json();
      if (!asked.ok) throw new Error(said.detail || "that trade cannot be signed");
    }
    const keys = keysOn(wallet, said.chain || t.chain
                        || (wallet.on && Object.keys(wallet.on)[0]));
    const shown = await coins.verifyLeg(said, keys);
    const signatures = [];
    for (const sighash of shown.hashes) {
      signatures.push(coinsHex(await coins.signInput(
        keys.key, unhex(sighash), coins.SINGLE_ANYONECANPAY)));
    }
    const back = await fetch("/account/trade/leg/sign", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({...t, chain: said.chain, raw: said.raw, signatures,
                            pubkey: coinsHex(keys.pubkey)}),
    });
    const checked = await back.json();
    if (!back.ok) throw new Error(checked.detail || "the node would not take it");
    return checked;                        // {leg, seal_to, what, held_until}
  });
}

/** A bundle, the proposer's half (2026-10-04): built by the node, this key's
 *  inputs signed here, everything the other player needs handed back to seal. */
export async function bundleBuild(wallet, t) {
  return working(async () => {
    const r = await askJson("/account/bundle/build", t);
    if (!r.ok) throw new Error(r.said.detail || "that trade cannot be built");
    const keys = keysOn(wallet, r.said.chain);
    const shown = await coins.verifyOffer(r.said, keys);
    const sigs = [];
    for (const h of shown.hashes) sigs.push(coinsHex(await coins.signInput(keys.key, unhex(h))));
    return {seal_to: r.said.seal_to, fee: shown.fee, coinsOf: shown.coinsOf, says: shown.says,
            handoff: {raw: r.said.raw, a: r.said.a, b: r.said.b, a_sigs: sigs,
                      a_pubkey: coinsHex(keys.pubkey), give: t.give, get: t.get}};
  });
}

/** A bundle, the other player's half: what this key is asked to sign. */
export async function bundleOffer(handoff, expect, chain) {
  return working(async () => {
    const r = await askJson("/account/bundle/fill", {handoff, expect, chain: chain || ""});
    if (!r.ok) throw new Error(r.said.detail || "that trade cannot be finished");
    return r.said;
  });
}

/** Sign this key's inputs of a bundle and send it. */
export async function bundleFill(wallet, handoff, offer, expect) {
  return working(async () => {
    const keys = keysOn(wallet, offer.chain || (wallet.on && Object.keys(wallet.on)[0]));
    const shown = await coins.verifyOffer(offer, keys);
    const signatures = [];
    for (const h of shown.hashes) signatures.push(coinsHex(await coins.signInput(keys.key, unhex(h))));
    const r = await askJson("/account/bundle/fill/sign", {handoff, expect, chain: offer.chain || "",
                                                          signatures, pubkey: coinsHex(keys.pubkey)});
    if (!r.ok) throw new Error(r.said.detail || "the node would not send it");
    return r.said;
  });
}

export async function sealTrade(wallet, toKeyHex, value) {
  const {mail, me} = await messenger(wallet);
  const sealed = mail.sealEnvelope(new TextEncoder().encode(JSON.stringify(value)),
                                   me, unhex(toKeyHex));
  return btoa(String.fromCharCode(...sealed));
}

export async function openTrade(wallet, b64) {
  const {mail, me} = await messenger(wallet);
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  // Two layers, as sealEnvelope makes them: the sealed box to this key, and
  // inside it the envelope that proves who sealed it.
  const inner = mail.openSealedBox(bytes, me);
  const opened = inner && mail.openEnvelope(inner, me);
  if (!opened) throw new Error("that trade was not sealed for this account");
  return JSON.parse(new TextDecoder().decode(opened.plain));
}

export async function offerTradeFill(leg, expect, chain) {
  return working(async () => {
    const asked = await fetch("/account/fill", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({leg, expect, chain: chain || ""}),
    });
    const offer = await asked.json();
    if (!asked.ok) throw new Error(offer.detail || "that trade cannot be finished");
    return offer;
  });
}

/* --- taking a price off another node's book, with this account's key ------
 *
 * `/account/take` says what the trade is, what it prices to, and whose key the
 * question has to be sealed to. These two are the question and the reading of
 * the answer, and both are thin on purpose. The sealing happens here, by
 * `sealForProgram`, to the key the node named: that is the arrangement that
 * lets an account ask at all. The node wraps and pays for a carrier it cannot
 * read, and a leg sealed to this key is readable in this browser and nowhere
 * else (D-169).
 *
 * An answer is found by `re`, the id of the question it answers, which is the
 * only thing in it this tab has to match on. Nothing else in it is trusted,
 * least of all the price: `/account/fill` re-derives the whole transaction from
 * the leg's own bytes and the chain and refuses one whose numbers do not say
 * the order it names (D-063).
 */
const BOOKANSWERS = (pubkey) => `arcade.bookanswers.${pubkey}`;

export async function askTheBook(wallet, to, body) {
  return working(async () => {
    const {mail, me} = await messenger(wallet);
    const asked = await talkAsk({op: "ask", to});
    const out = await signOffer(wallet, await talkAsk({
      op: "send", to,
      sealed: mail.sealForProgram(me, asked.seal_to, asked.stamp, body)}));
    return {txid: out.txid, fee: out.fee, to: asked.to};
  });
}

/** The book answers read since the last time, keyed by their question. */
export async function bookAnswers(wallet, wanted) {
  const {mail, me} = await messenger(wallet);
  const slot = BOOKANSWERS(coinsHex(me.publicKey));
  let kept = {cursor: 0, answers: {}};
  try { kept = JSON.parse(localStorage.getItem(slot) || "null") || kept; } catch (e) {}
  const read = await mail.programAnswers(me, kept.cursor);
  for (const a of read.answers) {
    const j = a.json || {};
    if (j.swap !== "fill" || !j.re) continue;
    kept.answers[j.re] = {re: j.re, ok: !!j.ok, leg: j.offer || null,
                          error: j.error || "", when: a.when, txid: a.txid};
  }
  // Answers are kept rather than re-derived, cursor and all: a maker answers
  // out of order, and an answer that is read once and then forgotten is an
  // answer that never arrived.
  kept.cursor = read.cursor;
  try { localStorage.setItem(slot, JSON.stringify(kept)); } catch (e) {}
  return wanted ? (kept.answers[wanted] || null) : kept.answers;
}

/** The transaction a book answer becomes, with this key signing nothing yet.
 *
 * The node builds it and cannot sign it, which is the whole arrangement: the
 * answer says which of the maker's coins go in and what they price to, and
 * `/account/take/build` puts this account's own coins behind that and stops
 * there. `answer.leg` is the maker's offer, kept as it arrived. The node will
 * build the very same transaction again when the signatures come back, so the
 * answer travels with the result rather than being forgotten -- a tab that
 * reloaded between the two has to be able to say what it was building.
 */
export async function bookTakeBuild(answer, chain) {
  return working(async () => {
    const asked = await fetch("/account/take/build", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({offer: answer, chain: chain || ""}),
    });
    const built = await asked.json();
    if (!asked.ok) {
      throw new Error(built.detail || "that answer cannot be made into a trade");
    }
    built.answer = answer;
    return built;
  });
}

/** Sign this account's half of a book answer, and hand it back to the maker.
 *
 * Two requests and one message, and no broadcast anywhere. `verifyOffer` is
 * what stops this tab signing the maker's input: it refuses anything before
 * `signed_from`, and anything whose coin is not this key's. The signatures go
 * to `/account/take/sign`, which pastes them in and returns the transaction
 * unfinished on purpose -- the maker's half is still an empty scriptSig, and
 * only the maker can fill it. So the third step carries it back as a `sign`
 * message, sealed to the key the node named, over the same `askTheBook` that
 * carried the question. What comes back is the message's txid, not the
 * trade's: the trade is the maker's node's to broadcast, and it says so in its
 * own answer.
 */
export async function signTake(wallet, built) {
  return working(async () => {
    const keys = keysOn(wallet, built.chain
                        || (wallet.on && Object.keys(wallet.on)[0]));
    const shown = await coins.verifyOffer(built, keys);
    const signatures = [];
    for (const sighash of shown.hashes) {
      signatures.push(coinsHex(await coins.signInput(keys.key, unhex(sighash))));
    }
    const done = await fetch("/account/take/sign", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({offer: built.answer, chain: built.chain || "",
                            raw: built.raw, signatures,
                            pubkey: coinsHex(keys.pubkey)}),
    });
    const half = await done.json();
    if (!done.ok) throw new Error(half.detail || "the node would not take it");
    const carried = await askTheBook(wallet, half.to, {
      swap: "sign", swapv: half.swapv, offer: half.offer, hex: half.hex});
    return {...half, fee: shown.fee, says: shown.says, message: carried.txid};
  });
}

/* --- filling a listing, with this account's own key ----------------------
 *
 * The other half of `list`. The seller's two signatures are already in the
 * book; `/account/buy` builds the transaction around them with this account's
 * coins behind, and says in `signed_from` where those coins start. So the
 * check is `coins.verifyOffer`, which refuses to sign any input before that
 * point or any coin this key does not hold, and the signatures go to
 * `/account/buy/sign`, which pastes them in and broadcasts -- this node signs
 * nothing of either side.
 */

export async function offerBuy(listing, chain, secret = "", page = "") {
  return working(async () => {
    const asked = await fetch("/account/buy", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({listing, chain: chain || "", secret: secret || "",
                            page: page || ""}),
    });
    const offer = await asked.json();
    if (!asked.ok) throw new Error(offer.detail || "that cannot be bought");
    // A claim's phrase goes with the signature too: the node decides the
    // trade again from scratch there, phrase and all.
    return {...offer, secret: secret || "", page: page || ""};
  });
}

/** sha256 of a claim phrase, as the node stores it: trimmed, UTF-8, hex. */
export async function claimHash(phrase) {
  const bytes = new TextEncoder().encode(String(phrase || "").trim());
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function buy(wallet, offer) {
  return working(async () => {
    const keys = keysOn(wallet, offer.chain
                        || (wallet.on && Object.keys(wallet.on)[0]));
    const shown = await coins.verifyOffer(offer, keys);
    const signatures = [];
    for (const sighash of shown.hashes) {
      signatures.push(coinsHex(await coins.signInput(keys.key, unhex(sighash))));
    }
    const done = await fetch("/account/buy/sign", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({listing: offer.listing, chain: offer.chain || "",
                            secret: offer.secret || "", page: offer.page || "",
                            raw: offer.raw, signatures,
                            pubkey: coinsHex(keys.pubkey)}),
    });
    const said = await done.json();
    if (!done.ok) throw new Error(said.detail || "the node would not take it");
    return {...said, fee: shown.fee, says: shown.says};
  });
}

/* --- buying from a shop, with this account's own key --------------------
 *
 * `/swap/{txid}` is where the operator's wallet buys, and every step of it
 * that costs anything is that wallet's: it seals the order, it carries the
 * message, it asks its owner to approve the trade. An account has a key
 * instead of a wallet, so the same four questions a shop page asks get
 * answered here instead, over `/account/shop` -- and the answer to each is
 * built by this node, checked by this tab, and signed nowhere else.
 *
 * **This is the only flow here that signs three things in a row**, and the
 * reason is not decoration:
 *
 *   the order      a message like any other -- verify, sign, broadcast;
 *   the buyer's    a transaction, checked with `coins.verifyOffer` before a
 *     half         digest is read, and NOT broadcast: the shop is the only
 *                  machine that can spend the piece it is selling;
 *   the signature  a second message, carrying the finished half's bytes onto
 *                  a trade this node will never be able to complete itself.
 *
 * Two transactions where the wallet spends one, and one approval that does
 * not exist. That is the price of a node that cannot act for the key it was
 * never given, which is the arrangement everything else in this file is
 * built on: a node that could seal an order and countersign a trade would be
 * holding the whole thing up by its own end, and the shop on the far side
 * would have no reason to care whether it was talking to a person at all.
 */

/** What the shop says, asked as this account rather than as this wallet. */
async function askShop(shop, body) {
  const asked = await fetch("/account/shop", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({shop, ...body}),
  });
  const said = await asked.json();
  if (!asked.ok) throw new Error(said.detail || "the node would not speak of this shop");
  return said;
}

/** The messaging key behind these words -- what a shop seals its answer to. */
async function messenger(wallet) {
  const mail = await import("/messaging.js");
  return {mail, me: await mail.identity(wallet.phrase)};
}

//: How far through the node's machine messages each key in this tab has
//: read, and everything opened since. Answers are kept rather than re-derived
//: because a page asks for them twice and a shop answers out of order: an
//: answer that is read once and then forgotten is an answer that never
//: arrives. Reading costs a decrypt and no coins, so it is done once.
const shopInbox = new Map();

export async function shopDoor(wallet, shop, ask) {
  return working(async () => {
    const op = String(ask.op || "");
    if (op === "shop") {
      const said = await askShop(shop, {op: "shop"});
      // `account` is the page's notice that the thing answering is a key and
      // not a wallet: no approval window is coming, because there is no
      // wallet to ask. `mine` is the refusal, from the chain rather than from
      // this node's keypool -- a shop's own account is not one of its
      // customers, and a self-sale would print a price on a public page as
      // though a stranger had paid it.
      return {...said, account: true};
    }
    if (op === "offer") {
      const asked = await askShop(shop, {op: "offer", listing: Number(ask.listing)});
      const {mail, me} = await messenger(wallet);
      const out = await signOffer(wallet, await askShop(shop, {
        op: "send", sealed: mail.sealForProgram(me, asked.seal_to, asked.stamp,
                                                asked.order)}));
      return {txid: out.txid, buyer: asked.order.buyer, to: asked.to,
              listing: Number(ask.listing)};
    }
    if (op === "replies") {
      const {mail, me} = await messenger(wallet);
      const who = mail.hex(me.publicKey);
      const kept = shopInbox.get(who) || {cursor: 0, answers: []};
      const found = await mail.programAnswers(me, kept.cursor);
      kept.answers = kept.answers.concat(found.answers).slice(-200);
      kept.cursor = Math.max(kept.cursor, found.cursor);
      shopInbox.set(who, kept);
      return {replies: kept.answers.map((a) => ({json: a.json, txid: a.txid,
                                                 when: a.when}))};
    }
    if (op === "accept") {
      return await shopSign(wallet, shop, ask.offer);
    }
    throw new Error(`a shop page asked for ${op || "nothing"}`);
  });
}

/** Sign the buyer's half and hand it to the shop, in two more transactions.
 *
 * The half is verified before a single digest of it is signed, which is what
 * makes the second signature safe: `signed_from` says which input belongs to
 * the shop and `coins.verifyOffer` refuses to sign one that is not this
 * key's, so a node cannot hide a coin of its own in the trade and have an
 * account pay for it. Then `/account/shop/sign` rebuilds the same bytes and
 * compares them -- if a block landed in between, these signatures stand over
 * a different transaction and the shop would be right to refuse them, which
 * is said here rather than three blocks later.
 */
async function shopSign(wallet, shop, offer) {
  const half = await askShop(shop, {op: "accept", offer});
  const coins = await import("/coins.js");
  const keys = keysOn(wallet, half.chain || (wallet.on && Object.keys(wallet.on)[0]));
  const shown = await coins.verifyOffer(half, keys);
  const signatures = [];
  for (const sighash of shown.hashes) {
    signatures.push(coinsHex(await coins.signInput(keys.key, unhex(sighash))));
  }
  const finished = await fetch("/account/shop/sign", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({shop, offer, raw: half.raw, signatures,
                          pubkey: coinsHex(keys.pubkey)}),
  });
  const signed = await finished.json();
  if (!finished.ok) {
    throw new Error(signed.detail || "the node would not finish this trade");
  }
  const {mail, me} = await messenger(wallet);
  const out = await signOffer(wallet, await askShop(shop, {
    op: "send", sealed: mail.sealForProgram(me, signed.seal_to, signed.stamp,
                                            signed.order)}));
  // `request: null` and `account: true` together are what `swap.js` reads as
  // "there is nothing to wait for": the decision already happened, in this
  // tab, with this key.
  return {txid: out.txid, fee: out.fee, says: shown.says, what: signed.what,
          cut: signed.cut || {}, request: null, account: true};
}

/* --- a page speaking to another node, with this account's own key ---------
 *
 * `/node/{txid}` is where the operator's wallet speaks for a page: it seals
 * with its identity, broadcasts from a funded address, and keeps the page's
 * letters so the answer can find the page that started it. An account has a
 * key instead of a wallet, so the questions `node.js` asks get answered
 * here instead, over `/account/talk` -- and as with a shop, the answer to
 * each is built by this node, sealed in this tab, and signed nowhere else.
 *
 * The conversation lives in this tab and not on the node, which is a
 * decision (D-169) rather than an oversight: a node that remembers its
 * pages' chats is holding the one thing this whole arrangement exists not
 * to hold, and an answer is sealed to the account's key, which only this
 * browser has anyway. One log per tab, sorted out per page: a page reads
 * replies from the nodes IT wrote to, exactly as the wallet's door
 * promises, and a page's floor at its first send keeps one page's answers
 * out of another page's inbox.
 */
const talkInbox = new Map();      // messaging key -> {cursor, seq, log, pages}

async function talkAsk(body) {
  const asked = await fetch("/account/talk", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  const said = await asked.json();
  if (!asked.ok) throw new Error(said.detail || "the node would not carry this");
  return said;
}

/** Open the machine messages since this tab last read, and file what opens. */
async function talkRead(me, box) {
  const mail = await import("/messaging.js");
  const found = await mail.programAnswers(me, box.cursor);
  for (const row of found.answers) {
    box.log.push({seq: ++box.seq, sender: row.sender, txid: row.txid,
                  when: row.when, block: row.height, json: row.json});
  }
  if (box.log.length > 500) box.log.splice(0, box.log.length - 500);
  box.cursor = Math.max(box.cursor, found.cursor);
}

export async function talkDoor(wallet, page, ask) {
  return working(async () => {
    const op = String(ask.op || "");
    const {mail, me} = await messenger(wallet);
    const who = mail.hex(me.publicKey);
    let box = talkInbox.get(who);
    if (!box) {
      box = {cursor: 0, seq: 0, log: [], pages: new Map()};
      talkInbox.set(who, box);
    }
    let log = box.pages.get(page);
    if (!log) {
      log = {sent: [], keys: [], floor: box.seq};
      box.pages.set(page, log);
    }
    if (op === "identity") {
      const said = await talkAsk({op: "identity"});
      return {pubkey: who, network: said.network, maxbytes: said.maxbytes,
              contactcode: await mail.contactCode(said.network, me.publicKey)};
    }
    if (op === "sent") return {sent: log.sent.slice(0, 50)};
    if (op === "replies") {
      await talkRead(me, box);
      const after = Math.max(Number(ask.after || 0), log.floor);
      return {replies: box.log.filter(
        (a) => a.seq > after && log.keys.includes(a.sender)).slice(
        0, Math.max(1, Math.min(Number(ask.limit || 100), 200))).map(
        (a) => ({id: a.seq, txid: a.txid, when: a.when, block: a.block,
                 frompubkey: a.sender, json: a.json,
                 body: a.json === null ? "" : JSON.stringify(a.json)}))};
    }
    if (op === "send") {
      const asked = await talkAsk({op: "ask", to: String(ask.to || "")});
      const out = await signOffer(wallet, await talkAsk({
        op: "send", to: String(ask.to || ""),
        sealed: mail.sealForProgram(me, asked.seal_to, asked.stamp,
                                    ask.body === undefined ? {} : ask.body)}));
      log.floor = Math.max(log.floor, box.seq);   // nothing before this is an
      if (!log.keys.includes(asked.to)) log.keys.push(asked.to);   // answer to it
      log.sent.unshift({to: asked.to, txid: out.txid,
                        when: Math.floor(Date.now() / 1000)});
      return {txid: out.txid, fee: out.fee, to: asked.to};
    }
    throw new Error(`a page asked the node door for ${op || "nothing"}`);
  });
}

/* --- what a page remembers, in the browser that shows it -------------------
 *
 * The wallet's `/storage` door keeps a page's things in the node's own
 * database: one shelf per page, which everyone who uses that wallet shares,
 * because on that machine they are the same person. A public node cannot be
 * anybody's memory bank -- its pagestore IS one shelf for every reader --
 * so for an accounts' viewer the decision (D-169) is that a page's memory
 * lives in the browser that shows it, on a shelf keyed to that account and
 * that page alone. The same shape the page already codes against, the same
 * caps pagestore.py stands by, and nothing of it ever reaches the server.
 */
const SHELF_KEYS = 1000;
const SHELF_VALUE = 65536;            // pagestore's MAX_VALUE, as characters
const SHELF_TOTAL = 1048576;          // and its MAX_TOTAL

const SHELF_HISTORY = 10;             // pagestore's HISTORY: earlier values kept per key

// Whose shelf this is. Only a clear answer from /account picks one: a stranger (signed out) gets the
// "anon" shelf, but an /account that did not answer -- a restart, a timeout -- REFUSES the call. It used
// to fall back to "anon", which handed a signed-in player's game an empty shelf: the game took them for a
// new player and saved the new one over the old in its own records (@tbuuol, ASHVALE, 2026-10-20).
async function shelfName(page) {
  let asked;
  try { asked = await fetch("/account", {cache: "no-store"}); }
  catch (e) { asked = null; }
  if (!asked || !asked.ok)
    throw new Error("the wallet did not answer: try again in a moment");
  let said;
  try { said = await asked.json(); }
  catch (e) { throw new Error("the wallet did not answer: try again in a moment"); }
  return "arcade.pagestore." + ((said && said.address) || "anon") + "." + page;
}

// The history sits beside the shelf, not under its prefix: "arcade.pagehistory.<who>.<page>".
function historyName(name) { return "arcade.pagehistory." + name.slice("arcade.pagestore.".length); }

// File what the shelf is about to lose for `key`, newest first, HISTORY per key and SHELF_TOTAL in all.
// The history is a nicety: a browser that will not hold it drops the oldest, never the save itself.
function shelfKeep(name, items, key, next) {
  if (!Object.prototype.hasOwnProperty.call(items, key) || items[key] === next) return;
  const slot = historyName(name);
  let hist = {};
  try { hist = JSON.parse(localStorage.getItem(slot) || "{}") || {}; } catch (e) { hist = {}; }
  const now = Date.now() / 1000;
  hist[key] = [{value: items[key], replaced: now}].concat(hist[key] || []).slice(0, SHELF_HISTORY);
  const all = [];
  Object.keys(hist).forEach((k) => hist[k].forEach((h) => all.push([h.replaced, k, h])));
  all.sort((a, b) => b[0] - a[0]);
  let total = 0;
  all.forEach(([, k, h]) => {
    total += k.length + String(h.value).length;
    if (total > SHELF_TOTAL) hist[k] = hist[k].filter((x) => x !== h);
  });
  Object.keys(hist).forEach((k) => { if (!hist[k].length) delete hist[k]; });
  for (;;) {
    try { localStorage.setItem(slot, JSON.stringify(hist)); return; }
    catch (e) {
      const oldest = all.filter(([, k, h]) => hist[k] && hist[k].includes(h)).pop();
      if (!oldest) { try { localStorage.removeItem(slot); } catch (e2) {} return; }
      hist[oldest[1]] = hist[oldest[1]].filter((x) => x !== oldest[2]);
      if (!hist[oldest[1]].length) delete hist[oldest[1]];
    }
  }
}

export async function storageDoor(page, ask) {
  const op = String(ask.op || "");
  const name = await shelfName(page);
  let items = {};
  try { items = JSON.parse(localStorage.getItem(name) || "{}"); }
  catch (e) { items = {}; }
  if (op === "load") return {items: items};
  if (op === "history") {
    let hist = {};
    try { hist = JSON.parse(localStorage.getItem(historyName(name)) || "{}") || {}; } catch (e) { hist = {}; }
    const only = ask.key === undefined || ask.key === null || ask.key === "" ? null : String(ask.key);
    if (only === null) return {history: hist};
    return {history: hist[only] ? {[only]: hist[only]} : {}};
  }
  const before = Object.assign({}, items);
  if (op === "set") {
    const key = String(ask.key === undefined ? "" : ask.key);
    const value = String(ask.value === undefined ? "" : ask.value);
    if (!key || key.length > 256) throw new Error("a key is 1 to 256 characters");
    if (value.length > SHELF_VALUE)
      throw new Error("a value is at most 65,536 characters");
    const had = Object.prototype.hasOwnProperty.call(items, key);
    const total = Object.keys(items).reduce(
      (n, k) => n + k.length + items[k].length, 0);
    if (!had && Object.keys(items).length >= SHELF_KEYS)
      throw new Error("a page may keep at most 1000 keys");
    if (total - (had ? key.length + items[key].length : 0)
        + key.length + value.length > SHELF_TOTAL)
      throw new Error("a page may keep at most 1,048,576 characters in all");
    items[key] = value;
  } else if (op === "remove") {
    delete items[String(ask.key)];
  } else if (op === "clear") {
    items = {};
  } else {
    throw new Error("op must be set, remove, clear or history");
  }
  Object.keys(before).forEach((k) => { if (before[k] !== items[k]) shelfKeep(name, before, k, items[k]); });
  try {
    if (Object.keys(items).length) localStorage.setItem(name, JSON.stringify(items));
    else localStorage.removeItem(name);
  } catch (e) {
    throw new Error("this browser would not hold that for the page: "
                    + ((e && e.name) || e));
  }
  return {};
}

/* --- the feed, tips and reactions ----------------------------------------
 *
 * Nothing here is encrypted and nothing ever was: every row of the feed
 * was readable by anybody with a node the moment it was mined. What an
 * account changes is only who pays and whose name is on it.
 */

export async function post(wallet, text) {
  return signAndSend(wallet, "/account/post", {text});
}

export async function react(wallet, txid, kind, text) {
  return signAndSend(wallet, "/account/react", {txid, kind, text: text || ""});
}

/** The offer behind a reaction, handed over unsigned — which is what a tip needs.
 *
 * A like is the same size whoever presses it, so `react` can ask for it and
 * sign it in one go. A tip moves somebody's coins, and the transaction that
 * does it says who and how much; `checked` reads that off the bytes and
 * `confirm` is what spends, which is the handshake every send in this file
 * uses. It is deliberately not `signAndSend`: a page that spends coins on the
 * way to being able to say what they cost has already spent them.
 */
export async function offerReact(txid, kind, amount, chain = "") {
  return working(async () => {
    const asked = await fetch("/account/react", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({txid, kind, amount: String(amount || ""), chain: chain || ""}),
    });
    const offer = await asked.json();
    if (!asked.ok) throw new Error(offer.detail || "that cannot be done");
    return offer;
  });
}

/** The shape every one of these has: ask for an offer, sign it, send it. */
export async function signAndSend(wallet, where, body) {
  return working(async () => {
    const asked = await fetch(where, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    const offer = await asked.json();
    if (!asked.ok) throw new Error(offer.detail || "that cannot be done");
    return await signOffer(wallet, offer);
  });
}

/* --- setting an account up, without asking ------------------------------
 *
 * A name that is only in this node's table is not a name, and an account
 * nobody can write to is not reachable. Both are fixed by two
 * transactions, and neither is a decision anybody should have to find a
 * button for -- so signing up does them.
 *
 * It cannot happen AT signup: both spend coins, and the coins come from
 * the faucet in a transaction that has to be in a block first. So this
 * runs afterwards, while the twelve words are on screen being written
 * down, which is the one moment somebody is not waiting for the page.
 *
 * Every step reports, and every step can fail without costing the
 * account: the buttons on /me do the same things, so a node that was busy
 * or a faucet that was empty means "press this later" rather than "start
 * again".
 */

export async function setUp(wallet, identity, tag, {onStep, faucetRefusal, fruit} = {}) {
  const step = (what, how) => { if (onStep) onStep({what, how}); };
  const out = {claimed: "", announced: "", trouble: []};

  step("coins", "waiting");
  const said = await waitForCoins(180);
  if (!((said.balance || 0) + (said.incoming || 0))) {
    // Said plainly, in the faucet's own words if it gave any. The alternative
    // is a page that waits three minutes on coins that were never going to
    // come and then says nothing about why -- which is how a dry faucet gets
    // read as a broken signup, by the one person it was honest with.
    out.trouble.push((faucetRefusal
      || "no coins arrived, so your name is not claimed yet")
      + ". That finishes by itself: once coins are in your wallet, opening "
      + "Your arcade claims the name and publishes your key");
    step("coins", "none");
    return out;
  }
  step("coins", "here");

  if (fruit) {
    // An operator's fruit name (2026-10-20): the arcade holding it sends it here; there is nothing to claim.
    step("name", "coming from the fruit stand");
  } else {
    step("name", "claiming");
    try {
      out.claimed = (await claim(wallet, tag)).txid;
      step("name", "on its way");
    } catch (e) {
      out.trouble.push(`the name could not be claimed: ${e.message || e}`);
      step("name", "failed");
    }
  }

  // The key, whether or not the name went: they are separate things and
  // somebody unreachable is worse off than somebody unnamed.
  // Tried twice: the first failure is usually the name's transaction still
  // settling on the node, and a signup that leaves an account unreachable
  // is worse than one that takes ten seconds longer (9 of 43 names had no key,
  // 2026-09-26). Every page tries again later too (base.html).
  step("key", "publishing");
  const mail = await import("/messaging.js");
  for (let attempt = 1; attempt <= 2 && !out.announced; attempt++) {
    try {
      out.announced = (await mail.announce(wallet, identity, tag)).txid;
      step("key", "on its way");
    } catch (e) {
      if (attempt === 1) { await new Promise((ok) => setTimeout(ok, 10000)); continue; }
      out.trouble.push(`your key was not published yet (it is tried again by `
        + `itself): ${e.message || e}`);
      step("key", "failed");
    }
  }

  // "On its way" is where this stopped for a year, and it is not what the
  // person wants to know. A broadcast a node relayed is not a name other
  // people can find: the claim has to be in a block and read by the index,
  // and the same for the key. So wait for the node to say it has seen them,
  // which is the only answer that means "confirmed" anywhere on this site --
  // `state()` answers from the same index every other page is drawn from,
  // not from a memory of relaying something. Quietly: nothing here is
  // blocked on it, and a tab that is closed early costs nothing, because
  // both transactions are already out.
  if (out.claimed || out.announced || fruit) {
    const seen = await settle(!!out.claimed || !!fruit, !!out.announced, tag, step);
    if ((out.claimed || fruit) && !seen.name) {
      out.trouble.push("your name is broadcast but this node has not indexed "
        + "it yet, so people cannot find you by it this minute");
    }
    if (out.announced && !seen.key) {
      out.trouble.push("your key is broadcast but this node has not indexed "
        + "it yet, so nobody can write to you this minute");
    }
  }
  return out;
}

/** Until the node's own index reports the claim and the key.
 *
 * Five minutes of asking every few seconds, because a block on this chain
 * takes about a minute and the index reads one behind. Coming back to the
 * page later shows the truth either way -- this only decides what the
 * signup page is allowed to say before it is looked at again.
 */
async function settle(wantName, wantKey, tag, step, seconds = 300) {
  const until = Date.now() + seconds * 1000;
  let name = false, key = false;
  for (;;) {
    let said = null;
    try { said = await state(); } catch (e) { said = null; }
    if (said) {
      name = String(said.tag || "").toLowerCase() === String(tag).toLowerCase();
      key = !!said.announced;
    }
    if (wantName) step("name", name ? "confirmed" : "confirming");
    if (wantKey) step("key", key ? "confirmed" : "confirming");
    if ((!wantName || name) && (!wantKey || key)) return {name, key};
    if (Date.now() > until) return {name, key};
    await new Promise((r) => setTimeout(r, 7000));
  }
}

/* --- keeping a wallet open while somebody moves around ------------------
 *
 * These are server-rendered pages: navigating replaces the document, and
 * a key held in a variable goes with it. That is why posting worked on
 * /me and nowhere else -- the feed had no key and could not sign.
 *
 * So an unlocked wallet is kept in `sessionStorage`, which is per TAB and
 * is gone when the tab closes.
 *
 * **What that costs, said plainly.** The words are then readable by any
 * script running on this origin. The protection is that there is no
 * third-party script here at all -- the two vendored libraries are served
 * from this node and pinned by hash (vendor/PROVENANCE.md), everything
 * else is this application's own, and no post, message or inscription is
 * ever rendered into the page as markup.
 *
 * `localStorage` was the alternative and is worse: it would outlive the
 * tab, the browser and the person walking away from the machine.
 */

const OPEN_WALLET = "arcade-open-wallet";
/* Every wallet unlocked in this tab, for switching between accounts signed in
 * on the node's own machine without typing a password again (2026-09-30).
 * The same place and the same lifetime as OPEN_WALLET: this tab, until it
 * closes. */
const OPEN_WALLETS = "arcade-open-wallets";

function openWallets() {
  try { return JSON.parse(sessionStorage.getItem(OPEN_WALLETS) || "[]"); } catch (e) { return []; }
}

function keepOpen(phrase) {
  if (!phrase) return;
  const kept = openWallets().filter((p) => p !== phrase);
  kept.unshift(phrase);
  try { sessionStorage.setItem(OPEN_WALLETS, JSON.stringify(kept.slice(0, 6))); } catch (e) {}
}

/* The wallets an account imported (plan item 18), once its sealed record is open: where the open words are, for as
 * long (this tab, shared with the site's other tabs below, gone on lock and sign-out), by account, because one tab
 * can hold several accounts. {<account pubkey>: {updated, wallets: [...]}} -- `stale` when the record was sealed under
 * an earlier password and is not open. */
const OPEN_IMPORTS = "arcade-open-imports";
/** The key the record is sealed with, while this page is open and only after the password was typed here and proved
 *  against the main wallet: {account, key (a non-extractable AES key), salt, iterations}. Never the password, never
 *  in storage; a new page asks again. */
let importKey = null;

function openImportsAll() {
  try { return JSON.parse(sessionStorage.getItem(OPEN_IMPORTS) || "{}") || {}; } catch (e) { return {}; }
}

function mergeImports(given) {
  const all = openImportsAll();
  for (const [who, got] of Object.entries(given || {})) {
    const have = all[who];
    if (!have || Number(got.updated || 0) >= Number(have.updated || 0)) all[who] = got;
  }
  try { sessionStorage.setItem(OPEN_IMPORTS, JSON.stringify(all)); } catch (e) {}
}

function keepImports(account, opened) {
  const all = openImportsAll();
  all[String(account).toLowerCase()] = opened;
  try { sessionStorage.setItem(OPEN_IMPORTS, JSON.stringify(all)); } catch (e) {}
  try { if (TABS) TABS.postMessage({imports: {[String(account).toLowerCase()]: opened}}); } catch (e) {}
}

/* Unlocked once, unlocked everywhere (2026-09-26: "If you unlock on one
 * page, it should stay unlocked on all pages"). The open wallet lives in THIS
 * tab's sessionStorage, which a navigation keeps and a new tab does not have. So
 * the tabs of this site tell each other, over a BroadcastChannel (same origin
 * only: an inscribed page is on the pages host and cannot hear it): a tab that
 * opens locked asks, and any unlocked tab answers. Nothing is written to disk --
 * close the last tab and it is locked -- and locking one locks them all. */
// One name per page, shared by every copy of this module the page loaded.
const THIS_PAGE = (typeof window !== "undefined")
  ? (window.__arcadePage = window.__arcadePage || Math.random().toString(36).slice(2))
  : "worker";
const TABS = (typeof BroadcastChannel !== "undefined")
  ? new BroadcastChannel("arcade-open-wallet") : null;
if (TABS) {
  TABS.onmessage = (e) => {
    const m = e.data || {};
    if (m.ask === "phrase") {
      let phrase = null;
      try { phrase = sessionStorage.getItem(OPEN_WALLET); } catch (x) {}
      if (phrase) TABS.postMessage({phrase});
      // The imported wallets open in this tab go with it (plan item 18): the same place, the same lifetime.
      const imports = openImportsAll();
      if (phrase && Object.keys(imports).length) TABS.postMessage({imports});
    } else if (m.phrase) {
      try {
        if (!sessionStorage.getItem(OPEN_WALLET)) sessionStorage.setItem(OPEN_WALLET, m.phrase);
      } catch (x) {}
    } else if (m.imports) {
      mergeImports(m.imports);
    } else if (m.locked) {
      try { sessionStorage.removeItem(OPEN_WALLET); } catch (x) {}
      try { sessionStorage.removeItem(OPEN_IMPORTS); } catch (x) {}
      importKey = null;
    } else if (m.switched && m.from !== THIS_PAGE) {
      // Another tab switched accounts: this one is showing the old one. Not
      // this page, which may hold more than one copy of this module.
      try { sessionStorage.removeItem(OPEN_WALLET); } catch (x) {}
      location.reload();
    }
  };
}

let _noTabUntil = 0;   // nobody answered just now: opened() does not wait again for a while
function fromAnotherTab(ms = 400) {
  if (!TABS) return Promise.resolve(null);
  return new Promise((resolve) => {
    const other = new BroadcastChannel("arcade-open-wallet");
    const done = (v) => { clearTimeout(t); other.close(); resolve(v); };
    const t = setTimeout(() => done(null), ms);
    other.onmessage = (e) => { if (e.data && e.data.phrase) done(e.data.phrase); };
    other.postMessage({ask: "phrase"});
  });
}

export function remember(phrase) {
  try { sessionStorage.setItem(OPEN_WALLET, phrase); } catch (e) {}
  keepOpen(phrase);
  // Tabs already open and locked open now too.
  try { if (TABS) TABS.postMessage({phrase}); } catch (e) {}
}

/** Sign out of this node on this browser (2026-09-28): save what is not
 *  yet saved (the sealed mailbox), then forget this browser's copy, the unlocked
 *  wallet in every tab, and the session. What the account owns stays on the
 *  chain and in its sealed copy on the node; signing in again brings it back. */
export async function signOut(chain) {
  try {
    const held = chain ? await opened(chain) : null;
    if (held) {
      const {mail, me} = await messenger(held);
      await Promise.race([mail.syncMailbox(me), new Promise((ok) => setTimeout(ok, 6000))]);
    }
  } catch (e) { /* nothing unsaved is worth staying signed in for */ }
  try { (await import("/messaging.js")).wipeLocal && await (await import("/messaging.js")).wipeLocal(); } catch (e) {}
  forgetOpen();
  await fetch("/auth/logout", {method: "POST"});
}

export function forgetOpen() {
  let phrase = null;
  try { phrase = sessionStorage.getItem(OPEN_WALLET); } catch (e) {}
  try { sessionStorage.removeItem(OPEN_WALLET); } catch (e) {}
  // Locked means the imported wallets too, in every tab (the `locked` message below clears theirs).
  try { sessionStorage.removeItem(OPEN_IMPORTS); } catch (e) {}
  importKey = null;
  if (phrase) {
    try { sessionStorage.setItem(OPEN_WALLETS,
            JSON.stringify(openWallets().filter((p) => p !== phrase))); } catch (e) {}
  }
  try { if (TABS) TABS.postMessage({locked: true}); } catch (e) {}
}

/** Switch this browser to another account signed in on it. Its wallet opens
 *  at once if it was unlocked in this tab before; otherwise it opens locked,
 *  and asks for its password the first time it has to sign. */
export async function switchAccount(pubkey, chain) {
  const answer = await fetch("/auth/switch", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({pubkey}),
  });
  const said = await answer.json();
  if (!answer.ok) throw new Error(said.detail || "that account could not be switched to");
  let current = null;
  try { current = sessionStorage.getItem(OPEN_WALLET); } catch (e) {}
  if (current) keepOpen(current);
  let found = null;
  for (const phrase of openWallets()) {
    try {
      const w = await walletFrom(phrase, chain ? chain.network : undefined,
                                 chain ? chain.version : undefined);
      if (w.pubkey === String(pubkey).toLowerCase()) { found = phrase; break; }
    } catch (e) { /* a phrase that no longer opens is skipped */ }
  }
  try {
    if (found) sessionStorage.setItem(OPEN_WALLET, found);
    else sessionStorage.removeItem(OPEN_WALLET);
  } catch (e) {}
  // Other tabs were showing the account that is no longer current.
  try { if (TABS) TABS.postMessage({switched: true, from: THIS_PAGE}); } catch (e) {}
  return said;
}

/** The wallet unlocked in this tab, or null. */
export async function opened(chain) {
  let phrase = null;
  try { phrase = sessionStorage.getItem(OPEN_WALLET); } catch (e) {}
  if (!phrase) {
    // a second, longer ask before giving up (2026-10-08, asked to sign in two or
    // three times): a phone wakes a backgrounded tab slower than 400 ms, and the page
    // then asked for a password another tab already had open
    if (Date.now() >= _noTabUntil) {
      phrase = (await fromAnotherTab()) || (TABS ? await fromAnotherTab(1500) : null);
      if (!phrase) _noTabUntil = Date.now() + 30000;   // a tab that unlocks later sends it anyway
    }
    if (phrase) { try { sessionStorage.setItem(OPEN_WALLET, phrase); } catch (e) {} }
  }
  if (!phrase) return null;
  try {
    const wallet = await walletFrom(phrase, chain.network, chain.version);
    // Every chain, not only the one the page named. A page that shows a
    // mainnet balance and then cannot sign for it is worse than one that
    // never offered; deriving a second key costs one PBKDF2-free
    // derivation from a seed that is already here.
    return await everyChain(wallet, await chains());
  } catch (e) {
    forgetOpen();
    return null;
  }
}

/** Unlock with a password. The same thing as signing in, because it is.
 *
 * A session cookie lasts thirty days and a tab does not, so somebody who
 * comes back tomorrow is signed in with no wallet open. That is the only
 * time a password is asked for twice, and the page says which of the two
 * situations it is in rather than looking like a second login.
 */
export async function unlockHere(tag, password, chain) {
  return signIn(tag, password, chain);
}

/* --- tipping a post: coins or a token, on either chain ---------------------
 *
 * 2026-10-13: "When a user selects tip on the feed, it should give them
 * the options of testnet PEP mainnet PEP and testnet or mainnet token", and the
 * same on the Games tab. One flow for both pages, so the two cannot drift.
 *
 * The form carries `pay` ("coin:<network>" or "token:<network>", from
 * templates/_tip.html), `token` and `amount`. A coin tip is one transaction on
 * the chain picked: the coins and the note naming the post. A token tip is the
 * token send on its own chain (to the address the author published there, which
 * the node works out from `tip_of`), then a TIP_TOKEN note on the tag chain
 * naming that send. A mainnet one still goes through the mainnet review in
 * `confirm`: real coins are asked about twice, on purpose.
 */
let tipHeld = null;

/** Keep a tip form's three steps in step: the coin named for the network picked, and
 *  the token list (this account's own holdings there) when "a token" is picked.
 *  Call once per page. */
export function tipPicker(root = document, tokensFrom = "/account/tokens") {
  root.addEventListener("change", async (e) => {
    const box = e.target;
    if (!box.form || !(box.hasAttribute("data-tip-net") || box.hasAttribute("data-tip-what"))) return;
    const form = box.form, net = form.elements.net, what = form.elements.what;
    const list = form.elements.token, pick = net.selectedOptions[0];
    // The coin is said as the network's own: PEP TEST, PEP or DOGE.
    if (what.options[0]) what.options[0].textContent = (pick && pick.dataset.coin) || "coins";
    list.hidden = what.value !== "token";
    if (what.value !== "token") return;
    list.textContent = "";
    try {
      if (!tipHeld) tipHeld = ((await (await fetch(tokensFrom)).json()).chains) || [];
      const here = (tipHeld.find((c) => c.network === net.value) || {}).tokens || [];
      for (const t of here.filter((t) => Number(t.balance) > 0)) {
        const o = document.createElement("option");
        o.value = t.property_id; o.dataset.name = t.name;
        o.textContent = `${t.name} (${t.display})`;
        list.appendChild(o);
      }
    } catch (err) { /* the send says why if it is tried */ }
    if (!list.options.length) {
      const o = document.createElement("option");
      o.value = "";
      o.textContent = `you hold no tokens on ${(pick && pick.dataset.label) || net.value}`;
      list.appendChild(o);
    }
  });
}

/** Tip the post `txid` with what the form says. Null if the person said no. */
export async function tip(held, form, txid, kinds, ask) {
  const choice = (form.elements.net || {}).selectedOptions?.[0];
  const network = choice ? choice.value : "";
  const what = (form.elements.what || {}).value || "coin";
  const amount = ((form.elements.amount || {}).value || "").trim();
  const who = form.dataset.authorName || "whoever made this";
  if (what === "token") {
    const pick = form.elements.token.selectedOptions[0];
    if (!pick || !pick.value) throw new Error("pick a token you hold");
    const offer = await working(async () => {
      const asked = await fetch("/account/token/send", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({property_id: Number(pick.value), amount, tip_of: txid,
                              chain: network})});
      const said = await asked.json();
      if (!asked.ok) throw new Error(said.detail || "that token cannot be sent");
      return said;
    });
    const shown = await checked(offer, held);
    const ok = await ask(`Tip ${amount} ${pick.dataset.name}?`,
      `${amount} ${pick.dataset.name} (${(choice && choice.dataset.label) || network}) go to ${who}, and then a small note `
      + "on the post says it was a tip: two small transactions, network fee "
      + `${shown.coinsOf(shown.fee)} for the send. It is a payment: it cannot be taken back.`,
      "Send the tip");
    if (!ok) return null;
    const sent = await confirm(held, offer);
    return await react(held, txid, kinds.tiptoken, sent.txid);
  }
  const offer = await offerReact(txid, kinds.tip, amount, network);
  const shown = await checked(offer, held);
  const coin = (choice && choice.dataset.coin) || "coins";
  const ok = await ask("Send this tip?",
    `Tip ${amount} ${coin} to ${who}. The network fee is `
    + `${shown.coinsOf(shown.fee)}. It is a payment: it cannot be taken back.`, "Send the tip");
  if (!ok) return null;
  return await confirm(held, offer);
}

/* --- a tab's own lock-script trades (trustless-trading-plan.md, phase 2) -------------
 *
 * 2026-10-13: trades "only available if they leave their tab open so that
 * transaction building is complete on the client side solely ... 100% trustless with
 * lock scripts". The lock is built in this tab (coins.htlcRedeem) and funded with an
 * ordinary send to its address; spending it -- a claim with the secret, a refund after
 * the time -- is asked of the node unsigned (/account/trade/spend), read here, signed
 * here over a digest worked out here from the lock's own script, and only then handed
 * back to be put in place and broadcast (/account/trade/spend/sign).
 */

/** Claim (`kind` "claim", with `secret` hex) or refund one lock coin to this account. */
/** A lock spend built by the node and checked and signed HERE: [node's answer, signature]. */
async function signLockSpend(wallet, {chain, redeem, txid, vout, kind, secret, leg, other = null, presign = false}) {
    // `other`: the swap's other leg, which a token's operator fee is worked out from (operatorFee).
    const what = legWhat(leg, other);
    const asked = await fetch("/account/trade/spend", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({chain, redeem, txid, vout, kind, leg: what}),
    });
    const said = await asked.json();
    if (!asked.ok) throw new Error(said.detail || "that lock cannot be spent now");
    const keys = keysOn(wallet, said.chain || chain);
    const lock = coins.htlcParse(unhex(redeem));
    if (!lock) throw new Error("that is not a lock script. Nothing was signed.");
    const me = coinsHex(keys.pubkey);
    if (kind === "claim" && coinsHex(lock.claimer) !== me) throw new Error("that lock pays somebody else. Nothing was signed.");
    if (kind === "refund" && coinsHex(lock.refunder) !== me) throw new Error("that lock refunds somebody else. Nothing was signed.");
    // The secret is not signed (it goes in the input script); a held claim may not know it yet.
    if (kind === "claim" && !(presign && !secret)) {
      const hashed = coinsHex(await coins.sha256Of(unhex(secret)));
      if (hashed !== coinsHex(lock.secretHash)) throw new Error("that secret does not open this lock. Nothing was signed.");
    }
    const tx = coins.parseTx(said.raw);
    // A Doginal comes out with two inputs, both under this lock: the Doginal (output 0 of the lock's
    // transaction) first and whole, then its reserve (output 1), spent whole as the fee (htlc.doginal_out).
    // A DRC-20 / PRC-20 lock is the same shape (token20_swap): its note at output 0, the reserve at 1.
    const dg = what.kind === "doginal" || what.kind === "token20";
    const wanted = dg ? [[txid, 0], [txid, 1]] : [[txid, Number(vout)]];
    if (tx.inputs.length !== wanted.length || tx.inputs.some((x, i) => x.txid !== wanted[i][0] || x.vout !== wanted[i][1]))
      throw new Error("the node asked to spend a different coin. Nothing was signed.");
    const mine = coins.p2pkh(await coins.hash160(keys.pubkey));
    const same = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
    // A coin comes out to this account alone. A token or NFT comes out by one OP_RETURN,
    // what is left of the lock's coin back into the lock, and this account LAST -- the
    // ledger's recipient (escrow.move_out). A Doginal comes out first and whole to this account.
    // A CLAIM also pays the other side's operator exactly the fee its lock holds, as one output of
    // its own (2026-10-21) -- worked out here from the terms, never taken from the node.
    const lockScript = await coins.htlcScript(unhex(redeem));
    const op = kind === "claim" && what.fee ? {value: BigInt(what.fee), script: await payeeScript(what.fee_to)} : null;
    if (op && !op.script) throw new Error("the operator fee's address cannot be paid. Nothing was signed.");
    const isOp = (o) => op && same(o.script, op.script) && !same(o.script, mine);
    const fine = (o) => same(o.script, mine) || (isOp(o) && o.value === op.value)
      || (what.kind !== "coin" && !dg && (o.script[0] === 0x6a || same(o.script, lockScript)));
    const ours = tx.outputs.length ? tx.outputs[dg ? 0 : tx.outputs.length - 1] : null;
    if (!tx.outputs.length || !tx.outputs.every(fine) || !same(ours.script, mine)
        || tx.outputs.filter(isOp).length > 1
        || (dg && tx.outputs.length > 2)
        || (what.kind !== "coin" && !dg && tx.outputs.filter((o) => o.script[0] === 0x6a).length !== 1))
      throw new Error("that spend pays somebody else. Nothing was signed.");
    if (op && !same(op.script, mine) && tx.outputs.filter(isOp).length !== 1)
      throw new Error("that claim leaves out the operator fee the swap agreed. Nothing was signed.");
    // The sender paid the network fee (2026-10-21): a coin claimed on the new terms comes out whole.
    if (kind === "claim" && what.kind === "coin" && leg && leg.fee_bps != null) {
      const got = tx.outputs.filter((o) => same(o.script, mine)).reduce((n, o) => n + o.value, 0n);
      if (got < BigInt(leg.amount)) throw new Error("that claim pays you less than the amount agreed. Nothing was signed.");
    }
    if (kind === "refund" && tx.locktime !== lock.locktime)
      throw new Error("a refund must carry the lock's own time. Nothing was signed.");
    // One signature per input, each over a digest worked out HERE from the lock's own script.
    const asked2 = said.sighashes || [said.sighash];
    if (asked2.length !== tx.inputs.length)
      throw new Error("the node asked for a different number of signatures. Nothing was signed.");
    const signatures = [];
    for (let i = 0; i < tx.inputs.length; i++) {
      const digest = coinsHex(await coins.sighashAll(tx, i, unhex(redeem)));
      if (digest !== String(asked2[i]).toLowerCase())
        throw new Error("the node asked for a signature over different bytes. Nothing was signed.");
      signatures.push(coinsHex(await coins.signInput(keys.key, unhex(digest))));
    }
    return [said, signatures];
}

export async function tradeSpend(wallet, {chain, redeem, txid, vout, kind, secret, leg, other = null}) {
  return working(async () => {
    const [said, signatures] = await signLockSpend(wallet, {chain, redeem, txid, vout, kind, secret, leg, other});
    const back = await fetch("/account/trade/spend/sign", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({chain: said.chain, redeem, raw: said.raw, signature: signatures[0], signatures, kind,
                            secret: kind === "claim" ? secret : "", leg: legWhat(leg, other)}),
    });
    const done = await back.json();
    if (!back.ok) throw new Error(done.detail || "the node would not send it");
    return done;                                  // {ok, txid}
  });
}

/** Hand this node a pre-signed claim or refund (2026-10-13): it finishes the swap if
 *  this tab is closed. Same checks as tradeSpend; the node adds only the secret, and only
 *  sends at the right moment (arcade/trade_watch.py). Claims need no secret here when
 *  `secret_lock` says where it will show up (this account's own lock, once claimed). */
export async function tradePresign(wallet, {chain, redeem, txid, vout, kind, secret, leg, other = null, secret_lock,
                                            min_conf, not_after, trade}) {
  const [said, signatures] = await signLockSpend(wallet, {chain, redeem, txid, vout, kind,
                                                           secret: secret || "", leg, other, presign: true});
  const asked = await fetch("/account/trade/watch", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({chain: said.chain, redeem, txid, vout, kind, raw: said.raw, signature: signatures[0], signatures,
                          leg: legWhat(leg, other),
                          secret: secret || "", secret_lock: secret_lock || null,
                          min_conf: min_conf || 0, not_after: not_after || 0, trade: trade || ""}),
  });
  const done = await asked.json();
  if (!asked.ok) throw new Error(done.detail || "the node would not hold it");
  return done;
}

/** A lock as its chain shows it (null if not there yet). */
export async function tradeFacts(chain, txid, vout, leg = null, redeem = null) {
  if (!txid) return null;
  const asked = await fetch("/account/trade/facts", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({chain, txid, vout, leg: legWhat(leg), redeem: redeem ? coinsHex(redeem) : ""}),
  });
  const said = await asked.json();
  if (!asked.ok) throw new Error(said.detail || "the node could not read that lock");
  return said.seen;
}

/** The pay-to-pubkey-hash script an operator fee is paid to, from its address; null if it is not one (payeeOk). */
async function payeeScript(address) {
  if (!(await payeeOk(address))) return null;
  return coins.p2pkh(coins.unbase58(address).slice(1, 21));
}

/** What moves in a leg, as the node's trade routes take it: {kind, asset, amount}, and its operator fee worked out
 *  HERE from the terms ({fee, fee_to}, 2026-10-21): what goes into this account's own lock on top, or what its claim
 *  of the other lock pays the other side's operator. */
function legWhat(leg, other = null) {
  if (!leg) return {kind: "coin", asset: "", amount: 0};
  const fee = operatorFee(leg, other);
  return {kind: leg.kind || "coin", asset: leg.asset || "", amount: leg.amount,
          ...(fee ? {fee, fee_to: leg.fee_to} : {})};
}

/* The trade state machine: arcade/trade.py's next_step, rule for rule (tests compare
 * them). Pure: what the chains show in, the one thing to do next out. */
export const TRADE = {
  LOCK_SECONDS: {mainnet: 2 * 3600, testnet: 2 * 3600},
  CONFIRMATIONS: 0, CONFIRMATIONS_BIG: 1,       // 0 below the big line, 1 at or above it (2026-10-17; trade.py)
  BIG: {main: 1_000_000 * 100_000_000, doge: 1_000 * 100_000_000, ltc: 2 * 100_000_000,
       bel: 200 * 100_000_000, lky: 20 * 100_000_000},
  CLAIM_MARGIN: 20 * 60, LOCK_MARGIN: 30 * 60,
  LOCK_RESERVE: 2_000_000,          // htlc.SPEND_FEE + htlc.DUST (trade.LOCK_RESERVE)
  DOGINAL_RESERVE: 2_000_000,       // htlc.DOGINAL_RESERVE
  // Who pays a lock's fees (2026-10-21; trade.py "who pays the fees of a lock"): the claim's network fee per
  // chain (trade.claim_fee = htlc.spend_fee of config.NETWORKS), the least an operator fee output may be (trade.fee_floor
  // = htlc.dust_of), and the version bytes it may be paid to (trade.PAYEE_VERSIONS). tests/test_trade_fees.py holds
  // these to the Python numbers.
  CLAIM_FEE: {main: 1_000_000, test: 1_000_000, regtest: 1_000_000, doge: 1_000_000, ltc: 10_000, ltctest: 10_000,
              bel: 10_000, lky: 1_000_000},
  FEE_FLOOR: {main: 1_000_000, test: 1_000_000, regtest: 1_000_000, doge: 1_000_000, ltc: 10_000, ltctest: 10_000,
              bel: 10_000, lky: 1_000_000},
  FEE_BPS_CEILING: 10_000,
  PAYEE_VERSIONS: [25, 30, 47, 48, 56, 111, 113],
};
//: The real-coin chains (config.py `real_coins`): their legs are mainnet tier (trade.Leg.tier).
//: Each chain's coin as a leg says it (config.py `coin`).
const COIN_WORD = {doge: "DOGE", main: "PEP", ltc: "LTC", ltctest: "LTC TEST", bel: "BEL", lky: "LKY"};
const REAL_COINS = new Set(["main", "doge", "ltc", "bel", "lky"]);
const tierOf = (leg) => REAL_COINS.has(leg.network) ? "mainnet" : "testnet";

/** 0, or 1 when a mainnet coin leg is at or above its line (trade.confirmations_for). */
export function confirmationsFor(give, get, big = TRADE.BIG) {
  // A DRC-20 / PRC-20 lock counts only once a copy of the ord index has read its note (token20_swap): never 0-conf.
  if (give.kind === "token20" || get.kind === "token20") return Math.max(TRADE.CONFIRMATIONS_BIG, 1);
  for (const leg of [give, get]) {
    const line = big[leg.network];
    if (leg.kind === "coin" && tierOf(leg) === "mainnet" && line && leg.amount >= Number(line)) return TRADE.CONFIRMATIONS_BIG;
  }
  return TRADE.CONFIRMATIONS;
}

/** {initiatorLocktime, responderLocktime} for a trade starting now (trade.plan_locktimes). */
export function planLocktimes(give, get, now) {
  const tier = (tierOf(give) === "mainnet" || tierOf(get) === "mainnet") ? "mainnet" : "testnet";
  return {t1: Math.floor(now) + TRADE.LOCK_SECONDS[tier],
          t2: Math.floor(now) + Math.floor(TRADE.LOCK_SECONDS[tier] / 2)};
}

/** Is `seen` exactly the lock the terms say? "" if so, else why not (trade.check_lock). "Holds at least": a lock
 *  that holds more passes, which is how an arcade from before 2026-10-21 still accepts an updated one's lock. */
export async function checkLock(trade, seen, theirs) {
  const leg = theirs ? trade.get : trade.give;
  const other = theirs ? trade.give : trade.get;
  const redeem = theirs ? trade.theirRedeem : trade.myRedeem;
  const script = coinsHex(await coins.htlcScript(redeem));
  if (seen.script_hex !== script) return "that lock's script is not the one agreed (hash, keys or lock time differ)";
  if ((leg.kind === "doginal" || leg.kind === "token20") && (seen.vout !== 0 || !seen.asset_ok))
    return "that lock does not hold the agreed Doginal at the start of output 0, with its reserve beside it";
  if ((leg.kind === "doginal" || leg.kind === "token20") && operatorFee(leg, other)
      && Number(seen.reserve || 0) < doginalReserve(leg, other))
    return "the reserve beside that Doginal is too small to pay its spend and the operator fee agreed";
  if (seen.value < lockValue(leg, other)) {
    if (leg.kind === "coin") {
      if (leg.fee_bps == null) return `that lock holds ${seen.value}, less than the ${leg.amount} agreed`;
      return `that lock holds ${seen.value}, less than the ${leg.amount} agreed with its network fee `
        + `(${claimFee(leg.network)}) and operator fee (${operatorFee(leg, other)}) on top`;
    }
    return "that lock holds too little coin to move its token or NFT out again";
  }
  if (leg.kind !== "coin" && !seen.asset_ok) return "the ledger does not show the agreed token or NFT at that lock";
  return "";
}

/** What the claim (or refund) of a coin lock on `network` pays the network (trade.claim_fee). */
export function claimFee(network) {
  return TRADE.CLAIM_FEE[network] ?? 1_000_000;
}

/** The least an operator fee output may be on `network` (trade.fee_floor): its dust line. */
export function feeFloor(network) {
  return TRADE.FEE_FLOOR[network] ?? 1_000_000;
}

/** The operator fee `leg`'s lock holds and its claim pays to `leg.fee_to` (trade.operator_fee, 2026-10-21:
 *  "percent with a floor"): 0 with no rate or no payee (terms from before that day too); else rate x base // 10,000,
 *  rounded down -- in BigInt, so a big amount lands on the same satoshi as Python's -- at least the chain's floor. The
 *  base is the leg's own amount for coin; for a token, NFT or Doginal, `other`'s when that is coin on the same chain,
 *  else nothing (the floor alone). */
export function operatorFee(leg, other = null) {
  const bps = Math.min(Math.trunc(Number(leg.fee_bps || 0)), TRADE.FEE_BPS_CEILING);
  if (!(bps > 0) || !leg.fee_to) return 0;
  const kind = leg.kind || "coin";
  let base = 0;
  if (kind === "coin") base = Number(leg.amount);
  else if (other && (other.kind || "coin") === "coin" && other.network === leg.network) base = Number(other.amount);
  const pct = Number((BigInt(Math.trunc(base)) * BigInt(bps)) / 10000n);
  return Math.max(feeFloor(leg.network), pct);
}

/** The coin locked beside a Doginal (trade.doginal_reserve): the spend's own reserve and the operator fee. */
export function doginalReserve(leg, other = null) {
  return TRADE.DOGINAL_RESERVE + operatorFee(leg, other);
}

/** The least coin a lock output for `leg` must hold (trade.lock_value): the amount, the claim's network fee and the
 *  operator fee for coin; the reserve and the operator fee for a token or NFT. A leg on old terms (no fee_bps, an
 *  arcade from before 2026-10-21) is held to the old rule. A Doginal's lock output holds its own coin, whatever that
 *  is worth; its reserve stands beside it (checkLock). */
export function lockValue(leg, other = null) {
  const kind = leg.kind || "coin";
  if (kind === "doginal" || kind === "token20") return 1;
  if (kind !== "coin") return TRADE.LOCK_RESERVE + operatorFee(leg, other);
  if (leg.fee_bps == null) return Number(leg.amount);
  return Number(leg.amount) + claimFee(leg.network) + operatorFee(leg, other);
}

/** What this trader puts into its OWN lock for `leg` (trade.fund_value): lockValue as an updated arcade reads it,
 *  whatever terms the other side sent -- the claim's network fee is always the sender's. A Doginal: its reserve. */
export function fundValue(leg, other = null) {
  if ((leg.kind || "coin") === "doginal" || leg.kind === "token20") return doginalReserve(leg, other);
  return lockValue(leg.fee_bps == null ? {...leg, fee_bps: 0, fee_to: ""} : leg, other);
}

/** `leg` with this arcade's lock-fee terms ({bps, to} from /account chains[].lock_fee; {} = Off: rate 0, still the
 *  new terms) (trade.with_fee). */
export function withFee(leg, terms) {
  const bps = Math.trunc(Number((terms && terms.bps) || 0));
  const to = bps > 0 ? String((terms && terms.to) || "") : "";
  const out = {...leg, fee_bps: to ? bps : 0};
  if (to) out.fee_to = to; else delete out.fee_to;
  return out;
}

/** Is `address` somewhere an operator fee can be paid (trade.payee_ok): base58check, a 20-byte hash, a
 *  pay-to-pubkey-hash version this software knows? */
export async function payeeOk(address) {
  if (typeof address !== "string" || address.length < 26 || address.length > 36) return false;
  try {
    const raw = coins.unbase58(address);
    if (raw.length !== 25) return false;
    const sum = (await coins.hash256(raw.slice(0, 21))).slice(0, 4);
    if (!sum.every((b, i) => b === raw[21 + i])) return false;
    return TRADE.PAYEE_VERSIONS.includes(raw[0]);
  } catch (e) { return false; }
}

/** A leg's lock-fee terms are well formed (trade_messages.fee_terms): none, or a whole rate 0..10,000 with a payable
 *  address when above 0. A proposal that fails this is refused before anything is locked: its claim could not be
 *  built. */
async function feeTermsOk(leg) {
  if (!leg || leg.fee_bps == null) return true;
  if (!Number.isInteger(leg.fee_bps) || leg.fee_bps < 0 || leg.fee_bps > TRADE.FEE_BPS_CEILING) return false;
  return leg.fee_bps === 0 || await payeeOk(leg.fee_to);
}

/** May this lister lock `proposed` (its own side as the taker wrote it) under the terms it listed (`stood`)? Yes with
 *  no operator fee asked of it (an old arcade, a book that dropped the fields: rate 0), or exactly the rate and payee
 *  listed; never a payee or rate the taker chose (node_trader.fee_terms_fit). */
function feeTermsFit(proposed, stood) {
  if (!proposed.fee_bps) return true;
  return proposed.fee_bps === Number((stood && stood.fee_bps) || 0) && proposed.fee_to === String((stood && stood.fee_to) || "");
}

//: This arcade's lock-fee terms per chain (/account chains[].lock_fee), read once per page.
let lockFeesKnown = null;
export async function lockFeeOf(network) {
  if (!lockFeesKnown) {
    try {
      const said = await (await fetch("/account", {cache: "no-store"})).json();
      lockFeesKnown = Object.fromEntries((said.chains || []).map((c) => [c.network, c.lock_fee || {}]));
    } catch (e) { return {}; }
  }
  return lockFeesKnown[network] || {};
}

/** [step, why]: "lock" | "wait" | "claim" | "refund" | "done" | "abort" (trade.next_step).
 *  `trade` = {role, give, get, secret?, secretHash, t1, t2, myRedeem, theirRedeem}. */
export async function nextTradeStep(trade, facts, now) {
  const need = trade.need || confirmationsFor(trade.give, trade.get);
  const mine = facts.mine, theirs = facts.theirs;
  const myTime = trade.role === "initiator" ? trade.t1 : trade.t2;
  const theirTime = trade.role === "initiator" ? trade.t2 : trade.t1;
  if (theirs && theirs.spent_by_me) return ["done", "I claimed their lock"];
  if (mine && mine.spent_by_me) return ["done", "my lock came back to me (refunded)"];
  if (theirs && !theirs.spent_by_script_sig) {
    const bad = await checkLock(trade, theirs, true);
    if (bad) {
      if (mine && !mine.spent_by_script_sig)
        return now >= myTime ? ["refund", bad] : ["wait", bad + "; waiting to refund mine"];
      return ["abort", bad];
    }
  }
  if (trade.role === "initiator") {
    if (!mine) return ["lock", "I lock first"];
    if (!theirs || theirs.confirmations < need) {
      if (now >= myTime) return ["refund", "the other side never locked; my lock time has passed"];
      return ["wait", "waiting for the other lock to confirm"];
    }
    if (theirs.spent_by_script_sig) return [now < myTime ? "wait" : "refund", "their lock went back to them"];
    if (now < theirTime - TRADE.CLAIM_MARGIN) return ["claim", "their lock is confirmed and agreed: claim it (this reveals the secret)"];
    if (now >= myTime) return ["refund", "too late to claim safely; my lock time has passed"];
    return ["wait", "too close to their lock time to claim safely; waiting to refund"];
  }
  if (!theirs || theirs.confirmations < need) {
    if (mine && now >= myTime) return ["refund", "my lock time has passed"];
    return ["wait", "waiting for the initiator's lock to confirm"];
  }
  if (!mine) {
    if (now >= theirTime - TRADE.LOCK_MARGIN || now >= trade.t2 - TRADE.LOCK_MARGIN)
      return ["abort", "too little time left to lock safely"];
    return ["lock", "their lock is confirmed and agreed: lock mine"];
  }
  if (mine.spent_by_script_sig && !mine.spent_by_me) {
    const secret = await secretFromScriptSig(unhex(mine.spent_by_script_sig), trade.myRedeem);
    if (!secret) return ["wait", "my lock was spent without the secret?"];
    trade.secret = coinsHex(secret);
    if (now < theirTime) return ["claim", "the secret is on the chain: claim their lock"];
    return ["abort", "the secret came too late to claim their lock"];
  }
  if (now >= myTime) return ["refund", "the initiator never claimed; my lock time has passed"];
  return ["wait", "waiting for the initiator to claim (that reveals the secret)"];
}

/** The secret a claim revealed (htlc.secret_from_script_sig): sig, secret, OP_1, script. */
export async function secretFromScriptSig(scriptSig, redeem) {
  const pushes = [];
  let at = 0;
  while (at < scriptSig.length) {
    const op = scriptSig[at++];
    if (op === 0x00 || op === 0x51) { pushes.push(new Uint8Array([op])); continue; }
    let n;
    if (op >= 1 && op <= 75) n = op;
    else if (op === 0x4c) n = scriptSig[at++];
    else if (op === 0x4d) { n = scriptSig[at] | (scriptSig[at + 1] << 8); at += 2; }
    else return null;
    pushes.push(scriptSig.slice(at, at + n));
    at += n;
  }
  if (pushes.length !== 4 || pushes[2].length !== 1 || pushes[2][0] !== 0x51) return null;
  if (coinsHex(pushes[3]) !== coinsHex(redeem)) return null;
  const lock = coins.htlcParse(redeem);
  const secret = pushes[1];
  if (!lock || secret.length !== 32) return null;
  if (coinsHex(await coins.sha256Of(secret)) !== coinsHex(lock.secretHash)) return null;
  return secret;
}

/* --- the tab trader: one trade, run while this tab is open -----------------------
 *
 * Steps travel over the PEP testnet Messenger (2026-10-13: "Can't we use the Pepe
 * coin test net node to node communication protocol?"): sealed HERE to the other trader's
 * key (messaging.js sealForProgram), carried by this node as one testnet transaction
 * (/account/talk), answers opened HERE (programAnswers). Every move is nextTradeStep on
 * what the chains show. The trade -- the secret included -- is kept in this browser, so a
 * tab closed mid-trade can still claim or refund when it is opened again.
 */

/** Run `fn` while holding this browser's one spending lock (a Web Lock, so other tabs wait
 *  too): two spends built at once pick the same coin, and the node refuses the second
 *  (txn-mempool-conflict). Not re-entrant -- never call it from inside itself. */
export async function spending(fn) {
  if (!(globalThis.navigator && navigator.locks)) return fn();
  return navigator.locks.request("arcade-spend", () => fn());
}

const TRADES = "arcade-trades";
//: A lock's address on each chain: P2SH version bytes (arcade/config.py). The node's own
//: word (/account chains[].p2sh) wins: a chain standing in for another has its own.
const P2SH_VERSION = {main: 22, doge: 22, ltc: 50, ltctest: 58, test: 196, regtest: 196,   // ltc: "M" (2026-10-17)
                      bel: 30, lky: 5};    // bel: the node refuses a lock there (config.require_p2sh_payable)
let p2shKnown = null;
async function p2shOf(network) {
  if (!p2shKnown) {
    try {
      const said = await (await fetch("/account")).json();
      p2shKnown = Object.fromEntries((said.chains || []).filter((c) => c.p2sh != null).map((c) => [c.network, c.p2sh]));
    } catch (e) { p2shKnown = {}; }
  }
  return p2shKnown[network] ?? P2SH_VERSION[network];
}

function tradesOf(wallet) {
  try { return JSON.parse(localStorage.getItem(`${TRADES}:${wallet.pubkey}`) || "{}"); }
  catch (e) { return {}; }
}
function keepTrade(wallet, trade) {
  const all = tradesOf(wallet);
  const was = all[trade.id];
  all[trade.id] = trade;
  try { localStorage.setItem(`${TRADES}:${wallet.pubkey}`, JSON.stringify(all)); } catch (e) {}
  // A swap on one of this tab's listed orders just ended (2026-10-19: "no two users can fill the same
  // order"): taken whole, or its side locked and refunded, the order never stands again; called off with nothing
  // locked, it is back on every book.
  const ended = (t) => t && (t.state === "done" || t.state === "aborted");
  if (trade.role === "responder" && trade.order && ended(trade) && !ended(was)) {
    if (trade.state === "done" || trade.myLock) withdrawOrder(wallet, trade.order).catch(() => {});
    else orderTaking(wallet, trade.order, false).catch(() => {});
  }
}

/** Tell this account's node that a swap on its listed order is under way (or fell through): it goes off (or back
 *  on) its book and every other arcade's at once (/account/orders/taking, the book room). */
async function orderTaking(wallet, id, taking) {
  const book = myOrdersOf(wallet);
  const o = book.orders[id];
  if (o) { if (taking) o.taking = true; else delete o.taking; keepMyOrders(wallet, book); }
  await fetch("/account/orders/taking", {method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({id, taking})});
}
export function myTrades(wallet) {
  return Object.values(tradesOf(wallet));
}

/** One trade step, sealed in this tab to `toKey` (hex, a messaging key), carried by this node. */
export async function tradeSay(wallet, toKey, body, options = {}) {
  return spending(() => _tradeSay(wallet, toKey, body, options));
}

/* Swap talk goes over the mesh only, never the chain (2026-10-17: "I don't think we should
 * be using the chain at all"). It was the fallback when the other page was not on the mesh, and a
 * wallet showed a chain message every few seconds. Now a message the mesh cannot deliver yet waits
 * in this browser's outbox and is tried again on every pass (tradePass) for OUTBOX_FOR; a proposal
 * is not queued -- the Buy says at once that the seller's page is not open. Locks, claims and
 * refunds are transactions on their chains as always; only the talk about them moved. */
const OUTBOX = "arcade-trade-outbox", OUTBOX_FOR = 30 * 60 * 1000;
function outbox(list) {
  if (list === undefined) {
    try { return JSON.parse(localStorage.getItem(OUTBOX) || "[]") || []; } catch (e) { return []; }
  }
  try { localStorage.setItem(OUTBOX, JSON.stringify(list.slice(-200))); } catch (e) {}
  return list;
}

async function meshSay(wallet, toKey, body) {
  const {mail, me} = await messenger(wallet);
  const asked = await fetch("/account/talk", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({op: "ask", to: toKey}),
  });
  const said = await asked.json();
  if (!asked.ok) throw new Error(said.detail || "the node could not address that");
  const sealed = mail.sealForProgram(me, said.seal_to, said.stamp, body);
  try {
    const relayed = await fetch("/account/talk/relay", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({to: String(toKey).toLowerCase(), sealed}),
    });
    return relayed.ok && !!(await relayed.json()).mesh;
  } catch (e) { return false; }
}

async function _tradeSay(wallet, toKey, body, options = {}) {
  if (await meshSay(wallet, toKey, body)) return {mesh: true};
  if (body && body.step === "propose") {
    // A node answering for an order may be between two passes: a few more tries before saying nobody is there.
    for (let i = 0; i < 4; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      if (await meshSay(wallet, toKey, body)) return {mesh: true};
    }
    throw new Error("the seller's arcade is not answering right now, so nothing was locked. Try again in a moment");
  }
  if (!options.meshOnly) {
    const box = outbox().filter((m) => !(m.to === toKey && m.body.id === body.id && m.body.step === body.step));
    box.push({to: toKey, body, at: Date.now()});
    outbox(box);
  }
  return {mesh: false, queued: !options.meshOnly};
}

/** Try the outbox again: whatever the mesh takes now is sent, the rest waits (up to OUTBOX_FOR). */
async function flushOutbox(wallet) {
  const box = outbox();
  if (!box.length) return;
  const left = [];
  for (const m of box) {
    if (Date.now() - m.at > OUTBOX_FOR) continue;
    let sent = false;
    try { sent = await meshSay(wallet, m.to, m.body); } catch (e) { sent = false; }
    if (!sent) left.push(m);
  }
  outbox(left);
}

/** Trade steps sealed to this tab since `after`: {answers: [{json, sender}], cursor}. */
export async function tradeHeard(wallet, after, meshAfter = 0) {
  const {mail, me} = await messenger(wallet);
  const got = await mail.programAnswers(me, after);
  // And what the mesh brought (talk_relay): the same sealed messages, opened the same way.
  let meshCursor = Number(meshAfter || 0);
  const mesh = [];
  try {
    const key = coinsHex(me.publicKey || me.public || me.pub);
    const r = await fetch(`/account/talk/inbox?key=${key}&after=${meshCursor}`, {cache: "no-store"});
    if (r.ok) {
      const said = await r.json();
      for (const m of said.messages || []) {
        const opened = mail.openProgram(m.payload, me);
        if (opened) mesh.push({json: opened.json, sender: opened.sender, stamp: opened.stamp, via: "mesh"});
      }
      meshCursor = Math.max(meshCursor, Number(said.cursor || 0));
    }
  } catch (e) { /* the chain still carries everything the mesh could not */ }
  return {cursor: got.cursor, meshCursor,
          answers: [...mesh, ...got.answers].filter((a) => a.json && a.json.t === "trade" && a.json.v === 1)};
}

const coinsText = (sats) => (Number(sats) / 1e8).toFixed(8).replace(/\.?0+$/, "");

/** Take an order (trade_orders.py) as the initiator: secret made here, terms proposed. */
/** What a swap costs, in words, from the side of whoever gives `give` and gets `get` -- shown before anything is
 *  signed. 2026-10-21: the sender of each lock pays its fees, so they come ON TOP of what is given and the
 *  other side receives exactly it: the lock's own transaction (about one minimum fee), the network fee of the other
 *  side's claim (a coin lock; htlc.spend_fee) or the reserve that moves a token or NFT out again (trade.LOCK_RESERVE),
 *  and this arcade's operator fee when it charges one (operatorFee). `give` may carry its fee terms already (fee_bps,
 *  fee_to); else this arcade's are read (lockFeeOf). What you receive is the amount agreed, whole -- unless the other
 *  side's arcade is not updated yet (its leg carries no fee_bps), when its claim fee still comes off it.
 *  `options.taking`: `get` is the order's own side, so whether its arcade is updated is known. */
export async function swapFees(give, get, options = {}) {
  // LTC and LTC TEST too (2026-10-18: Litecoin's legs read "PEP TEST").
  const coin = (n) => COIN_WORD[n] || "PEP TEST";
  // Litecoin pays its own rate (htlc.spend_fee, 2026-10-18): about 0.0001 LTC, not the Dogecoin family's 0.01.
  const sendFee = (n) => n === "ltc" || n === "ltctest" || n === "bel" ? 10_000 : 1_000_000;   // Bellscoin: Litecoin's shape
  const kindOf = (l) => l.kind || "coin";
  const mine = give.fee_bps == null ? withFee(give, await lockFeeOf(give.network)) : give;
  const net = coin(give.network);
  const onTop = [`about ${coinsText(sendFee(give.network))} ${net} to send your lock`];
  if (kindOf(give) === "coin") onTop.push(`${coinsText(claimFee(give.network))} ${net} for the network fee of the other side's claim`);
  else if (kindOf(give) === "doginal") onTop.push(`${coinsText(TRADE.DOGINAL_RESERVE)} ${net} that goes in with it to pay for moving it out`);
  else if (kindOf(give) === "token20") onTop.push(`${coinsText(TRADE.DOGINAL_RESERVE)} ${net} that goes in with it to pay for `
    + "moving it out, and the fees of the two transfer notes and four transactions that put the tokens into the lock");
  else onTop.push(`${coinsText(TRADE.LOCK_RESERVE)} ${net} that goes in with it to pay for moving it out`);
  const cut = operatorFee(mine, get);
  if (cut) onTop.push(`${coinsText(cut)} ${net} operator fee (${Number(mine.fee_bps) / 100}%) to this arcade`);
  let back;
  if (kindOf(get) !== "coin") back = "You receive what you are getting, whole; its fees are the other side's.";
  else if (options.taking && get.fee_bps == null) back = `${coinsText(claimFee(get.network))} ${coin(get.network)} comes off `
    + "what you receive: the other side's arcade is not updated yet and leaves its claim fee in its lock.";
  else if (options.taking) back = `You receive exactly the ${coin(get.network)} agreed; the other side pays its own fees.`;
  else back = `You receive exactly the ${coin(get.network)} agreed; the taker pays its own fees (a taker on an arcade `
    + `not updated yet leaves ${coinsText(claimFee(get.network))} ${coin(get.network)} of it as its claim fee).`;
  return `Fees you pay on top: ${onTop.join("; ")}. The other side receives exactly what you give. ${back}`;
}

export async function takeOrder(wallet, order, options = {}) {
  const mineOn = (network) => coinsHex(keysOn(wallet, network).pubkey);
  // This side's lock carries this arcade's operator fee (withFee); the lister's carries the terms its order names, or
  // none from an arcade before 2026-10-21 (that lock is then held to the old rule). 2026-10-21.
  const give = withFee({network: order.get.network, kind: order.get.kind, asset: order.get.asset || "",
                        amount: order.get.amount,
                        giver_key: mineOn(order.get.network), taker_key: order.take_key || order.trade_key},
                       await lockFeeOf(order.get.network));
  const get = {network: order.give.network, kind: order.give.kind, asset: order.give.asset || "",
               amount: order.give.amount,
               giver_key: order.trade_key, taker_key: mineOn(order.give.network)};
  if (order.give.fee_bps != null) {
    get.fee_bps = Number(order.give.fee_bps);
    if (order.give.fee_to) get.fee_to = String(order.give.fee_to);
    if (!(await feeTermsOk(get))) throw new Error("that swap's fee terms cannot be met, so nothing was asked");
  }
  const secret = crypto.getRandomValues(new Uint8Array(32));
  const secretHash = await coins.sha256Of(secret);
  const {t1, t2} = planLocktimes(give, get, Date.now() / 1000);
  const id = "tr-" + coinsHex(crypto.getRandomValues(new Uint8Array(8)));
  const trade = {id, role: "initiator", order: order.id, peer: order.msg_key, give, get,
                 secret: coinsHex(secret), secretHash: coinsHex(secretHash), t1, t2,
                 myLock: null, theirLock: null, agreed: false, cancelled: false,
                 state: "proposed", note: "proposed; waiting for the other side to accept",
                 cursor: 0, created: Date.now(), need: confirmationsFor(give, get, await swapBig()),
                 // Said yes to in the Take dialog, which showed the amounts and fees: the lock is
                 // not asked about again (real coins included -- 2026-10-13).
                 approved: !!options.approved};
  keepTrade(wallet, trade);
  await sayAlive(wallet);        // reachable over the mesh before the seller answers (talk_relay)
  try {
    await tradeSay(wallet, order.msg_key, {t: "trade", v: 1, id, step: "propose", order: order.id,
                                           give, get, secret_hash: trade.secretHash, t1, t2});
  } catch (e) {
    // Nothing was locked: a proposal that never went out leaves no trade behind.
    const all = tradesOf(wallet);
    delete all[id];
    try { localStorage.setItem(`${TRADES}:${wallet.pubkey}`, JSON.stringify(all)); } catch (x) {}
    throw e;
  }
  return trade;
}

//: The node's "big swap" lines (/account swap_big): at or above one, a swap waits 1 confirmation; below, none.
let bigKnown = null;
async function swapBig() {
  if (!bigKnown) {
    try { bigKnown = (await (await fetch("/account")).json()).swap_big || TRADE.BIG; }
    catch (e) { bigKnown = TRADE.BIG; }
  }
  return bigKnown;
}

async function redeemsOf(trade) {
  const h = unhex(trade.secretHash);
  const myTime = trade.role === "initiator" ? trade.t1 : trade.t2;
  const theirTime = trade.role === "initiator" ? trade.t2 : trade.t1;
  return {
    myRedeem: coins.htlcRedeem(h, unhex(trade.give.taker_key), unhex(trade.give.giver_key), myTime),
    theirRedeem: coins.htlcRedeem(h, unhex(trade.get.taker_key), unhex(trade.get.giver_key), theirTime),
  };
}

async function sayAccept(wallet, trade) {
  await tradeSay(wallet, trade.peer, {t: "trade", v: 1, id: trade.id, step: "accept"});
  const kept = tradesOf(wallet)[trade.id] || trade;
  kept.acceptSent = true;
  keepTrade(wallet, kept);
}

/** One pass of one trade: read answers, read the chains, take the one safe step. */
export async function tradeTick(wallet, id) {
  let trade = tradesOf(wallet)[id];
  if (!trade || trade.state === "done" || trade.state === "aborted") return trade;
  if (trade.acceptSent === false && !trade.myLock) {
    await sayAccept(wallet, trade);
    trade = tradesOf(wallet)[id];
  }
  const heard = await tradeHeard(wallet, trade.cursor, trade.meshCursor);
  trade.cursor = heard.cursor;
  trade.meshCursor = heard.meshCursor;
  for (const a of heard.answers) {
    if (a.json.id !== trade.id || a.sender !== trade.peer) continue;
    if (a.json.step === "accept") trade.agreed = true;
    else if (a.json.step === "locked") trade.theirLock = {txid: a.json.txid, vout: a.json.vout};
    else if (a.json.step === "cancel") { trade.cancelled = true; trade.cancelReason = String(a.json.reason || "").slice(0, 200); }
  }
  if (trade.cancelled && !trade.myLock) {
    trade.state = "aborted";
    trade.note = trade.cancelReason ? `the other side said no: ${trade.cancelReason}; nothing was locked`
                                    : "the other side walked away; nothing was locked";
    keepTrade(wallet, trade); return trade;
  }
  // Nobody answered: the lister's page is closed (or its wallet locked), and only it can accept.
  // Called off with nothing locked, rather than "proposed" for ever (2026-10-17: the Buy
  // button "is not working ... the lock script is not being sent").
  if (!trade.agreed && trade.role === "initiator" && Date.now() - (trade.created || 0) > PROPOSE_WAIT) {
    trade.state = "aborted";
    trade.note = "no answer: the seller's arcade page is not open, so nothing was locked. Try again when it is";
    keepTrade(wallet, trade);
    tradeSay(wallet, trade.peer, {t: "trade", v: 1, id: trade.id, step: "cancel"}).catch(() => {});
    return trade;
  }
  if (!trade.agreed) { keepTrade(wallet, trade); return trade; }
  // Accepted, and the taker never locked (2026-10-17: Trade history showed two "accepted" swaps an hour old):
  // nothing of ours was locked, so it is simply called off rather than left "waiting" until its lock time.
  if (trade.role === "responder" && !trade.myLock && !trade.theirLock
      && Date.now() - (trade.created || 0) > RESPONDER_WAIT) {
    trade.state = "aborted";
    trade.note = "the taker never locked their side, so nothing was locked";
    keepTrade(wallet, trade);
    return trade;
  }
  const {myRedeem, theirRedeem} = await redeemsOf(trade);
  const facts = {
    mine: trade.myLock ? await tradeFacts(trade.give.network, trade.myLock.txid, trade.myLock.vout,
                                          trade.give, myRedeem) : null,
    theirs: trade.theirLock ? await tradeFacts(trade.get.network, trade.theirLock.txid, trade.theirLock.vout,
                                               trade.get, theirRedeem) : null,
  };
  const now = Math.floor(Date.now() / 1000);
  const view = {...trade, myRedeem, theirRedeem};
  const [step, why] = await nextTradeStep(view, facts, now);
  if (view.secret && !trade.secret) trade.secret = view.secret;
  trade.note = why;
  if (step === "lock") {
    const address = await coins.htlcAddress(myRedeem, await p2shOf(trade.give.network));
    const sent = await spending(async () => {
    let offer;
    // The sender pays (2026-10-21): the claim's network fee and this arcade's operator fee go in on top, so the
    // other side receives exactly the amount agreed (fundValue).
    if (trade.give.kind === "coin") offer = await offerSend(address, coinsText(fundValue(trade.give, trade.get)), trade.give.network);
    else if (trade.give.kind === "token20") {
      // A DRC-20 / PRC-20 token (token20_swap): four offers -- note 1's commit and reveal, the tokens to the lock with
      // note 2's commit, then note 2 on the lock -- each checked and signed here; the last is the lock.
      let step20 = await token20Post({chain: trade.give.network, redeem: coinsHex(myRedeem),
                                      leg: legWhat(trade.give, trade.get)}, "/account/trade/lock20");
      if (step20.lock !== address) throw new Error("the node built a different lock. Nothing was signed.");
      const lid = step20.lock20;
      for (let n = 0; n < 4 && step20.step !== "done"; n++) {
        await confirm(wallet, step20, trade.approved ? {reviewed: true} : null);
        step20 = await token20Post({lock20: lid}, "/account/trade/lock20");
      }
      if (step20.step !== "done") throw new Error("the token lock did not finish: " + (step20.detail || step20.step));
      return {txid: step20.lock_txid};
    } else {
      // A token or NFT: the node builds it (payload, then the lock with the coin that pays
      // its way out, last); this tab checks and signs it like any other offer.
      const asked = await fetch("/account/trade/lock", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({chain: trade.give.network, redeem: coinsHex(myRedeem), leg: legWhat(trade.give, trade.get)}),
      });
      offer = await asked.json();
      if (!asked.ok) throw new Error(offer.detail || "the node could not build that lock");
      if (offer.lock !== address) throw new Error("the node built a different lock. Nothing was signed.");
    }
    // Approved in the Take or List dialog (amounts and fees shown there): not asked again.
    return confirm(wallet, offer, trade.approved ? {reviewed: true} : null);
    });
    const script = coinsHex(await coins.htlcScript(myRedeem));
    // A Doginal's lock is output 0 (its reserve output 1, under the same script).
    let vout = trade.give.kind === "doginal" || trade.give.kind === "token20" ? 0 : -1;
    for (let n = 0; n < 4 && vout < 0; n++) {
      const seen = await tradeFacts(trade.give.network, sent.txid, n).catch(() => null);
      if (seen && seen.script_hex === script && seen.value < lockValue(trade.give, trade.get)) continue;
      if (seen && seen.script_hex === script) vout = n;
    }
    trade.myLock = {txid: sent.txid, vout: Math.max(0, vout)};
    keepTrade(wallet, trade);
    await tradeSay(wallet, trade.peer, {t: "trade", v: 1, id: trade.id, step: "locked",
                                        network: trade.give.network, txid: sent.txid, vout: trade.myLock.vout});
  } else if (step === "claim") {
    const done = await tradeSpend(wallet, {chain: trade.get.network, redeem: coinsHex(theirRedeem),
      txid: trade.theirLock.txid, vout: trade.theirLock.vout, kind: "claim", secret: trade.secret, leg: trade.get,
      other: trade.give});
    await tradeSay(wallet, trade.peer, {t: "trade", v: 1, id: trade.id, step: "claimed",
                                        network: trade.get.network, txid: done.txid});
  } else if (step === "refund") {
    const done = await tradeSpend(wallet, {chain: trade.give.network, redeem: coinsHex(myRedeem),
      txid: trade.myLock.txid, vout: trade.myLock.vout, kind: "refund", leg: trade.give, other: trade.get});
    await tradeSay(wallet, trade.peer, {t: "trade", v: 1, id: trade.id, step: "refunded",
                                        network: trade.give.network, txid: done.txid});
  } else if (step === "done") trade.state = "done";
  else if (step === "abort") trade.state = "aborted";
  else trade.state = trade.myLock ? "locked" : "accepted";
  keepTrade(wallet, trade);
  if (trade.state !== "done" && trade.state !== "aborted") {
    await holdSpends(wallet, trade, view, facts).catch((e) => { trade.holdNote = String(e.message || e); });
    keepTrade(wallet, trade);
  }
  return {...trade, step};
}

/** Give this node what it needs to finish the swap with the tab closed (trade_watch.py):
 *  the refund of my lock, and my claim of theirs -- each signed here, once. */
async function holdSpends(wallet, trade, view, facts) {
  const hex = coinsHex;
  if (trade.myLock && !trade.heldRefund) {
    await tradePresign(wallet, {chain: trade.give.network, redeem: hex(view.myRedeem), txid: trade.myLock.txid,
                                vout: trade.myLock.vout, kind: "refund", leg: trade.give, other: trade.get,
                                trade: trade.id});
    trade.heldRefund = true;
  }
  if (trade.heldClaim || !trade.theirLock || !facts.theirs || facts.theirs.spent_by_script_sig) return;
  if (await checkLock(view, facts.theirs, true)) return;            // not the lock agreed: never
  const theirTime = trade.role === "initiator" ? trade.t2 : trade.t1;
  const base = {chain: trade.get.network, redeem: hex(view.theirRedeem), txid: trade.theirLock.txid,
                vout: trade.theirLock.vout, kind: "claim", leg: trade.get, other: trade.give, trade: trade.id};
  const need = trade.need || confirmationsFor(trade.give, trade.get);
  if (trade.role === "initiator") {
    // A token or NFT is only known to be in the lock once the ledger has read it confirmed;
    // claiming an empty one would hand over the secret for nothing.
    if (trade.get.kind !== "coin" && !(facts.theirs.confirmations >= need && facts.theirs.asset_ok)) return;
    await tradePresign(wallet, {...base, secret: trade.secret, min_conf: need,
                                not_after: theirTime - TRADE.CLAIM_MARGIN});
  } else {
    if (!trade.myLock) return;
    await tradePresign(wallet, {...base, not_after: theirTime,
      secret_lock: {network: trade.give.network, txid: trade.myLock.txid, vout: trade.myLock.vout,
                    redeem: hex(view.myRedeem)}});
  }
  trade.heldClaim = true;
}


/* --- a tab LISTING its own swaps --------------------------------------------------
 *
 * The order is inscribed from this account (an order batch, trade_orders.py) naming its
 * keys -- one per chain, which is what `take_key` is for -- its messaging key, and this
 * node, so other arcades show it while this node is online. A tab's order lives an hour
 * and is posted again while the tab stays open: a closed tab's swaps leave by themselves.
 * Proposals are answered HERE, and only exactly this order's terms are accepted.
 */

const MY_ORDERS = "arcade-my-orders";
export const TAB_ORDER_LIFE = 3600;
//: A delegated order stands until it is taken or withdrawn: its node answers for it.
export const DELEGATED_ORDER_LIFE = 30 * 86400;
//: Coin set beside a delegated order's side for its lock and the way back (network fees), per chain.
const DELEGATED_FEES = 5_000_000;
const TAB_REPOST = 45 * 60 * 1000;

function myOrdersOf(wallet) {
  try { return JSON.parse(localStorage.getItem(`${MY_ORDERS}:${wallet.pubkey}`) || "{\"orders\":{},\"cancels\":[],\"posted\":0,\"cursor\":0}"); }
  catch (e) { return {orders: {}, cancels: [], posted: 0, cursor: 0}; }
}
function keepMyOrders(wallet, book) {
  try { localStorage.setItem(`${MY_ORDERS}:${wallet.pubkey}`, JSON.stringify(book)); } catch (e) {}
}
export function myOrders(wallet) {
  return Object.values(myOrdersOf(wallet).orders);
}

//: One post at a time from this tab: the page's tick re-posting while a List or Withdraw
//: is posting would spend the same coins twice (txn-mempool-conflict).
let posting = Promise.resolve();

/** The order batch for this tab's listed swaps, inscribed from this account. */
export function postMyOrders(wallet) {
  const run = posting.then(() => spending(() => postNow(wallet)));
  posting = run.catch(() => {});
  return run;
}

async function postNow(wallet) {
  const book = myOrdersOf(wallet);
  const {me} = await messenger(wallet);
  const acct = await (await fetch("/account")).json();
  if (!acct.mesh_node) throw new Error("this node is not on the mesh, so no other arcade would see the swap");
  const tags = (acct.chains || []).find((c) => c.tags) || {};
  const now = Math.floor(Date.now() / 1000);
  const orders = Object.values(book.orders).map((o) => o.delegated
    // Finished by this account's node with the order's own key: its messaging key, no page needed, a long life.
    ? {id: o.id, give: o.give, get: o.get, trade_key: o.trade_key, take_key: o.take_key,
       msg_key: o.msg_key, node: o.node, tab: false, delegated: true, expires: now + DELEGATED_ORDER_LIFE}
    : {id: o.id, give: o.give, get: o.get, trade_key: o.trade_key, take_key: o.take_key,
       msg_key: coinsHex(me.publicKey || me.public || me.pub), node: acct.mesh_node, tab: true,
       expires: now + TAB_ORDER_LIFE});
  // To this account's node, not the chain (2026-10-17: "there's no reason why my browser should be posting
  // orders to the chain"): free, on the book at once; other arcades hear of it in the mesh's book room (book_relay.py).
  const asked = await fetch("/account/orders/post", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({orders, cancels: book.cancels || []}),
  });
  const sent = await asked.json().catch(() => ({}));
  if (!asked.ok) throw new Error(sent.detail || "the swap could not be posted");
  // Read again: the cursor, or another listing, may have moved while this was posting.
  const now2 = myOrdersOf(wallet);
  now2.posted = Date.now();
  now2.cancels = (now2.cancels || []).filter((c) => !(book.cancels || []).includes(c));
  keepMyOrders(wallet, now2);
  return sent;
}

/** A delegated order (2026-10-17): its side moved to the order's own key, the key handed to this account's node,
 *  which then answers, locks and claims for it -- the seller can close every page. Proceeds go to the wallet's own
 *  address on the chain it is paid on. */
async function delegateOrder(wallet, id, give, get, approved) {
  const k = await coins.orderKey(wallet.seed, id);
  const giveKeys = keysOn(wallet, give.network);
  const deposit = await coins.address(k.pubkey, giveKeys.version);
  const ok = approved ? {reviewed: true} : null;
  const send = async (url, body) => {
    const {ok: good, said} = await askJson(url, body);
    if (!good) throw new Error(said.detail || "the deposit could not be made");
    return confirm(wallet, said, ok);
  };
  // The node is told first (it starts watching the deposit address), then the side goes there.
  const {ok: told, said} = await askJson("/account/orders/delegate", {
    order: {id, give, get}, key: coinsHex(k.key), deposit: {[give.network]: deposit},
    payout: {[get.network]: keysOn(wallet, get.network).address}});
  if (!told) throw new Error(said.detail || "this arcade's node would not take the order on");
  // The node locks with the fees on top (2026-10-21: the claim's network fee and its operator fee, node_trader
  // _delegated_lock), so the deposit carries them; whatever a taker's terms leave unused comes back with the rest.
  const fee = said.lock_fee || {};
  const lockNeeds = fundValue(withFee(give, fee), get);
  if (give.kind === "coin") {
    await confirm(wallet, await offerSend(deposit, coinsText(lockNeeds + DELEGATED_FEES), give.network), ok);
  } else {
    if (give.kind === "token") {
      await send("/account/token/send", {property_id: give.asset, to: deposit, units: give.amount, chain: give.network});
    } else {
      await send("/account/nft/send", {piece: give.asset, to: deposit, chain: give.network});
    }
    await confirm(wallet, await offerSend(deposit, coinsText(lockNeeds + DELEGATED_FEES), give.network), ok);
  }
  return {trade_key: coinsHex(k.pubkey), take_key: coinsHex(k.pubkey), msg_key: said.msg_key, node: said.node,
          deposit, lock_fee: fee};
}

/** List a swap from this tab: give `give` {network, kind?, asset?, amount}, get `get` (coin, token or NFT).
 *  `options.delegated`: its node finishes it with the seller away (delegateOrder). */
export async function listOrder(wallet, give, get, options = {}) {
  const short = await canFund({network: give.network, kind: give.kind || "coin", asset: String(give.asset || ""),
                               amount: Number(give.amount)}, get);
  if (short) throw new Error(`You can't list this swap yet: ${short}.`);
  const book = myOrdersOf(wallet);
  const id = "ord-" + coinsHex(crypto.getRandomValues(new Uint8Array(8)));
  const side = (s) => ({network: s.network, kind: s.kind || "coin", asset: String(s.asset || ""),
                        amount: s.kind === "nft" ? 1 : Math.round(Number(s.amount))});
  const sides = {give: side(give), get: side(get)};
  const keys = options.delegated
    ? {...(await delegateOrder(wallet, id, sides.give, sides.get, !!options.approved)), delegated: true}
    : {trade_key: coinsHex(keysOn(wallet, give.network).pubkey), take_key: coinsHex(keysOn(wallet, get.network).pubkey)};
  // The side this listing locks names this arcade's lock-fee terms (2026-10-21), so a taker on any arcade knows
  // what the lock holds and what its claim pays; a delegated order's are the ones its node stood it with.
  sides.give = withFee(sides.give, options.delegated ? keys.lock_fee : await lockFeeOf(give.network));
  delete keys.lock_fee;
  book.orders[id] = {id, ...sides, labels: [give.label || "", get.label || ""],
                     approved: !!options.approved, ...keys};
  keepMyOrders(wallet, book);
  await postMyOrders(wallet);
  await sayAlive(wallet);
  return book.orders[id];
}

export async function withdrawOrder(wallet, id) {
  const book = myOrdersOf(wallet);
  delete book.orders[id];
  book.cancels = [...new Set([...(book.cancels || []), id])];
  book.withdrawn = [...new Set([...(book.withdrawn || []), id])].slice(-200);   // kept after the cancel is posted
  keepMyOrders(wallet, book);
  // Off this node's book at once (2026-10-17: Cancel "says your order is off the books, but it
  // stays there"), and every other arcade's through the book room.
  try {
    await fetch("/account/orders/cancel", {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({ids: [id]})});
  } catch (e) { /* the chain's cancel still takes it off */ }
  await postMyOrders(wallet);
}

/** Drop this browser's copies of delegated orders its node no longer holds open (filled, or sent back), so the
 *  seller's page stops showing them with Cancel. Asked at most every few seconds. */
let delegatedAsked = 0;
export async function syncDelegated(wallet) {
  const book = myOrdersOf(wallet);
  const mine = Object.values(book.orders || {}).filter((o) => o.delegated);
  if (!mine.length || Date.now() - delegatedAsked < 1500) return false;
  delegatedAsked = Date.now();
  let said = null;
  try { said = await (await fetch("/account/orders/delegated", {cache: "no-store"})).json(); } catch (e) { return false; }
  const states = Object.fromEntries((said.orders || []).map((o) => [o.id, o.state]));
  let changed = false;
  for (const o of mine) {
    const st = states[o.id];
    if (st === "taking") {
      if (!o.taking) { o.taking = true; changed = true; }          // a buyer's swap is under way: no Cancel now
    } else if (st && st !== "open") {
      delete book.orders[o.id];
      book.withdrawn = [...new Set([...(book.withdrawn || []), o.id])].slice(-200);
      changed = true;
    } else if (o.taking) {
      delete o.taking; changed = true;                              // it fell through: it stands again
    }
  }
  if (changed) {
    keepMyOrders(wallet, book);
    try { globalThis.dispatchEvent(new CustomEvent("arcade:trades")); } catch (e) {}
  }
  return changed;
}

/** The ids this browser has withdrawn: never drawn as a book row again, whatever a page still shows. */
export function myCancelled(wallet) {
  return myOrdersOf(wallet).withdrawn || [];
}

/** Answer proposals against this tab's orders: exactly the order's terms are accepted (and
 *  traded as the responder), anything else is cancelled. Re-posts the orders when due. */
export async function answerProposals(wallet) {
  const book = myOrdersOf(wallet);
  const heard = await tradeHeard(wallet, book.cursor, book.meshCursor);
  book.cursor = heard.cursor;
  book.meshCursor = heard.meshCursor;
  keepMyOrders(wallet, book);
  const trades = tradesOf(wallet);
  const now = Math.floor(Date.now() / 1000);
  for (const a of heard.answers) {
    const p = a.json;
    if (p.step !== "propose" || trades[p.id]) continue;
    // A proposal is answered once: heard again (another tab, the chain and the mesh copy, a
    // cursor read over) it is not answered again.
    book.refused = book.refused || [];
    if (book.refused.includes(p.id)) continue;
    const mine = book.orders[p.order];
    const fits = mine && p.get && p.give
      && p.get.network === mine.give.network && p.get.kind === mine.give.kind
      && (p.get.asset || "") === mine.give.asset && p.get.amount === mine.give.amount
      && p.get.giver_key === mine.trade_key
      && p.give.network === mine.get.network && p.give.kind === mine.get.kind
      && (p.give.asset || "") === mine.get.asset && p.give.amount >= mine.get.amount
      && p.give.taker_key === mine.take_key
      && Number.isInteger(p.t1) && Number.isInteger(p.t2) && p.t2 < p.t1
      && p.t2 - TRADE.LOCK_MARGIN > now && /^[0-9a-f]{64}$/.test(p.secret_hash || "")
      // The operator fee in this lister's own lock: none, or exactly what it listed; the taker's own terms well formed,
      // or the claim of the taker's lock could not be built (2026-10-21).
      && feeTermsFit(p.get, mine.give) && await feeTermsOk(p.give) && await feeTermsOk(p.get);
    const refuse = async (reason) => {
      book.refused = [...book.refused, p.id].slice(-300);
      keepMyOrders(wallet, book);
      // Said once, over the mesh; a refusal is not worth queueing.
      await tradeSay(wallet, a.sender, {t: "trade", v: 1, id: p.id, step: "cancel", reason},
                     {meshOnly: true}).catch(() => {});
    };
    if (!fits) { await refuse(mine ? "those are not this swap's terms" : "no such swap here"); continue; }
    // First taker wins (2026-10-19): a lock moves the whole order, so a second swap could only refund.
    if (mine.taking || Object.values(trades).some((t) => t.order === p.order && t.role === "responder"
                                                      && t.state !== "done" && t.state !== "aborted")) {
      await refuse("somebody else is taking this swap"); continue;
    }
    // Able to lock its side? If not, say so now, before the taker locks anything.
    const short = await canFund(p.get, p.give);
    if (short) { await refuse(`the lister cannot fund its side (${short})`); continue; }
    const trade = {id: p.id, role: "responder", order: p.order, peer: a.sender, give: p.get, get: p.give,
                   secret: "", secretHash: p.secret_hash, t1: p.t1, t2: p.t2, myLock: null, theirLock: null,
                   agreed: true, acceptSent: false, cancelled: false, state: "accepted",
                   note: "accepted; waiting for their lock", cursor: book.cursor, meshCursor: book.meshCursor,
                   created: Date.now(),
                   need: confirmationsFor(p.get, p.give, await swapBig()),
                   approved: !!mine.approved};      // approved when it was listed
    keepTrade(wallet, trade);
    trades[trade.id] = trade;
    await orderTaking(wallet, p.order, true).catch(() => {});        // off every book now
    // Kept BEFORE it is said, and said again by tradeTick until it goes: an accept that
    // failed to send must not leave the other side waiting for ever.
    await sayAccept(wallet, trade).catch(() => {});
  }
  // Again only what has gone out before: a new listing posts itself (listOrder).
  const again = myOrdersOf(wallet);
  if (Object.keys(again.orders).length && again.posted && Date.now() - again.posted > TAB_REPOST)
    await postMyOrders(wallet);
}


/* --- the trade runner: every arcade page, background tabs too ---------------------------
 *
 * 2026-10-13: "The trade loop should be ran on every arcade page ... and even if
 * the tab is in the background, it should continue to run." base.html starts this on every
 * page. One tab in the browser runs it at a time -- a Web Lock, held for as long as the tab
 * lives, so when that tab closes the next arcade tab takes over -- and its beat comes from a
 * worker's timer: a background page's own timers are slowed to once a minute (Chrome's
 * intensive throttling), a dedicated worker's are not, and the worker's beat reaches the
 * page as a message, not a timer. Each beat: answer proposals to this tab's listings,
 * re-post them when due, and take the next safe step of every open trade.
 * Pages that show trades listen for "arcade:trades" (this tab) and "storage" (another tab).
 */

export const TRADE_BEAT = 8000;
let runnerChain = null, runnerBusy = false, runnerStarted = false;
const kicks = (() => { try { return new BroadcastChannel("arcade-trade-kick"); } catch (e) { return null; } })();

/* Which tab works on the swaps: a lease, not a lock held until the tab closes (2026-10-17: two
 * listings never reached the book). On a phone a tab in the background is frozen but kept its lock, so
 * the tab in front never ran: no heartbeat, nobody to answer a taker. The lease is renewed by the tab
 * that runs, every pass; a frozen tab cannot renew it, so it goes stale in LEASE_STALE, and a tab that
 * is looked at takes it at once from one that is not. */
const LEASE_KEY = "arcade-trade-lease", LEASE_STALE = 5000;
function mayRun() {
  let lease = null;
  try { lease = JSON.parse(localStorage.getItem(LEASE_KEY) || "null"); } catch (e) { lease = null; }
  const now = Date.now(), hidden = !!(globalThis.document && document.hidden);
  const mine = lease && lease.id === THIS_PAGE;
  const free = !lease || now - Number(lease.at || 0) > LEASE_STALE || (!hidden && lease.hidden);
  if (!mine && !free) return false;
  try { localStorage.setItem(LEASE_KEY, JSON.stringify({id: THIS_PAGE, at: now, hidden})); } catch (e) {}
  return true;
}

async function tradePass() {
  if (!runnerChain) return;
  if (runnerBusy) { if (globalThis.__arcadeTradeRunner) mayRun(); return; }   // still at it: keep the lease
  if (!mayRun()) { globalThis.__arcadeTradeRunner = false; return; }
  globalThis.__arcadeTradeRunner = true;
  runnerBusy = true;
  try {
    const w = await opened(runnerChain);
    if (!w) return;                       // locked: nothing can be signed, nothing to do
    // Every 1.5 s while a swap is running or listed; otherwise at the old, gentler pace.
    if (!swapsBusy(w)) {
      if (Date.now() - lastIdlePass < TRADE_BEAT) return;
      lastIdlePass = Date.now();
    }
    await flushOutbox(w).catch(() => {});
    await syncDelegated(w).catch(() => {});
    if (myOrders(w).length) await answerProposals(w).catch(() => {});
    // A finished swap goes into its market's history (swap_history): Recent trades, the chart.
    for (const t of myTrades(w)) {
      if (t.state === "done" && !t.reported && (t.reportTries || 0) < 20 && t.myLock && t.theirLock)
        await reportDone(w, t).catch(() => {});
    }
    for (const t of myTrades(w)) {
      if (t.state === "done" || t.state === "aborted") continue;
      try { await tradeTick(w, t.id); }
      catch (e) {
        const kept = tradesOf(w)[t.id];
        if (kept) { kept.note = String(e.message || e); keepTrade(w, kept); }
      }
    }
    try { globalThis.dispatchEvent(new CustomEvent("arcade:trades")); } catch (e) {}
  } finally { runnerBusy = false; }
}

/** "" when this wallet can fund the lock for `give` right now, else why not, in words
 *  (2026-10-17: a GOLD/DOGE swap stuck because the bid could not pay its own lock).
 *  Asked before a listing is posted and before a take is accepted, so nobody locks against a
 *  side that was never going to lock. "" too when the node cannot say: the lock itself will. */
export async function canFund(give, get = null) {
  return (await fundsReader())(give, get);
}

/** A reader for many sides at once (one /account, one /account/tokens): (give) -> "" or why not, in words.
 *  The token itself is counted too (2026-10-17: a buyer holding 75 GOLD could press Sell on a bid for 100). */
export async function fundsReader() {
  let acct = null, toks = null;
  try { acct = await (await fetch("/account", {cache: "no-store"})).json(); } catch (e) { acct = null; }
  const show = (n) => (n / 1e8).toFixed(4).replace(/\.?0+$/, "");
  return async (give, get = null) => {
    try {
      if (!acct) return "";
      const c = (acct.chains || []).find((x) => x.network === give.network);
      if (!c) return "";
      const kind = give.kind || "coin";
      const coin = COIN_WORD[give.network] || "PEP TEST";
      if (kind === "token") {
        if (toks === null) {
          try { toks = await (await fetch("/account/tokens", {cache: "no-store"})).json(); } catch (e) { toks = {}; }
        }
        const t = ((toks.chains || []).find((x) => x.network === give.network) || {tokens: []}).tokens
          .find((x) => String(x.property_id) === String(give.asset));
        const held = Number((t && t.balance) || 0);
        if (held < Number(give.amount)) {
          const name = (t && t.name) || `token #${give.asset}`;
          const units = (n) => (t && t.divisible === false) ? String(n) : show(n);
          return `you hold ${units(held)} ${name} and this needs ${units(Number(give.amount))}`;
        }
      }
      const fee = 1_000_000;                                    // about 0.01 to send the lock
      // What the lock holds, with the claim's network fee and this arcade's operator fee on top (2026-10-21).
      const legF = give.fee_bps == null ? withFee({...give, kind}, c.lock_fee || {}) : {...give, kind};
      const need = kind === "coin" ? fundValue(legF, get) + fee
        : kind === "doginal" ? Number(c.doginal_lock_need || TRADE.LOCK_RESERVE + fee) + operatorFee(legF, get)
        // A DRC-20 / PRC-20 lock: two notes and four transactions -- about twice a Doginal's lock (the tokens
        // themselves are checked by the node when the lock is made).
        : kind === "token20" ? 2 * Number(c.doginal_lock_need || TRADE.LOCK_RESERVE + fee) + operatorFee(legF, get)
        : fundValue(legF, get) + fee;
      const have = Number(c.spendable ?? c.balance ?? 0);
      if (have >= need) return "";
      return `not enough ${coin} to lock it: about ${show(need)} ${coin} is needed and this wallet has ${show(have)}`;
    } catch (e) { return ""; }
  };
}

/** Tell this node a swap finished, for its market's Recent trades and chart (swap_history). */
async function reportDone(wallet, t) {
  const side = (l) => ({network: l.network, kind: l.kind || "coin", asset: String(l.asset || ""), amount: Number(l.amount)});
  const r = await fetch("/account/trade/done", {method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: t.id, role: t.role, order: t.order || "", give: side(t.give), get: side(t.get),
                          my_lock: t.myLock.txid, their_lock: t.theirLock.txid})});
  const kept = tradesOf(wallet)[t.id];
  if (!kept) return;
  if (r.ok) kept.reported = true;
  else kept.reportTries = (kept.reportTries || 0) + (r.status === 409 ? 0.25 : 1);   // a lock not seen yet: patient
  keepTrade(wallet, kept);
}

//: How long a take waits for the lister's page to accept before it is called off (nothing locked).
export const PROPOSE_WAIT = 120 * 1000;
//: How long an accepted swap waits for the taker's lock before it is called off with nothing locked.
export const RESPONDER_WAIT = 30 * 60 * 1000;

//: How often an open page with listed swaps says it is here (order_presence.ALIVE_FOR is 12 s).
export const ALIVE_BEAT = 3000;

/** Tell this node the page holding the account's listed swaps, or a swap in progress, is open
 *  with its wallet unlocked: its listings stand (order_presence) and the mesh can reach it with
 *  swap messages (talk_relay). */
export async function sayAlive(wallet = null) {
  if (!runnerChain && !wallet) return;
  if (!wallet && !globalThis.__arcadeTradeRunner) return;     // the tab holding the lease says it
  try {
    const w = wallet || await opened(runnerChain);
    if (!w) return;
    const listed = myOrders(w).some((o) => !o.delegated);   // a delegated order's node answers for it
    const running = myTrades(w).some((t) => t.state !== "done" && t.state !== "aborted");
    if (!listed && !running) return;
    const {me} = await messenger(w);
    await fetch("/account/orders/alive", {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({orders: listed, talk: coinsHex(me.publicKey || me.public || me.pub)})});
  } catch (e) { /* the next beat tries again */ }
}

/** Start the runner in this tab (it waits its turn behind any other arcade tab running it). */
//: How often a page works on its swaps while one is running or listed (2026-10-17: "now it is not
//: fast enough"): each swap step waited up to TRADE_BEAT for the next pass, five or six times over.
export const TRADE_BEAT_BUSY = 1500;
let lastIdlePass = 0;

/** Whether this wallet has a swap running or listed: then passes run every TRADE_BEAT_BUSY. */
function swapsBusy(w) {
  return myOrders(w).some((o) => !o.delegated) || outbox().length > 0
    || myTrades(w).some((t) => t.state !== "done" && t.state !== "aborted");
}

export function startTradeRunner(chain, beat = TRADE_BEAT) {
  if (runnerStarted) return;
  runnerStarted = true;
  runnerChain = chain;
  const run = async () => {
    let worker = null;
    try {
      const src = `setInterval(() => postMessage(0), ${Number(Math.min(beat, TRADE_BEAT_BUSY))});`;
      worker = new Worker(URL.createObjectURL(new Blob([src], {type: "text/javascript"})));
      worker.onmessage = () => { tradePass(); };
    } catch (e) { setInterval(tradePass, Math.min(beat, TRADE_BEAT_BUSY)); }
    if (kicks) kicks.onmessage = () => { tradePass(); };
    // This page answers its listed swaps, so it tells its node it is here (order_presence):
    // every arcade shows them only while it does (2026-10-17).
    setInterval(sayAlive, ALIVE_BEAT);
    // Brought to the front: take the lease now rather than at the next beat.
    try { document.addEventListener("visibilitychange", () => { if (!document.hidden) tradePass(); }); } catch (e) {}
    tradePass();
  };
  run();
}

/** Run a pass now: in this tab if it is the runner, else in whichever tab is. */
export function kickTrades() {
  if (globalThis.__arcadeTradeRunner) tradePass();
  else if (kicks) kicks.postMessage("kick");
}

/* --- imported wallets (plan item 18, 2026-10-20) --------------------------------------------------------
 *
 * "We should be able to import any with private key or 12 word seed phrase, even if that wallet has not been used
 * in Dogecoin arcade." A key (WIF) or a phrase typed into the Wallet page becomes a wallet beside the account's own,
 * by name. Its secret never leaves this browser unsealed: the account keeps a SECOND sealed record on the node
 * (/account/imported), made with `sealText` -- the same AES-256-GCM under PBKDF2-SHA512 at KDF_ITERATIONS as `seal`,
 * under the same password -- and opened with it at sign-in. The main wallet's blob, which older arcades read, is not
 * changed in any way. The node is told the imported ADDRESS only, with a signature by its key, so it can watch it.
 */

/** The record's text sealed with a key made from the password (`keyed`: {key, salt, iterations}). The parameters
 *  travel with it, as `seal`'s do; unlike `seal`, the text is kept exactly (a private key is case-sensitive). */
async function sealText(text, keyed) {
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const sealed = await crypto.subtle.encrypt({name: "AES-GCM", iv: nonce}, keyed.key, enc.encode(text));
  return {kdf: "PBKDF2-SHA512", iterations: keyed.iterations, salt: hex(keyed.salt), nonce: hex(nonce),
          sealed: hex(sealed)};
}

async function openText(blob, keyed) {
  try {
    const plain = await crypto.subtle.decrypt({name: "AES-GCM", iv: unhex(blob.nonce)}, keyed.key,
                                              unhex(blob.sealed));
    return new TextDecoder().decode(plain);
  } catch (e) {
    throw new Error("that password does not open your imported wallets.");
  }
}

/** The key for a record: the password through the same KDF `seal` uses, over the record's own salt (a new record
 *  gets a new random one). */
async function recordKey(password, blob) {
  if (blob && blob.kdf !== "PBKDF2-SHA512") throw new Error("your imported wallets are sealed in a way this page does not know");
  const salt = blob ? unhex(blob.salt) : crypto.getRandomValues(new Uint8Array(16));
  const iterations = blob ? Number(blob.iterations) : KDF_ITERATIONS;
  return {key: await keyFrom(password, salt, iterations), salt, iterations};
}

/** What the node keeps for this account's imports: the sealed record (parsed), when it was written, the main
 *  wallet's sealed blob, each chain's imported addresses with balance and read-back state, the main wallets' names. */
export async function importedOnNode() {
  const got = await fetch("/account/imported", {cache: "no-store"});
  const said = await got.json().catch(() => ({}));
  if (!got.ok) throw new Error(said.detail || "this arcade did not answer about your imported wallets");
  let record = null;
  try { record = said.blob ? JSON.parse(said.blob) : null; } catch (e) { record = null; }
  return {...said, record};
}

/** The account's imported wallets open in this tab: {updated, wallets} or {stale: true}, or null when not open. */
export function importsOpen(account) {
  return openImportsAll()[String(account || "").toLowerCase()] || null;
}

/** Open the record with the password that just opened the main wallet (sign-in, a backup file). Never fails a
 *  sign-in: a record sealed under an earlier password (the Restore page set a new one) is marked `stale`. */
async function openImportsWith(wallet, password) {
  try {
    const said = await importedOnNode();
    if (!said.record) { keepImports(wallet.pubkey, {updated: 0, wallets: []}); return; }
    const keyed = await recordKey(password, said.record);
    let opened;
    try {
      opened = JSON.parse(await openText(said.record, keyed));
    } catch (e) {
      keepImports(wallet.pubkey, {updated: said.updated, stale: true, wallets: []});
      return;
    }
    importKey = {account: wallet.pubkey, ...keyed};
    keepImports(wallet.pubkey, {updated: said.updated, version: said.version,
                                wallets: (opened && opened.wallets) || []});
    await tidyImports(wallet, password);
  } catch (e) { /* the record is read again on the Wallet page */ }
}

/** Open a record sealed under an EARLIER password (after a Restore), with that password. Only opens it in this
 *  tab: the next change seals it again under the current password. */
export async function openStaleImports(wallet, oldPassword) {
  const said = await importedOnNode();
  if (!said.record) throw new Error("you have no imported wallets on this arcade");
  const opened = JSON.parse(await openText(said.record, await recordKey(oldPassword, said.record)));
  keepImports(wallet.pubkey, {updated: said.updated, wallets: (opened && opened.wallets) || [], reseal: true});
  return opened.wallets || [];
}

/** The record's key for a change: the one this page already proved, else from `password` -- which must open the
 *  main wallet to the words open in this tab (so an import is never sealed under a mistyped password nobody could
 *  open again). `{reseal}` when the node's record was sealed under an earlier password and is re-sealed now. */
async function keyForChange(wallet, said, password) {
  const record = said.record;
  if (importKey && importKey.account === wallet.pubkey
      && (!record || hex(importKey.salt) === String(record.salt))) return {keyed: importKey};
  if (!password) {
    const e = new Error("Type your password to seal your imported wallets.");
    e.needsPassword = true;
    throw e;
  }
  if (!said.vault) {
    throw new Error("This arcade keeps no sealed copy of your wallet, so there is no password to seal imported "
      + "wallets under here.");
  }
  const words = await open(said.vault, password).catch(() => {
    const e = new Error("That is not your password.");
    e.needsPassword = true;
    throw e;
  });
  if (signer.normalise(words) !== signer.normalise(wallet.phrase)) {
    throw new Error("That password opens a different wallet than the one open here. Nothing was changed.");
  }
  if (record) {
    const keyed = await recordKey(password, record);
    try {
      await openText(record, keyed);
      importKey = {account: wallet.pubkey, ...keyed};
      return {keyed: importKey};
    } catch (e) {
      // Sealed under an earlier password: sealed again, under this one, from the copy open in this tab.
      const here = importsOpen(wallet.pubkey);
      if (!here || !here.reseal || Number(here.updated) !== Number(said.updated)) {
        throw new Error("Your imported wallets were sealed under an earlier password. Open them with that password "
          + "first; they are sealed under your new one from then on.");
      }
      importKey = {account: wallet.pubkey, ...(await recordKey(password, null))};
      return {keyed: importKey, reseal: here.wallets};
    }
  }
  importKey = {account: wallet.pubkey, ...(await recordKey(password, null))};
  return {keyed: importKey};
}

/* --- the record between arcades (2026-10-21: "an imported wallet needs to be automatically broadcast to
 * other arcades that you are signed in with") ----------------------------------------------------------------------
 * Every version of the record is numbered (one more each change) and signed HERE by the account's login key, over
 * sha256("DogecoinArcade imported wallets\n" + pubkey + "\n" + version + "\n" + sha256(sealed text)), so another
 * arcade can check it against the account's key and keep it (mesh/importshare.py, wallet_imports.record_digest).
 * The plaintext keeps a tombstone for each removed wallet (`gone`: id -> when) and each wallet when it last changed
 * (`at`), so two copies changed apart (two devices, one offline) fold together with nothing lost and nothing removed
 * coming back. */

async function sha256Hex(bytes) {
  return hex(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)));
}

/** The 32 bytes the login key signs for one version of the record (wallet_imports.record_digest). */
export async function recordDigest(pubkey, version, text) {
  const line = `DogecoinArcade imported wallets\n${String(pubkey).toLowerCase()}\n${Number(version)}\n`
    + await sha256Hex(enc.encode(text));
  return new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(line)));
}

async function mergedDigest(pubkey, version, digests) {
  const line = `DogecoinArcade imported wallets merged\n${String(pubkey).toLowerCase()}\n${Number(version)}\n`
    + [...digests].map((d) => String(d).toLowerCase()).sort().join(",");
  return new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(line)));
}

/** What an imported address's key signs so this arcade may ask another for its coins (wallet_imports.coins_digest). */
async function coinsDigest(tag, network, address, host, minute) {
  const line = `DogecoinArcade imported coins\n${String(tag).toLowerCase()}\n${network}\n${address}\n`
    + `${String(host).toLowerCase()}\n${minute}`;
  return new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(line)));
}

const changedAt = (w) => Number(w.at || w.made || 0);

/** Fold another copy's wallets and tombstones into these (both changed in place): the newest change of each
 *  wallet wins, a tombstone newer than a wallet's last change removes it, and one key imported twice is kept once. */
export function foldImports(wallets, gone, other) {
  for (const [id, at] of Object.entries((other && other.gone) || {})) {
    if (!(Number(gone[id] || 0) >= Number(at))) gone[id] = Number(at);
  }
  for (const w of (other && other.wallets) || []) {
    const at = wallets.findIndex((x) => x.id === w.id);
    if (at < 0) {
      if (!wallets.some((x) => x.chain === w.chain && x.address && x.address === w.address)) wallets.push({...w});
    } else if (changedAt(w) > changedAt(wallets[at])) {
      wallets[at] = {...w};
    }
  }
  for (let i = wallets.length - 1; i >= 0; i--) {
    if (Number(gone[wallets[i].id] || 0) >= changedAt(wallets[i])) wallets.splice(i, 1);
  }
}

/** Tombstones are kept 180 days and at most 400. */
function trimGone(gone) {
  const old = Date.now() - 180 * 86400000;
  return Object.fromEntries(Object.entries(gone).filter(([, at]) => Number(at) > old)
    .sort((a, b) => Number(b[1]) - Number(a[1])).slice(0, 400));
}

/** Change the record: read it, open it, fold in any copies of it put aside here (the node keeps them when another
 *  arcade's newer version replaced one written here: accounts.ImportedWallets.adopt), apply `change` to its wallets,
 *  seal it, sign it as the next version, hand it back -- read again and retried when another tab or device wrote it in
 *  between (the node's 409), so neither loses the other's change. `{tidy}`: only save when that changes something
 *  (a copy folded in, or a record from before signatures). */
async function changeImports(wallet, change, password, {tidy = false} = {}) {
  for (let attempt = 0; attempt < 4; attempt++) {
    const said = await importedOnNode();
    const {keyed, reseal} = await keyForChange(wallet, said, password);
    let wallets = reseal ? JSON.parse(JSON.stringify(reseal)) : [];
    const gone = {};
    if (said.record && !reseal) {
      const body = JSON.parse(await openText(said.record, keyed));
      wallets = body.wallets || [];
      Object.assign(gone, body.gone || {});
    }
    const merged = [];
    const before = JSON.stringify([wallets, gone]);
    for (const one of said.siblings || []) {
      let blob, body = null;
      try { blob = JSON.parse(one.blob); } catch (e) { continue; }
      try { body = JSON.parse(await openText(blob, keyed)); } catch (e) {
        if (password) {
          try { body = JSON.parse(await openText(blob, await recordKey(password, blob))); } catch (x) { body = null; }
        }
      }
      if (!body) continue;                 // sealed under a password this page does not have: folded in later
      foldImports(wallets, gone, body);
      merged.push(one.digest);
    }
    if (tidy && said.signed && !merged.length) return null;
    if (tidy && said.signed && JSON.stringify([wallets, gone]) === before) {
      // The copies hold nothing this version lacks (usually the one this arcade wrote before another's newer one
      // came): dropped with no new version, or two arcades would answer each other's versions with newer ones.
      const signature = hex(await wallet.sign(await mergedDigest(wallet.pubkey, Number(said.version), merged)));
      await fetch("/account/imported/siblings/drop", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({merged, merged_signature: signature})});
      return null;
    }
    const out = await change(wallets, gone);
    const text = JSON.stringify(await sealText(JSON.stringify({v: 1, wallets, gone: trimGone(gone)}), keyed));
    const version = Number(said.version || 0) + 1;
    const signature = hex(await wallet.sign(await recordDigest(wallet.pubkey, version, text)));
    const body = {blob: text, version, signature, base: said.updated || 0};
    if (merged.length) {
      body.merged = merged;
      body.merged_signature = hex(await wallet.sign(await mergedDigest(wallet.pubkey, version, merged)));
    }
    const put = await fetch("/account/imported", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    });
    const got = await put.json().catch(() => ({}));
    if (put.status === 409) continue;
    if (!put.ok) throw new Error(got.detail || "your imported wallets were not saved");
    keepImports(wallet.pubkey, {updated: got.updated, version: got.version, wallets});
    return out;
  }
  throw new Error("Your imported wallets kept changing in another tab. Close the other tabs and try again.");
}

/** After the record opens with the password: a record from before signatures is saved again, signed, and copies
 *  put aside are folded in (2026-10-21). Never fails an unlock. */
async function tidyImports(wallet, password) {
  try {
    const said = await importedOnNode();
    if (!said.record || (said.signed && !(said.siblings || []).length)) return;
    await changeImports(wallet, () => {}, password, {tidy: true});
  } catch (e) { /* tried again at the next unlock */ }
}

/** The account's signed record from the arcade it came from (`from`, or the one this node remembers), kept by this
 *  node when it checks. A few seconds at most: a sign-in never waits on another arcade for long. */
async function bringImports(from) {
  try {
    await Promise.race([
      fetch("/account/imported/bring", {method: "POST", headers: {"Content-Type": "application/json"},
                                        body: JSON.stringify({from: from || ""})}),
      new Promise((ok) => setTimeout(ok, 12000))]);
  } catch (e) { /* the mesh brings it later */ }
}

const syncTried = new Set();

/** The imported wallets in the open record that this arcade is not watching for the account (the record came from
 *  another arcade): each registered here with its key's proof (`sync`, an allowance of its own), with a second proof
 *  by the same key that lets this arcade ask the others the account uses for its coins (wallet_imports.coins_digest)
 *  instead of reading the chain back. `said` is importedOnNode()'s answer. How many were registered. */
export async function syncImported(account, said, onStart) {
  const here = importsOpen(account);
  if (!here || here.stale || here.reseal || !said || !said.signed
      || Number(here.updated || 0) !== Number(said.updated || 0)) return 0;
  const running = await chains();
  let n = 0;
  for (const w of here.wallets || []) {
    if (!w.address || (said.no_imports || []).includes(w.chain)) continue;
    const chain = running.find((c) => c.network === w.chain);
    if (!chain) continue;
    if (((said.chains || {})[w.chain] || []).some((x) => x.address === w.address)) continue;
    const key = `${account}:${w.chain}:${w.address}`;
    if (syncTried.has(key)) continue;
    syncTried.add(key);
    if (onStart && !n) onStart();
    try {
      const keys = await importedKeys(w, chain);
      const mine = keys.on[chain.network];
      if (mine.address !== w.address) continue;
      const proof = await coins.importProof(account, chain.network, mine.address, mine.pubkey);
      const body = {chain: chain.network, address: mine.address, kind: keys.kind, pubkey: hex(mine.pubkey),
                    signature: hex(await coins.signHash(mine.key, proof)), sync: true};
      if (said.host && said.tag) {
        const minute = Math.floor(Date.now() / 60000);
        const digest = await coinsDigest(said.tag, chain.network, mine.address, said.host, minute);
        body.coins = {minute, asker: said.host, signature: hex(await coins.signHash(mine.key, digest))};
      }
      const asked = await fetch("/account/imported/address", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
      if (asked.ok) n++;
    } catch (e) { /* tried again on the next page */ }
  }
  return n;
}

const KIND_IDS = {legacy: "p2pkh", "p2sh-p2wpkh": "p2sh-p2wpkh", p2wpkh: "p2wpkh"};

/** The keys an imported wallet signs with on its chain, shaped as a wallet's `on[network]` (so `checked` and
 *  `confirm` take it as they take the account's own): from its private key, or its phrase at the arcade's own
 *  path (m/44'/<coin>'/0'/0/0), or the SegWit kind's path (m/49', m/84'), as the main wallet derives them. */
export async function importedKeys(entry, chain) {
  const network = chain.network;
  const kind = addressKinds(network).find((k) => k.id === (entry.addr_kind || "legacy")) || {id: "legacy"};
  let key, pubkey;
  if (entry.kind === "wif") {
    const got = await coins.fromWif(entry.secret, network);
    if (kind.id !== "legacy" && !got.compressed) throw new Error("a SegWit address needs a compressed key");
    ({key, pubkey} = got);
  } else {
    const wrong = await signer.complaint(entry.secret);
    if (wrong) throw new Error(wrong);
    const seed = await signer.toSeed(signer.normalise(entry.secret));
    const made = kind.id === "p2wpkh" ? await coins.nativeSegwitKey(seed, network, 0)
      : kind.id === "p2sh-p2wpkh" ? await coins.p2shSegwitKey(seed, network, 0)
      : await coins.coinKey(seed, network, 0);
    ({key, pubkey} = made);
  }
  const address = kind.id === "p2wpkh" ? await coins.p2wpkhAddress(pubkey, kind.hrp)
    : kind.id === "p2sh-p2wpkh" ? await coins.p2shSegwitAddress(pubkey, kind.scripthash)
    : await coins.address(pubkey, chain.version);
  const keys = {network, version: chain.version, key, pubkey, address, ...(kind.id === "p2sh-p2wpkh" ? {nested: true} : {})};
  return {on: {[network]: keys}, kind: KIND_IDS[kind.id] || "p2pkh"};
}

/** "Wallet N", N the lowest number no wallet name on that chain uses (2026-10-20). */
export function nextWalletName(names) {
  const used = new Set((names || []).map((n) => (/^Wallet (\d+)$/.exec(String(n || "")) || [])[1])
    .filter(Boolean).map(Number));
  let n = 1;
  while (used.has(n)) n++;
  return "Wallet " + n;
}

const cleanName = (text) => String(text || "").split(/\s+/).filter(Boolean).join(" ").slice(0, 40);

/** Import a wallet on `chain` ({network, version}): `kind` "wif" or "phrase", its `secret`, an optional `name`, and
 *  `addrKind` (a SegWit kind where the chain has them). `otherNames`: every other wallet name on that chain, for the
 *  next free "Wallet N". The node registers the address first: an import it refuses (a rescan running on that
 *  chain, the cap) keeps NOTHING. Then the record is sealed with the secret in it; should that fail, the address is
 *  taken off again. Returns the node's word on its read-back and the new wallet. */
export async function importWallet(chain, {kind, secret, name = "", addrKind = "legacy", otherNames = [], password = ""}) {
  return working(async () => {
    const wallet = await opened(chain);
    if (!wallet) throw new Error("Unlock your wallet first.");
    const text = kind === "phrase" ? signer.normalise(secret) : String(secret || "").trim();
    if (kind === "phrase") {
      const wrong = await signer.complaint(text);
      if (wrong) throw new Error(wrong);
    } else if (kind !== "wif") throw new Error("Paste a private key or a seed phrase.");
    const entry = {id: hex(crypto.getRandomValues(new Uint8Array(8))), chain: chain.network, kind, secret: text,
                   addr_kind: addrKind || "legacy", name: "", made: Date.now()};
    const keys = await importedKeys(entry, chain);
    const mine = keys.on[chain.network];
    if (wallet.on[chain.network] && wallet.on[chain.network].address === mine.address) {
      throw new Error("That is your arcade wallet's own key on this coin, already here.");
    }
    // Before anything is registered: the password, when this page does not hold the record's key yet.
    const said = await importedOnNode();
    await keyForChange(wallet, said, password);
    const here = importsOpen(wallet.pubkey);
    const dupe = ((here && here.wallets) || []).find((w) => w.chain === chain.network && w.address === mine.address);
    if (dupe) throw new Error(`That wallet is already here, as “${dupe.name}”.`);
    const proof = await coins.importProof(wallet.pubkey, chain.network, mine.address, mine.pubkey);
    const signature = hex(await coins.signHash(mine.key, proof));
    const asked = await fetch("/account/imported/address", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({chain: chain.network, address: mine.address, kind: keys.kind,
                            pubkey: hex(mine.pubkey), signature}),
    });
    const answer = await asked.json().catch(() => ({}));
    if (!asked.ok) {
      const e = new Error(answer.detail || "this arcade did not take that wallet");
      e.busy = !!answer.busy;
      throw e;
    }
    entry.address = mine.address;
    try {
      await changeImports(wallet, (wallets) => {
        if (wallets.some((w) => w.chain === chain.network && w.address === mine.address)) return;
        const names = otherNames.concat(wallets.filter((w) => w.chain === chain.network).map((w) => w.name));
        entry.name = cleanName(name) || nextWalletName(names);
        wallets.push(entry);
      }, password);
    } catch (e) {
      if (!answer.already) {
        fetch("/account/imported/forget", {method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({chain: chain.network, address: mine.address})}).catch(() => {});
      }
      throw e;
    }
    return {...answer, wallet: {...entry, secret: undefined}};
  });
}

/** Rename an imported wallet ("" gives it the lowest free "Wallet N"). */
export async function renameImported(chain, id, name, {otherNames = [], password = ""} = {}) {
  const wallet = await opened(chain);
  if (!wallet) throw new Error("Unlock your wallet first.");
  return changeImports(wallet, (wallets) => {
    const it = wallets.find((w) => w.id === id);
    if (!it) throw new Error("That wallet is not in your imported wallets any more.");
    it.name = "";
    it.at = Date.now();
    const names = otherNames.concat(wallets.filter((w) => w.chain === it.chain && w.id !== id).map((w) => w.name));
    it.name = cleanName(name) || nextWalletName(names);
    return it.name;
  }, password);
}

/** Take an imported wallet off: out of the sealed record first, then the node stops watching its address. Its coins
 *  are not touched. */
export async function removeImported(chain, id, {password = ""} = {}) {
  const wallet = await opened(chain);
  if (!wallet) throw new Error("Unlock your wallet first.");
  let gone = null;
  await changeImports(wallet, (wallets, tombs) => {
    const at = wallets.findIndex((w) => w.id === id);
    if (at >= 0) gone = wallets.splice(at, 1)[0];
    tombs[id] = Math.max(Date.now(), gone ? changedAt(gone) + 1 : 0);   // so a copy elsewhere does not bring it back
  }, password);
  if (gone && gone.address) {
    await fetch("/account/imported/forget", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({chain: gone.chain, address: gone.address})}).catch(() => {});
  }
  return gone ? {...gone, secret: undefined} : null;
}

/** Name the account's MAIN wallet on one chain (not a secret: kept by the node). */
export async function nameMainWallet(network, name) {
  const asked = await fetch("/account/imported/name", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({chain: network, name: cleanName(name)}),
  });
  const said = await asked.json().catch(() => ({}));
  if (!asked.ok) throw new Error(said.detail || "that name was not saved");
  return said.name;
}

/** Whether this page holds the record's key (so a change needs no password). */
export function importsKeyHere(account) {
  return !!(importKey && importKey.account === String(account || "").toLowerCase());
}

/** Open the record in this tab with the account's password (a tab that was unlocked before imports existed, or
 *  whose copy was lost). A password that opens the main wallet and not the record means the record was sealed under
 *  an earlier one: said so (`stale`), and `openStaleImports` takes that earlier password. */
export async function openImports(wallet, password) {
  const said = await importedOnNode();
  if (!said.record) { keepImports(wallet.pubkey, {updated: 0, wallets: []}); return []; }
  const keyed = await recordKey(password, said.record);
  try {
    const opened = JSON.parse(await openText(said.record, keyed));
    importKey = {account: wallet.pubkey, ...keyed};
    keepImports(wallet.pubkey, {updated: said.updated, version: said.version,
                                wallets: (opened && opened.wallets) || []});
    await tidyImports(wallet, password);
    return (importsOpen(wallet.pubkey) || {}).wallets || opened.wallets || [];
  } catch (e) {
    const main = said.vault ? await open(said.vault, password).catch(() => null) : null;
    if (main && signer.normalise(main) === signer.normalise(wallet.phrase)) {
      keepImports(wallet.pubkey, {updated: said.updated, stale: true, wallets: []});
      const stale = new Error("Your imported wallets were sealed under an earlier password (before it was changed). "
        + "Type that earlier password to open them.");
      stale.stale = true;
      throw stale;
    }
    throw new Error("That is not your password.");
  }
}
