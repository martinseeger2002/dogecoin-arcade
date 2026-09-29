# Prize pools

A prize pool is how a game, a puzzle or any other page pays out tokens to
whoever wins, without its owner being there. This guide is in three parts:
making a pool, building a page that pays from one, and what a player sees.

## What a pool is

A pool is a set of identical lots of one token, say 20 lots of 50, each sold
for a small price you choose to whoever gives the right phrase. A winner pays
that price plus the network fee, and the tokens and the coins move in one
transaction.

* **Hidden.** A pool is on no public page and in no order book, so nobody can
  buy it out from the Exchange. It opens only with its phrase.
* **At its own address.** Your wallet makes an address for each pool from your
  same twelve words, so there is nothing new to back up. One send moves the
  pool's tokens, and the coins its lots stand on, to that address. Nothing but
  a claim, or closing the pool, ever sends from it, so nothing else you do can
  spend a prize before it is claimed.
* **Tied to your game**, if you want. A pool bound to one of your inscriptions
  pays out only while you hold that inscription. Send the game to somebody
  else, or sell it, and its pool stops paying. A claim from any other page is
  refused.

## Making a pool

On *Wallet → Tokens*, press *Prize lots* beside the token and fill in:

| | |
|---|---|
| Tokens per lot | what one winner gets |
| How many lots | up to 25 in one pool; make more pools for more |
| Price per lot | 0.01 coins or more, paid by the winner to you |
| Days | how long the pool is offered |
| Phrase | the secret a winning page hands over. Type your own, or press *Make one* |
| Pays out only while I hold inscription | optional: the game's number or id |

Then *Set them aside*. Your wallet does three things:

1. It registers the pool's own address with this node.
2. It sends the tokens and two small coins per lot to that address. You
   confirm this send like any other.
3. When that send is in a block (a minute or two), it signs every lot with the
   pool's key.

If you close the page while it waits, open *Wallet → Tokens* again: the pool
is under *Your prize pools* with *Finish setting up*.

A game can only be bound to a pool after the game is inscribed, so inscribe
the game first. The phrase can be chosen before that: write it into the game,
then type the same phrase into the form.

## Running a pool

*Wallet → Tokens → Your prize pools* lists each pool: how many lots are left,
what each pays, its price, and which game it is bound to.

**Cancel and close** sends the pool's unclaimed tokens and all of its coins
back to you in one transaction. Once that is in a block, no lot of that pool
can be claimed. Pools made before pool addresses existed have *Withdraw*
instead, which spends the coins their lots stand on and leaves their tokens
where they always were.

The coins a winner pays for a lot arrive at the pool's address, and come
back to you with everything else when you close it.

## A page that pays out

A page learns about its own pool from:

`GET /r/claimpool/<inscription>`

```json
{ "inscription": "…", "open": true, "listing": "…", "lots_left": 20,
  "what": "50 PLASMA", "price": 1000000 }
```

* `open` is false when nothing is left, or the seller no longer holds the
  page. Say "cash-out closed" rather than letting a player try.
* `price` is in the chain's smallest unit: 100,000,000 is one coin, so
  1,000,000 is 0.01.
* It never includes the phrase.

When a player wins, the page asks the wallet it is framed in to claim:

```js
parent.postMessage({arcade: "claim", seq: 1, secret: "<phrase>"}, "*");
```

A page bound to a pool leaves out `listing`: the wallet finds the pool for
the page that asked. A pool that is not bound needs its listing id,
`listing: "<id>"`, which the Prize lots form shows when it is made.

The answers come back as messages with the same `seq`:

* `{arcade: "claim", seq, heard: true}`: the wallet has it and is asking the
  player.
* `{arcade: "claim", seq, ok: true, txid}`: claimed. It arrives with the next
  block.
* `{arcade: "claim", seq, error}`: not claimed. `"Cancelled."` means the
  player closed the card; anything else is a reason to show them.

If no `heard` arrives within a few seconds, the page is not open inside the
arcade, and should say so.

**The phrase is in your page's code, and anyone can read it.** Somebody
determined can claim without playing, for the same price as a winner. Build
the phrase inside the page rather than writing it out plainly, and size a
pool as a prize, not a vault.

## What a player sees

When they win, the arcade shows its own card over the game, outside the
game's reach: "Claim 50 PLASMA for 0.01 coins?", who set it aside, the
network fee, and *Claim them* or *Cancel*. The claim is one transaction that
their browser builds and checks before signing: it pays the seller's pool and
delivers the tokens to the player, or does nothing at all. The tokens show in
their wallet when the block lands.
