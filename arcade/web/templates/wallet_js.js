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

export const MIN_PASSWORD = 12;

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
  return {
    phrase,
    seed,
    pubkey: account.pubkey,
    sign: account.sign,
    coinKey: coin.key,
    coinPubkey: coin.pubkey,
    address: await coins.address(coin.pubkey, version),
  };
}

/* --- the two things a person does ---------------------------------------- */

export async function available(tag) {
  return (await fetch(`/signup/${encodeURIComponent(tag)}`)).json();
}

export async function signUp(tag, password, options) {
  return working(() => _signUp(tag, password, options));
}

async function _signUp(tag, password, {network, version}) {
  if ((password || "").length < MIN_PASSWORD) {
    throw new Error(`a password of at least ${MIN_PASSWORD} characters. `
      + "It is what opens your wallet from anywhere, and nobody can reset it.");
  }
  const phrase = await signer.generate();
  const wallet = await walletFrom(phrase, network, version);
  const blob = await seal(phrase, password);
  const answer = await fetch("/signup", {
    method: "POST", headers: {"Content-Type": "application/json"},
    // The coin public key travels too: a Class B payload puts it in every
    // output so the dust stays spendable by whoever sent it, and the node
    // cannot derive it from anything it holds.
    body: JSON.stringify({tag, pubkey: wallet.pubkey,
                          address: wallet.address,
                          coin_pubkey: coinsHex(wallet.coinPubkey), blob}),
  });
  const said = await answer.json();
  if (!answer.ok) throw new Error(said.detail || "that did not work");
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
  if (wallet.pubkey !== said.pubkey) {
    // The blob opened, and produced a different key than the node has on
    // file. That is not a wrong password -- it is a wallet that does not
    // match its own name, and going on would sign in as somebody else.
    throw new Error("that wallet does not match this name. Nothing was opened.");
  }
  // Prove it to the node the ordinary way: a signature over a nonce.
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
  return {...result, tag: said.tag, address: said.address, wallet};
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

  // Sign the bytes the node handed over, and nothing else. The
  // transaction is never parsed here: a signer that reads what it signs is
  // a signer that can be talked into reading it wrongly. What protects the
  // person is that the node showed what it was doing, and that it
  // broadcasts what it offered rather than what comes back.
  const signatures = [];
  for (const sighash of offer.sighashes) {
    signatures.push(coinsHex(await coins.signInput(wallet.coinKey,
                                                   unhex(sighash))));
  }
  const done = await fetch("/account/sign", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({offer: offer.offer, signatures,
                          pubkey: coinsHex(wallet.coinPubkey)}),
  });
  const said = await done.json();
  if (!done.ok) throw new Error(said.detail || "the node would not take it");
  return {...said, fee: offer.fee, what: offer.what};
}

const coinsHex = (bytes) => [...new Uint8Array(bytes)]
  .map((b) => b.toString(16).padStart(2, "0")).join("");

/** What the node knows about this account: address, coins, name. */
export async function state() {
  return (await fetch("/account")).json();
}

/** Wait for coins to arrive, so a claim is not offered before it can pay. */
export async function waitForCoins(seconds = 120) {
  const until = Date.now() + seconds * 1000;
  while (Date.now() < until) {
    const said = await state();
    if (said.balance > 0) return said;
    await new Promise((r) => setTimeout(r, 3000));
  }
  return state();
}

/* --- sending coins -------------------------------------------------------
 *
 * The same handshake as a claim, carrying money. Two calls rather than
 * one, and deliberately: `offer` comes back with who is being paid, how
 * much and what it costs, and nothing is signed until somebody has been
 * shown that and said yes. A one-call send would be a page that spends
 * without asking.
 */

export async function offerSend(to, amount) {
  return working(async () => {
    const asked = await fetch("/account/send", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({to, amount}),
    });
    const offer = await asked.json();
    if (!asked.ok) throw new Error(offer.detail || "that cannot be sent");
    return offer;
  });
}

export async function confirm(wallet, offer) {
  return working(async () => {
    const signatures = [];
    for (const sighash of offer.sighashes) {
      signatures.push(coinsHex(await coins.signInput(wallet.coinKey,
                                                     unhex(sighash))));
    }
    const done = await fetch("/account/sign", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({offer: offer.offer, signatures,
                            pubkey: coinsHex(wallet.coinPubkey)}),
    });
    const said = await done.json();
    if (!done.ok) throw new Error(said.detail || "the node would not take it");
    return said;
  });
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
    const signatures = [];
    for (const sighash of offer.sighashes) {
      signatures.push(coinsHex(await coins.signInput(wallet.coinKey,
                                                     unhex(sighash))));
    }
    const done = await fetch("/account/sign", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({offer: offer.offer, signatures,
                            pubkey: coinsHex(wallet.coinPubkey)}),
    });
    const said = await done.json();
    if (!done.ok) throw new Error(said.detail || "the node would not take it");
    return {...said, fee: offer.fee};
  });
}
