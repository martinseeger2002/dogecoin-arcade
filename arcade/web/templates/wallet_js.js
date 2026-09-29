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
export async function chains() {
  try {
    return (await (await fetch("/account")).json()).chains || [];
  } catch (e) { return []; }
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
  return {...result, tag: said.tag, address: said.address, wallet};
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

export async function offerSend(to, amount, chain) {
  return working(async () => {
    const asked = await fetch("/account/send", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({to, amount, chain: chain || ""}),
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
async function signOffer(wallet, offer, given = null) {
  const keys = given || keysOn(wallet, offer.chain
                      || (wallet.on && Object.keys(wallet.on)[0]));
  const shown = await coins.verifyOffer(offer, keys);
  const signatures = [];
  for (const sighash of shown.hashes) {
    signatures.push(coinsHex(await coins.signInput(keys.key, unhex(sighash))));
  }
  const done = await fetch("/account/sign", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({offer: offer.offer, signatures,
                          pubkey: coinsHex(keys.pubkey)}),
  });
  const said = await done.json();
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

export async function confirm(wallet, offer) {
  return working(() => signOffer(wallet, offer));
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
    const opened = await askJson("/account/pools/open",
                                 {chain, index, pubkey: coinsHex(keys.pubkey)});
    if (!opened.ok) throw new Error(opened.said.detail || "the pool could not be opened");
    const funding = await askJson("/account/pools/fund", nft ? {
      chain, pool: keys.address, kind: "nft", pieces, price: String(spec.price),
      days: spec.days || 0, bound: spec.game ? String(spec.game) : "", once: !!spec.once,
      claim_hash: await claimHash(phrase)} : {
      chain, pool: keys.address, property_id: spec.token, lot: String(spec.lot),
      count: Number(spec.lots), price: String(spec.price), days: spec.days || 0,
      once: !!spec.once,
      bound: spec.game ? String(spec.game) : "", claim_hash: await claimHash(phrase)});
    if (!funding.ok) throw new Error(funding.said.detail || "the pool could not be funded");
    onStep(nft ? "Putting coins into the pool\u2026" : "Moving the tokens and coins into the pool\u2026");
    const sent = await signOffer(wallet, funding.said);
    for (let n = 0; n < pieces.length; n++) {
      onStep(`Moving piece ${n + 1} of ${pieces.length} into the pool\u2026`);
      const moved = await askJson("/account/nft/send", {piece: pieces[n], to: keys.address, chain});
      if (!moved.ok) throw new Error(moved.said.detail || "a piece could not be moved");
      await signOffer(wallet, moved.said);
    }
    const built = await askJson("/account/pools/legs", {chain, pool: keys.address, fund: sent.txid});
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
export async function offerPrize(pool, lot, sigs, secret, page, chain) {
  return working(async () => {
    const r = await askJson("/account/prize", {pool, lot, signatures: sigs, secret, page,
                                               chain: chain || ""});
    if (!r.ok) throw new Error(r.said.detail || "that prize cannot be claimed");
    return {...r.said, _ask: {pool, lot, signatures: sigs, secret, page, chain: chain || ""}};
  });
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
    const keys = await poolKeys(wallet, chain, Number(info.index));
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

export async function fill(wallet, leg, offer) {
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
                            signatures, pubkey: coinsHex(keys.pubkey)}),
    });
    const said = await done.json();
    if (!done.ok) throw new Error(said.detail || "the node would not take it");
    return {...said, fee: shown.fee, says: shown.says};
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

async function shelfName(page) {
  let who = "anon";
  try {
    const asked = await fetch("/account", {cache: "no-store"});
    if (asked.ok) who = (await asked.json()).address || "anon";
  } catch (e) { /* a stranger's browser is its own shelf */ }
  return "arcade.pagestore." + who + "." + page;
}

export async function storageDoor(page, ask) {
  const op = String(ask.op || "");
  const name = await shelfName(page);
  let items = {};
  try { items = JSON.parse(localStorage.getItem(name) || "{}"); }
  catch (e) { items = {}; }
  if (op === "load") return {items: items};
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
    throw new Error("op must be set, remove or clear");
  }
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
export async function offerReact(txid, kind, amount) {
  return working(async () => {
    const asked = await fetch("/account/react", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({txid, kind, amount: String(amount || "")}),
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

export async function setUp(wallet, identity, tag, {onStep, faucetRefusal} = {}) {
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

  step("name", "claiming");
  try {
    out.claimed = (await claim(wallet, tag)).txid;
    step("name", "on its way");
  } catch (e) {
    out.trouble.push(`the name could not be claimed: ${e.message || e}`);
    step("name", "failed");
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
  if (out.claimed || out.announced) {
    const seen = await settle(!!out.claimed, !!out.announced, tag, step);
    if (out.claimed && !seen.name) {
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

/* Unlocked once, unlocked everywhere (2026-09-26: "If you unlock on one
 * page, it should stay unlocked on all pages"). The open wallet lives in THIS
 * tab's sessionStorage, which a navigation keeps and a new tab does not have. So
 * the tabs of this site tell each other, over a BroadcastChannel (same origin
 * only: an inscribed page is on the pages host and cannot hear it): a tab that
 * opens locked asks, and any unlocked tab answers. Nothing is written to disk --
 * close the last tab and it is locked -- and locking one locks them all. */
const TABS = (typeof BroadcastChannel !== "undefined")
  ? new BroadcastChannel("arcade-open-wallet") : null;
if (TABS) {
  TABS.onmessage = (e) => {
    const m = e.data || {};
    if (m.ask === "phrase") {
      let phrase = null;
      try { phrase = sessionStorage.getItem(OPEN_WALLET); } catch (x) {}
      if (phrase) TABS.postMessage({phrase});
    } else if (m.phrase) {
      try {
        if (!sessionStorage.getItem(OPEN_WALLET)) sessionStorage.setItem(OPEN_WALLET, m.phrase);
      } catch (x) {}
    } else if (m.locked) {
      try { sessionStorage.removeItem(OPEN_WALLET); } catch (x) {}
    }
  };
}

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
  try { sessionStorage.removeItem(OPEN_WALLET); } catch (e) {}
  try { if (TABS) TABS.postMessage({locked: true}); } catch (e) {}
}

/** The wallet unlocked in this tab, or null. */
export async function opened(chain) {
  let phrase = null;
  try { phrase = sessionStorage.getItem(OPEN_WALLET); } catch (e) {}
  if (!phrase) {
    phrase = await fromAnotherTab();
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
