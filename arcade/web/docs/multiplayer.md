# Multiplayer and trades: players meeting in your game

Two page APIs let an inscribed game be played *together*:

* **`arcade.realtime`**: rooms where players on any arcade meet and send each
  other small messages many times a second, such as positions, moves and chat.
* **`arcade.swap.trade`**: one player offers another an NFT for an NFT, a token
  or coins, and both items move in one transaction, or neither does.

Neither knows anything about your game. A room is a name, a message is your own
data, and a trade is "this piece for that". What they mean is up to you.

A room can also carry a video call (`room.media`), and an app can ring a player
whose app is closed (`arcade.realtime.ring`). Both are below.

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
| `room.on('reconnecting', fn())` | the line dropped and the viewer is taking your seat again by itself; `room.online` is `false` until it is back. Show "reconnecting" if you like |
| `room.on('rejoined', fn(me))` | you were seated again after the line dropped: the others may have seen you leave and come back, so **send your state again** (position, gear) |
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
* A player whose page closes leaves at once. One whose page vanishes without a
  word (a phone that lost its network) keeps its seat for about 30 seconds,
  then is dropped. Nothing is stored: anything that has to be true later
  belongs on the chain.

### When the node restarts

A node restarts now and then (an update, a reboot), and every room on it goes
with it. Your page does not have to notice: the viewer takes the player's seat
again by itself, and the room carries on. It never gives up while the game is
open: it tries again every 15 seconds at most, and at once when the network or
the tab comes back. Your page hears `reconnecting` when the line drops and
`rejoined` when the seat is back.

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

### Busy places: cells and full rooms

One room for a whole town means everybody hears everybody: a thousand players
sending five messages a second is five thousand a second for each of them. Two
helpers keep a crowd cheap:

```js
// A full room sends you on: town, town#2, town#3 ... (room.name says which).
arcade.realtime.join('town', {cap: 50});

// The world in square cells: you hear the cells within sight, and speak in your own.
arcade.realtime.area('world', {cell: 64, view: 48, x: px, y: py}).then(function (a) {
  a.on('message', function (data, p) { movePlayer(p, data); });
  a.on('join', addPlayer); a.on('leave', removePlayer);
  // as the player walks:
  a.move(px, py);                // cells come and go by themselves
  a.send({x: px, y: py});        // heard by everyone who can see your cell
});
```

Keep `view` smaller than `cell` and a player is in at most four cells, whatever the
size of the world. A player standing in two of your cells is one player to you:
`join` once, `leave` when they are out of all of them.

### Any node is the way in

A room is the same room on every node, so a player can use whichever node answers
them fastest. The viewer showing a game asks its own node for the other nodes it
can reach over https (their tunnel addresses, and arcade domains on the chain),
times a hello to each, and plays through the quickest one if it is clearly faster
than its own; it checks again every ten minutes, and goes back to its own node if
the chosen one stops answering. A player signed in on one node is still themselves
on another: their wallet signs that node's one-day room certificate. A node's
tunnel carries only its room routes (`/realtime/...`) -- never the wallet or the
rest of the arcade. Nothing changes for a game: `arcade.realtime` works the same.

### Fast games: the fast lane

Rooms are made for games where a few updates a second is plenty. An action game
(a shooter, a racer, a brawler) with up to about 8 players can ask for the
**fast lane**, which connects the players' browsers directly to each other.
It connects up to 20 players in a room: each browser links to at most 19 others.

```js
arcade.realtime.join('match-1', {fast: true}).then(function (room) {
  if (!room.fast) return useRoomSendInstead();       // this browser has no WebRTC
  room.fast.on('message', function (data, p) { moveTank(p, data); });
  room.fast.on('peer', function (p, state) { showLink(p, state); });
  setInterval(function () { room.fast.send({x: x, y: y, yaw: yaw}); }, 33);   // 30 a second
  room.fast.send({kill: victimId}, {reliable: true});                          // arrives, in order
});
```

* **`room.fast.send(value)`** goes to every other player in the room, straight
  from browser to browser. It is unordered and never resent: a lost update is
  skipped, never waited for, which is what a position wants.
* **`room.fast.send(value, {reliable: true})`** arrives, in order: kills, scores,
  anything that must not be lost.
* Up to 16 KB a message, and no rate limit of the arcade's own; your players'
  connections are the limit.
* **`room.fast.on('peer', fn(player, state))`**: `'connecting'`, `'direct'`,
  `'room'` or `'gone'`. **`room.fast.peers()`** gives each player's state.
* The room itself still works as before. `room.send`, `join` and `leave` events
  are how players find each other, and the fast lane is set up through it.

**When two players cannot connect directly.** Some players sit behind routers
that refuse direct connections (carrier NAT on some phone networks and satellite
internet). Most connect anyway, often over IPv6. A pair that cannot connect is in
state `'room'`: their fast sends go through the room instead, about three a
second (reliable ones in order, and of the rest only the latest). The game keeps
working for those two, slower.

**The call relay.** An operator can give their arcade a relay that carries such
pairs at full speed (Admin → Network → Call relay, a Cloudflare TURN key). The
viewer asks for it with your app's id, and gets it only:

* for a signed-in player (guests never get the relay);
* for the apps the operator chose (none chosen yet means every app);
* while this month's relay use is under the operator's limit.

A pair that connects through the relay is `'direct'` like any other. Nothing
changes for your page.

**Many players: one host, a star.** By default every pair of players links (up to
20 in a room), and every fast send goes to all of them. A game with a host (a
server-like player that keeps the world) can link a star instead, so each player
holds one connection and hears only what is meant for them:

```js
arcade.realtime.join('zone-1', {fast: true, link: []}).then(function (room) {
  room.fast.link([hostId]);                        // a player: link with the host only
  // the host: room.fast.link(null, {max: 60})     // everybody, up to 60 links
  room.fast.send(bytes, {to: playerId});           // one player only (an id or an array)
  room.fast.on('message', function (data, p) {      // an ArrayBuffer when bytes were sent
    if (data instanceof ArrayBuffer) readPacket(new DataView(data));
  });
});
```

* **`join(name, {fast: true, link: [ids]})`** and **`room.fast.link(ids, {max})`**:
  link only with these players (`null` = everybody again, the default). A pair
  links only when both want to; to the other side a player who will not is
  `'gone'`. `max` raises the number of links from 19 up to 60.
* **`room.fast.send(value, {to})`**: only those players get it; the others never
  see it.
* **Bytes:** send an `ArrayBuffer` or a typed array and it arrives as an
  `ArrayBuffer`. To a player in state `'room'` only up to 300 bytes go (as the
  room carries them).

The connections are made by the viewer, not your page. Your page still reaches
nothing but the viewer, and the other players' addresses are never shown to it.

### Camera and microphone: `room.media`

A video call, or a game with voice, uses `room.media` on a room joined with
`{fast: true}`. A sandboxed page cannot open the camera itself, so the viewer
opens it, sends it to the players your page names, and draws the pictures over
your page where your page says.

Say `"media": true` in your card's JSON, so the card shows a **Camera &
microphone** switch from the start. (Without it, the switch appears only after
your app first asks.) Nothing is opened until the player turns the switch on;
until then `start` answers `{ok: false, error: 'off'}` and the viewer tells the
player where to turn it on.

```js
arcade.realtime.join('call-x7', {fast: true}).then(function (room) {
  if (!room.media) return noCall();                 // no WebRTC here
  room.media.start({video: true, audio: true}).then(function (r) {
    // {ok: true, video, audio}, or {ok: false, error: 'off' | 'denied' | ...}
    if (r.error === 'off') say('Turn on Camera & microphone on this app\'s card');
  });
  room.media.place('me', {x: 10, y: 10, w: 160, h: 120}, {fit: 'cover', mirror: true});
  room.fast.on('peer', function (p, state) {
    if (state === 'direct' && p.address === friend) {   // only the player you mean
      room.media.share(p.id, true);                     // your picture and sound go to them
      room.media.hear(p.id, true);                      // their sound plays
      room.media.place(p.id, {x: 0, y: 140, w: 640, h: 480}, {fit: 'contain'});
    }
  });
});
```

| | |
|---|---|
| `start({video, audio})` | opens the camera, the microphone or both. Called again mid-call, the new tracks replace the old (the camera joins the microphone, say) |
| `stop()` | camera and microphone off; every share ends |
| `share(id, on)` | send your picture and sound to one player: `true`, `false`, or `{video, audio}` for one kind only. Refused with `not direct` until that player's link is direct |
| `hear(id, on)` | play that player's sound. Nobody's sound plays until you say |
| `place(id or 'me', rect, {fit, mirror})` | where a picture is drawn: `{x, y, w, h}` in your page's CSS pixels, or `null` to hide it. `fit` is `'cover'` or `'contain'`. At most 16 pictures |
| `mute({video, audio})` | `true` mutes that kind, `false` unmutes it |
| `on('media', fn(player, {video, audio}))` | a player is sending to you |
| `on('media-off', fn(player))` | they stopped |
| `on('state', fn(s))` | `'on'`, `'off'` (the player pressed Stop) or `'denied'` |

* **Share only with the player you mean.** A room's name can be guessed, and
  anybody who joins it is a peer. Check `p.address` before you call `share`
  or `hear`.
* **Media is direct only.** It goes over a direct link or the call relay, and
  never through the room.
* **The player can always see it and stop it.** While the camera or microphone
  is on, the viewer shows so over your page, with a Stop button your page
  cannot cover.
* Place the pictures again when your layout moves.

### Ringing a player whose app is closed

`arcade.realtime.ring` wakes another player's phone with a notification, even
when your app is closed there. Tapping it opens your app.

```js
arcade.realtime.ring(friendAddress, {line: 'Video call', link: 'call=x7'})
  .then(function (r) { /* {ok: true, rung: true} or {ok: true, rung: false, why} or {ok: false, error} */ });

// In the app that was rung, when it opens:
arcade.realtime.ringed().then(function (ring) {
  if (ring) joinCall(ring.link);        // {from, tag, link, line, at}, or null
});
```

Say `"notify": true` in your card's JSON: only then does the card show a
**Notifications** switch, and the player's phone must allow the arcade's
notifications. A ring wakes somebody only when all of these hold, checked by their own
node:

* they turned on Notifications on your app's card;
* the caller is in their address book;
* the caller is signed in with an unlocked wallet, which signs who they are;
* not too often: one ring from the same caller every 15 seconds, at most 30 an
  hour for one pair, and at most 20 a minute from one caller.

`line` is up to 80 characters and `link` up to 200. The notification says who
is ringing (their @name), which app, and your `line`. `ringed()` answers only
in the app that was rung, and only for a ring from the last ten minutes.

### A service for your game: a bank, a dealer, a referee

Some games need something that is always in the room: a bank that pays out, a
dealer, a referee. You can run one for your own game on your own node, with
Python and no browser. It joins your game's rooms as your node's own account, so
players see your @tag, and nobody can run one in your name: every node checks the
certificate with its own Core.

```python
from arcade.service import Room

with Room("mygame", "bank") as room:          # your game's name, the room's name
    for event in room.events():               # join, leave, message -- comes back by itself after a restart
        if event["type"] == "message":
            room.send({"t": "ack", "to": event["member"]["id"]})
```

Run it on the node's machine, as the user the node runs as: it reads the node's
service key (`~/.dogecoinarcade/realtime-service.key`, made when the node starts,
readable only by that user). The key works only from the machine itself, never
through the door. `python -m arcade.service mygame bank` prints a room's events,
a starting point. A service has the same limits as a player (512 bytes, about
five messages a second); anything that must last goes on the chain.

### Many nodes, one mesh

Every node carries its own players into the same rooms, so it does not matter
which node a player uses. A node finds others from the chain's mesh
announcements, from `--mesh-peer`, from what its peers pass on, and from the
peers it reached before (`mesh-peers.json`, kept for 30 days), so a restart never
depends on any one node still being there. It keeps six links and swaps its
slowest for a faster peer when it finds one.

A node behind a router that cannot forward a port (carrier NAT, Starlink) still
dials out. If it has IPv6 it announces that address and meets other IPv6 nodes
halfway: both dial each other on the same beat of the clock, so each router lets
the other in. And if `cloudflared` is installed, it also opens a free Cloudflare
quick tunnel and announces that `wss://` address, so even IPv4-only players' nodes
can reach it. The mesh's own encryption runs inside the tunnel, so Cloudflare
carries only ciphertext; nodes still link directly wherever they can, and
`--mesh-tunnel off` turns it off.

The node's Cores ride the mesh too: for up to four linked arcades per chain (the
quickest links), the node gives each Core a local peer address that leads,
through the mesh link, to that arcade's Core. Two nodes behind routers that Core
alone could never connect still sync and relay to each other. Core traffic is capped
at 256 KB/s per link and sent in small pieces, so games stay quick; `--mesh-core off`
turns it off. Anybody with a public address can run a node that everyone else
links to automatically -- it announces itself, and the others dial it within a
minute. More such nodes, from more people, make the mesh harder to break.

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

Both players must be signed in; guests cannot trade. Signing a trade uses the
wallet, so your app needs **Wallet access** on its card. If it is off, the
viewer asks the player over your app the first time it reaches for the wallet;
a No refuses that one trade, and the game plays on.

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

## Putting your game on the Apps tab

A multiplayer game is listed like any other: see [Apps](games.md), and set
`"multiplayer": true` in its card (`{"game": {...}}` or `{"app": {...}}`).
