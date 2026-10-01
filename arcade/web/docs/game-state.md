# Game state: a game's own record on each NFT

A piece can carry **state that belongs to a game**: a condition that wears down,
a charge, a kill count, an enchantment. That state:

* **travels with the piece** through every trade, sale and escrow, because it
  is kept by the piece's id and not by whoever holds it: a damaged sword is
  still damaged after it is sold;
* **can be changed only by the game**: its judge decides, and the address the
  game names as its publisher writes it;
* **is readable by anyone, on every arcade**: the piece's page shows it under
  *In games*, labelled the way the game asks;
* **only ever moves forward**: every record has a sequence number, the highest
  one counts, and an older record replayed changes nothing.

It is written to the chain as a small public announcement, so every arcade
reads the same state with no server in between. One announcement carries up to
60 pieces, so a game writes once per play session, not once per hit.

## Setting it up: the game's JSON

```json
{
  "game": {"name": "My Game", "family": "my-game"},
  "state": {
    "publisher": "<the address that writes state>",
    "judge": "<the inscription id of your judge>",
    "params": {"anything": "your judge reads"},
    "show": {"c": "Condition", "k": "Kills"},
    "node": "https://… (only if the publisher is not the arcade players use)"
  }
}
```

| Field | What it is |
|---|---|
| `family` | the id your game's versions share; state is kept per family, so a new version of your game sees the old state |
| `publisher` | the address whose announcements count, normally your referee node's own address (its `/r/referee` says it as `state_publisher`) |
| `judge` | decides each change (see below) |
| `show` | how a page labels each key of your state; keys not listed are shown as they are |

A family belongs to the creator who first declared it. Another creator using
the same family name is ignored, so nobody can write state for your items by
copying your JSON. To change your publisher or judge, inscribe a new version of
your game with the same family: your newest declaration is the one in force.

## The judge

The same kind of judge prize pools and escrows use: `judge(seed, inputs, params)`,
in a sandbox with no network, no clock and no randomness.

```js
function judge(seed, inputs, params) {
  // params.items:   [{piece, state}]: what the page asks to write
  // params.current: {piece: state or null}: what is written now
  // params.family:  your family id; inputs: whatever the page sent as `replay`
  if (wornFairly(inputs, params)) return {update: true};
  return {update: false, why: "that run does not wear it that much"};
}
```

`update: true` writes exactly the states asked for.

## The page: `arcade.state`

```html
<script src="/r/state.js"></script>
<script>
arcade.state.get(swordTxid, 'my-game').then(function (s) {
  var condition = s ? s.state.c : 100;          // never written: as new
});

// After a session, everything that wore down, in one go:
arcade.state.update([{piece: swordTxid, state: {c: 61}},
                     {piece: shieldTxid, state: {c: 88}}],
                    {replay: theSessionsInputs});
</script>
```

| Call | What it does |
|---|---|
| `get(piece)` | every game's state for one piece: `[{family, game, name, state, seq, height, show}]` |
| `get(piece, family)` | one family's entry, or `null` if it has written none |
| `update(items, {replay})` | asks your game to write new state; resolves `{txids, updates}` |

* A state is a small JSON object, at most 96 bytes, such as `{"c": 61}`. Keep
  it to numbers and short words; it is a record, not a save file.
* An update is in the chain's pool within seconds and in a block within about
  a minute. `get` shows it once its block lands.
* Anybody may ask for an update; your judge decides. The page never holds a
  key.

The same reads work without a page: `GET /r/state/<piece>` and
`GET /r/state/<family>/<piece>`.

## Breaking and making items

* **A broken item** is simplest as state: `{"broken": true}`. It stays a piece
  its owner holds and can trade, and your game treats it as broken.
* **Destroying an item for real** works through an *escrow*: a player puts it
  at stake, and your escrow judge releases it to an address nobody can spend
  from.
* **Making new items** works through an NFT prize pool of pieces you made in
  advance: your judge releases one when a player forges it. They stay pieces
  of your own collection.
