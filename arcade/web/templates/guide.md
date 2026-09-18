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

**Red means somebody said something.** A channel with posts you have not seen
carries a red count in the list, and so does a conversation with unread
messages. Opening one clears its own count and nobody else's — reading
#trading does not silence #releases. Your own posts are never news to you.


Channels anybody can read. Nothing here is encrypted and nothing can be
deleted — that is the point of them.

* Posts carry a name, text and optionally a file, in the same chunked carriage
  as a private message but unsealed, so any node can reassemble one with no key
  and no identity.
* **A picture is a post**: you can post one with nothing typed.
* **No confirmation step on testnet**, for a post or a message: the coins are
  free and what it cost is reported as it happens. Mainnet still shows the
  bill first.
* **A board open on another screen refreshes itself** when something is
  posted, rather than waiting to be reloaded.
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
* **One name, one token.** A name already issued on that chain is refused —
  first claim wins, in chain order. Names are compared folded (lower case,
  letters and digits only), so *Dogecoin Arcade*, *dogecoin arcade* and
  *Dogecoin-Arcade* are the same name and the rule cannot be stepped around
  with the space bar. The wallet checks before it builds anything, the
  mempool included: a name claimed by a transaction still waiting for its
  block is claimed. Managed and fixed share one namespace, since a reader
  cannot see the difference between them.
* **A name is plain ASCII.** Letters, digits, spaces and punctuation, with at
  least one letter or digit. Characters from other alphabets are refused
  rather than quietly ignored: *Dogecoin* written with a Cyrillic *o* looks
  identical on every page and would be a different token, and a rule that
  deletes what it does not recognise is a rule that can be worked around by
  typing more of it. Lookalikes *within* ASCII — a zero for an O, a one for
  an l — are still allowed: they are visible on the page, and folding them
  would collide names people mean to be different (*A1*, *W3*, *COIN2*).
* **Send** tokens to an address, **grant** and **revoke** on a managed token,
  and **hand the issuer role** to somebody else.
* Balances, holders and history come from replaying the chain, so every node
  agrees. A transaction the engine cannot understand stops the index rather
  than being skipped — a silently skipped transaction is how two nodes come to
  disagree about who owns what.
* Mainnet and testnet are separate ledgers with their own start blocks. When
  a release moves a start block above everything a node has indexed, that
  index moves itself aside — kept, named after the new floor — and rebuilds
  from it. Nothing is deleted: a floor published wrong is undone by renaming
  a file back.

---

**A token can wear an inscription as its icon.** Give the inscription's id
when creating a token, or pick one of your own pictures from the list beside
the field. Inscribe the picture from the NFTs page first if it is not on the
chain yet — one thing at a time, and the id is what the token needs. The icon
is shown wherever the token is — the markets table, its market page, the token list.
A token without one gets a plain mark, never its initials: two letters of a
name read as a ticker symbol, and nothing here has one.
The picture is an inscription on the same chain, so it is served by whichever
node is looking at it rather than by a website that can go away.

An Omni issuance has five strings and no sixth, so the icon rides in `data`:
the inscription's id alone when there is nothing else to say, and
`{"about": "...", "icon": "<id>"}` when there is. A link pasted into the link
field is read as an icon too. **A token with an icon is carried as Class B** —
an id is 64 characters and a Class C payload is 76 for the whole issuance,
name and all, so no shortening would fit it into one OP_RETURN. That costs the
multisig encoding and some dust you can sweep back; the confirm screen shows
it before anything is paid. None of it is
consensus — the property on the chain is exactly what it always was, and a
node that never heard of the convention shows the same token with the JSON as
its description. It costs about thirty bytes of the issuance, and an
issuance cannot be corrected afterwards, so an icon that is not an
inscription on this chain is refused before it is paid for.

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

**The file is on the chain, and only there.** This node does not keep a copy
of an inscription's bytes: it keeps which transactions carried them, and reads
them back when somebody asks — reassembling, checking the result against the
manifest's own sha256, and handing it over. That is a stronger guarantee than
a stored copy, not a weaker one: a stored blob is trusted, a reassembled one
is proved. Recently-served files are cached briefly so a wall of a hundred
tiles is not a hundred trips to the node. A node that would rather hold the
bytes — a gallery answering strangers — can say so.


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
  collections, so nobody can slip their own items into somebody else's — not
  while you are inscribing it, not afterwards.
* **A set is sealed by its own #1.** One piece per edition, first claim wins:
  a build inscribed twice does not double the set, because the second #7 is
  not admitted to it. And when the #1 declares a `supply`, the set is full at
  that many — nothing joins after, and nothing numbered past it joins at all.
  The wizard writes the size on every set it inscribes, so a page can say
  *17 of 100* as a fact read off the chain. A piece that is refused is still
  an inscription: paid for, owned, sellable — just not a member of that set.
* **A set is not inscribed twice by accident either.** The wizard refuses a
  build whose collection is already on the chain from that address, or is
  already being inscribed here, at the pricing step and again at the press —
  before the node is asked anything, because a second run is a bill for a
  second copy of every piece.
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

**A set can describe itself, on its #1.** The first piece is the one a
collection is known by, so that is where collection-level details are read
from. The **collection wizard** asks for them at step 2 — description,
website, Twitter, and a thumbnail — and writes them onto that piece; or put
them there yourself, in a `collection` object beside the name:

```json
{"name": "Goofball #1", "edition": 1,
 "collection": {"name": "Goofball",
                "description": "100 hand drawn goofballs",
                "url": "https://goofball.example",
                "twitter": "@goofballs",
                "supply": 100}}
```

`description`, `url` (or `website`, or `external_url`), `twitter`, `discord`,
`telegram`, `supply`, `artist` and `icon` are read — **field by field**, the
object first and the item's own top level after, so adding one of them never
hides another. (A HashLips `description` at the top level keeps being read
after the wizard writes an object for the thumbnail.) Everything else is ignored, text
is capped, and a link that is not http(s) is not shown as a link — an
inscription is written by anybody and this ends up on a page. A HashLips
build already writes `description` and `external_url` at the top level of
every item, and those are read whether or not there is an object.

**The set's face is its #1 unless it says otherwise.** A marketplace shows
the first piece, because that is the one people recognise — but `icon` names
another inscription to use instead, and the wizard takes its id at step 2. It is an inscription on this chain either way: a face on
somebody's website is a face that disappears when the hosting does. One this
node cannot draw falls back to #1 rather than to a broken image.

**Membership is not decided by any of that.** A piece belongs to the set its
own `name` or a `collection` STRING says, exactly as before — an object is
not a string, so a set that describes itself is still filed by its name. Two
nodes cannot disagree about what is in a collection because one of them
understood a richer JSON.

**The IPFS pointer is not inscribed.** A HashLips build writes
`"image": "ipfs://NewUriToReplace/1.png"` into every item. Here the picture
IS the inscription — its content, in full, served by every node that has it —
so that field names a place the art is not, at about fifty bytes an item that
somebody pays for and nobody can follow. It is dropped before the set is
priced. An `image` that is a real http(s) link is left alone: that resolves,
and it is the creator's decision. Everything else is inscribed exactly as the
build wrote it, including `edition` and `attributes`, which is what
membership, numbering and rarity are read from.

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

**A token can sell itself from a launchpad.** On the token's own page, its
issuer can inscribe a page that sells a fixed lot of the token for a fixed
price — the mintpad's idea with the wall taken out. It is an inscription whose
JSON names a shop, so any node can read it and buying is one swap carrying
both sides; it is written from the address that holds the tokens, and it runs
out when that address no longer holds a lot to sell. **Previewed before it is
inscribed**, with the token's own icon and the price you typed, so what you
pay for is what you already looked at. It appears under Mintpads in the
Exchange by itself.


## The exchange

Everything for sale on a chain, read from the chain itself. Four tabs, four
questions.

* **Offers** — what somebody has offered for an NFT of yours, and what you
  have offered for somebody else's. Accept or refuse; accepting builds the
  seller's half of one transaction and the buyer's wallet signs the other.
* **Mintpads** — every mintpad with pieces left. A pad appears because it is
  on the chain, not because anybody listed it, and disappears when it mints
  out.
* **Tokens** — the markets: every token paired against the chain's coin, with
  its icon, last price, the day's move, high, low, volume and the spread
  between best bid and best ask. Busiest first, and a box to find one by
  name. Click a market for its candle chart, its **order book** — asks above
  the spread, bids below, each row shaded by how much of the side it is —
  its recent trades, and the form that puts an order on it. An order rests on
  the chain until it is cancelled.
  * **An ask holds its tokens back**, so the book cannot show what the seller
    has since spent. **A bid holds nothing**: no covenant on this chain can
    reserve coins and still let a wallet spend, so a bid is an intent and the
    page says so.
  * **Nothing is matched by the engine.** A fill is a swap — one transaction
    carrying both legs, which is the only way a coin leg and a token leg move
    together. The book is what is on offer; the swap is how it settles.
  * **A book that crosses itself fills itself, from either end.** When one of
    your bids sits at or above somebody's ask, this node takes the ask; when
    one of your asks is crossed by somebody's bid, it takes the bid. Whichever
    side is not resting is the one that acts, so nothing waits for the other
    person to notice. It takes the smaller of the two amounts, and a
    **partial fill** leaves the maker's order on the book shrunk by exactly
    what was taken — an ask by the tokens that came out of its reserve, a bid
    by what was bought out of it. Where your own bid was the one that acted,
    it is withdrawn at that price and re-posted for the rest before anything
    is asked for, because nothing else would reduce it in flight. One fill at
    a time per pair, never an order whose maker this node cannot reach, and
    never your own. The switch is on the Overview.
* **NFTs** — the marketplace. It opens on the **popular collections** —
  popular meaning traded, and traded recently, because that is the question a
  market answers — and on **what has just sold**: the piece, what it went for,
  how long ago, who sold it and who holds it now, read from the swaps on the
  chain. Then every collection on the chain, one row each,
  with its **floor**, how many of it are for sale, how many have an offer
  standing, and what it last traded for **in coins** — a piece sold for a
  token is its own market, and one axis cannot hold both. The row's picture
  is **#1 of the set**. Click one and the whole collection opens — **what can
  be bought first** (cheapest first, so the first tile is the floor), then
  the pieces somebody has offered for, then the set **in edition order** —
  #1, #2, #3 — because that is how a set is known and how anybody asks for a
  piece of it. Under those two: **for sale now**, the asks standing on this
  chain, each with a one-press buy. The page opens with the set's #1 as its face, what the set says
  about itself, and a strip of numbers: floor, for sale, offers, owners,
  pieces and what has sold. Every piece is a tile — the picture, its name,
  **who made it and who holds it now**, its price if it has one, and a
  *Make offer* box on every one of them. Shops selling a single NFT are
  listed under the table. An NFT that has moved drops off by itself: the
  listing is read from the chain every time, so there is nothing to un-list.

**Charts of what actually traded.** Tokens and NFTs both get candles, built
from the swaps on the chain — there is no order book to draw, so what is
drawn is what people paid. A day with no trade is a dot on the axis, never a
line ruled to the next price. **One chart per collection**, drawn on that
collection's own page beside the pieces it prices, and one currency to a
chart: what a Goofball goes for says nothing about what a Doge Punk goes
for, and coins and tokens on one axis is adding pounds to metres. Drawn as
SVG in the page: no charting library is fetched from anywhere, and the
page's CSP would stop it if it tried.

**Putting a price on one.** *Sell it* — on any NFT of yours, from
**Wallet → NFTs**, from its tile in a collection, or from the piece's own
page — asks a price and broadcasts an **ask**: one OP_RETURN saying "this
piece, for this much" (47 bytes of payload; about 250 as a whole
transaction, with its inputs, change and signature). It is the other half of an offer, and the two are
symmetrical: an offer is a buyer's word about somebody else's piece, an ask
is the holder's about their own, and both are said on the chain rather than
kept anywhere, so every node has the same book and a seller's wallet can be
switched off without withdrawing the price.

* **It costs a flat fee and locks nothing.** Nothing on this chain can hold
  an inscription back from its owner, and an ask does not pretend to — the
  same reason a bid holds no coins.
* **Only the holder can price a piece**, and the newest ask for a piece is
  the one that counts, so re-pricing is just another ask.
* **A price shows the moment it is broadcast**, marked as being in the
  mempool until its block lands — a marketplace that shows nothing for ten
  minutes looks like a listing that failed. It is checked exactly as a block
  would check it, so the pool never shows what a block would refuse.
* **A piece that moves takes its price with it.** An ask is live only while
  the address that made it still holds what it names, so selling or sending a
  piece withdraws its price with no transaction at all. *Take the price off*
  withdraws one deliberately.
* **Buying is offering exactly what was asked**, in one press: the offer
  reaches the holder's wallet, and the swap moves the piece and the payment
  together or not at all.
* **An offer that meets the price is accepted for you.** A price said in
  public is a promise to sell at it, so the seller's node answers a matching
  offer — same currency, at least the amount — without asking again, exactly
  as it answers an order at a shop. It never sells for less, never sells a
  piece whose price has been taken off or that has moved, and never sells the
  same piece twice; a better offer wins, and at the same price the earliest
  one. Only offers in a block are acted on. The switch is on the Overview,
  under **Selling**; turned off, offers wait for you.

Asks are read from a height, like everything else that makes valid what used
to be invalid; before it, an ask made on that chain would be read by nobody,
and the page says so before the fee.

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

**The @ is punctuation.** Anywhere a name can be typed — a send, a message, a
search — it works with or without it: `@boxa` and `boxa` are the same name.
What tells a name from an address is the shape, not the sigil: a tag is at
most 24 characters of `a-z`, `0-9` and `_`, and an address is 34 of
mixed-case base58.

Case is free when you search, and with an `@` it is free everywhere — `@a test machine`
is plainly a name. A **bare** name in a field that spends is read as a name
only if it is written as one, in lower case, so that a mistyped address is
still answered with "that is not a valid address" rather than "nobody holds
that name".

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

**What it says in the journal.** The interface logs what it did at `info` --
blocks read, a store rebuilt, a sale answered, a release announced -- so
`journalctl -u arcade-web` is a record of the wallet's own decisions rather
than only of its complaints. `--log-level warning` quiets it; `debug` is for
when something is wrong.


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
