# Prize pools

A prize pool is how a game, a puzzle or any other page pays out tokens, or
NFTs, to whoever wins, without its owner being there. A pool is an inscription: you
make one by inscribing a short piece of JSON, and delete it by inscribing
another. Every DogecoinArcade node reads pools from the chain, so a pool pays
out through any node, and all nodes agree on what is left.

## What a pool is

A pool is a set of identical lots of one token, say 20 lots of 50, each sold
for a small price you choose to whoever gives the right phrase. A winner pays
that price plus the network fee, and the tokens and the coins move in one
transaction.

* **Its own address.** Your wallet makes an address for each pool from your
  same twelve words, so there is nothing new to back up. One send moves the
  pool's tokens, and the coins its lots stand on, to that address. Nothing but
  a claim, or deleting the pool, ever sends from it.
* **On the chain.** The pool inscription holds the pool's terms in the open,
  and the signed lots sealed with the phrase. Anyone with the phrase can open
  them; nobody else learns anything from them.
* **No double claims.** Each lot stands on two coins of its own. Two claims of
  one lot spend the same coins, so the network accepts only one, whichever
  nodes they came through. The pool's address holds exactly one lot of tokens
  for every lot, so every lot is always covered.
* **Tied to your game**, if you name one. The pool then pays out only while you
  hold the game's inscription: send or sell the game and its pool stops
  paying, on every node. A claim from any other page is refused.

## Making a pool

Inscribe the game first, if the pool is for one. Then, on the NFTs page, open
*Inscribe one thing*, open *JSON beside the file*, and write:

```json
{"name": "GHOST FLEET cash-out",
 "prizepool": {"token": 19, "lot": "50", "lots": 20, "price": "0.01",
               "game": "#201", "phrase": "<your phrase>"}}
```

| field | |
|---|---|
| `token` | the token's number (it is on the token's page) |
| `lot` | how much one winner gets, as a decimal: `"50"`, or `"0.5"` for a divisible token |
| `lots` | how many winners: 1 to 25. Make more pools for more |
| `price` | what a winner pays you per lot, in coins: `"0.01"` or more |
| `game` | optional: the inscription it pays out for, as `#number` or its id |
| `phrase` | the secret the game hands over when somebody wins |
| `name` | optional: what the pool is called on the chain |
| `once` | optional: `true` lets each wallet claim from this pool only once |

Press *Inscribe it*; there is no file to choose. Your wallet then:

1. asks you to confirm the pool, and then the send that moves the tokens and
   the coins into the pool's own address;
2. signs every lot with the pool's key and seals the signatures with the
   phrase;
3. inscribes the pool: its terms in the JSON, and the sealed lots as its
   content. **The phrase itself is never inscribed.**

When the pool inscription is in a block, it pays out through any node.

## A pool of NFTs

A pool can hand out NFTs instead of a token: one piece per winner, whichever
free piece the wallet picks. Write `"kind": "nft"` and name the pieces:

```json
{"name": "GHOST FLEET Arsenal drops",
 "prizepool": {"kind": "nft", "pieces": ["#301", "#302", "#303"],
               "price": "0.01", "game": "#201", "once": true,
               "phrase": "<your phrase>"}}
```

or, instead of `pieces`, every piece of one of your collections that your
wallet holds (up to 25):

```json
{"prizepool": {"kind": "nft", "collection": "GHOST FLEET Arsenal",
               "price": "0.01", "game": "#201", "once": true,
               "phrase": "<your phrase>"}}
```

| field | |
|---|---|
| `kind` | `"nft"` |
| `pieces` | the pieces it hands out, as `#number` or id: 1 to 25, all held by your wallet |
| `collection` | instead of `pieces`: a collection your wallet made. Only the pieces it made count, so a set of the same name by somebody else is never mixed in, and neither are pieces of it you bought back |
| `creator` | optional, with `collection`: another maker's address, to pool pieces of their set that you hold |
| `price`, `game`, `phrase`, `once`, `name` | as for a token pool |

**A collection is minted only by the wallet that made it.** A pool cannot mint:
it hands out pieces that already exist. For a collection that never seals
(a supply of 0, shown as "of ∞"), mint a batch, pool it, and when it runs
low mint more and inscribe another pool for the same game. A page can have
several pools at once.

Making an NFT pool is one send that puts the coins the lots stand on at the
pool's address, then one send per piece to move it there, then the
inscription. A piece can be claimed once its move is in a block; until then
the page sees it as `waiting`. Deleting an NFT pool sends every unclaimed piece
back to you, one transaction each, and then the coins.

## A pool with a referee

A plain pool pays whoever gives its phrase, and the phrase is in your page's
code. A pool with a **referee** pays only for a verified win: the game plays
from a seed the referee hands out, records the player's moves, and claims with
them, and the referee replays them under your game's rules (a *judge*, which
you inscribe) before it signs. The phrase alone no longer pays: a claim without
the referee's signature is refused by the network. Add `referee` to the pool's
JSON:

```json
"referee": {"node": "https://app.dogecoinarcade.com", "judge": "#<your judge>",
            "require": {"won": true}, "params": {"stage": 10}}
```

Writing a judge, the page's side and the limits are in
[The referee](referee.md).

## A pool that pays its own claims

A refereed pool can pay for its claims itself, so a player collects a prize the
moment the game says so: no card, no unlocked wallet, no coins of their own.
Add `"fee": "pool"` beside `referee`; the price is then nothing:

```json
{"name": "Drops as they happen",
 "prizepool": {"token": 19, "lot": "1", "lots": 25, "fee": "pool", "game": "#201",
               "phrase": "<your phrase>",
               "referee": {"judge": "#<your judge>", "require": {"won": true}}}}
```

* Each lot keeps back 0.02 coins of the coins it stands on for its claim: the
  network fee and the player's receiving output. That is what the pool costs
  you per lot; deleting the pool returns the rest, as always.
* A claim is built and sent by the arcade: the lot's own coins in, the prize
  to the player. The referee signs all of it, only after your judge passes the
  run, and only to the wallet the seed was issued to. The player signs nothing.
* An NFT lot hands its piece over outright, rather than selling it.
* It needs a referee. Without one, anybody with the phrase could empty the pool
  for free.

The page asks exactly as for any refereed claim (`{arcade: "claim", pool,
replay}`); the answer comes back without the player being asked.

## Deleting a pool

Inscribe, from the same wallet:

```json
{"prizepool_delete": "#<the pool inscription's number>"}
```

Your wallet shows the transaction that sends the pool's unclaimed tokens and
all of its coins back to you, then inscribes the deletion. Once both are in a
block, no lot can be claimed on any node. Deleting is the only way to take
the tokens back: a pool stays open until you delete it, or until every lot is
claimed.

## A page that pays out

A page learns about its own pool from:

`GET /r/claimpool/<inscription>`

```json
{ "inscription": "…", "kind": "chain", "pool": "…", "open": true,
  "lots_left": 20, "free": [0, 1, 2], "what": "50 PLASMA", "price": 1000000 }
```

* `open` is false when nothing is left, the pool was deleted, or its creator no
  longer holds the page. Say "cash-out closed" rather than letting a player
  try.
* `price` is in the chain's smallest unit: 100,000,000 is one coin, so
  1,000,000 is 0.01.
* It never includes the phrase.

`GET /r/prizepool/<pool inscription>` gives the same for a pool by its own
id, with its terms. Its `kind` is `"token"` or `"nft"`, `what` says what one
lot is ("50 PLASMA", "a piece of GHOST FLEET Arsenal"), `waiting` counts lots
not yet in a block, and `why` says why a closed pool is closed.

A page with more than one pool (a token cash-out and a collection of drops,
say) lists them all, newest first, with deleted ones left out:

`GET /r/claimpools/<inscription>` → `{"inscription": "…", "pools": [ … ]}`

Each entry is shaped like `/r/prizepool`, and `pool_id` is the one to claim
from.

When a player wins, the page asks the wallet it is framed in to claim:

```js
parent.postMessage({arcade: "claim", seq: 1, secret: "<phrase>"}, "*");
```

The wallet finds the page's token pool, opens the lots with the phrase,
picks a free one and asks the player. To claim from one pool in particular (an
NFT pool, or one of several), name it:

```js
parent.postMessage({arcade: "claim", seq: 2, secret: "<phrase>",
                    pool: "<pool_id>"}, "*");
``` The answers come back as
messages with the same `seq`:

* `{arcade: "claim", seq, heard: true}`: the wallet has it and is asking the
  player.
* `{arcade: "claim", seq, ok: true, txid, piece}`: claimed. It arrives with
  the next block. `piece` says what they got, for an NFT pool the piece.
* `{arcade: "claim", seq, error}`: not claimed. `"Cancelled."` means the player
  closed the card; anything else is a reason to show them.

If no `heard` arrives within a few seconds, the page is not open inside the
arcade, and should say so.

**The phrase is in your page's code, and anyone can read it.** Somebody
determined can claim without playing, for the same price as a winner. `once`
limits that to one lot per wallet, not per person. Build
the phrase inside the page rather than writing it out plainly, and size a
pool as a prize, not a vault.

## What a player sees

When they win, the arcade shows its own card over the game, outside the
game's reach: "Claim 50 PLASMA for 0.01 coins?", who set it aside, the
network fee, and *Claim them* or *Cancel*. The claim is one transaction that
their browser builds and checks before signing: it pays the pool and delivers
the tokens to the player, or does nothing at all. The tokens show in their
wallet when the block lands.

## Pools made before

Pools made from the Prize lots form, before pools were inscriptions, keep
working on the node they were made on. They are listed under *Wallet → Tokens
→ Your prize pools*, with *Withdraw* or *Cancel and close*.
