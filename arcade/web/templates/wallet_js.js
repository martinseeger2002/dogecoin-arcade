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

/** Prove a wallet to the node the only way a node believes, and open it
 *  from here -- the same two requests whichever way the words arrived.
 */
async function seatWith(wallet) {
  const challenge = await (await fetch("/auth/challenge")).json();
  const signature = await wallet.sign(
    signer.loginMessage(challenge.origin, challenge.nonce));
  const opened = await fetch("/auth/login", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({pubkey: wallet.pubkey, nonce: challenge.nonce,
                          signature: hex(signature), join: true}),
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
  return {...said, fee: offer.fee, what: offer.what};
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
 * What is signed is never parsed here. The transaction is the node's; it
 * said what it was doing before anybody pressed anything, and it
 * broadcasts what it OFFERED rather than what comes back.
 */
async function signOffer(wallet, offer) {
  const keys = keysOn(wallet, offer.chain
                      || (wallet.on && Object.keys(wallet.on)[0]));
  const signatures = [];
  for (const sighash of offer.sighashes) {
    signatures.push(coinsHex(await coins.signInput(keys.key, unhex(sighash))));
  }
  const done = await fetch("/account/sign", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({offer: offer.offer, signatures,
                          pubkey: coinsHex(keys.pubkey)}),
  });
  const said = await done.json();
  if (!done.ok) throw new Error(said.detail || "the node would not take it");
  return said;
}

export async function confirm(wallet, offer) {
  return working(() => signOffer(wallet, offer));
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

/** The shape every one of these has: ask for an offer, sign it, send it. */
export async function signAndSend(wallet, where, body) {
  return working(async () => {
    const asked = await fetch(where, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    const offer = await asked.json();
    if (!asked.ok) throw new Error(offer.detail || "that cannot be done");
    const said = await signOffer(wallet, offer);
    return {...said, fee: offer.fee};
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

export async function setUp(wallet, identity, tag, {onStep} = {}) {
  const step = (what, how) => { if (onStep) onStep({what, how}); };
  const out = {claimed: "", announced: "", trouble: []};

  step("coins", "waiting");
  const said = await waitForCoins(180);
  if (!((said.balance || 0) + (said.incoming || 0))) {
    out.trouble.push("no coins arrived, so your name is not claimed yet");
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
  step("key", "publishing");
  try {
    const mail = await import("/messaging.js");
    out.announced = (await mail.announce(wallet, identity, tag)).txid;
    step("key", "on its way");
  } catch (e) {
    out.trouble.push(`your key was not published: ${e.message || e}`);
    step("key", "failed");
  }
  return out;
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

export function remember(phrase) {
  try { sessionStorage.setItem(OPEN_WALLET, phrase); } catch (e) {}
}

export function forgetOpen() {
  try { sessionStorage.removeItem(OPEN_WALLET); } catch (e) {}
}

/** The wallet unlocked in this tab, or null. */
export async function opened(chain) {
  let phrase = null;
  try { phrase = sessionStorage.getItem(OPEN_WALLET); } catch (e) {}
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
