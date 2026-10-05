# Multiplayer and trades: players meeting in your game

Two page APIs let an inscribed game be played *together*:

* **`arcade.realtime`**: rooms where players on any arcade meet and send each
  other small messages many times a second, such as positions, moves and chat.
* **`arcade.swap.trade`**: one player offers another an NFT for an NFT, a token
  or coins, and both items move in one transaction, or neither does.

Neither knows anything about your game. A room is a name, a message is your own
data, and a trade is "this piece for that". What they mean is up to you.

## How it works, in one paragraph

Arcade nodes link to each other directly, peer to peer, with no server in the
middle and no website involved. When your page joins a room, the arcade showing
it joins that room on the mesh, and every other arcade with a player in the
same room relays messages to it. Players are identified by their own chain
address, proved by a signature their wallet makes once a day, so a name shown
in your game is the name on the chain. Visitors who are not signed in play as
guests.

## Rooms: `arcade.realtime`

```html
<script src="/r/realtime.js"></script>
<script>
arcade.realtime.join('town', {game: 'my-game'}).then(function (room) {
  if (!room.online) return playSolo();          // never stop anyone playing
  room.members().forEach(addPlayer);
  room.on('join',    function (p) { addPlayer(p); });
  room.on('leave',   function (p) { removePlayer(p); });
  room.on('message', function (data, p) { movePlayer(p, data); });
  setInterval(function () { room.send({x: me.x, y: me.y}); }, 250);
});
</script>
```

### `arcade.realtime.join(name, {game})`

Resolves to a room. It never rejects: if there is no mesh, the page was opened
outside an arcade viewer, or anything else goes wrong, the room has
`online: false` and a reason in `room.why`. Play on alone in that case.

* **`name`**: 1 to 60 characters. Rooms with the same name in the same game
  are the same room on every arcade.
* **`game`**: which game's rooms these are. It defaults to the inscription
  being played, so only copies of the same page meet. If your game has several
  versions (new inscriptions), pass a short id they all share, like
  `'my-game'` (letters, digits, `.` `_` `:` `-`, up to 64), and every version
  meets in the same rooms.

### The room

| | |
|---|---|
| `room.online` | `true` when you are in the room |
| `room.me` | you, as a player (see below) |
| `room.members()` | everyone in the room, you included |
| `room.send(value)` | send to everyone else; a string as is, anything else as JSON |
| `room.on('message', fn(data, player))` | `data` is parsed JSON when it parses |
| `room.on('join', fn(player))`, `room.on('leave', fn(player))` | |
| `room.on('closed', fn(why))` | the room is gone for good (the node unreachable for about ten minutes); join again to retry |
| `room.leave()` | leave the room |

A **player** is `{id, address, tag, guest, node}`. Key your game state by `id`.
`address` and `tag` are the player's chain address and @tag (without the @),
proved by their own key; for a guest they are `null` and `""`, and `guest` is
`true`.

### Limits

* **512 bytes** per message (UTF-8), and about **5 messages a second** per
  player, with short bursts of up to 10. `send` rejects past either.
* Delivery is best-effort and not ordered between players: send *state*
  ("I am at 12,40"), not changes ("I moved 2 left"), and the next message
  corrects any that went missing.
* You never hear your own messages.
* A player whose page has gone is dropped after about 10 seconds. Nothing is
  stored: anything that has to be true later belongs on the chain.

### When the node restarts

A node restarts now and then (an update, a reboot), and every room on it goes
with it. Your page does not have to notice: the viewer takes the player's seat
again by itself, retrying until the node is back, and the room carries on.

* The players who are not back yet arrive as `leave`, and come back as `join`,
  so a game that keeps its player list from those two events stays right.
* A guest keeps its `id`, so other players see the same player return. If that
  name was taken in the meantime the guest gets a new one: `room.me` changes,
  so read it again rather than keeping a copy.
* A `send` while the seat is being taken again is rejected; send state, and
  the next message puts things right.

### Leaving

Nothing is required. When the tab closes or navigates, the viewer leaves every
room. Call `room.leave()` when a player moves to another part of your game,
then `join()` the next room. Being in several rooms at once is fine.

## Trades: `arcade.swap.trade`

A trade window between two players in the same room. Load both scripts:

```html
<script src="/r/realtime.js"></script>
<script src="/r/swap.js"></script>
<script>
arcade.swap.onTrade(function (t) {
  if (t.status === 'incoming') showTradeWindow(t);     // somebody offered you one
  if (t.status === 'settled') refreshInventory();
});

// Offer my sword for 25 of a token:
arcade.swap.trade({with: player.id,
                   give: {inscription: swordTxid},
                   get:  {token: 26, amount: '25'}});

// The other player, answering:
arcade.swap.answer(t.id, true);
</script>
```

A side is one of:

* `{inscription: '<txid>'}`: an NFT,
* `{token: <property id>, amount: '25'}`: an amount of a token, as a decimal
  string,
* `{coins: '1.5'}`: coins.

Both players must be signed in; guests cannot trade.

### Calls

| | |
|---|---|
| `trade({with, give, get})` | offer `give` for `get` to the player whose room id is `with`; resolves to `{id}` |
| `answer(id, accept, why)` | say yes or no to a trade offered to you |
| `cancel(id)` | withdraw before it is sent |
| `items()` | what this player holds: `{address, inscriptions, balances}`, for drawing a trade window |
| `onTrade(fn)` | `fn(trade)` on every change, on both sides |

A **trade** event is `{id, status, with, give, get, role, why, txid}`. `give`
and `get` are always from the point of view of the page that hears it, and
`with` is the other player. `status` is one of:

| status | |
|---|---|
| `proposed` | you offered it |
| `incoming` | it was offered to you; show it and call `answer` |
| `accepted` | the other side said yes |
| `signed` | your half is signed and on its way to the other player |
| `broadcast` | the trade is sent; `txid` names it |
| `settled` | it is in a block and the NFT really moved: both sides moved |
| `declined`, `cancelled`, `failed` | it is over; `why` says why when there is a reason |

### Several things each way

Pass lists to trade several things at once -- two swords and 50 Gold for a
ring, a stack of 20 of one token, two copies of the same piece:

```js
arcade.swap.trade({with: player.id,
                   give: [{inscription: swordA}, {inscription: swordB},
                          {token: 26, amount: '50'}],
                   get:  [{inscription: ring}]});
```

* Up to 64 things each way: NFTs, token amounts, and coins (coins one way only).
* It is still ONE transaction both players sign: everything moves, or nothing.
* No NFT is needed on either side; a single `{token, amount}` each way works too.
* Trade events carry `give` and `get` as lists for a trade made this way.
* A trade of lists is read from an announced block (`bundles_from`) on each
  chain; before it, `trade` says the chain does not read them yet.

### What players see

Your page never touches a key. Each player approves in their own arcade viewer,
on a card your page cannot draw or press:

* the player who offers confirms when they offer,
* the player offered confirms when they accept,
* whoever finishes the trade sees the network fee and confirms once more.

The side giving an NFT signs first; when both sides are NFTs, the player who
offered does. That half travels to the other player **sealed to their key**,
because a signed half is a promise anybody holding it could complete. Each
player needs to have published their tag (their messaging key) once; `trade`
says so if one of them has not.

### Settling

The trade is one transaction both players signed. Both items move in the same
block, or nothing moves. Until `broadcast`, either player can walk away and
nothing is spent. After `settled`, the NFT and the payment are where your game
expects them; read them with `items()`.

## Putting your game on the Games tab

A multiplayer game is listed like any other: see *Games: listing yours on the
Games tab*, and set `"multiplayer": true` in its `game` JSON.
