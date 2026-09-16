# DogecoinArcade — what it does

Everything the application can do, what each thing costs, and what it
deliberately does not do. Written against the code; where a number appears it
was measured rather than estimated.

---

## The shape of it

DogecoinArcade is a **sidecar**: it runs beside Pepecoin Core (or Dogecoin
Core) on your own machine and reads the chain the node already has. It never
holds your keys — the node's wallet does — and it never asks for a passphrase.
Everything it knows, it learned by replaying blocks, which means two
installations reading the same chain agree without talking to each other.

| | |
|---|---|
| Interface | `http://127.0.0.1:8420`, bound to loopback because it can spend |
| Chains | Pepecoin and Dogecoin, mainnet and testnet |
| Messaging | **testnet only**, by design (D-010) |
| Tokens, inscriptions, @tags | mainnet and testnet |
| Storage | your node's datadir for the chain; `~/.dogecoinarcade` for what the app remembers |

---

## Messages

Private messages between two people, encrypted end to end and carried on the
chain.

* **Read from the mempool, not only from blocks.** A message is a
  transaction, so waiting for a block meant waiting a block; the scanner
  reads what is on its way as well, and a message usually appears within
  seconds. It says "in the pool, not in a block yet" until its block lands.
  Balances, the order book and inscriptions are still read from blocks and
  only from blocks — those are what two nodes must agree about.
* **Sealed to one key.** Every message is a `crypto_box_seal` to the
  recipient's X25519 key, with the sender authenticated inside the envelope.
  A forged sender fails inner authentication and is recorded as a forgery
  rather than shown.
* **Your own copy is the only one you will ever have.** A sealed message
  cannot be read back off the chain by the person who sent it — that is the
  encryption doing its job, not a gap — so the plaintext is kept locally when
  it is sent.
* **Long messages and files** are split across several transactions, sealed
  once as a whole. A split wallet funds each piece independently so they all
  go at once rather than one per block.
* **Pictures can be sent Large, Medium or Small.** Re-encoded in the browser
  before anything is uploaded; each button shows what that size actually
  costs. A setting that would come out larger than the original is not
  offered.
* **Progress is honest.** A long send says "36 of 51 transactions in blocks"
  and is marked confirmed only when all of it is — which is also when the
  recipient can read it.

Costs a transaction fee, and for a file, dust in the outputs that carry it.
Both are shown before anything is sent.

---

## The public board

Channels anybody can read. Nothing here is encrypted and nothing can be
deleted — that is the point of them.

* Posts carry a name, text and optionally a file, in the same chunked carriage
  as a private message but unsealed, so any node can reassemble one with no key
  and no identity.
* **A picture is a post**: you can post one with nothing typed.
* **Name an inscription and the board shows a card for it** — its number,
  name, type and size as *this* node's index has them, with a button to the
  viewer. Never the inscription's own page: a post is written by a stranger,
  an inscription can be a page of scripts, and the viewer is the one place
  with a sandbox around it. A txid this node has not indexed stays text.
* Channels are just names. There is no step to create one.
* Loads 40 posts at a time with a way back through older ones.

---

## Tokens

An Omni-style token ledger, indexed from the chain.

* **Create** a token with a fixed supply, or a managed one you can grant and
  revoke against later.
* **Send** tokens to an address, **grant** and **revoke** on a managed token,
  and **hand the issuer role** to somebody else.
* Balances, holders and history come from replaying the chain, so every node
  agrees. A transaction the engine cannot understand stops the index rather
  than being skipped — a silently skipped transaction is how two nodes come to
  disagree about who owns what.
* Mainnet and testnet are separate ledgers with their own start blocks.

---

## NFTs

A file written onto the chain **in full and uncompressed**, owned by an
address. Called an inscription in the code and on the wire, and NFTs in the
interface, because that is the word people arrive with.

* **Any file, any size.** There is no policy ceiling. The only limit is
  arithmetic: the countdown that marks the last piece is two bytes, so 65,536
  pieces — about 498 MB.
* **An immutable JSON field** travels with it, inscribed with the content and
  covered by its hash. It cannot be edited afterwards, so it is checked for
  validity before it is paid for.
* **Owned and transferable.** An inscription belongs to an address and moves
  only when its owner sends it. Bound to an address rather than to a
  satoshi, so spending your coins never moves your inscriptions by accident.
* **Numbered** in chain order at the moment the last piece lands, so every
  node replaying the same chain assigns the same numbers.
* **Verified on reassembly** against the SHA-256 in its manifest: a missing
  piece reads as missing rather than as a broken picture.

What it costs, measured:

| File | Transactions | Chain | Fee | Dust | Total | After sweeping |
|---|---|---|---|---|---|---|
| 100 KB | 14 | 205 KB | 2.1 | 18.1 | 20.1 | **2.1** |
| 1 MB | 138 | 2.0 MB | 20.2 | 178.0 | 198.3 | **20.2** |
| 5 MB | 688 | 10.1 MB | 100.9 | 887.5 | 988.4 | **100.9** |

The dust is not lost: every data output is a 1-of-3 including your own key, so
it is spendable again by whoever made the inscription.

### What an inscribed page can call

An inscription can be an HTML page, and that page can ask this node questions —
the same idea as ordinals' recursive endpoints, with the same paths where the
meaning matches.

| Endpoint | Returns |
|---|---|
| `/content/<id>` | the bytes, with the type its creator gave it |
| `/r/inscription/<id>` | owner, creator, number, type, length, hash |
| `/r/metadata/<id>` | the JSON field, parsed |
| `/r/inscriptions` | a page of them, newest first |
| `/r/inscriptions/<address>` | what an address owns |
| `/r/collections` | the collections on the chain, newest first |
| `/r/collection/<creator>/<name>` | one collection: its items in edition order, and its traits |
| `/r/blockheight`, `/r/blocktime` | where the chain is |
| `/r/balances/<address>` | token balances at any address |
| `/r/tag/<name>`, `/r/address/<address>` | who holds a @tag, and the other way |
| `/r/wallet` | which wallet is looking — can be switched off |
| `/r/send` | **asks** the wallet to send coins, tokens or an inscription; the person approves |
| `/r/tx/<txid>` | whether a transaction is confirmed, and how deep |
| `/r/storage.js` | `arcade.storage`: what the wallet remembers for the page, since a sandbox has no `localStorage` |
| `/r/node.js` | `arcade.node`: node-to-node messages sent as the wallet the page runs in, and the replies to them |
| `/r/swap.js` | `arcade.swap`: a shop page's listings, and a buy button that ends in one transaction both sides signed |
| `/r/owner.js` | `arcade.owner`: a page sending, unasked, from the wallet that created it and holds it |

An inscription is `<id>` by its number or its creating transaction id.

**Why this is safe to serve.** Inscribed code is written by strangers and runs
in the browser of a wallet that can spend. It is loaded into a frame with
`sandbox` and no `allow-same-origin`, so it has an opaque origin: no cookies, no
access to the page around it, no navigating the top window. These endpoints are
the **only** URLs in the application that answer a cross-origin request — every
other page says nothing about CORS, so an inscription cannot read them. Content
is served with `sandbox` in its own policy and `nosniff`, and a type we will not
render is handed over as a download rather than guessed at.

---

## Collections

A set of inscriptions that belong together — a HashLips build, or anything that
names itself the same way.

* **Filed from the chain, not from a form.** An inscription whose JSON has a
  `name` like `Doge Punks #12` is filed under the collection *Doge Punks* as
  edition 12, by its creator — the shape every HashLips build already writes.
  A JSON `collection` field names the set outright. The rule reads only what
  is on the chain, so every node files the same sets identically, and a set
  cannot be edited, hijacked or renamed afterwards.
* **One set is one creator's.** Two addresses inscribing the same name make two
  collections, so nobody can slip their own items into somebody else's.
* **Ordered by edition**, with the traits counted across the set, so a page
  shows *Blue background — 3 of 5, 60%* without anybody having filled that in.
* **A wizard inscribes a whole build.** Point it at a HashLips `build` folder
  (or upload the `json` and `images` folders from a phone). It reads
  `_metadata.json`, shows every item with its picture and its JSON, prices the
  whole run, and asks once. Each item's own HashLips JSON goes on the chain in
  its inscription's JSON field, compacted and otherwise untouched.
* **Pause, resume, survive a crash.** Every transaction is written to disk the
  moment the node takes it, before the next is built. Pause stops between
  pieces; resume carries on from the next unsent one; a crash or a power cut
  restarts the run on the next start, and pieces the node took but the
  computer never wrote down are found again in the chain index rather than
  paid for twice.
* **Funded in batches.** The wallet is split into outputs sized for the
  largest of the next 120 items, so each piece has its own confirmed coin and
  a thousand-item run does not wait a block per transaction.
* **Messages still go through** while a run is on: the send lock is held one
  item at a time.
* **It can sell itself.** Tick the mintpad at the pricing step, name a price
  in coins or in a token, and the run inscribes a mintpad when the last item
  is on its way — a page that shows the collection and hands over a random
  one for that price, to anyone who opens it in a wallet. Last, and only if
  nothing failed: a pad for a collection half of which never went up would be
  selling things that do not exist. Untick it and nothing extra is inscribed.

---

## Shops and swaps

A shop is an inscription. Its JSON lists what it sells and where its owner's
node listens; the page inscribed with it is the storefront.

* **The terms are on the chain.** `give` and `take` per listing — coins, a
  token amount, an inscription, or a random item of the owner's collection
  that the shop still holds, which is what a minting event is. Both wallets
  read them from their own ledgers; nobody's page is trusted about a price.
* **One transaction or nothing.** A swap is a Class C payload naming both
  legs, in a transaction both parties sign. The engine moves both legs when
  the block lands or refuses the whole thing: a buyer who routes the
  seller's own coins back has paid nothing and gets nothing.
* **A sale reserves what it sold** until its block is indexed. Between
  broadcast and indexing the ledger still names the seller as the owner of
  what was just sold, so without this a second buyer is offered the same
  piece — refused by the engine when it lands, but only after they have paid
  a message fee to be told no.
* **The seller is not asked.** The shopkeeper answers every order from the
  block watcher's thread: checks it still holds the item, checks the buyer
  holds the price, locks one of its outputs and sends an offer; then checks
  the buyer's half-signed transaction against exactly that offer before it
  signs and broadcasts. It cannot sell what the JSON does not list, sell it
  for less, or sell from a shop this wallet did not create and hold.
* **It answers orders, not answers.** An answer carries the txid it answers
  and whether it worked; an order carries neither. A shopkeeper that could
  not tell them apart would answer the other shop's refusals for ever, a
  message a block out of each wallet, which is what two of these nodes did
  for eleven minutes before it was fixed.
* **The buyer is asked, once.** The offer arrives as a node-to-node message,
  the buyer's wallet builds the transaction and shows it in the approvals
  pop-up like any other send, and Approve signs the buyer's half only.
* **Any kind for any kind**: tokens for coins, a random NFT for tokens, one
  inscription for another. Send the shop inscription away and the shop is
  closed.
* **A page of your own may send without asking.** A page this wallet created
  and holds is the owner's own words: `arcade.owner.send` builds, signs and
  broadcasts at once, and the send is listed with the approvals as one that
  came from a page of your own.

Testnet only, like the messages it travels on (D-010). Swaps are read from a
starting height per chain (`swaps_from`), so a swap in a block before it is
recorded as unread rather than as an old transaction meaning something new.

---

## The exchange

Everything for sale on a chain, read from the chain itself. Four tabs, four
questions.

* **Offers** — what somebody has offered for an NFT of yours, and what you
  have offered for somebody else's. Accept or refuse; accepting builds the
  seller's half of one transaction and the buyer's wallet signs the other.
* **Mintpads** — every mintpad with pieces left. A pad appears because it is
  on the chain, not because anybody listed it, and disappears when it mints
  out.
* **Tokens** — every token paired against the chain's coin, with its last
  price and the day's move. Click a pair for its candle chart, its **order
  book** — asks and bids, best price first — and the form that puts an order
  on it. An order rests on the chain until it is cancelled.
  * **An ask holds its tokens back**, so the book cannot show what the seller
    has since spent. **A bid holds nothing**: no covenant on this chain can
    reserve coins and still let a wallet spend, so a bid is an intent and the
    page says so.
  * **Nothing is matched by the engine.** A fill is a swap — one transaction
    carrying both legs, which is the only way a coin leg and a token leg move
    together. The book is what is on offer; the swap is how it settles.
* **NFTs** — single NFTs for sale, part of a collection or not. An NFT
  that has moved drops off by itself: the listing is read from the chain
  every time, so there is nothing to un-list.

**Charts of what actually traded.** Tokens and NFTs both get candles, built
from the swaps on the chain — there is no order book to draw, so what is
drawn is what people paid. A day with no trade is a dot on the axis, never a
line ruled to the next price. **One chart per collection**, and one currency
to a chart: what a Goofball goes for says nothing about what a Doge Punk
goes for, and coins and tokens on one axis is adding pounds to metres. Drawn
as SVG in the page: no charting library is fetched from anywhere, and the
page's CSP would stop it if it tried.

**An offer is made with what you have.** The form lists the tokens this
wallet holds with their balances, and the coin balance beside the coins
option; it does not draw a token picker when the wallet holds none. The door
checks again when the form comes back. Offering something you do not hold is
a fee spent to be told no by somebody else, a block later, for something
your own wallet could see at once.

**A wallet is told before it pays.** A buyer's side of a swap is two
transactions — the half they sign and the message that carries it — so it
needs two confirmed outputs. A wallet with one is refused before the order
goes out, not three transactions later, and told to split the address.

**An offer can be made on any NFT**, listed or not: open one you do not own
and press *Make offer*. It is **said on the chain**, not sent to anybody:
somebody holding an NFT never asked to be reachable, and most have published
no key at all, so the holder's own wallet finds the offer by watching its own
things — any address, no announcement needed. It fits one OP_RETURN, so it
costs a flat fee and locks nothing.

An offer already accepted says so, and says who it is waiting for and until
when, rather than offering to accept it again.

It is not a PSBT — the buyer cannot build the transaction, because the
seller's own output has to be its first input. The holder's *Accept* builds
the seller's half and sends it to the buyer, whose wallet signs its half
automatically **only if it names the same item and the same price** that was
offered. Which is why making an offer needs *your* key published: you are the
one asking for an answer. An offer on something of yours is never answered
without you.

---

## Approvals

A page or a program can ask this wallet to send something. It cannot send.

* **One queue for everything that has value**: coins, tokens,
  inscriptions and the buyer's half of a swap, asked for by an inscribed
  page in its sandbox (`POST /r/send`, `arcade.swap`) or by a program on the
  bot RPC (`da_requestsend`, `da_requesttoken`, `da_requestinscription`).
* **Nothing is built on the caller's word.** A request is a row that says
  what, to whom and why. When you look at it, the transaction is built and
  shown — fee, every output, the change coming back — and goes out only when
  you press *Approve and send*. Refusing costs nothing.
* **Shown everywhere you are**: in a pop-up in front of the inscription that
  asked, the moment it asks; a banner on every other page; and on the phone
  over the remote tunnel. Only a request made while that page is open pops up
  over it; what was already waiting is listed under the page with a link, not
  opened over a page that never asked. The page that asked cannot see the
  pop-up or press anything in it, and it is told afterwards whether the
  transaction is confirmed, and how deep.
* **What can be refused at once is refused at once**: an address on the other
  chain, a token that does not exist, an inscription that is not yours to
  give. A request nobody answers within an hour expires; no more than twenty
  wait at once.
* **What was never asked is still written down.** A page of your own and a
  shop of your own may send without asking — they are your own words and your
  own listing. Both are written to the same queue the moment they go, already
  decided, so the one page a person reads shows everything this wallet signed
  for something that is not a person. A shop's line reads from the shop's
  side: "sold 100 Arcade Test for 1.00000000 coins".
* The caller's own words — what it calls itself, why it asks — are shown in
  quotes as exactly that.

---

## @tags

A handle that belongs to an address. `@robin`.

* **One name to an address, one address to a name.** A name pointing at two
  people is not a name.
* **First claim wins, in chain order** — the only ordering every node already
  agrees on.
* **Changing your tag frees the old one** for anybody else to take. Holding
  names you no longer use is how a namespace fills with nothing.
* **Transferable** to another address, by its holder, and refused rather than
  dropped on an address that already holds one.
* Lower case, `a-z 0-9 _`, 2 to 24 characters. No hyphens (too easily confused
  with the several dashes that are not hyphens), no dots (they read as domains),
  and a short reserved list for names a reader would take as official.

A claim is 13 bytes, so it fits an OP_RETURN: naming yourself costs one small
transaction. **Claim or change one from the address book**, in two steps like
a send: the transaction is shown before anything is broadcast, and a name
that is malformed, reserved, already yours or already somebody's is refused
before it costs anything.

* **Shown wherever this wallet says who somebody is**: the messenger's
  conversation list and header, and the address book. A name you typed wins —
  it is yours — then the @tag, then the address. A stale or missing index
  shows the address rather than a name it cannot vouch for.
* **Published with your key.** An announcement carries your key, your address
  on this chain, the same wallet's address on the other chain, and your @tag.
  The tag is the part a reader can *check*: their own index says who holds the
  name, and an announcement that disagrees is shown as disagreeing rather than
  believed. The name you type is no longer published at all — it travels with
  a first message, and stays yours to give people in your own book.

---

## The address book

Who is who, and where to pay them. Stored only on this computer — not
published, not on any chain, and not visible to anyone you message.

* A contact holds a name, a testnet address, a mainnet address, a contact code
  and your own notes. The contact code is their identity and can only be set
  when the entry is created.
* **Names found by scanning are kept apart from names you typed.** A name you
  wrote always wins over one read off the chain, because a published name is a
  claim rather than proof.
* **Introduce myself** sends your @tag and your addresses with a first message
  to somebody new, so they know who is writing. Untick it to stay anonymous.
  There is no name to type: your @tag is the only name this wallet gives out
  for you, because it is the only one a reader can check.
* *Scan for published addresses* lists people who have published a messaging
  key on the chain and are not already in your book. Publishing is optional, so
  plenty of people will never appear there.

---

## Remote

Reach the wallet from your phone, from anywhere, for a while.

Press *Open the tunnel*, choose 4 hours, 12 hours or a day, and scan the QR
code. No account, no port forwarding, nothing to change on your router.

**What protects it** — not the random hostname, which is a URL and therefore
not a secret:

| | |
|---|---|
| A key | The QR carries a key as well as the address. The address alone opens a locked page. |
| A deadline | It closes itself. A door left open by accident is the failure this exists to prevent. |
| No bot RPC | `/rpc/*` is refused through the tunnel outright. It has its own key and it can spend. |
| A door for the pages | An inscribed page runs in a sandbox that can carry no cookie, so it gets a second hostname of its own, opened alongside and never shown: it serves only the page's content and the page API (`/content/*`, `/r/*`), and answers 404 to everything else. |

It tells a phone from this machine by Cloudflare's own headers, not by address —
cloudflared runs on your machine and connects to `127.0.0.1`, so a phone in
another country arrives from localhost.

---

## The bot RPC

A JSON-RPC endpoint for programs: airdrop bots, faucets, leaderboards, anything
that reads balances or moves tokens without a person clicking.

* Speaks **Omni Core's method names** (`omni_getbalance`, `omni_send`,
  `omni_listproperties`…), so anyone who has scripted against Omni knows it.
* **Cookie authentication** like bitcoind: `~/.dogecoinarcade/rpc.cookie`,
  mode 0600, rewritten at every start. A browser tab cannot present it.
* **Sending is two calls.** `omni_send*` builds, funds and signs but returns the
  transaction **unsent** with its fee; `omni_broadcast` sends exactly those
  bytes. A bot sees the fee before it pays it.
* `arcade-rpc` is the shell client; `examples/airdrop.py` is a working bot.

See [Bot RPC](bot-rpc.html) for the full method list.

---

## Node-to-node messages

An encrypted channel for one arcade to talk to another — for marketplaces, bots
and anything built on top.

* The same sealed envelope as a private message, so the same guarantees:
  addressed to one key, authenticated as from yours.
* **Its own envelope type**, so machine traffic never lands in a human's
  conversation, and its own cursor, so a program can read a queue and resume
  exactly where it stopped.
* **Every message says what API it speaks**: a protocol number and four bytes
  over the API surface itself, inside the ciphertext where it cannot be edited
  in flight. Two nodes on different builds that speak the same API are
  compatible, and mismatch is reported rather than enforced.
* Methods: `da_identity`, `da_send`, `da_broadcast`, `da_inbox`, `da_markread`.
* **An inscribed page can use it too**: `arcade.node` sends as the wallet the
  page runs in and reads only the answers to what that page sent. No key of
  its own, no approval — a message moves nothing and lives on testnet — and
  thirty an hour per page, so a page in a loop is a nuisance and not a cost.

On the chain rather than peer-to-peer because a custom P2P message does not
propagate — Pepecoin tolerates an unknown command and drops it without
forwarding. The mempool already gossips to every node in seconds.

---

## Wallets, keys and backup

* **One address a chain**, holding the coins, the tokens and the NFTs. Every
  awkward thing a wallet of many addresses does came from the alternative: a
  token stranded on a receiving address with no coins to move it, an NFT on
  one address and the published key on another, a balance in three piles
  needing three transactions. Change comes back to the sender everywhere.
  Address reuse links a wallet's activity together for anyone reading the
  chain; that is the price, and this ledger is public anyway.
* **What lands elsewhere walks home by itself** — a few things every couple
  of blocks, coins first, because a token cannot move off an address that
  cannot pay its own fee. It waits for anything else that is sending, so it
  cannot collide with a collection run part way through, and it never sees
  an output held for an open offer. Testnet only: sweeping somebody's
  mainnet coins without asking is spending their money for them.
* **On a real chain, only the wallet this application made.** A node there is
  usually somebody's own wallet too, and those coins are not the arcade's to
  show, to spend or to gather; what the arcade made is filed under its own
  accounts. On a test chain the node exists to run this, so every address in
  it is the arcade's and everything is gathered. An address carries its chain
  in its version byte, so nothing has to remember to pass a flag.
* **Three tabs: Wallet, Tokens, NFTs.** The wallet answers what you have —
  coins per chain, token balances with the form that sends them, and the
  inscriptions you hold with a Send on each. The Tokens and NFTs *sections*
  answer the other question, what exists on the chain and how to make more.
  Each points at the other.
* **One balance a token, not one an address.** A wallet holding a token on
  four addresses holds one balance of it; the addresses are behind "where it
  sits". A send comes out of one address, so the wallet picks the *smallest*
  pile that can cover it — spending a small pile up rather than breaking a
  large one — preferring one that can also pay its own fee.
* **More than one pile is more than one transaction**, not a refusal. When no
  single pile covers the amount, the wallet uses the largest first and takes
  the remainder from the smallest pile that covers it. All of them are shown
  before any is broadcast, and one yes sends the set.
* One wallet per chain, with a two-step send: the decoded transaction and its
  fee are shown before anything is broadcast.
* **Change comes back to the address that paid**, so the messaging identity is
  not quietly emptied by an ordinary send.
* **Split for fast sending** cuts the wallet into many small outputs, so the
  pieces of a long message fund themselves independently and go at once.
* **Fees are what the miner counts.** The node prices a transaction by its
  *virtual* size — bytes, or twenty per signature operation, whichever is
  more — and a bare multisig output counts twenty. Every funding call goes
  through `arcade/fees.py`, which counts the sigops of what it just built
  and raises the fee rate until the block assembler would take it at
  0.01 PEP/kB; a plain send is unchanged, an inscription chunk pays about
  0.004 PEP per data output. Before this, testnet mined one piece per
  block.
* **Mine a testnet block** from the wallet page, any time. The node hashes
  in batches of about thirty seconds, the page says how many hashes so far
  and how long a block takes at today's difficulty, and Stop mining stops
  after the batch it is on. Testnet only, because mainnet has miners.
* Your wallet **is** your identity. There is no passphrase and nothing to write
  down: back up `wallet.dat` and everything comes back with it — coins,
  messages and your published key.
* **Publishing your key** lives on the address book, beside the name it
  publishes. There is no page of keys and fingerprints: a fingerprint is
  plumbing, and showing one asks a person to check something they have no way
  to check.

---

## What it does not do

* **No custom peer-to-peer protocol.** See above: it would not propagate.
* **No encrypted messaging on mainnet.** Testnet only, deliberately (D-010).
* **No send-to-owners** (Omni type 3). The engine does not implement it and a
  transaction of that type would stop the index.
* **No automatic matching.** There is an order book, but the engine does not
  cross it: with the chain's own coin on one side, somebody has to sign, so a
  fill is a swap between two wallets rather than a match made by the rules.
* **It cannot read your sent messages back off the chain.** Nothing can. That
  is what sealing to the recipient means.
