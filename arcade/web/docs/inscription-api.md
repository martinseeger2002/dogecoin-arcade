# Writing an inscription that runs

A reference for anyone inscribing an HTML page, a script, or anything that asks
this node questions. Every claim here about what an inscribed page can and
cannot do was measured in a browser, by inscribing a page that tries it — see
`tests/test_inscription_sandbox.py`.

---

## 1. What an inscription is

A file written onto the chain in full and uncompressed, owned by an address,
with an immutable JSON field beside it. It is split across as many transactions
as it needs (7,646 bytes each), reassembled by every node that indexes the
chain, and verified against the SHA-256 in its own manifest.

It is addressed two ways, and both work everywhere an id is accepted:

```
/content/0                                                    by number
/content/2ea169a4c7b1...  (64 hex characters)                 by transaction id
```

Length decides which: 64 characters is always a transaction id, because a hex
string can be all digits and `0000…0001` is a perfectly ordinary txid.

---

## 2. How your page is served

```
GET /content/<id>
Content-Type: <whatever you inscribed>
Access-Control-Allow-Origin: *
X-Content-Type-Options: nosniff
Cache-Control: public, max-age=31536000, immutable
Content-Security-Policy:
  sandbox allow-scripts allow-pointer-lock;
  default-src 'self' data: blob:;
  script-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:;
  style-src  'self' 'unsafe-inline' data:;
  img-src 'self' data: blob:;  media-src 'self' data: blob:;
  font-src 'self' data:;  connect-src 'self';
  frame-src 'self';  object-src 'none';  base-uri 'none';  form-action 'none'
```

The viewer loads it into

```html
<iframe src="/content/<id>" sandbox="allow-scripts allow-pointer-lock"
        referrerpolicy="no-referrer"></iframe>
```

Note what is **not** in that sandbox list: no `allow-same-origin`. Your page
runs in an **opaque origin**. That one omission is what makes it safe to run a
stranger's code inside a wallet that can spend.

### Where your page turns up

Two places, both with the same sandbox:

* **The viewer**, `/inscriptions/<id>/view`, one page at a time.
* **The feed.** When anybody writes `/content/<id>` in a post, an HTML
  inscription is drawn in a frame under that post with exactly the CSP and
  sandbox above, an image is shown inline, and anything the node cannot
  identify stays a link.

So write your page as though several copies of it might be on one screen at
once, none of them focused, some of them below the fold and loaded lazily.
Three things follow. Do not assume you are the only frame, do not assume
anybody has clicked anything before your code runs, and do not use
`window.top` or `parent` for anything -- the sandbox refuses it, and the page
around you is somebody else's feed rather than the viewer you tested in.

Nothing else changes: no `allow-same-origin` there either, the same endpoints
answer, and `location.pathname` is still `/content/<id>` so a page still
knows which inscription it is.

Keep every address **root-relative** (`/r/wallet`, `/content/<id>`), as in
every example here. When the wallet is reached remotely, your page is
served from a hostname of its own that carries nothing but `/content/*` and
`/r/*` — a page that hard-codes a host, or reaches for a wallet page, breaks
there and nowhere else.

### What that means for you, measured

| You try | What happens |
|---|---|
| `<script>` inline in your file | **runs** |
| `eval`, `new Function` | **runs** |
| `document.cookie` | `SecurityError` |
| `parent.document` | `SecurityError` |
| `top.location = …` | `SecurityError` |
| `localStorage`, `sessionStorage` | `SecurityError` — use `arcade.storage`, §3 |
| `fetch('/content/…')`, `fetch('/r/…')` | **works** |
| `fetch('/tokens')` or any other wallet page | blocked by CORS |
| `fetch('https://anywhere-else/')` | blocked by the policy |
| `<form>` submission | blocked |

The last two are worth dwelling on. **Your page cannot phone home.** It cannot
tell anybody that it was looked at, cannot load a font or a script from a CDN,
and cannot beacon out an address it read. Everything it needs must be on the
chain already. This is not a courtesy: it is `connect-src 'self'` plus
`default-src 'self'`, enforced by the browser.

So: **inscribe your dependencies**, or do without them.

---

## 3. The endpoints

Every one is a read. There is no endpoint that spends, signs, sends or changes
anything — the wallet's own controls are not reachable from inside the frame.

All return JSON with `Access-Control-Allow-Origin: *` unless noted.

### Content

| | |
|---|---|
| `GET /content/<id>` | the bytes, with the content type its creator gave it |
| `GET /content/<id>?download=1` | the same, as an attachment |

A content type this node will not render in a page — anything outside
`image/ video/ audio/ text/plain text/html text/css application/json
application/javascript application/pdf font/ model/` — is served as
`application/octet-stream` with `Content-Disposition: attachment`. An
inscription is arbitrary bytes and guessing at them is how a text file becomes
a script.

If the node did not keep that content (it keeps its own in full and describes
everybody else's), you get `404` and enough to go and get it yourself:

```json
{ "error": "this node did not keep the content of that inscription",
  "sha256": "b1946ac92492d2347c6235b4d2611184…", "length": 135150 }
```

### About an inscription

`GET /r/inscription/<id>`

```json
{ "id": "2ea169a4c7b1…", "number": 41, "creator": "nAlice1…", "owner": "nBob22…",
  "block": 1486617, "contenttype": "image/jpeg", "length": 135150,
  "sha256": "b1946ac9…", "transactions": 18, "held": true,
  "json": { "name": "Sunrise", "collection": "first" },
  "rawjson": "{\"name\": \"Sunrise\", \"collection\": \"first\"}",
  "collection": "first", "edition": null }
```

**The creator's JSON comes with it**, parsed, and with every entry of every
listing too — so a gallery of a hundred inscriptions is one call, not a hundred
and one. `json` is `null` when there was none or when what was inscribed does
not parse; `rawjson` is always exactly what is on the chain, because that is a
fact and our ability to parse it is not.

`GET /r/metadata/<id>` — the JSON field, parsed for you:

```json
{ "json": { "name": "Sunrise", "collection": "first" },
  "raw": "{\"name\": \"Sunrise\", \"collection\": \"first\"}" }
```

`json` is `null` when there was none, or when what was inscribed does not parse
— `raw` is always exactly what is on the chain. Ordinals hands back hex-encoded
CBOR and leaves you to decode it; this is JSON because that is what it was
written as.

### Lists

| | |
|---|---|
| `GET /r/inscriptions` | a page of them, newest first |
| `GET /r/inscriptions/<address>` | everything one address owns |
| `GET /r/inscriptions/count` | how many there are |

Each entry is the same shape as `/r/inscription/<id>`, **including its JSON**.

Parameters on `/r/inscriptions`, all optional:

| | |
|---|---|
| `limit` | how many, up to 500. Default 100. |
| `offset` | how many to skip — walking back through pages |
| `after` | only numbers above this one, for a page polling for new work |
| `owner`, `creator` | filter to an address |

`/r/inscriptions/count` takes `owner` and `creator` too, so a page can size its
own paging before it asks for anything.

### Collections

`collection` and `edition` on every inscription say which set the index filed
it in. The rule reads only the inscribed JSON, so every node files the same
sets: a string `collection` field names the set outright; otherwise a `name`
ending in `#<number>` — `Doge Punks #12`, the shape every HashLips build
writes — files it as edition 12 of *Doge Punks*, taking `edition` from the JSON
if there is one. A set belongs to the address that created its items: two
creators using one name are two sets, so nobody can put their own items into
somebody else's.

| | |
|---|---|
| `GET /r/collections` | the sets, newest first. `limit` (up to 500), `offset`, `creator` |
| `GET /r/collections/count` | how many. `creator` |
| `GET /r/collection/<creator>/<name>` | one set and a page of its items, by edition |

```json
{ "creator": "nAlice1…", "name": "Doge Punks", "count": 100,
  "firstnumber": 41, "lastnumber": 140, "firstedition": 1, "lastedition": 100,
  "cover": "2ea169a4c7b1…", "covertype": "image/png" }
```

`cover` is the lowest edition's id, for a thumbnail. The single-collection
call adds `items` — the same shape as `/r/inscription/<id>` — with `limit`
(up to 500) and `offset`, and with `traits=1` a count of every trait value
across the whole set, read from the items' HashLips `attributes`:

```json
{ "creator": "…", "name": "Doge Punks", "count": 100, "…": "…",
  "items": [ { "id": "…", "number": 41, "edition": 1, "json": { "…": "…" } } ],
  "traits": { "Background": { "Blue": 60, "Red": 40 }, "Hat": { "Cap": 12 } } }
```

So the rarity of any item is one call: `traits[t][v] / count`.

```js
// everything in one collection, in one call
const set = await (await fetch('/r/collection/' + creator + '/' +
                   encodeURIComponent('Doge Punks') + '?limit=500')).json();
```

### The chain

| | |
|---|---|
| `GET /r/blockheight` | a number, or `null` if the node is not reachable |
| `GET /r/blocktime` | UNIX seconds of the newest block, or `null` |

### Balances and names

`GET /r/balances/<address>`

```json
[ { "propertyid": 3, "name": "Arcade Test", "balance": "989,995",
    "units": 98999500000000, "divisible": true } ]
```

`balance` is the string a person reads; `units` is the integer the ledger
actually holds, so a page doing arithmetic never has to parse the display form
back. Only non-zero holdings appear.

`GET /r/tag/<name>` → `{ "tag": "alice", "address": "nAlice1…" }`
(`address` is `null` if nobody holds it)

`GET /r/address/<address>` → `{ "address": "nAlice1…", "tag": "alice" }`
(`tag` is `null` if they have not claimed one)

### Asking the wallet to send

A page cannot spend. It can **ask**: file a request naming what should move
and to whom, and the person looking at the page is shown the transaction it
would be — built, signed, decoded, fee and every output — and approves or
refuses it in the wallet, outside your sandbox. You never see a key, never
choose the coins, and never learn anything you could not read from the chain.

`POST /r/send` with a JSON body (send `Content-Type: application/json`; the
preflight is answered):

| field | |
|---|---|
| `kind` | `coins`, `token` or `inscription` |
| `to` | an address on this chain, or a `@tag` |
| `amount` | for `coins` and `token`: what to send, as a decimal string |
| `propertyid` | for `token` |
| `inscription` | for `inscription`: its number or id. It must be this wallet's |
| `from` | optional: which of the wallet's addresses pays. Left out, the wallet chooses |
| `label` | optional: what your page calls itself, shown in quotes to the person |
| `note` | optional: why, in a sentence, shown in quotes to the person |

```js
const asked = await (await fetch('/r/send', {
  method: 'POST', headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({kind: 'token', to: '@shop', propertyid: 3, amount: '1',
                        label: 'Hat Shop', note: 'one red hat'})})).json();
// {id: '402d9a31…', status: 'pending', ...}
```

`202` with the request; `400` with an `error` for anything that can be refused
at once — an address on the other chain, a token that does not exist, an
inscription this wallet does not own, more than twenty requests already
waiting. Then poll:

```js
const answer = await (await fetch('/r/send/' + asked.id)).json();
// status: 'pending' | 'sent' (with txid) | 'denied' | 'failed' (with error) | 'expired'
```

A request nobody answers within an hour expires. What is approved is priced
against the wallet as it is when the person looks, not when you asked; a
balance that was there and is not by then fails then, not silently.

**What this means for the person.** A request is a question, never an
action: the page in the frame can file one, and so can any other site that
knows the wallet's address — which is why filing needs no key, and why the
answer always goes through a page of the wallet that shows exactly what would
go out. The bot RPC has the same queue (`da_requestsend`, `da_requesttoken`,
`da_requestinscription`, `da_request` in bot-rpc.md), so a program can be
given the ability to ask without the ability to spend.

**Being told when it is final.** `sent` means the wallet broadcast it; it
does not mean the chain has it. Watch the txid:

`GET /r/tx/<txid>`

```json
{ "txid": "e88aa7…", "confirmed": true, "confirmations": 3, "conflicted": false,
  "block": 7211043, "blockhash": "…", "time": 1789431022 }
```

Any transaction the node knows, not only ones you asked for. `confirmations`
is `0` while it waits in the mempool; `conflicted` is true when another
transaction spending the same coins was confirmed instead, and this one never
will be. `404` when the node has never heard of it. A shop hands over the hat
at whatever depth it trusts — one confirmation, or six — and that is the
shop's decision, not the wallet's. `GET /r/send/<id>` carries the same
`confirmations` and `confirmed` once a request is `sent`, so polling one
endpoint is enough.

**How the person sees it.** When your page runs in the viewer, the request
opens in front of it at once: the transaction as built, fee and every output,
and *Approve and send* / *Refuse*. That pop-up belongs to the wallet, outside
your sandbox — your page cannot see it, cannot press anything in it, and
cannot frame the approval page itself (`frame-ancestors 'self'`). On every
other page of the wallet, including when the wallet is reached remotely, the same
request waits under *Approvals*. Only a request filed while your page is open
(or in the seconds it took to load) pops up over it; one left waiting from
earlier is listed under the page with a link, and never opened over a page
that did not ask.

### Remembering things

Your page has no `localStorage`: an opaque origin has no storage, and the
flag that would give it some would give it the wallet's origin too. What it
has instead is the wallet's memory of it, the same shape and the same on
every device the wallet is used from:

```html
<script src="/r/storage.js"></script>
<script>
arcade.storage.ready.then(function (s) {
  var best = Number(s.getItem('best') || 0);        // synchronous once ready
  if (score > best) s.setItem('best', String(score)); // a promise
});
</script>
```

`getItem`, `setItem`, `removeItem`, `clear`, `key(i)`, `length`, and `all()`
for a copy of everything. Reads are synchronous once `ready` resolves; writes
return a promise that rejects when the wallet refuses — a key over 256
characters, a value over 64 KiB, more than 1,000 keys or 1 MiB in all — and
the mirror is rolled back. Strings only, as in `localStorage`; `JSON.stringify`
what is not one.

It is kept under **your inscription's id, by the viewer**, which knows which
frame it is talking to: another page cannot read or write yours, and yours
cannot reach theirs, whatever id it claims. It is *this wallet's* memory — a
high score, a settings panel, a half-finished game — not the chain: nothing
here is inscribed, shared or provable. `ready` rejects after five seconds when
no wallet is listening, which is what happens when somebody opens
`/content/<id>` in a tab of its own; a page should still work then, with
nothing remembered.

**When no wallet is behind the page.** On a public instance there is no
wallet at all, and the door still answers: the memory is this browser's own
`localStorage`, under your inscription's id and the account looking at it —
an unsigned visitor shares one anonymous shelf. Same shape, same limits, the
same refusals word for word. It does not sync to the account's other devices
and a cleared browser forgets it; what a page must *prove* belongs on the
chain, which is where storage was never going to help it.

### Talking to another node

Your page cannot fetch anything off the machine it runs on (§2). What it can
do is send a **node-to-node message** — sealed to another DogecoinArcade
node's key, sent as the wallet you are running in, carried on the chain —
and read what that node sends back. The shop's own node, the game's table,
a bot that fills orders: give the page that node's public key or contact
code (`da_identity` on the other node prints both) and it can talk to it.

```html
<script src="/r/node.js"></script>
<script>
var SHOP = 'ef12…';   // the shop node's public key, 64 hex, or its contact code
arcade.node.send(SHOP, {order: 'hat', size: 'L'})
  .then(function (r) { console.log('sent as', r.txid, 'cost', r.total); });
var stop = arcade.node.listen(function (reply) {
  console.log('the shop says', reply.json || reply.body);
});
</script>
```

* `send(to, body)` — `body` is a string or anything JSON-shaped, up to one
  transaction (about 7 KB; `identity()` tells you the exact `maxbytes`).
  Resolves to `{txid, fee, total, size, fromaddress}`; rejects with a reason
  when the wallet refuses.
* `replies({after, limit})` — what the nodes this page wrote to have said,
  oldest first, each `{id, txid, block, frompubkey, body, json, protocol,
  apihash, compatible}`. `after` is your cursor: keep the last `id` you
  handled (in `arcade.storage`, say) and pass it back.
* `listen(handler, {after, every})` — polls `replies` and calls `handler`
  once per new reply; returns a function that stops it. Without `after` it
  starts from now.
* `identity()` — this node's own `pubkey` and `contactcode`, in case the page
  wants to show them; the other node learns them from the envelope anyway.
* `sent()` — what this page has sent, newest first.

**Nobody is asked.** A message moves no value, and the messaging chain is
testnet, only and always: what a message costs is one testnet transaction
from a wallet that keeps itself funded. So it goes out at once — unlike a
payment, which is a request until the person says yes. What stops a page in
a loop from spending the wallet's testnet coins is a cap: **thirty messages
an hour per page**, after which `send` rejects until the hour turns.

When an **account** is looking at the page on a public instance the door
answers too: the message goes out under the account's own messaging key —
the key that signs its feed posts — sealed in the page's browser before the
node ever sees it, paid by the account, and metered by the account's
message dial (thirty an hour) rather than the per-page cap. The node reads
nothing of it; it is handed ciphertext and a destination and offers the
carrier for the account's signature. A mainnet account is refused there:
sealed messages are testnet's, only and always.

**You read only your own conversations.** The wallet's inbox also carries
what other pages and the bots on the bot RPC are told; `replies` shows you
what came from nodes *you* wrote to, from the moment you first wrote to
them, and nothing else. The other node answers *the wallet*, not your page —
your page has no key — so two pages that write to the same node both see its
answers; put something of your own in the message if you need to tell them
apart. Like storage, all of this is keyed by the frame the viewer put you
in: a page opened in a tab of its own has no wallet listening and `send`
rejects after a minute.

For an account's page the same rule holds with a shorter memory: the node
keeps no conversation for anybody, so answers are read in the tab, off the
chain, from the moment the page first wrote — and only from the nodes that
page wrote to. Reload and the cursor starts again; the replies are still
where the chain left them.

### A shop, and buying from one

A **shop** is an inscription whose JSON says what it sells. The page inscribed
with that JSON is the storefront; `arcade.swap` gives it the whole back end.
Nobody types a price into a form: the terms are in the inscription, covered
by its hash, and both wallets read them from their own ledgers before
anything is asked of anybody.

```json
{"shop": {"node": "arcade:test:…",
          "listings": [
            {"give": {"token": 3, "amount": "100"}, "take": {"coins": "2"}},
            {"give": {"collection": "Pixel Pals", "pick": "random"},
             "take": {"token": 3, "amount": "10"}},
            {"give": {"inscription": 57}, "take": {"inscription": 58}}]}}
```

`node` is where the seller's wallet listens — its contact code or public key
(`da_identity`). Each listing's `give` is what the seller hands over and `take`
what the seller gets; either side is `coins`, a `token` with an `amount`, an
`inscription` by number or id, or (give only) a `collection` of the shop's
creator's with `"pick": "random"` — a random item of it the shop still holds,
which is what a minting event is.

```html
<script src="/r/swap.js"></script>
<script>
arcade.swap.shop().then(function (s) {
  s.listings.forEach(function (l) {
    var b = document.createElement('button');
    b.textContent = 'Buy ' + arcade.swap.describe(l.give) + ' for ' + arcade.swap.describe(l.take);
    b.disabled = !!l.available || !s.open || !s.ready;
    b.onclick = function () {
      arcade.swap.buy(l.n, {step: function (t) { status.textContent = t; }})
        .then(function (r) { status.textContent = 'swapped in ' + r.txid; })
        .catch(function (e) { status.textContent = e.message; });
    };
    document.body.appendChild(b);
  });
});
</script>
```

* `shop()` — `{shop, node, seller, listings, mine, open, ready, height, from}`.
  Each listing is `{n, give, take, text, available}`; `text` is the listing
  in words (`100 Arcade Test for 2 coins`), and `available` is `null` or the
  reason the shop cannot give it right now (sold out, the seller no longer
  holds it). `open` says the creator still holds the shop; `mine`
  that the wallet looking is the one that keeps it; `ready` that this chain
  reads swaps at its height (`from` is the block they start).
* `buy(n, {step, timeout})` — the whole exchange: ask the shop's node for an
  offer, wait for it, put it in front of the buyer, wait for the yes, wait
  for the seller's signature. `step(text)` is told each stage. Resolves to
  `{txid, offer, give, take}`; rejects with the shop's reason, the buyer's
  refusal, or `gave up waiting` after `timeout` ms (30 minutes by default).
* The stages on their own, for a page that wants to draw them: `offer(n)`,
  `awaitOffer(txid)`, `accept(offer)`, `status(request)`,
  `awaitDecision(request)`, `awaitSwap(offer)`.
* `describe(leg)` — a leg in words: `100 Arcade Test`, `a random Pixel Pals`,
  `inscription #57 (Pixel Pals #12)`, `2 coins`.

**What the buyer sees, and why it is safe.** The offer comes back from the
shop's node as a node-to-node message with the two legs made concrete and
one of the seller's outputs locked for it. The buyer's wallet builds the
transaction itself — the seller's outpoint first, its own coins after, the
swap in `OP_RETURN` — and shows it in the approvals pop-up like any other
send; Approve signs the buyer's half only, and the half-signed transaction
goes back to the shop's node, which checks it against exactly what it
offered before signing and broadcasting. **One transaction, both legs, or
nothing**: the engine on every node refuses a swap that shorts either side
(`arcade/state.py`, `_swap`). The seller's side is answered by the
shopkeeper in that wallet without anybody pressing anything, because the
owner already said yes in writing when they inscribed the shop. The page
never holds a key and cannot change a price.

**Timing.** Every step but the buyer's own yes is a message on the chain, and
a message is in a block or it is nowhere: on testnet a minute or several,
twice, then the swap. Say so in the page rather than spin.

**Who may sell.** The wallet that *created* the shop inscription and still
*holds* it. Send the inscription away and the shop is closed; inscribe the
same JSON yourself and you have your own shop, selling from your wallet.
Testnet only, because node-to-node messages are.

### A page that is its owner's own words

A page asks with `arcade.send` and the person is asked. A page this wallet
**created** and still **holds** is that person's own words, so it is not:
`arcade.owner.send` builds, signs and broadcasts at once, and the send is
written in the approvals list as one that came from a page of your own —
a raffle page paying its winner, a shop restocking, a game paying out.

```html
<script src="/r/owner.js"></script>
<script>
arcade.owner.identity().then(function (me) {
  if (!me.owner) return;   // somebody else's wallet: ask instead (arcade.send)
  return arcade.owner.send({kind: 'token', propertyid: 3, amount: '5', to: winner});
}).then(function (r) { if (r) console.log('sent', r.txid); });
</script>
```

* `identity()` — `{owner, creator, holder, network}`; `owner` is true only
  in the wallet that created the page and holds it.
* `send(request)` — the same body as `POST /r/send`. Resolves to `{txid,
  request, what, fee}`; rejects, with a message the page can show, in any
  other wallet, on mainnet, or when the send cannot be built.

Testnet only: nothing leaves mainnet without a person looking at it.

On a public instance, where accounts sign for themselves, this door opens
for nobody: `send` rejects, saying that a page may not spend an account's
coins unattended — the operator's door opens because the wallet holding the
key is the machine that made the page, and a node holding nobody's keys has
no such standing. A page that sells from this browser is a shop, and
answers through `arcade.swap` instead. `identity()` stays honest either
way: `owner` is true in an account's tab exactly when the signed-in account
created the page and still holds it.

### Somebody by name

`GET /r/profile/<tag>`

```json
{ "tag": "alice", "address": "nAlice1…", "pending": false,
  "mainnet": "PAlice1…", "bio": "makes pixel art",
  "url": "https://dogecoinarcade.com",
  "picture": "a3f1…", "content": "/content/a3f1…" }
```

Everything a person published under that name, in one lookup: both addresses
to pay them at, a line about themselves, a link, and the inscription they use
as a face. It repeats what they put on the chain themselves and says nothing
about the wallet drawing your page — so unlike `/r/wallet` there is nothing
here to switch off.

* `picture` is given **only while the chain says they still hold that
  piece**. Somebody who sold their profile picture gets `""`, not a picture
  of somebody else's property.
* `pending` is true when the claim is in the mempool and not yet in a block.
  The name answers, and your page can say it is not settled.
* `404` when nobody holds the name — including a name whose claim nobody has
  broadcast.

Useful for greeting a visitor by name (`/r/wallet` gives you *their* tag),
showing whose shop a page belongs to, or crediting whoever made a piece.

### Who is looking

`GET /r/wallet`

```json
{ "network": "test", "mainnet": false,
  "addresses": ["nAlice1…", "nAlice2…"], "tag": "alice",
  "coin": { "spendable": 13847.945, "ticker": "" },
  "tokens": [ { "propertyid": 3, "name": "Arcade Test", "balance": "989,995",
                "units": 98999500000000, "divisible": true } ],
  "inscriptions": 2 }
```

One chain, said out loud. An inscription lives on exactly one, and a page shown
a balance from the other would be reading a number about a wallet that does not
exist where it is looking. Token balances are **summed across every address the
wallet has**: fifteen addresses holding one token is one balance, not fifteen.

This is the only endpoint about the viewer rather than about the chain. The
balances themselves are public — anyone with an index can look up any address —
but **which** address belongs to the person reading your page is the one thing
the chain does not say. It can be switched off, and when it is:

```json
{ "error": "this wallet does not tell inscriptions who is looking" }
```
with status `403`. Handle that: a page that breaks when somebody declines is a
page that punishes them for declining.

### Errors

Always JSON, always with the CORS header so you can read them:

```json
{ "error": "no such inscription" }
```

`404` for something that is not there, `403` for something you may not have.

---

## 4. Recursion: using other inscriptions

The point of `/content/<id>` being a plain URL is that inscribed pages can
build on each other. A library goes on the chain once and everything after it
refers to that one copy.

```html
<!doctype html><meta charset="utf-8">
<!-- a library somebody inscribed as number 12 -->
<script src="/content/12"></script>
<canvas id="c" width="600" height="600"></canvas>
<script>
  // my own metadata decides what to draw
  const id = location.pathname.split('/').pop();
  fetch('/r/metadata/' + id)
    .then(r => r.json())
    .then(meta => draw(meta.json ?? {}));

  function draw(traits) {
    const ctx = document.getElementById('c').getContext('2d');
    ctx.fillStyle = traits.background ?? '#111';
    ctx.fillRect(0, 0, 600, 600);
    // ... the library inscribed above is available here
  }
</script>
```

Three things make that work:

1. `location.pathname` inside the frame is `/content/<id>`, so **a page can
   always find out which inscription it is** without being told.
2. `/content/12` is same-origin from the frame's point of view for fetching
   purposes, and carries CORS, so `fetch` and `<script src>` both reach it.
3. Nothing external is needed, so nothing external can break it or watch it.

### Composing images

```html
<img src="/content/40">
<img src="/content/41">
```

Both are ordinary URLs and `img-src 'self'` permits them.

### Finding the rest of a collection

```js
const me = await (await fetch('/r/inscription/' + id)).json();
if (me.collection) {
  const set = await (await fetch('/r/collection/' + me.creator + '/' +
                     encodeURIComponent(me.collection) + '?limit=500')).json();
  const siblings = set.items.map(row => row.id);
}
```

There is no `/r/children` here, because there is no parent-child relationship
on this chain — the JSON field is where a collection is declared, and the
index files it from that alone.

---

## 4a. A worked example, on the chain

`examples/showcase/` in the repository is exactly the two inscriptions this
section describes, ready to inscribe:

| | |
|---|---|
| `arcade-lib.js` | a small library: every endpoint above wrapped, plus a canvas helper. 2,180 bytes, one transaction, about 0.43 to inscribe. |
| `showcase.html` | a page that loads that library **off the chain** and demonstrates the whole API against whatever node is reading it. |

It shows, live: the library arriving from `/content/<id>`; the page working out
which inscription it is from `location.pathname`; its own metadata; block
height and time; who is looking and what they hold; a picture composed from
another inscription by URL; something drawn by the inscribed library; and a
table of everything the sandbox refuses, each one actually attempted.

They must go on in order, because the page refers to the library by id:

```
python3 examples/showcase/inscribe-showcase.py             # what both will cost
# inscribe arcade-lib.js from the Inscriptions page, note its number
python3 examples/showcase/inscribe-showcase.py --library 7 # writes showcase.built.html
# inscribe showcase.built.html
```

`tests/test_showcase.py` inscribes both into a test chain, opens the page in
the real viewer and checks every section filled in — including that the canvas
has pixels on it, which only the inscribed library can put there.

---

## 5. Writing one

From the **Inscriptions** page: choose a file, add JSON if you want it, press
*Price it*, look at what it costs, press *Inscribe it*. Nothing is spent until
the second press.

The JSON is validated before it is priced. It cannot be edited afterwards —
ever, by anyone, including you — so a typo found at read time is found far too
late.

### What it will cost

| File | Transactions | Chain | Fee | Dust | Total | Net after sweeping |
|---|---|---|---|---|---|---|
| 10 KB | 2 | 29 KB | 0.3 | 2.6 | 2.9 | **0.3** |
| 100 KB | 14 | 205 KB | 2.1 | 18.1 | 20.1 | **2.1** |
| 1 MB | 138 | 2.0 MB | 20.2 | 178.0 | 198.3 | **20.2** |
| 5 MB | 688 | 10.1 MB | 100.9 | 887.5 | 988.4 | **100.9** |

Most of it is dust in the outputs that carry the data, and that dust is
**spendable again by whoever made the inscription** — every data output is a
1-of-3 multisig including your own key.

There is no ceiling. The only limit is arithmetic: the countdown that marks the
last piece is two bytes, so 65,536 pieces, about 498 MB.

### Being kind to the people who index it

* **Inscribe what you need, not what you can.** Every byte is permanent and
  somebody else's disk.
* **Put the collection in the JSON**, not in the filename.
* **Use a real content type.** `text/html`, `image/png`, `application/json`.
  It is the only thing that decides how your inscription is shown.
* A page that works with `/r/wallet` refused is a page that works for
  everybody.

---

## 6. Owning and moving one

An inscription belongs to an **address**, not to a satoshi. Spending your coins
never moves it; sending it is always something you did on purpose.

* Only the owner can send one, to the address named as the reference output.
* A transfer is 38 bytes, so it fits an OP_RETURN: moving one costs a single
  cheap transaction, not what making it cost.
* Numbers are assigned in chain order as each inscription completes, so every
  node replaying the chain assigns the same ones.

---

## 7. What does not exist, and why

* **No spending from inside a stranger's page.** No endpoint spends, signs,
  or sends for a page somebody else wrote. A page can *ask* (`POST /r/send`,
  §3) and be told yes or no; the yes is a button the person presses, in a
  pop-up the page cannot reach. The two exceptions are the person's own
  words: a page this wallet created and holds (`arcade.owner`, §3), and a
  shop's listings, which its owner wrote down when inscribing them
  (`arcade.swap`, §3). Both are testnet only. Where accounts sign for
  themselves, the shop door still answers — the account signs the trade and
  sees what it signs — and the owner door never sends: an unattended spend
  needs a key somebody handed to that machine, and a public node was handed
  none.
* **No `localStorage`.** An opaque origin has none. `arcade.storage` (§3) is
  the machine showing the page remembering for it instead — the wallet's
  pagestore when a wallet is looking, this browser's own storage when an
  account or a signed-out stranger is — always per inscription.
* **No external anything.** Not a CDN, not an analytics beacon, not a font.
  See §2.
* **No sockets, no servers.** A page reaches another machine one way: a
  node-to-node message (`arcade.node`, §3), on testnet, sealed, and answered
  the same way — carried by the wallet it runs in, or by the account's own
  messaging key where accounts sign for themselves.
* **No `/r/children`, `/r/parents`, `/r/sat`.** The first two describe a
  relationship this chain does not record, and the third is about satoshi
  ordinals, which this is not.
* **No CBOR.** The metadata field is JSON, and stays JSON.
* **No guarantee that every node holds every byte.** A node keeps its own
  inscriptions in full and describes the rest; `held` tells you, and the hash
  and length let you fetch and verify from anywhere.
