# Escrow: holding players' items for your game

Some games need to put a player's items at stake: a duel for each other's gear,
a dungeon where what you carry can be lost, a tournament buy-in. An NFT or a
token in somebody's wallet cannot be taken without their signature, so a game
that wants items to change hands by its rules has the player put them in an
**escrow** first.

An escrow is an address on the chain that holds a player's NFTs and tokens for
one game:

* **While the game is on**, the game's **referee** (an arcade node your game
  names, the same kind that pays prize pools) moves items out of it, to the
  player or to somebody else, and only when your game's **judge** says so.
* **After the unlock time** your game chose, the player alone can take
  everything back from their wallet, whatever the game or its referee says,
  even if the referee has vanished.

The arcade does not know your game's rules. Whether a duel was won, whether a
dropped item was picked up, who gets what: your judge decides. The escrow only
holds and moves.

## Setting it up: the game's JSON

Name a referee and a judge in your game inscription's JSON, next to `game`:

```json
{
  "game": {"name": "My Game"},
  "escrow": {
    "referee": {"pubkey": "02…", "node": "https://…"},
    "judge": "<the inscription id of your judge>",
    "params": {"anything": "your judge reads"},
    "unlock_hours": 24
  }
}
```

| Field | What it is |
|---|---|
| `referee.pubkey` | the referee node's key, from its `/r/referee` |
| `referee.node` | where that node is, if it is not the arcade the player uses |
| `judge` | your judge: a JavaScript inscription (see *The referee*) |
| `params` | handed to your judge on every decision |
| `unlock_hours` | the longest an escrow can stay locked: 1 to 720 hours |

## The judge

The same kind of judge a refereed prize pool uses: `judge(seed, inputs, params)`,
in a sandbox with no network, no clock and no randomness. For an escrow it is
asked one question, *may these items go to this address?*, and answers:

```js
function judge(seed, inputs, params) {
  // params.escrow: {address, owner, unlock, game, inscriptions, tokens}
  // params.to:     the address asking to receive them
  // params.items:  what it asks for
  // inputs:        whatever the page sent as `replay` (your game's evidence)
  if (provesTheWin(inputs, params)) return {release: true};
  return {release: false, why: "that duel was not won"};
}
```

`release: true` moves the items; anything else refuses them and shows `why`.
A judge may also return `to`, which must match the address asked for. The
`seed` is the escrow's address.

## The page: `arcade.escrow`

```html
<script src="/r/escrow.js"></script>
<script>
arcade.escrow.open({hours: 6}).then(function (e) {
  // e: {escrow, owner, owner_pubkey, unlock, referee, game}
  return arcade.escrow.deposit(e.escrow, [{inscription: swordTxid},
                                          {token: 26, amount: '50'}]);
});
</script>
```

| Call | What it does |
|---|---|
| `open({hours})` | this player's escrow in this game; unlocks on the hour, no later than `unlock_hours`. Sends nothing |
| `deposit(escrow, items)` | puts things in: one card for all of them, then one transaction each |
| `status(escrow)` | `{inscriptions, tokens, coins, owner, unlock, open}` |
| `release({escrow, owner, owner_pubkey, unlock, to, items, replay, coins})` | asks the referee; your judge decides |
| `mine()` | this player's escrows in this game that still hold something |

An item is `{inscription: id}` or `{token: id, amount: "50"}`.

* **Only signed-in players** can open or deposit, and each deposit is
  confirmed on a card in their own viewer and signed in their own browser.
* **Anybody may ask for a release** (the winner of a duel, say); the judge
  decides. A release needs the escrow's facts that `open` returned (`escrow`,
  `owner`, `owner_pubkey`, `unlock`), so share them with the other players your
  own way, for example over `arcade.realtime`. When the referee is a different
  node, pass `coins` from `status` too.
* **Each item moves in its own transaction**, so a release of three things is
  three transactions, in the next block.

## What players can always count on

* **They put things in themselves.** Nothing enters an escrow without the
  player's own signature, and the card says until when.
* **Before the unlock time**, items leave only by your judge's decision, signed
  by the referee your game named. Players trust that referee, as they trust a
  prize pool's.
* **After the unlock time**, the wallet page shows *Held in escrow* with a
  *Take back* button, and one press brings everything home. No game, judge or
  referee is involved; the chain enforces the time.

## Costs

Each deposit leaves a small amount of coin with the item (0.03 coins), which
pays for moving that item out again later, whichever way it goes. The rest
comes back with it.
