/* Private messages, opened and sealed in the browser.
 *
 * Until now the messaging identity came from the NODE's wallet: it signed
 * a fixed message with an address it owned and the X25519 key fell out of
 * that signature. An account with its own keys cannot use that -- the node
 * cannot sign for a key it does not hold -- and should not: a node that
 * can derive your identity can read your messages.
 *
 * So the identity comes from the same twelve words as everything else, at
 * the arcade's own branch (`arcade/seed.py`, purpose 24946'), and the
 * node's part shrinks to what it should always have been: it scans the
 * chain, keeps the candidate payloads, and hands them over. Opening them
 * happens here or not at all.
 *
 * **The format is not changed, and that is the constraint that decides
 * everything else here.** What is on the chain is a NaCl sealed box around
 * an authenticated box:
 *
 *     sealed_box(recipient, sender_public || box(sender, recipient, plain))
 *
 * and `sealed_box` is libsodium's `crypto_box_seal`: an ephemeral key, a
 * nonce that is **Blake2b-24 of the ephemeral and recipient public keys**,
 * and the ciphertext after it. WebCrypto has none of X25519,
 * XSalsa20-Poly1305 or Blake2b, so two vendored libraries do that work
 * (`vendor/PROVENANCE.md`). Inventing a different envelope for browser
 * accounts would mean two kinds of message on one chain and a wallet that
 * can read half of them.
 *
 * A nonce derived differently opens for nobody, silently, and looks
 * exactly like a wrong key -- which is why `sealedBox` below is written
 * against libsodium's definition rather than against what is convenient,
 * and why the test seals in Python and opens here.
 */

import "/vendor/tweetnacl.js";              // sets globalThis.nacl
import { blake2b } from "/vendor/hashes/blake2b.js";
import * as signer from "/signin.js";

const nacl = globalThis.nacl;

export const hex = (bytes) => [...new Uint8Array(bytes)]
  .map((b) => b.toString(16).padStart(2, "0")).join("");
export const unhex = (text) => new Uint8Array(
  (text.match(/../g) || []).map((pair) => parseInt(pair, 16)));

/* --- the identity --------------------------------------------------------
 * The same words, a different branch. Not the coin key and not the key
 * that signs into the node: the key that reads somebody's messages should
 * not be the key that spends their coins wearing a hat.
 */

export async function identity(phrase) {
  const seed = await signer.toSeed(phrase);
  const secret = await signer.derive(seed, signer.MESSAGING_BRANCH);
  const pair = nacl.box.keyPair.fromSecretKey(secret);
  return {secret: pair.secretKey, publicKey: pair.publicKey,
          fingerprint: await fingerprint(pair.publicKey)};
}

/** The short, readable identifier the rest of the application uses.
 *
 * SHA-256 of the public key, first eight bytes, in groups of four --
 * exactly what `keys.fingerprint_of` produces, because a fingerprint that
 * differs between the two halves is a fingerprint nobody can read aloud
 * to check.
 */
export async function fingerprint(publicKey) {
  const digest = new Uint8Array(
    await crypto.subtle.digest("SHA-256", publicKey));
  return [...digest.slice(0, 8)]
    .map((b) => b.toString(16).padStart(2, "0")).join("")
    .match(/..../g).join(" ");
}

/* --- crypto_box_seal, as libsodium defines it ---------------------------- */

const SEAL_NONCE = 24;

function sealNonce(ephemeralPublic, recipientPublic) {
  const both = new Uint8Array(64);
  both.set(ephemeralPublic);
  both.set(recipientPublic, 32);
  return blake2b(both, {dkLen: SEAL_NONCE});
}

export function sealedBox(message, recipientPublic) {
  const ephemeral = nacl.box.keyPair();
  const nonce = sealNonce(ephemeral.publicKey, recipientPublic);
  const boxed = nacl.box(message, nonce, recipientPublic, ephemeral.secretKey);
  const out = new Uint8Array(32 + boxed.length);
  out.set(ephemeral.publicKey);
  out.set(boxed, 32);
  return out;
}

export function openSealedBox(sealed, me) {
  if (sealed.length < 32 + 16) return null;
  const ephemeral = sealed.subarray(0, 32);
  const nonce = sealNonce(ephemeral, me.publicKey);
  return nacl.box.open(sealed.subarray(32), nonce, ephemeral, me.secret);
}

/* --- the envelope inside it ----------------------------------------------
 * `sender_public || box(sender, recipient, header || message)`. The inner
 * box is authenticated, so opening it proves who sent it -- which is the
 * whole reason there are two layers rather than one.
 */

export const SENDER_KEY_LEN = 32;

export function openEnvelope(envelope, me) {
  if (envelope.length < SENDER_KEY_LEN + 16) return null;
  const senderPublic = envelope.subarray(0, SENDER_KEY_LEN);
  const inner = envelope.subarray(SENDER_KEY_LEN);
  // crypto_box prepends its own 24-byte random nonce.
  const opened = nacl.box.open(inner.subarray(24), inner.subarray(0, 24),
                               senderPublic, me.secret);
  if (opened === null) return null;
  return {sender: senderPublic, plain: opened};
}

export function sealEnvelope(plain, me, recipientPublic) {
  const nonce = nacl.randomBytes(24);
  const boxed = nacl.box(plain, nonce, recipientPublic, me.secret);
  const envelope = new Uint8Array(SENDER_KEY_LEN + 24 + boxed.length);
  envelope.set(me.publicKey);
  envelope.set(nonce, SENDER_KEY_LEN);
  envelope.set(boxed, SENDER_KEY_LEN + 24);
  return sealedBox(envelope, recipientPublic);
}

/* --- the header, and the padding it exists to undo -----------------------
 *
 * What the scanner stores is `header || ciphertext || padding`, not a
 * sealed box. The header is cleartext -- it has to be readable before
 * anything is decrypted, so messages can be recognised at all -- and it
 * carries `clen`, the exact ciphertext length, which is there to discard
 * the NUL padding Class B adds to fill its last output.
 *
 * Skipping that is how the first message sent from a browser could not be
 * opened by a browser: the padded bytes were handed to the box, the MAC
 * failed, and it looked exactly like a message for somebody else.
 *
 * Being cleartext, the header is also tamperable, which is why every field
 * in it is copied inside the authenticated plaintext and compared on open.
 */

export const MAGIC = [0x61, 0x72, 0x63, 0x6d];     // "arcm"
export const VERSION = 1;
export const TYPE_SINGLE = 1;

export function readHeader(payload) {
  if (payload.length < 6) return null;
  for (let i = 0; i < 4; i++) if (payload[i] !== MAGIC[i]) return null;
  if (payload[4] !== VERSION) return null;
  const type = payload[5];
  if (type !== TYPE_SINGLE) return null;      // chunks and API are not read here
  if (payload.length < 8) return null;
  const clen = (payload[6] << 8) | payload[7];
  return {type, clen, length: 8, bound: payload.subarray(0, 6)};
}

function sameBytes(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

/** Both layers and the framing: what is on the chain, opened. */
export function openMessage(payload, me) {
  const header = readHeader(payload);
  if (header === null) return null;
  let blob = payload.subarray(header.length);
  if (header.clen) {
    if (header.clen > blob.length) return null;
    blob = blob.subarray(0, header.clen);     // discard Class B padding
  }
  const envelope = openSealedBox(blob, me);
  if (envelope === null) return null;
  const opened = openEnvelope(envelope, me);
  if (opened === null) return null;
  // The cleartext header, compared against the copy sealed inside. A
  // message whose framing was altered on the way is refused rather than
  // shown with the alteration.
  const inside = opened.plain.subarray(0, header.bound.length);
  if (!sameBytes(inside, header.bound)) return null;
  return {sender: opened.sender,
          plain: opened.plain.subarray(header.bound.length)};
}

/* --- what the node hands over, and what this does with it ----------------
 *
 * The node cannot tell which messages are whose, so it hands over what it
 * has seen and the browser finds out by trying. Most will not open; that
 * costs one failed authentication each and nothing on the chain.
 *
 * The cursor is kept here rather than there, for the same reason
 * everything else is: a node that remembered which messages an account had
 * read would be a node that knows which messages are that account's.
 */

const READ = "arcade-messages";
const BOOK = "book";

//: One version number for one database. Two openers asking for different
//: versions is how an upgrade silently never runs -- the second one is
//: refused as "version change" and the store it wanted is simply absent.
const SHELVES = 2;

function shelf(mode) {
  return new Promise((ok, no) => {
    const open = indexedDB.open(READ, SHELVES);
    open.onupgradeneeded = () => make(open.result);
    open.onerror = () => no(open.error);
    open.onsuccess = () => ok(open.result.transaction(
      ["mail", "marks"], mode));
  });
}

function make(db) {
  if (!db.objectStoreNames.contains("mail")) {
    db.createObjectStore("mail", {keyPath: "txid"});
  }
  if (!db.objectStoreNames.contains("marks")) db.createObjectStore("marks");
  if (!db.objectStoreNames.contains(BOOK)) {
    db.createObjectStore(BOOK, {keyPath: "tag"});
  }
}

const awaited = (request) => new Promise((ok, no) => {
  request.onsuccess = () => ok(request.result);
  request.onerror = () => no(request.error);
});

export async function cursorFor(pubkey) {
  try {
    const tx = await shelf("readonly");
    return (await awaited(tx.objectStore("marks").get(`cursor:${pubkey}`))) || 0;
  } catch (e) { return 0; }
}

async function rememberCursor(pubkey, cursor) {
  const tx = await shelf("readwrite");
  await awaited(tx.objectStore("marks").put(cursor, `cursor:${pubkey}`));
}

async function keep(message) {
  const tx = await shelf("readwrite");
  await awaited(tx.objectStore("mail").put(message));
}

export async function inbox() {
  const tx = await shelf("readonly");
  const all = await awaited(tx.objectStore("mail").getAll());
  return all.sort((a, b) => (b.when || 0) - (a.when || 0));
}

/* Anything of ours that is in the pool and not in a block.
 *
 * The node hands candidates out in `rowid` order and PROMOTES a mempool
 * row in place when its block arrives -- same row, same rowid, height
 * filled in (`store.add_candidate`). A browser that only ever asks for
 * rows after its cursor therefore sees every message exactly once, at
 * whatever height it had the moment it was handed over, and a message
 * read out of the pool would say "not in a block yet" for ever.
 *
 * So the scan rewinds to the oldest thing of ours that is still pending
 * and reads that window again. The node is told a cursor and nothing
 * else: the window it re-reads contains everybody's traffic, so asking
 * for it says nothing about which rows in it are ours. Asking about our
 * own txids by name would be the leak D-155 exists to avoid.
 *
 * Bounded by time rather than by rows: something that never confirmed is
 * stopped waiting for after six hours, and says so in the page.
 */
const PENDING_HOURS = 6;

async function rewindTo(after) {
  const now = Math.floor(Date.now() / 1000);
  let oldest = after;
  for (const letter of await inbox()) {
    if (letter.height) continue;                   // already in a block
    if (letter.when && now - letter.when > PENDING_HOURS * 3600) continue;
    // `cursor` once the node has handed this row over; before that -- an
    // outgoing message this browser has only just broadcast -- the cursor
    // it was sent at, which is the last row that can NOT be it.
    const seen = letter.cursor || letter.from_cursor || 0;
    if (seen && seen - 1 < oldest) oldest = seen - 1;
  }
  return Math.max(0, oldest);
}

/** Fetch what the node has seen since last time, and open what is ours. */
export async function collect(me, {onProgress} = {}) {
  let after = await cursorFor(hex(me.publicKey));
  if (!after) {
    // A device that has never synced starts where the account was made,
    // not at the beginning of the chain: nobody could have written to a
    // key that did not exist yet. This is the only saving available that
    // costs no privacy -- the node already knows when an account signed
    // up, so being told where to start tells it nothing new (D-155).
    try {
      const said = await (await fetch("/account")).json();
      after = said.mail_from || 0;
    } catch (e) { after = 0; }
  }
  // Everything this browser has sent and not yet seen in a block, by the
  // txid the node gave back. A candidate matching one of these is our own
  // message coming back off the chain, which is the only confirmation
  // available: it is sealed to THEM, so it will never open here.
  const awaiting = new Map();
  for (const letter of await inbox()) {
    if (letter.mine && !letter.height) awaiting.set(letter.txid, letter);
  }

  const highest = after;
  after = await rewindTo(after);
  let opened = 0, looked = 0;
  for (;;) {
    const answer = await fetch(`/account/messages?after=${after}&limit=200`);
    if (!answer.ok) throw new Error("the node would not answer");
    const said = await answer.json();
    for (const candidate of said.candidates) {
      const ours = awaiting.get(candidate.txid);
      if (ours) {
        await keep({...ours, cursor: candidate.cursor,
                    height: candidate.height,
                    when: candidate.height ? candidate.when : ours.when});
        continue;                     // sealed to them; nothing to open
      }
      looked += 1;
      const out = openMessage(unhex(candidate.payload), me);
      if (out !== null) {
        await keep({
          txid: candidate.txid,
          cursor: candidate.cursor,
          when: candidate.when,
          height: candidate.height,
          from_address: candidate.from_address,
          sender: hex(out.sender),
          peer: hex(out.sender),
          mine: false,
          body: hex(out.plain),
        });
        opened += 1;
      }
    }
    after = said.cursor;
    // The cursor only ever goes forward. Rewinding to re-read the pool is
    // a read, not a rewind of what has been seen -- storing the lower
    // number would make every later scan start from there.
    await rememberCursor(hex(me.publicKey), Math.max(highest, after));
    if (onProgress) onProgress({looked, opened, more: said.more});
    if (!said.more) break;
  }
  return {looked, opened, cursor: after};
}

/** Publish this account's messaging key, so anybody can write to it. */
export async function announce(wallet, me, tag) {
  const coins = await import("/coins.js");
  const asked = await fetch("/account/announce", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({key: hex(me.publicKey), tag: tag || ""}),
  });
  const offer = await asked.json();
  if (!asked.ok) throw new Error(offer.detail || "that cannot be published");
  const signatures = [];
  for (const sighash of offer.sighashes) {
    const signed = await coins.signInput(wallet.coinKey, unhex(sighash));
    signatures.push(hex(signed));
  }
  const done = await fetch("/account/sign", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({offer: offer.offer, signatures,
                          pubkey: hex(wallet.coinPubkey)}),
  });
  const said = await done.json();
  if (!done.ok) throw new Error(said.detail || "the node would not take it");
  return said;
}

/* --- writing to somebody -------------------------------------------------
 *
 * The sealing happens here, to a key looked up from the chain. The node is
 * handed ciphertext and an address: it builds the transaction that carries
 * them and never learns what is inside, or -- beyond the address it is
 * paying -- who it is for.
 */

export async function lookUp(name) {
  const answer = await fetch(`/account/who/${encodeURIComponent(name)}`);
  const said = await answer.json();
  if (!answer.ok) throw new Error(said.detail || "nobody by that name");
  return said;
}

/** The header the scanner reads back, bound into the sealed plaintext. */
function boundHeader() {
  // `arcm`, version 1, TYPE_SINGLE, and the fields the envelope binds.
  // Built here rather than taken from the node, because the header is
  // AUTHENTICATED inside the box: a header the node chose would be a
  // header the node could change.
  return new Uint8Array([0x61, 0x72, 0x63, 0x6d, 0x01, 0x01]);
}

export async function write(wallet, me, to, text) {
  return workingOn(async () => {
    const them = await lookUp(to);
    if (!them.key) throw new Error(them.detail || "they have no key published");
    const theirKey = unhex(them.key);

    const plain = new Uint8Array([...boundHeader(),
                                  ...new TextEncoder().encode(text)]);
    const sealed = sealEnvelope(plain, me, theirKey);

    const coins = await import("/coins.js");
    const asked = await fetch("/account/write", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({to: them.address, sealed: hex(sealed)}),
    });
    const offer = await asked.json();
    if (!asked.ok) throw new Error(offer.detail || "that cannot be sent");

    const signatures = [];
    for (const sighash of offer.sighashes) {
      signatures.push(hex(await coins.signInput(wallet.coinKey,
                                                unhex(sighash))));
    }
    const done = await fetch("/account/sign", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({offer: offer.offer, signatures,
                            pubkey: hex(wallet.coinPubkey)}),
    });
    const said = await done.json();
    if (!done.ok) throw new Error(said.detail || "the node would not take it");

    // Our own copy, kept here and nowhere else. What went on the chain is
    // sealed to THEM: it cannot be read back, not by this browser either,
    // so a conversation only has two sides if the sending side writes its
    // half down. The wallet does exactly this on its own machine (D-071).
    await keep({
      txid: said.txid,
      from_cursor: await cursorFor(hex(me.publicKey)),
      when: Math.floor(Date.now() / 1000),
      height: 0,
      to_address: them.address,
      sender: hex(me.publicKey),
      peer: them.key,
      tag: them.tag || String(to).replace(/^@/, ""),
      mine: true,
      body: hex(new TextEncoder().encode(text)),
    });
    return {...said, to: them.address, tag: them.tag, fee: offer.fee};
  });
}

/* The page must not reload under a send, for the same reason it must not
 * under a claim: the keys go with the document (see wallet.js). */
async function workingOn(what) {
  document.body.dataset.working = "yes";
  try {
    return await what();
  } finally {
    delete document.body.dataset.working;
  }
}

/* --- the address book ----------------------------------------------------
 *
 * In this browser, and nowhere else.
 *
 * The wallet's own book is "stored only on this computer" (D-137) and an
 * account's has a sharper reason to be: a book kept on the node would hand
 * the node the list of everybody you know, which is the social graph D-155
 * refused to leak in the messages themselves. Keeping the messages sealed
 * and the address book on the server would be a lock on the door and a
 * list of visitors in the window.
 *
 * What is in it is what the chain already says -- a name, the address it
 * points at, and the key published under it -- so losing this book loses
 * convenience and nothing else. Every name in it was claimed where
 * anybody can look, which is the only way in, exactly as it is on the
 * wallet's own page.
 */

async function bookShelf(mode) {
  return new Promise((ok, no) => {
    const open = indexedDB.open(READ, SHELVES);
    open.onupgradeneeded = () => make(open.result);
    open.onerror = () => no(open.error);
    open.onsuccess = () => ok(open.result.transaction([BOOK], mode));
  });
}

export async function book() {
  try {
    const all = await awaited((await bookShelf("readonly"))
                             .objectStore(BOOK).getAll());
    return all.sort((a, b) => a.tag.localeCompare(b.tag));
  } catch (e) { return []; }
}

export async function addToBook(tag) {
  // What the chain says right now, kept: a name that later moves to
  // somebody else still pays the person it meant when it was added.
  const them = await lookUp(tag);
  const entry = {tag: them.tag || String(tag).replace(/^@/, ""),
                 address: them.address, key: them.key || "",
                 fingerprint: them.fingerprint || "", added: Date.now()};
  await awaited((await bookShelf("readwrite")).objectStore(BOOK).put(entry));
  return entry;
}

export async function removeFromBook(tag) {
  await awaited((await bookShelf("readwrite")).objectStore(BOOK).delete(tag));
}

export async function findNames(text) {
  const answer = await fetch(`/account/find?q=${encodeURIComponent(text)}`);
  if (!answer.ok) return [];
  return (await answer.json()).matches;
}

/* --- conversations -------------------------------------------------------
 *
 * The chain carries messages; a conversation is something the reader
 * assembles. The node cannot do it -- it would have to know which
 * messages are yours and who each one is with, which is the graph D-155
 * refuses to hand over -- so it happens here, over what this browser has
 * opened and what it has sent.
 *
 * The peer of a message is the OTHER party's messaging key: the sender
 * for one that arrived, the recipient for one that went out. Not the
 * address, which changes nothing about who somebody is but does change
 * when they move coins, and not the @tag, which its holder can let go of
 * (D-137). A key is the one identifier in this that cannot be handed on.
 */

const peerOf = (letter) => letter.peer || letter.sender || "";

/** When this browser last looked at a conversation. */
async function readMark(peer) {
  try {
    const tx = await shelf("readonly");
    return (await awaited(tx.objectStore("marks").get(`read:${peer}`))) || 0;
  } catch (e) { return 0; }
}

export async function markRead(peer) {
  const tx = await shelf("readwrite");
  await awaited(tx.objectStore("marks").put(
    Math.floor(Date.now() / 1000), `read:${peer}`));
}

/** Every conversation, most recent first, the way the list draws them. */
export async function threads() {
  const letters = await inbox();
  const known = new Map((await book()).filter((e) => e.key)
                        .map((e) => [e.key, e]));
  const conversations = new Map();
  for (const letter of letters) {
    const peer = peerOf(letter);
    if (!peer) continue;
    let thread = conversations.get(peer);
    if (!thread) {
      const entry = known.get(peer);
      thread = {peer, tag: entry ? entry.tag : (letter.tag || ""),
                address: entry ? entry.address
                               : (letter.from_address || letter.to_address || ""),
                inBook: !!entry, last: 0, unread: 0, count: 0,
                preview: "", outgoing: false};
      conversations.set(peer, thread);
    }
    thread.count += 1;
    if (!thread.tag && letter.tag) thread.tag = letter.tag;
    if ((letter.when || 0) >= thread.last) {
      thread.last = letter.when || 0;
      thread.preview = text(letter);
      thread.outgoing = !!letter.mine;
    }
  }
  const out = [...conversations.values()];
  for (const thread of out) {
    const seen = await readMark(thread.peer);
    thread.unread = letters.filter(
      (l) => peerOf(l) === thread.peer && !l.mine && (l.when || 0) > seen).length;
  }
  return out.sort((a, b) => b.last - a.last);
}

/** One conversation, oldest first, which is the order it was said in. */
export async function conversation(peer) {
  const letters = await inbox();
  return letters.filter((l) => peerOf(l) === peer)
                .sort((a, b) => (a.when || 0) - (b.when || 0));
}

/** A message's words. Always decoded here, never handed round as markup. */
export function text(letter) {
  try {
    return new TextDecoder().decode(unhex(letter.body || ""));
  } catch (e) { return ""; }
}

/** The book entry for a key, so a conversation with nothing in it yet
 *  still says who it is with. */
export async function byKey(peer) {
  for (const entry of await book()) {
    if (entry.key && entry.key === peer) return entry;
  }
  return null;
}

/** Who a name belongs to, for starting a conversation with somebody new. */
export async function reach(name) {
  const wanted = String(name || "").trim().replace(/^@/, "");
  for (const entry of await book()) {
    if (entry.tag === wanted && entry.key) return entry;
  }
  const them = await lookUp(wanted);
  if (!them.key) throw new Error(
    "they have not published a key, so there is nowhere to send it");
  return {tag: them.tag || wanted, address: them.address, key: them.key,
          fingerprint: them.fingerprint || ""};
}
