# Step 2 — Message format and encryption

Status: **draft for the operator's approval. No implementation code written.**
Date: 2026-09-11

Every size in this document was **measured**, not estimated, using PyNaCl 1.5.0 on
this machine. The construction in §3 was prototyped and its failure modes tested
before being written down.

---

## 0. Context

The Messenger is part of DogecoinArcade and is **testnet-only, permanently**
(D-010), enforced in code by `require_messaging_network()`. It still designs to
**mainnet** standardness limits: testnet sets `fRequireStandard = false`
(`chainparams.cpp:108`), so a format that only worked there would be silently
unrelayable anywhere real.

Carrier decisions from D-009:

| Message size | Carrier | Chunking |
|---|---|---|
| any, ≤ 7,514 chars | **Class B** (bare multisig), one transaction | **none** |
| larger | chained Class B, countdown index | chunk header only here |
| key announcements | **Class C** (`OP_RETURN`), 38 bytes | n/a |

---

## 1. Identity keys

### Primitive

**X25519** (`crypto_box` keypair). 32-byte public key, 32-byte secret key
(*measured*).

These are **separate from wallet keys**, which matters for three concrete reasons
rather than as a principle:

1. A wallet key is exposed to signature-based attacks every time it spends; an
   encryption key that never signs is not.
2. A wallet key cannot be rotated without moving funds. A messaging key can be
   rotated freely.
3. Compromise of a spending key would otherwise retroactively decrypt every
   message ever sent to that person.

The operator's existing DogecoinArcade code encrypts to **secp256k1 wallet pubkeys**
(`encrypt_data.py`). This design deliberately departs from that; see D-011 for the
comparison.

### At rest

The secret key is stored encrypted. Key derivation is **Argon2id**
(`crypto_pwhash`), and the symmetric layer is **XSalsa20-Poly1305**
(`crypto_secretbox`).

Measured Argon2id presets, and the choice:

| Preset | ops | memory | Verdict |
|---|---|---|---|
| INTERACTIVE | 2 | 64 MiB | too weak for a long-lived identity key |
| **MODERATE** | **3** | **256 MiB** | **chosen** — 1.30 s on this Celeron (*measured*) |
| SENSITIVE | 4 | 1024 MiB | 1 GiB will fail or thrash on phones and small VPSes |

File layout (all fields fixed-width, little ambiguity by design):

```
magic        4   b"ARCK"
version      1   = 1
kdf_alg      1   = 1 (Argon2id)
ops          4   uint32  -- stored, not assumed
mem          4   uint32  -- stored, not assumed
salt        16   crypto_pwhash_SALTBYTES
nonce       24   crypto_secretbox nonce
ciphertext  48   32-byte secret key + 16-byte MAC
-----------------
total      102 bytes
```

**The KDF parameters are stored in the file, not hard-coded in the reader.** If we
raise the cost later, old files still open. Hard-coding today's parameters is the
standard way to make key files unreadable in three years.

### Backup

The encrypted file (102 bytes) is the backup unit: copy it anywhere, since it is
useless without the passphrase. An optional human-transcribable form encodes the
same 102 bytes as base58 with a checksum, for paper.

**Not** a BIP39 mnemonic: a mnemonic implies HD-wallet derivation semantics that do
not exist here, and inviting people to store a messaging key next to seed phrases
is a bad habit to encourage.

### Rotation

Publish a new key announcement (§2) **from the same wallet address**. Continuity is
established by the address, not by cryptography.

**Be clear about the limit this has:** if the wallet address is compromised, the
attacker can announce their own messaging key and future correspondents will use
it. There is no cryptographic chain from old key to new.

A stronger design adds a long-term **Ed25519** identity key that signs every
announcement, so rotation is chained and address compromise alone is insufficient.
That costs 64 bytes per announcement and a second key to protect.
**Recommendation: address-binding for v1, with this limitation stated in the UI**,
and the Ed25519 chain as a documented upgrade path.

---

## 2. Key publication

### Transaction

A **Class C** (`OP_RETURN`) transaction from the user's address:

```
magic      4   b"arcm"
version    1   = 1
type       1   = 3 (key announcement)
pubkey    32   X25519 public key
---------------
total     38 bytes
```

38 bytes fits Class C's 72-byte capacity with room to spare, and Class C costs one
`OP_RETURN` output rather than a marker plus multisig dust. Key announcements are
therefore much cheaper than messages — appropriate, since they are public data.

Only **one `OP_RETURN` per standard transaction** is permitted
(`policy/policy.cpp:115-119`), so an announcement is always its own transaction.

### What an announcement carries now

The 38-byte form above is what earlier versions publish and is still read. A
tail of optional sections follows it, each one a kind byte and its value, so
an older reader stops at the first kind it does not know and still gets the
key:

| kind | what | why |
|------|------|-----|
| `0x01` | identity address (20-byte hash160) + name | Without it the key is filed under whichever address funded the transaction, which changes with coin selection (D-062) |
| `0x02` | the other chain's address (20 bytes) | So one lookup gives somebody both places to pay you (D-137) |
| `0x03` | the `@tag` this address holds | Unlike a name, a reader can check it against the tag index |
| `0x04` | profile picture: an inscription's 32-byte txid | Honoured only while the chain says that address still holds the piece (D-138) |
| `0x05` | a bio, at most 160 bytes | Shown on their feed; refused rather than trimmed |
| `0x06` | a link, at most 120 bytes, `http(s)` only | It lands on other people's pages as something to click (D-145) |

A whole profile is therefore one transaction: name, both addresses, key,
face, bio and link. Anybody who finds the tag has everything without asking
the person or the node that drew it.

### Type 7: what was done to a post

The feed's reactions are one type with a kind byte rather than six types
(`arcade/messaging/feed.py`, D-138):

```
magic      4   b"arcm"
version    1   = 1
type       1   = 7 (feed action)
kind       1   1 like, 2 unlike, 3 reply, 4 share, 5 edit, 6 delete, 7 tip
target    32   the txid this is about
text     0..   present for reply, share and edit only
```

A like is 39 bytes. The target is any transaction, so a reply's parent may be
a post or another reply and nothing in the format knows the difference --
which is what makes threads free, and would make commenting on an inscription
free too.

A **tip** is the one kind that rides on a LEDGER chain rather than the
messaging one: the transaction that moves the coins carries this note saying
which post they were for, so a tip is an ordinary payment, costs one fee, and
works on mainnet where the rest of the feed does not.

### Fingerprint

```
fingerprint = SHA-256(pubkey)[:8]  ->  16 hex chars, shown in groups of four
example:      3f9a  c012  77b4  e105
```

Eight bytes is 64 bits: far beyond casual collision, short enough to read aloud
over a phone call, which is the entire point.

### What an on-chain announcement proves — and does not

**Proves:** whoever controlled the private key for that wallet address, at that
block height, published this X25519 public key. That is a real, timestamped,
tamper-evident binding between *an address* and *a key*.

**Does not prove:**

- that the address belongs to the person you think it does;
- that they still control the key today;
- that the key was generated safely, or that they know its secret half;
- anything at all about a name, handle or identity off-chain.

An announcement is a **key directory entry, not an identity certificate.** The
fingerprint exists so the binding can be confirmed out of band — a phone call, a
face-to-face check — which is the only step that actually establishes identity.

---

## 3. Encryption

### The construction

```
inner      = crypto_box(sender_sk, recipient_pk, header || message)   # nonce||ct||mac
envelope   = sender_x25519_pubkey (32) || inner
ciphertext = crypto_box_seal(recipient_pk, envelope)
```

Decryption reverses it: open the sealed box with the recipient's secret key, read
the sender's public key from the first 32 bytes, then open the inner box. If the
inner box opens, the sender genuinely holds the matching secret key.

**Measured overhead: 126 bytes** = 48 (sealed box) + 32 (sender key) + 40
(crypto_box nonce 24 + MAC 16) + 6 (authenticated header copy).

### Why this rather than the simpler options

| Option | Overhead | Sender authenticated | Sender hidden from observers | Deniable |
|---|---|---|---|---|
| `crypto_box_seal` alone | 48 | ❌ anyone can claim anyone | ✅ | — |
| `crypto_box` + key in clear | 72 | ✅ | ❌ **links sender publicly** | ✅ |
| **sealed ∘ crypto_box** | **126** | ✅ | ✅ | ✅ |
| sealed + Ed25519 signature | 144 | ✅ | ✅ | ❌ **non-repudiable** |

A plain sealed box is anonymous but **unauthenticated**: anyone can send you a
message claiming to be anyone, which is a serious weakness in a messenger.

A plain `crypto_box` authenticates but puts the sender's public key **in the clear
on-chain**, permanently linking sender and recipient to any observer — it discards
most of what we are trying to protect.

The signature variant is *worse than useless here*: non-repudiation means the
recipient can **prove to third parties** that you sent a message. For private
correspondence that is a liability, not a feature. `crypto_box`'s authentication is
deliberately **repudiable** — the recipient is convinced, but cannot convince
anyone else, because they could have forged it themselves with the shared secret.

126 bytes is **1.6 %** of a Class B transaction's capacity. The extra 78 bytes over
a bare sealed box buy sender authentication and are not worth economising on.

### Nonces

The inner `crypto_box` nonce is **24 bytes (192 bits) of `os.urandom`**, generated
fresh per message and carried in the ciphertext.

At 192 bits, random generation is safe without any counter or state: a collision
requires roughly 2⁹⁶ messages. The brief warns against deriving nonces from
transaction data — correctly, because an unbroadcast transaction can be rebuilt
with the same inputs and silently reuse a nonce under the same key pair, which
would be catastrophic. **We derive nonces from nothing.** The sealed box's own
ephemeral key is generated internally by libsodium and is fresh per call.

### Header binding

The cleartext header must be readable before decryption (to recognise and
reassemble messages), so it sits outside the ciphertext — and is therefore
tamperable. We bind it by **copying the header inside the authenticated
plaintext** and comparing on decrypt. A mismatch is rejected.

This is why the overhead is 126 and not 120. `crypto_box_seal` does not accept
associated data, so an authenticated copy is the clean way to get the same effect
without inventing anything.

### Verified failure modes

Prototyped and tested before writing this document. All five rejected:

| Attack | Result |
|---|---|
| Wrong recipient attempts decryption | rejected (`CryptoError`) |
| Single flipped bit in ciphertext | rejected |
| Tampered cleartext header | rejected — header mismatch |
| **Forged sender identity** (claim Alice's key, seal with Mallory's) | **rejected** |
| Correct recipient, intact message | round-trips, sender authenticated |

### Encrypt once, then chunk

The whole message is encrypted **once**, then the resulting ciphertext is split.
Never the reverse. Encrypting per chunk would multiply the 126-byte overhead by
the chunk count and leak the chunk boundary structure.

---

## 4. Sizing and cost

### Framing

Single-transaction message (inside the type-200 `AnyData` payload):

```
magic      4   b"arcm"
version    1   = 1
type       1   = 1 (single)
----------------
6 bytes, copied inside the ciphertext for authentication
```

Chunk header, for messages that exceed one transaction:

```
magic      4   b"arcm"
version    1   = 1
type       1   = 2 (chunk)
msg_id     8   random; ties chunks of one message together
countdown  2   big-endian; 0 marks the FINAL chunk
----------------
16 bytes
```

**The countdown is NOT authenticated.** A chunked message is sealed once, as a
whole, so there is exactly one authenticated header copy — but every chunk carries
a different countdown, so binding it would make reassembly impossible by
construction. Only magic, version, type and message id are bound (14 bytes for a
chunk, 6 for a single message). Excluding the countdown is safe: it is transport
framing, not content, and tampering with it can only cause a reassembly failure —
which the gap and ordering checks already detect, and which the UTXO chain makes
structurally hard in the first place. *This was found by a test during
implementation, not by inspection.*

The **countdown** is borrowed from the operator's Doginals design (`doginals.js:476`),
where the last chunk carries index 0. Completion is then self-describing: an
abandoned chain never reaches 0, so an incomplete message is always
distinguishable from a complete one — no separate "seal" transaction needed.

Ordering and sender-binding come from the **UTXO chain**: each transaction spends
the previous one's change output, so chunks cannot be reordered, skipped, or
extended by anyone else. Structural, not conventional.

### ⚠️ Correction: there are THREE dust thresholds, not two

Found by the Step 4 standardness tests, which is the only reason it was found at
all. `fundrawtransaction` refused every transaction with "Transaction amount too
small".

| Threshold | Value | Enforced by | Source |
|---|---|---|---|
| `DEFAULT_HARD_DUST_LIMIT` | 0.001 PEP | relay / standardness | `policy/policy.h:81` |
| `DEFAULT_DUST_LIMIT` | 0.01 PEP | extra-fee rule | `policy/policy.h:70` |
| **`DEFAULT_DISCARD_THRESHOLD`** | **0.01 PEP** | **the wallet will not CREATE smaller outputs** | `wallet/wallet.h:68` |

Earlier sections of this document used the relay limit of 0.001 PEP. That is
correct for what the *network* will relay, but we fund through the node's wallet,
and **the wallet's own floor binds first**. Output value is therefore **0.01 PEP**,
ten times what was originally budgeted.

**The dust is still mostly recoverable.** Every Class B multisig output embeds the
sender's redeeming pubkey (`encoding.cpp:37`), so it stays spendable and can be
swept. Genuinely spent per transaction: the single marker output, 0.01 PEP.

### Single-transaction messages

Overhead is **134 bytes** (48 sealed box + 32 sender key + 40 crypto_box +
6 authenticated header copy + 8 cleartext header). Maximum single-transaction
message: **7,512 characters**.

| Message | Payload | Multisig outs | Tx size | Fee | Dust | **Total** | Txs |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 chars | 234 B | 4 | ~678 B | 0.0068 | 0.0500 | **0.0568** | 1 |
| 500 chars | 634 B | 11 | ~1.5 kB | 0.0147 | 0.1200 | **0.1347** | 1 |
| 1,000 chars | 1,134 B | 19 | ~2.4 kB | 0.0237 | 0.2000 | **0.2237** | 1 |
| 2,000 chars | 2,134 B | 36 | ~4.3 kB | 0.0429 | 0.3700 | **0.4129** | 1 |
| 7,500 chars | 7,634 B | 128 | ~14.7 kB | 0.1469 | 1.2900 | **1.4369** | 1 |

All values in PEP. Dust is recoverable; fee is not.

### Chunked messages

| Message | Txs | Chain size | Fee | Dust | Total |
|---:|---:|---:|---:|---:|---:|
| 20,000 chars | 3 | ~44 kB | 0.44 | 3.87 | 4.31 |
| 50,000 chars | 7 | ~103 kB | 1.03 | 9.03 | 10.06 |

The 101 kB unconfirmed-chain size limit (`validation.h:76`) means roughly **6**
full transactions can chain before a confirmation is needed — send a batch, wait
one block (~60 s), continue.

### ⚠️ Correction: the header needs an explicit ciphertext length

Also found in Step 4, by an integration test. Class B pads its final packet with
NULs to a 30-byte boundary and Omni does not strip that padding
(`omnicore.cpp:1263`), so a payload arrives carrying up to 29 bytes the sender
never wrote. The sealed box sees trailing garbage and rejects the entire message.

The M1 notes had already flagged this hazard for inscriptions and said the fix
belonged in the header — and then it was not applied to the messaging header.
It is now: both message headers carry `clen`, the exact ciphertext length, and the
reader truncates to it before opening.

Stripping trailing NULs instead was considered and rejected: ciphertext ends in
NUL roughly one time in 256, so that would silently corrupt about 0.4 % of
messages.

Resulting header sizes: **8 bytes** single (magic 4, version 1, type 1, clen 2),
**18 bytes** chunked (+ msg_id 8, countdown 2). Authenticated copy: 6 and 14
bytes respectively — `clen` and `countdown` are transport framing and are
deliberately not bound.

### Comparison: Doginals-style P2SH (not built, per the brief)

| Carrier | Bytes/tx | Atomic | Ordering | Mainnet-standard |
|---|---|---|---|---|
| Class C `OP_RETURN` | 72 | ✅ | needs index | ✅ |
| **Class B multisig** | **7,646** | ✅ ≤7,646 | n/a, single tx | ✅ |
| Doginals P2SH scriptSig | ~1,500 | ❌ | UTXO chain | ✅ |

The P2SH technique is sound and mainnet-standard — its 1,500-byte constant is
tuned precisely to the 1,650-byte `scriptsig-size` ceiling
(`policy/policy.cpp:86`). It simply carries **5× less** per transaction than Class
B. We take its *mechanism* (UTXO chaining, countdown index) and not its carrier.

---

## 5. Delivery and discovery

### Trial decryption — **recommended default**

Scan every `arcm` payload and attempt a sealed-box open. **Measured on this
machine: 227 µs per attempt, 4,409/sec.**

| Messages on chain | Scan time |
|---:|---:|
| 10,000 | 2.3 s |
| 100,000 | 23 s |
| 1,000,000 | 3.8 min |

And this is the *worst* case — the indexer already stores every `arcm` payload, so
a scan is incremental, and only new messages are ever tried.

### Notification output

A dust output to the recipient's address makes lookup an index query rather than a
scan. It also **publicly and permanently links sender to recipient**.

### Recommendation

**Trial decryption by default; notification output only as an explicit per-message
opt-in.**

Content confidentiality is the easy half. The social graph is the harder and more
valuable target, and a notification output hands it over in exchange for saving
23 seconds per 100,000 messages. That is a bad trade, and it is irreversible —
the link is on-chain forever.

An opt-in is still worth having: a public announcement channel, or a
correspondent whose relationship with you is already public, loses nothing.

---

## 6. Threat model, stated plainly

**Hidden:** message content, and the sender's messaging identity (it is inside the
sealed box).

**Public and permanent, for every message:**

- the **sender's wallet address** — Class B deobfuscation is seeded with it, so it
  is structurally necessary, not incidental;
- the **timing**, to the block;
- the **approximate size** — transaction size reveals message length to within 30
  bytes;
- the **fee paid**;
- the **recipient**, if a notification output is used.

**Permanence.** Ciphertext on-chain cannot be deleted. There is **no forward
secrecy**: the sealed box encrypts to a long-term recipient key, so **if that key
leaks in ten years, every message ever sent to it becomes readable.** Key rotation
limits future exposure but does nothing for the past. This is the single most
important property for a user to understand.

**Obfuscation is not encryption.** Class B's keystream is derived from the public
sender address (`parsing.cpp:108-131`), so anyone can strip it. It provides zero
confidentiality. Our confidentiality comes entirely from the sealed box.

**Testnet is not durable.** Testnet chains can be reset or deeply reorganised.
Messages may simply vanish. Since the Messenger is testnet-only (D-010), this is a
permanent property of the product, not a testing caveat.

**Metadata analysis.** Even without notification outputs, an observer sees a
pattern of transactions from an address, with sizes and timings. Correlating that
with other activity is a real attack. We do not defend against it.

**Not defended against:** a compromised node, a compromised machine, a keylogged
passphrase, or coercion.

---

## 7. Mainnet standardness

Every transaction type must pass mainnet rules. *Verified in source* against
Pepecoin v1.1.0 (identical in Dogecoin 1.14.99):

| Rule | Limit | Our worst case | Source |
|---|---|---|---|
| Bare multisig relayed | `DEFAULT_PERMIT_BAREMULTISIG = true` | required | `validation.h:143` |
| Multisig shape | x-of-3 standard | 1-of-3 | `policy/policy.cpp:41-49` |
| Dust (hard) | 0.001 PEP | every output at or above | `policy/policy.h:81` |
| `OP_RETURN` script | ≤ 83 bytes | 42 bytes (announcement) | `script/standard.h:30` |
| `OP_RETURN` count | ≤ 1 per tx | 1, and never alongside Class B | `policy/policy.cpp:115-119` |
| Tx size | `MAX_STANDARD_TX_WEIGHT` | ~14.7 kB at maximum | `policy/policy.cpp:73` |
| Unconfirmed ancestors | 25 count / 101 kB | batched to stay under | `validation.h:74,76` |

**This is asserted, not yet proven.** Step 4 proves it by running a regtest node
with `-acceptnonstdtxn=0` and confirming every transaction the tool builds is
accepted there. That is the only evidence that counts, because testnet's
`fRequireStandard = false` would accept transactions mainnet rejects.

---

## 8. Open questions for the operator

1. **Ed25519 identity chain for rotation** — v1 as described, or now? It closes
   the address-compromise gap at the cost of 64 bytes per announcement and a
   second key to protect.
2. **Message size cap in the UI.** 7,514 characters is one transaction; beyond
   that costs multiple transactions and confirmation waits. Cap at one transaction
   by default, or allow longer with a warning?
3. **Do we need a "read receipt" or delivery confirmation?** Both would be
   additional on-chain messages, with their own metadata leakage.
4. **Passphrase policy.** Enforce a minimum length or a strength estimate, or
   accept whatever the user provides with a warning?
