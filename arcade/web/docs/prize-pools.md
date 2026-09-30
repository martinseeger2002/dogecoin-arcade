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
from a seed the referee hands out, records the player's moves, and sends them
with the claim. The referee replays them with your game's rules and signs the
claim only if the replay wins.

It works for any game whose rules can run without graphics and give the same
answer every time: turn-based, puzzles, shooters, racers.

### The judge

Inscribe your game's rules as plain JavaScript (content type
`text/javascript`) defining one function:

```js
function judge(seed, inputs, params) {
  // replay the run from the seed and the recorded inputs
  return {won: true, score: 1234};
}
```

* `seed` is a 64-character hex string. Build your random numbers from it
  (a seeded generator), never from `Math.random`.
* `inputs` is whatever your page recorded, as JSON: keys per frame, moves,
  choices. Up to 1 MB.
* `params` comes from the pool, so one judge can serve several pools (a
  stage number, a difficulty).
* No `Date`, no `Math.random`, no page, no network. Use a fixed timestep, and
  write your own `sin`, `cos` and `atan2` if your game needs them, so the
  browser and the referee agree to the last bit.
* It runs in QuickJS (`pip install quickjs==1.19.4`) with 64 MB and 5 seconds.
  Test it there before you inscribe it:

```python
import quickjs, json
ctx = quickjs.Context(); ctx.set_memory_limit(64 << 20); ctx.set_time_limit(5)
ctx.eval("delete globalThis.Date; Math.random = undefined;")
ctx.eval(open("judge.js").read())
ctx.set("s", seed); ctx.set("i", json.dumps(inputs)); ctx.set("p", json.dumps(params))
print(ctx.eval("JSON.stringify(judge(s, JSON.parse(i), JSON.parse(p)))"))
```

### Making a refereed pool

Add `referee` to the pool's JSON:

```json
{"prizepool": {"token": 19, "lot": "250", "lots": 10, "price": "0.01",
               "game": "#226", "once": true, "phrase": "<your phrase>",
               "referee": {"node": "https://app.dogecoinarcade.com",
                           "judge": "#<your judge>",
                           "require": {"won": true},
                           "params": {"stage": 10}}}}
```

| field | |
|---|---|
| `node` | the arcade that referees. Leave it out to make the node you are on the referee |
| `judge` | your judge inscription |
| `require` | `{"won": true}`, or `{"score_min": N}` |
| `params` | optional: handed to the judge |

A refereed pool's tokens, coins and pieces sit at a two-key address. A claim
needs the pool's signature (inside the inscription, sealed with the phrase)
**and** the referee's, which it gives only for a winning replay, over a claim
that pays the wallet that played. Knowing the phrase is not enough: a claim
built by hand without the referee is refused by the network itself. You close
the pool with your own wallet, as any other.

### The page's side

Before a run, ask for a seed:

```js
parent.postMessage({arcade: "seed", seq: 1, pool: "<pool_id>"}, "*");
// answers {arcade: "seed", seq: 1, seed, expires} or {error}
```

Play the run from that seed and record the inputs. When the player wins,
claim with the run:

```js
parent.postMessage({arcade: "claim", seq: 2, secret: "<phrase>", pool: "<pool_id>",
                    replay: {seed, inputs}}, "*");
```

A refusal says why: "the replay did not win", "that seed was used; a new run
needs a new seed", "that seed expired", "the judge ran too long". A pool with
no referee answers the seed request with "this pool has no referee", so a page
can fall back to a plain claim.

* A seed belongs to one wallet and one pool, works once, and lasts two hours.
* `GET /r/referee` on a node says its key, its engine and its limits.
* Every node takes the claim; the referee node judges it. If the referee is
  offline, claims wait, and the prizes stay in the pool.

**What a referee proves, and what it does not.** It proves that a winning run
was played from a fresh seed, by the wallet that claims. It does not prove a
person played it: a program that can win the game can win the prize. And the
seed is what stops a run being replayed: the same moves on another seed may
still win an easy stage, so pay out for hard ones (the final stage, a high
score), not for the first level.

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
