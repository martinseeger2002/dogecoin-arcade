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
export const TYPE_CHUNK = 2;
export const TYPE_API = 6;      // one program talking to another, below

//: Chunk framing, as `envelope.py` defines it. The countdown is deliberately
//: NOT authenticated: a chunked message is sealed once, as a whole, so there
//: is exactly one authenticated header, while every chunk carries its own
//: countdown. `clen` is THIS CHUNK's ciphertext length -- which is what lets
//: the NUL padding under the last one be thrown away without knowing the
//: total.
const CHUNK_HEADER_LEN = 18;          // magic4 version1 type1 msg_id8 clen2 countdown2
const CHUNK_BOUND_LEN = 14;           // magic4 version1 type1 msg_id8

export function readHeader(payload) {
  if (payload.length < 6) return null;
  for (let i = 0; i < 4; i++) if (payload[i] !== MAGIC[i]) return null;
  if (payload[4] !== VERSION) return null;
  const type = payload[5];
  if (type === TYPE_CHUNK) return null;         // reassembled, not read here
  if (type !== TYPE_SINGLE) return null;        // and API is not read here either
  if (payload.length < 8) return null;
  const clen = (payload[6] << 8) | payload[7];
  return {type, clen, length: 8, bound: payload.subarray(0, 6)};
}

/** One link of a chained message, read for what reassembly needs. */
export function readChunk(payload) {
  if (payload.length < CHUNK_HEADER_LEN) return null;
  for (let i = 0; i < 4; i++) if (payload[i] !== MAGIC[i]) return null;
  if (payload[4] !== VERSION || payload[5] !== TYPE_CHUNK) return null;
  return {
    type: TYPE_CHUNK,
    id: hex(payload.subarray(6, 14)),
    clen: (payload[14] << 8) | payload[15],
    countdown: (payload[16] << 8) | payload[17],
    length: CHUNK_HEADER_LEN,
    bound: payload.subarray(0, CHUNK_BOUND_LEN),
    cipher: payload.subarray(CHUNK_HEADER_LEN),
  };
}

function sameBytes(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

function concat(parts) {
  const all = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const part of parts) { all.set(part, at); at += part.length; }
  return all;
}

/** The two layers and the framing, given the ciphertext and its header.
 *
 * Both the single-payload path and the reassembled one end here, so the
 * checks cannot drift apart between them -- and the header compared is the
 * one copied inside the box, not the one the node happened to hand over.
 */
function openWith(cipher, me, bound) {
  const envelope = openSealedBox(cipher, me);
  if (envelope === null) return null;
  const opened = openEnvelope(envelope, me);
  if (opened === null) return null;
  // The cleartext header, compared against the copy sealed inside. A
  // message whose framing was altered on the way is refused rather than
  // shown with the alteration.
  const inside = opened.plain.subarray(0, bound.length);
  if (!sameBytes(inside, bound)) return null;
  return {sender: opened.sender, plain: opened.plain.subarray(bound.length)};
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
  return openWith(blob, me, header.bound);
}

/** Open what a chunked message turned out to be, given every chunk of it.
 *
 * `parts` is the chunk candidates this browser has and has not read -- the
 * rows the node handed over, in any order, belonging to anybody. The
 * framing is read out of each payload rather than out of a column, because
 * the payload is the part that gets authenticated.
 *
 * Completion is self-describing: the final chunk carries countdown 0 and
 * the rest count down to it, so an abandoned message is simply never
 * complete, and a message with a gap in it is not surfaced as a partial one.
 *
 * Groups are keyed by SENDER as well as by message id, because the id sits
 * in a cleartext header in plain view on the chain: anybody who reads one
 * can publish a chunk claiming it, and one injected chunk with an unused
 * countdown would make the real message look permanently incomplete.
 * Grouped this way an injected chunk forms its own group, which just fails
 * to decrypt, instead of poisoning the real one.
 *
 * `opened` is what arrived; `waiting` counts the groups left holding
 * something -- short of a piece, or apparently whole and not its own;
 * `spent` names the chunks that were READ, which are the only ones worth
 * deleting: nothing can tell a message that has not finished arriving from
 * one that never will, so the rest waits and ages out instead.
 */
export function assemble(parts, me) {
  const groups = new Map();
  for (const part of parts) {
    const chunk = readChunk(unhex(part.payload));
    if (chunk === null) continue;
    const key = `${part.from_address || ""}|${chunk.id}`;
    if (!groups.has(key)) groups.set(key, new Map());
    groups.get(key).set(chunk.countdown, {part, chunk});
  }
  const opened = [];
  const spent = [];
  let waiting = 0;
  for (const chunks of groups.values()) {
    const last = Math.max(...chunks.keys());
    if (!chunks.has(0) || chunks.size !== last + 1) {
      waiting += 1;
      continue;                       // the tail, or a piece of the middle, is out
    }
    const ordered = [];
    for (let n = last; n >= 0; n--) ordered.push(chunks.get(n));
    const cipher = concat(ordered.map(({chunk}) => chunk.clen
      ? chunk.cipher.subarray(0, chunk.clen) : chunk.cipher));
    const out = openWith(cipher, me, ordered[0].chunk.bound);
    // Nothing. Either it is not ours or it is not the whole of it, and the
    // two cannot be told apart: the countdown runs down to zero, so a
    // message whose FIRST chunk has not landed looks exactly like a shorter
    // message that is complete. So the pieces stay -- a group is only
    // thrown away once it has actually been read, and everything else ages
    // out on its own.
    if (out === null) { waiting += 1; continue; }
    const txids = ordered.map(({part}) => part.txid);
    const tail = ordered[ordered.length - 1].part;
    spent.push(...txids);
    // The first chunk is the one the conversation points at, and the last
    // is the one whose block it really finished in -- the same two the
    // scanner stores for a chunked message.
    opened.push({
      sender: out.sender, plain: out.plain,
      txid: ordered[0].part.txid, from_address: ordered[0].part.from_address,
      when: tail.when, height: tail.height, cursor: tail.cursor,
    });
  }
  return {opened, waiting, spent};
}

/* --- pieces waiting for the rest of themselves ---------------------------
 *
 * A chunked message arrives over dozens of transactions, and the node hands
 * over whatever has landed. A browser that threw away the pieces it could
 * not finish would depend on which page of the chain it happened to be
 * looking at: the first chunk and the last can be hours apart, and the
 * cursor only ever moves forward. So an unfinished group waits here, in
 * this browser, and for no longer than a day -- a message that was never
 * finished is not worth carrying, and one that was is deleted on being read.
 */

const PART_DAY = 86400;
//: Not a limit on message size -- a limit on what a browser that receives
//: nobody's mail in particular can end up holding. A chunk that is not
//: anybody's here is still somebody's, and it stays until it either
//: finishes or ages out.
const PART_CAP = 400;

async function stashParts(parts) {
  // `got` is when THIS browser first saw the row, which is the only clock
  // that answers "how long have I been holding this". A block time is not:
  // a chain that is syncing, a reorg, or a chain whose blocks run behind
  // the wall all make pieces look years old, and the message would be
  // thrown away before it was ever finished.
  const got = Math.floor(Date.now() / 1000);
  const tx = await partShelf("readwrite");
  for (const part of parts) await awaited(tx.objectStore("parts").put(
    {...part, got}));
}

async function spentParts(txids) {
  const tx = await partShelf("readwrite");
  for (const txid of txids) await awaited(tx.objectStore("parts").delete(txid));
}

/** What is held, longest-held first, with the stale and the excess gone. */
async function waitingParts() {
  const tx = await partShelf("readwrite");
  const store = tx.objectStore("parts");
  const held = (await awaited(store.getAll()))
    .sort((a, b) => (a.got || 0) - (b.got || 0));
  const cutoff = Math.floor(Date.now() / 1000) - PART_DAY;
  const fresh = [];
  for (const part of held) {
    if ((part.got || 0) >= cutoff && held.length - fresh.length <= PART_CAP) {
      fresh.push(part);
      continue;
    }
    await awaited(store.delete(part.txid));
  }
  return fresh;
}

/** Open whatever of the held pieces is complete now; leave the rest waiting. */
async function finishParts(me) {
  const parts = await waitingParts();
  if (!parts.length) return {opened: [], waiting: 0, spent: []};
  const out = assemble(parts, me);
  if (out.spent.length) await spentParts(out.spent);
  return out;
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
const PARTS = "parts";

//: One version number for one database. Two openers asking for different
//: versions is how an upgrade silently never runs -- the second one is
//: refused as "version change" and the store it wanted is simply absent.
//: Three now: the messages this browser opened, the marks and cursors that
//: say what it has read, and the pieces of chunked messages it is waiting
//: to finish. (The address book shares the same database, in `book`.)
const SHELVES = 3;

function shelf(mode) {
  return new Promise((ok, no) => {
    const open = indexedDB.open(READ, SHELVES);
    open.onupgradeneeded = () => make(open.result);
    open.onerror = () => no(open.error);
    open.onsuccess = () => ok(open.result.transaction(
      ["mail", "marks"], mode));
  });
}

function partShelf(mode) {
  return new Promise((ok, no) => {
    const open = indexedDB.open(READ, SHELVES);
    open.onupgradeneeded = () => make(open.result);
    open.onerror = () => no(open.error);
    open.onsuccess = () => ok(open.result.transaction(PARTS, mode));
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
  if (!db.objectStoreNames.contains(PARTS)) {
    db.createObjectStore(PARTS, {keyPath: "txid"});
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
    const loose = [];
    for (const candidate of said.candidates) {
      const ours = awaiting.get(candidate.txid);
      if (ours) {
        await keep({...ours, cursor: candidate.cursor,
                    height: candidate.height,
                    when: candidate.height ? candidate.when : ours.when});
        continue;                     // sealed to them; nothing to open
      }
      looked += 1;
      const bytes = unhex(candidate.payload);
      // What a row IS comes out of its own first bytes, never out of a
      // column: the framing is inside the thing that gets authenticated,
      // and a node that described a row wrongly would otherwise describe
      // its way into changing what this browser thinks it read.
      const header = readHeader(bytes);
      if (header === null) {
        if (readChunk(bytes) === null) continue;
        loose.push({txid: candidate.txid, cursor: candidate.cursor,
                    when: candidate.when, height: candidate.height,
                    from_address: candidate.from_address,
                    payload: candidate.payload});
        continue;
      }
      const out = openMessage(bytes, me);
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
    // The pieces of this page join the pieces kept from every earlier one,
    // and whatever is now whole gets opened. A message in pieces is one
    // message, so it is filed under the transaction that started it -- the
    // row the node promotes when the pool copy finally lands in a block.
    if (loose.length) await stashParts(loose);
    for (const piece of (await finishParts(me)).opened) {
      await keep({
        txid: piece.txid, cursor: piece.cursor, when: piece.when,
        height: piece.height, from_address: piece.from_address,
        sender: hex(piece.sender), peer: hex(piece.sender),
        mine: false, body: hex(piece.plain),
      });
      opened += 1;
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

/** Publish something this account says about itself, and sign it here.
 *
 * `where` is `/account/announce` or `/account/profile`: both are one key
 * announcement, both are built by the node over an address it watches and
 * signed nowhere else, and the only difference is which fields the
 * announcement carries. The node's offer is checked the way the chain will
 * check it before a single byte of it is signed (`coins.verifyOffer`).
 */
async function publishAboutMe(wallet, me, where, body) {
  const coins = await import("/coins.js");
  const asked = await fetch(where, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  const offer = await asked.json();
  if (!asked.ok) throw new Error(offer.detail || "that cannot be published");
  const keys = keysFor(offer, wallet);
  const shown = await coins.verifyOffer(offer, keys);
  const signatures = [];
  for (const sighash of shown.hashes) {
    const signed = await coins.signInput(keys.key, unhex(sighash));
    signatures.push(hex(signed));
  }
  const done = await fetch("/account/sign", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({offer: offer.offer, signatures,
                          pubkey: hex(keys.pubkey)}),
  });
  const said = await done.json();
  if (!done.ok) throw new Error(said.detail || "the node would not take it");
  return said;
}

/** Publish this account's messaging key, so anybody can write to it. */
export async function announce(wallet, me, tag) {
  return publishAboutMe(wallet, me, "/account/announce",
                        {key: hex(me.publicKey), tag: tag || ""});
}

/** Publish this account's own profile: its face, a line, a link.
 *
 * The tag is not part of this. The node takes it off the chain, because
 * changing a face is not changing a name, and a name typed into this
 * request would be a name the account might not hold.
 *
 * A field you leave out is kept as it stands; a field you send empty is
 * taken down. One announcement replaces the whole of a profile, so a form
 * that started blank would drop a bio every time somebody changed their
 * picture -- which is why `me.html` prefills these from `wallet.state()`.
 */
export async function publishProfile(wallet, me, fields) {
  const body = {key: hex(me.publicKey)};
  for (const field of ["pfp", "bio", "url"]) {
    if (fields && Object.prototype.hasOwnProperty.call(fields, field))
      body[field] = String(fields[field] || "");
  }
  return publishAboutMe(wallet, me, "/account/profile", body);
}

/** The keys for the chain an offer was built for.
 *
 * The same rule `wallet.js` follows for the same reason: the offer says
 * which chain it is on, and signing its bytes with another chain's key
 * makes a signature that verifies against nothing.
 */
function keysFor(offer, wallet) {
  const on = (wallet.on && wallet.on[offer.chain]) || {};
  return {key: on.key || wallet.coinKey, pubkey: on.pubkey || wallet.coinPubkey,
          address: on.address || wallet.address};
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

    const keys = keysFor(offer, wallet);
    const shown = await coins.verifyOffer(offer, keys);
    const signatures = [];
    for (const sighash of shown.hashes) {
      signatures.push(hex(await coins.signInput(keys.key, unhex(sighash))));
    }
    const done = await fetch("/account/sign", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({offer: offer.offer, signatures,
                            pubkey: hex(keys.pubkey)}),
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
    return {...said, to: them.address, tag: them.tag, fee: shown.fee};
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

/* --- what a program says, and what it answers ----------------------------
 *
 * Type 6. The same sealed box as a letter, addressed to a program rather
 * than to a person -- which is why `readHeader` above refuses it on
 * purpose: machine traffic that turns up in somebody's chat is worse than
 * no machine traffic at all. Everything in this section is the other door,
 * and none of it goes near the messenger's inbox or its cursor.
 *
 * A shop is what this is for. An order, an offer, and a signature are
 * commands a node answers on the spot, and the answer comes back sealed to
 * the key that asked -- which, for an account, is a key no node has. So the
 * sealing is here, with the rest of the keys.
 *
 * **The stamp goes inside.** `apilib.stamp()` is eight bytes ("DA", the
 * protocol, four bytes over the API surface) and `api.seal` puts it in the
 * plaintext, where the box authenticates it: a stamp on the outside is a
 * stamp any relay could edit. The node hands it over rather than letting
 * this file invent one, because the fingerprint is over *its* API and only
 * it knows what it speaks -- and `stampLooksRight` is what the page checks
 * before trusting what it was handed. A node that answered with somebody
 * else's stamp would be answered with somebody else's command bytes.
 */

const PROGRAM_HEADER = new Uint8Array([0x61, 0x72, 0x63, 0x6d, 0x01, TYPE_API]);
const PROGRAM_STAMP_LEN = 8;            // magic2 protocol1 fingerprint4 reserved1

/** Does the stamp this node offered look like a stamp at all?
 *
 * Only this much can be known here: the magic and the protocol. The four
 * bytes after them are the node's own API and this browser has no way to
 * recompute them -- which is exactly why they are checked for shape here
 * and for agreement by whoever compares one answer against the offer that
 * asked for it.
 */
export function stampLooksRight(stampHex) {
  const stamp = unhex(stampHex || "");
  return stamp.length === PROGRAM_STAMP_LEN && stamp[0] === 0x44
    && stamp[1] === 0x41 && stamp[2] === 1;
}

/** The ciphertext half of a program message: JSON in, hex out.
 *
 * Ciphertext only, as with a private letter. `api.seal` returns a finished
 * payload with its header on the front and the node writes the header of
 * the transaction it is putting bytes into, so handing that over twice
 * prefixes two headers and the node's trial decrypt fails with "not
 * addressed to us, or corrupt" -- silently, because a candidate it cannot
 * open is a candidate it says nothing about.
 */
export function sealForProgram(me, keyHex, stampHex, body) {
  const key = unhex(keyHex);
  if (key.length !== 32) throw new Error("a program is addressed by a key");
  if (!stampLooksRight(stampHex)) {
    throw new Error("that is not an API stamp, so nothing here is sealed to it");
  }
  const plain = concat([PROGRAM_HEADER, unhex(stampHex),
                        new TextEncoder().encode(JSON.stringify(body))]);
  return hex(sealEnvelope(plain, me, key));
}

/** Open what a program sent, or null. Never throws: this is tried against
 *  every row the node hands over, and almost all of them belong to others.
 */
export function openProgram(payload, me) {
  const blob = typeof payload === "string" ? unhex(payload) : payload;
  if (blob.length < 8) return null;
  for (let i = 0; i < 4; i++) if (blob[i] !== MAGIC[i]) return null;
  if (blob[4] !== VERSION || blob[5] !== TYPE_API) return null;
  const clen = (blob[6] << 8) | blob[7];
  let cipher = blob.subarray(8);
  if (clen) {
    if (clen > cipher.length) return null;
    cipher = cipher.subarray(0, clen);     // discard Class B padding
  }
  const out = openWith(cipher, me, PROGRAM_HEADER);
  if (out === null) return null;
  const stamp = out.plain.subarray(0, PROGRAM_STAMP_LEN);
  let json = null;
  try {
    json = JSON.parse(new TextDecoder().decode(out.plain.subarray(PROGRAM_STAMP_LEN)));
  } catch (e) {
    return null;            // opened, and is not a command: not this page's
  }
  return {sender: hex(out.sender), stamp: hex(stamp), json,
          protocol: stamp.length === PROGRAM_STAMP_LEN ? stamp[2] : 0};
}

/** What the programs answered, since `after`, opened with this key.
 *
 * Read with an explicit cursor and never with the one the messenger keeps:
 * a page polling for a shop's answer must not mark somebody's letters read,
 * and an answer re-read twice costs a decrypt and no coins. With nothing to
 * start from it begins where the account was made, for the reason `collect`
 * gives -- nothing could have answered a key that did not exist yet.
 *
 * What will not open is passed over, including this account's own orders:
 * those are sealed to the shop, so a reader that opened every row here were
 * a reader that opens strangers' trades.
 */
export async function programAnswers(me, after) {
  let cursor = Number(after || 0);
  if (!cursor) {
    try {
      cursor = (await (await fetch("/account")).json()).mail_from || 0;
    } catch (e) { cursor = 0; }
  }
  const start = cursor, answers = [];
  for (;;) {
    const answer = await fetch(`/account/messages?after=${cursor}&limit=200`);
    if (!answer.ok) throw new Error("the node would not say what it was asked");
    const said = await answer.json();
    for (const row of said.candidates) {
      const opened = openProgram(row.payload, me);
      if (opened !== null) {
        answers.push({json: opened.json, sender: opened.sender, stamp: opened.stamp,
                      txid: row.txid, when: row.when, height: row.height});
      }
    }
    cursor = said.cursor;
    if (!said.more) break;
  }
  return {answers, cursor: Math.max(start, cursor)};
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

/* --- what is inside a message ---------------------------------------------
 *
 * `arcade/messaging/content.py` says what a decrypted body means and
 * `arcade/media.py` says which of its bytes are safe to show. The wallet's
 * own page has always gone through them; this is the same page with the
 * node left out, so it goes through the same rules, written out again for a
 * browser that has to apply them alone.
 *
 * A body with no marker is exactly what it always was -- words -- so a
 * message from before any of this existed still reads, and a message that
 * was built wrong still shows as whatever it is rather than vanishing.
 */

const BODY_MAGIC = new Uint8Array([0x01, 0x41, 0x52, 0x43, 0x42]);
const BODY_VERSION = 1;
const INLINE_MAX = 12 * 1024 * 1024;

const decodeBytes = (bytes) => new TextDecoder("utf-8", {fatal: false}).decode(bytes);

/** Do `bytes` start with `prefix` -- spelled as text or as bytes -- at `at`? */
function begins(bytes, prefix, at) {
  const from = at || 0;
  if (bytes.length < from + prefix.length) return false;
  for (let i = 0; i < prefix.length; i++) {
    const want = typeof prefix === "string" ? prefix.charCodeAt(i) : prefix[i];
    if (bytes[from + i] !== want) return false;
  }
  return true;
}

function span(bytes, from, to) {
  return String.fromCharCode.apply(null, bytes.subarray(from, to));
}

/** What a sender chose to call a file. It is a display name and nothing
 *  more -- this browser never writes it to a disk -- but it is somebody
 *  else's string, so it is cleaned exactly where the node cleans it. */
function safeName(given) {
  const path = String(given || "").replace(/\\/g, "/");
  const leaf = path.slice(path.lastIndexOf("/") + 1);
  let clean = "";
  for (const c of leaf) {
    const at = c.codePointAt(0);
    if (at < 0x20 || (at >= 0x7f && at <= 0x9f)) continue;
    if ('<>:"|?*'.indexOf(c) >= 0) continue;
    clean += c;
  }
  clean = clean.trim().replace(/^\.+/, "").replace(/\.+$/, "");
  return clean.length ? clean.slice(0, 120) : "attachment";
}

/** Which files may be SHOWN, from their own leading bytes and never from
 *  the type the sender declared -- which is theirs to invent.
 *
 * The list is formats a decoder reads, not a language that runs. SVG and
 * PDF are missing from it on purpose: both are XML or worse with an
 * execution model, and an attachment is somebody else's bytes opened inside
 * the origin that holds a wallet. Anything else still arrives, whole, and
 * is offered as a file to save.
 */
export function sniff(bytes) {
  if (bytes.length > INLINE_MAX || bytes.length < 12) return null;
  const media = (kind, mime, label) => ({kind, mime, label});
  if (begins(bytes, "\u0089PNG\r\n\u001a\n", 0)) return media("image", "image/png", "PNG image");
  if (begins(bytes, "\u00ff\u00d8\u00ff", 0)) return media("image", "image/jpeg", "JPEG image");
  if (begins(bytes, "GIF87a", 0) || begins(bytes, "GIF89a", 0)) {
    return media("image", "image/gif", "GIF image");
  }
  if (begins(bytes, "RIFF", 0) && begins(bytes, "WEBP", 8)) {
    return media("image", "image/webp", "WebP image");
  }
  if (begins(bytes, "RIFF", 0) && begins(bytes, "WAVE", 8)) {
    return media("audio", "audio/wav", "WAV audio");
  }
  if (begins(bytes, "ID3", 0)) return media("audio", "audio/mpeg", "MP3 audio");
  // A bare MPEG frame sync: eleven set bits, and a layer that is not the
  // reserved one. Checked narrowly because two loose bytes match a great deal.
  if (bytes[0] === 0xff && (bytes[1] & 0xe0) === 0xe0 && (bytes[1] & 0x06) !== 0) {
    return media("audio", "audio/mpeg", "MP3 audio");
  }
  if (begins(bytes, "OggS", 0)) {
    const head = decodeBytes(bytes.subarray(0, 64));
    if (head.indexOf("theora") >= 0 || head.indexOf("video") >= 0) {
      return media("video", "video/ogg", "Ogg video");
    }
    return media("audio", "audio/ogg", "Ogg audio");
  }
  if (begins(bytes, "ftyp", 4)) {
    const brand = span(bytes, 8, 12);
    if (["isom", "iso2", "mp41", "mp42", "avc1", "MSNV", "dash", "M4V ", "mmp4"]
      .indexOf(brand) >= 0) return media("video", "video/mp4", "MP4 video");
    if (brand === "M4A " || brand === "M4B ") {
      return media("audio", "audio/mp4", "M4A audio");
    }
    if (brand === "qt  ") return media("video", "video/quicktime", "QuickTime video");
    return null;                       // an unknown brand stays a download
  }
  if (begins(bytes, "\u001aE\u00df\u00a3", 0)) {
    const head = span(bytes, 0, Math.min(bytes.length, 256));
    if (head.indexOf("webm") >= 0) return media("video", "video/webm", "WebM video");
    if (head.indexOf("matroska") >= 0) {
      return media("video", "video/x-matroska", "Matroska video");
    }
    return null;
  }
  return null;
}

/** A body: its words, its file, and what the sender said about themselves.
 *  Never throws -- these bytes came from somebody else. */
export function parseBody(body) {
  const bytes = body instanceof Uint8Array ? body : unhex(body || "");
  const plain = () => ({text: decodeBytes(bytes), file: null, profile: null,
                        plain: true});
  if (!begins(bytes, BODY_MAGIC, 0) || bytes.length < BODY_MAGIC.length + 3) {
    return plain();
  }
  if (bytes[BODY_MAGIC.length] !== BODY_VERSION) return plain();   // newer, or not one
  const size = (bytes[6] << 8) | bytes[7];
  let said;
  try {
    said = JSON.parse(decodeBytes(bytes.subarray(8, 8 + size)));
  } catch (e) { return plain(); }
  if (said === null || typeof said !== "object" || Array.isArray(said)) return plain();

  let file = null;
  if (said.file !== null && typeof said.file === "object") {
    let data = bytes.subarray(8 + size);
    // The bytes that are here, not the length that was claimed: a message
    // that stopped early gives a short file rather than a broken one.
    if (Number.isInteger(said.file.size) && said.file.size >= 0
      && said.file.size <= data.length) data = data.subarray(0, said.file.size);
    file = {name: safeName(said.file.name || ""),
            type: String(said.file.type || "") || "application/octet-stream",
            bytes: data, size: data.length, media: sniff(data)};
  }
  let profile = null;
  if (said.profile !== null && typeof said.profile === "object") {
    // Only what was said. `Profile.as_dict` on the node leaves an empty
    // field out altogether, and a profile that is all blanks is no profile.
    const part = (value, cap) => String(value || "").slice(0, cap);
    const name = part(said.profile.name, 80);
    const testnet = part(said.profile.testnet_address, 64);
    const mainnet = part(said.profile.mainnet_address, 64);
    if (name || testnet || mainnet) {
      profile = {};
      if (name) profile.name = name;
      if (testnet) profile.testnet_address = testnet;
      if (mainnet) profile.mainnet_address = mainnet;
    }
  }
  return {text: typeof said.text === "string" ? said.text : "",
          file, profile, plain: false};
}

function bodyOf(letter) {
  try {
    return parseBody(unhex(letter.body || ""));
  } catch (e) {
    return {text: "", file: null, profile: null, plain: true};
  }
}

/** A message's words, whatever else it carried. Always decoded here, never
 *  handed round as markup. */
export function text(letter) {
  const body = bodyOf(letter);
  if (body.file === null) return body.text;
  return body.text || `[sent ${body.file.name}]`;
}

/** A message's file, with what can safely be done with it. */
export function attachment(letter) {
  return bodyOf(letter).file;
}

/** What the sender volunteered about themselves, which they may not have. */
export function profile(letter) {
  return bodyOf(letter).profile;
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

/* --- the contact code -----------------------------------------------------
 *
 * `arcade:<network>:<base58 public key>:<check>` -- a way to exchange a
 * messaging key that never touches the chain (arcade/messaging/contact.py).
 * Built here, in the browser, from the same public key `identity()`
 * derives: the node is not asked and has nothing to add. Two people who
 * exchange this by any channel -- paper, chat, a QR code -- can message
 * each other with nothing published anywhere.
 */

export async function contactCode(network, publicKey) {
  const coins = await import("/coins.js");
  const digest = await crypto.subtle.digest("SHA-256", publicKey);
  const check = hex(digest).slice(0, 4);   // the first two bytes, as hex --
                                            // matches contact.py's [:4]
  return `arcade:${network}:${coins.base58(publicKey)}:${check}`;
}
