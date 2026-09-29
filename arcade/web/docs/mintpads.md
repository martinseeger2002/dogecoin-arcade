# Mintpads: designing your own

A mintpad is a page that sells a collection or a token to whoever opens it:
press the button, confirm, and a random piece of the collection (or a lot of
the token) is yours. The *Launchpad wizard* (*Create → Launchpad wizard*)
makes one from a set of ready-made looks. This guide is for building your own
page instead: any design, any animation, anything a page can do, selling
through the same wallet machinery.

## How a mintpad works

A mintpad is two things, and only one of them is the page.

* **The sale** is what makes a mint possible, and it lives outside the page:
  * **A collection:** every piece is signed over for sale at its price in
    advance, and those signed sales are put on the chain, so every node can
    sell from them. The wizard does this for you.
  * **A token:** a resting sell order on the Exchange for the supply you want
    to sell. A mint takes one lot from it.
* **The page** is an ordinary inscription: HTML that shows what is left and
  asks the wallet around it to buy one. It holds no keys and no prices of its
  own; it reads everything live, so it never goes stale.

What makes a page a mintpad is the JSON inscribed beside it. The Exchange's
*Mintpads* tab lists every page whose JSON says it is a pad for its own
creator's collection or token, as long as there is something left to mint.
If you inscribe a new pad for the same collection or token, it replaces the
old one in that list.

## 1. Set up the sale

**A collection:** run the *Launchpad wizard* for it once, with your price. It
signs your pieces over, puts the sales on the chain and inscribes a
standard page. Your own page, inscribed afterwards, replaces the standard
one on the Exchange, and both keep working.

**A token:** put a sell order on the Exchange (*Exchange → Tokens →* your
token *→ Sell*) for the whole amount you want to sell, at your price per
token. No wizard needed. Each mint takes one lot from that order, and
partial fills are automatic.

## 2. Build the page

Start from a working one: open any mintpad's page on its own at
`/content/<its id>` and view its source. The standard pages are short, and
everything below is visible in them.

### A collection's pad

`GET /r/mintpad/<creator>/<collection>`

```json
{ "left": 12, "prices": [1000000],
  "next": { "listing": "…", "piece": "…", "number": 57, "edition": 12,
            "price": 1000000, "maker": "…" },
  "listed": ["…", "…"] }
```

* `left` is how many pieces can still be minted; `next` is one of them,
  picked at random each time you ask. A mint is always a random piece.
* `prices` are in the chain's smallest unit (100,000,000 is one coin).
* `GET /r/collection/<creator>/<collection>` gives the pictures and the
  count, for drawing the collection.

To mint, ask the wallet:

```js
parent.postMessage({arcade: "mint", seq: 1, listing: next.listing,
                    piece: next.piece, name: "My Collection #12"}, "*");
```

### A token's pad

`GET /r/book/<property id>?address=<your address>`

```json
{ "property_id": 19, "name": "PLASMA", "divisible": true,
  "asks": [ { "order": "…", "address": "…", "tokens": 100000000000,
              "coins": 1000000000, "pending": false, "taking": 0 } ] }
```

* Use an ask that is not `pending` and has at least one lot free:
  `tokens - taking` units.
* The price of one lot is `floor(coins × lot ÷ tokens)` in the smallest unit.
* Amounts are in base units. For a divisible token, one whole token is
  100,000,000 units.

To mint a lot, ask the wallet to take it from the order:

```js
parent.postMessage({arcade: "take", seq: 1, order: ask.order, units: LOT}, "*");
```

### The answers, for both

The wallet answers with messages carrying the same `arcade` and `seq`:

* `{heard: true}`: it has the request and is asking the person.
* `{ok: true, txid}`: bought. It is theirs when the block lands; read the
  counts again after a few seconds.
* `{error}`: not bought. `"Cancelled."` means the person closed the card;
  anything else is a reason to show them.

If no `heard` arrives within about four seconds, the page is not open inside
the arcade: say "Open this mintpad on DogecoinArcade to mint from it."

A page shown in a post can ask for the room it needs:
`parent.postMessage({arcade: "size", height: 640}, "*")`.

What the person sees is the arcade's own confirmation card, outside your
page: what they get, the price, the network fee. The purchase is one
transaction their own browser checks and signs. Your page cannot change the
price or press anything on their behalf.

## 3. Inscribe it as a mintpad

Inscribe your page from your own wallet, with *Inscribe one thing* on the
NFTs page. Open *JSON beside the file* and enter one of these, with **your
own address** as the creator (it is on *Wallet → Coins*):

A collection:

```json
{"name": "Space Frogs mintpad",
 "mintpad": {"creator": "<your address>", "collection": "Space Frogs"}}
```

A token (`lot` in base units: 50 whole tokens of a divisible token is
5000000000):

```json
{"name": "PLASMA mintpad",
 "tokenpad": {"creator": "<your address>", "property_id": 19, "lot": 5000000000}}
```

The creator has to be the address that inscribes the page, so nobody can
make a pad that claims to sell your collection. Once the inscription is in a
block, your page is on the Exchange under *Mintpads*.

## Good to know

* The page is permanent, but the sale is not: what is left, the price and
  the pictures are read live. A sold-out pad leaves the Mintpads list by
  itself.
* To stop a token pad, cancel the sell order on the Exchange. To stop a
  collection pad, cancel its listings on each piece.
* A page can only sell what you have put up for sale. Everything it sends
  goes through the buyer's confirmation card.
* Games that pay out instead of selling use prize pools; see the Prize pools
  guide.
