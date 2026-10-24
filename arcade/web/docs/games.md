# Games: listing yours on the Apps tab

The *Games* tab lists every game anyone has inscribed, on every arcade, ranked
by what players say about them. Players can press *Play* to open a game full
screen, like or dislike it, comment on it, discuss it and tip its maker.

A game gets onto the tab by saying so in its own JSON. There is nothing to
register, nobody to ask, and no fee beyond the inscription itself: every arcade
reads the same chain, so a game you inscribe appears on all of them.

## What makes an inscription a game

Two things, both decided when you inscribe it:

1. **It is a page.** The inscription's content type is `text/html`. That page
   is what *Play* opens, so it is either the whole game or a small launcher
   that opens it (see the recipes below).
2. **Its JSON has a `game` object with a name.** The JSON box is on the
   inscribe form, under the file: *JSON, optional — inscribed with it and
   never editable*.

```json
{
  "game": {
    "name": "Moon Miner",
    "description": "Dig, upgrade the drill, reach the core.",
    "version": "1.0",
    "players": "1",
    "multiplayer": false,
    "wallet": true,
    "family": "moon-miner",
    "cover": "<txid of a picture you inscribed>",
    "play": "<txid of the game page, if this page is only its card>",
    "genre": "arcade"
  }
}
```

Only `name` is required. Everything else is optional, and anything that is the
wrong shape is simply left out rather than refusing the game.

| Field | What it is | Limit |
|---|---|---|
| `name` | The game's name, shown on its card. **Required.** | 60 characters |
| `description` | One or two lines about it. | 280 characters |
| `version` | Shown as `v1.0` on the card. | 20 characters |
| `players` | How many can play, in your own words: `"1"`, `"2-4"`, `"1-50"`. | 20 characters |
| `multiplayer` | `true` puts a *multiplayer* badge on the card. Only a real `true` counts. | — |
| `family` | A short id your versions share, such as `moon-miner`: letters, digits, `.` `_` `:` `-`. | 64 characters |
| `cover` | The txid of a picture you inscribed. It fills the card; without one, the card shows the name. | a 64-character txid |
| `play` | The txid of the page *Play* opens, when this inscription is only the game's card. Counts only if you inscribed both. | a 64-character txid |
| `genre` | One word or two: `rpg`, `racing`, `puzzle`. Shown as a tag. | 24 characters |

The JSON is inscribed with the page and can never be edited, so check it
before you inscribe. Text is trimmed to its limit and extra spaces are folded,
and a `cover` that is not a txid is ignored.

## One card per game, and how to update it

The tab shows **one card per maker and name**: the newest inscription by the
same creator with the same `name` (capital letters do not matter). To release
a new version, inscribe the new page with the same name and a new `version`.
Its card replaces the old one.

* Only you can replace your game. The card belongs to the address that
  inscribed it, so somebody else's page with the same name is a different game
  with a card of its own.
* Likes, dislikes, comments and tips belong to the inscription they were given
  to. A new version starts its own count; the old version keeps its history and
  still plays from its own link.
* An older version is not deleted. It is still on the chain and still plays;
  it just is not the card on the tab any more.

## Removing a game

The maker of a game can take its card off the tab, the same way a post's
author removes a post from the feed: press *Remove* on your own card. Only the
address that inscribed the card sees the button, and only its removal counts.

* The card disappears from the *Games* tab on every arcade, and so do older
  versions with the same name: removing a game does not bring an older one back.
* The inscription itself stays on the chain, as everything on a chain does.
  It still plays from its own link; it is just no longer listed.
* To list the game again, inscribe a new version with the same name.

## Wallet access, and lookalike games

A game can ask a player's wallet to trade, pay, put things in escrow or sign. Anybody can inscribe a game,
including a copy of yours under a lookalike name (an extra letter, a digit for a letter), so **no game gets
wallet access until the player turns it on**:

* Every card on the Apps tab has a **Wallet access** switch, off until the player turns it on, next to
  whether their wallet is unlocked in that browser. Turning it on names the game's publisher (its @name and
  address) and asks the player to check that it is the real one.
* **A game that uses the wallet does not play until the switch is on.** Say so on your card with
  `"wallet": true`, and Play waits for the switch, and so does the game's own page. A game that asks for the
  wallet while its switch is off (to trade, pay, send, mint, claim, put into escrow or sign) is stopped
  there: its page is replaced by a word pointing to its card, nothing is asked, and from then on that
  browser holds the game until the switch is on. So a game that uses the wallet should say `"wallet": true`,
  or its players meet the stop mid-game.
* A game that never asks for the wallet plays without the switch: it saves (`arcade.storage`) and joins rooms as
  the player (`arcade.realtime`), and that signature only proves who the player is and moves nothing.
* A page that is not a game (a shop, a mintpad) has no card to switch, and asks the player once when it first
  needs the wallet.

The switch belongs to the page Play opens (`play`, or the card itself), and the player's browser keeps it.

**Lookalike cards are hidden.** Each arcade checks new cards against the games already listed. A card by a
different publisher with a name that reads the same once the lookalike letters are taken out, or with the same
cover, goes to the operator's model; if it looks made to be mistaken for the other game, it is left off the
Apps tab and its `/play` link until the operator has looked. The first publisher of a name counts as the real
one, unless the operator trusts another. Pick a name of your own and nothing here affects you.

## Camera and microphone (video calls, voice)

A game can show players to each other and let them talk: a video call, a table with voices. The camera and
microphone are the player's, so they work like Wallet access:

* Say `"media": true` on your card. The card then shows a **Camera & microphone** switch, off until the player
  turns it on; turning it on names the publisher and asks the player to check it is the real one.
* A game whose switch is off and that asks anyway is told `off`, and the arcade says over the game where to turn
  it on (the game is not stopped: it can still play without them). A page that is not a game asks the player once.
* Your page never touches the camera (a sandboxed page cannot). The arcade opens it, sends it to the players you
  name, and draws the pictures over your page where you say. While the camera or microphone is on, the arcade shows
  so in the top-right corner of your page, with a **Stop** button you cannot cover; Stop tells your game `off`.

It rides the fast lane (`arcade.realtime.join(name, {fast: true})`, up to 20 players a room) and only over a
direct link: pictures and sound never go through the room.

```js
arcade.realtime.join('call-x7', {fast: true}).then(function (room) {
  room.media.start({video: true, audio: true});              // {ok, video, audio} or {ok: false, error: 'off' | 'denied'}
  room.media.place('me', {x: 10, y: 10, w: 160, h: 120}, {fit: 'cover', mirror: true});
  room.fast.on('peer', function (p, state) {
    if (state !== 'direct' || p.address !== friend) return;   // only the player you mean
    room.media.share(p.id, true);                             // or {video: false, audio: true}: the voice only
    room.media.hear(p.id, true);                              // their sound plays
    room.media.place(p.id, {x: 0, y: 140, w: 640, h: 480}, {fit: 'contain'});
  });
  room.media.on('media', function (p, has) { /* has = {video, audio} */ });
  room.media.on('media-off', function (p) { });
  room.media.on('state', function (s) { /* 'on', 'off', 'denied' */ });
});
```

**Nothing is sent and nothing is heard until you say so.** A room's name can be guessed, so being in your room gives
nobody your player's picture: `share` names who gets it (by kind), and nobody's sound plays until you `hear` them.
Check `p.address` (proved by that player's own key) before you share. Rectangles are your page's CSS pixels
(`getBoundingClientRect`), drawn exactly there, with rounded corners inside the rectangle; place them again when
your layout moves, and keep your buttons out from under them: the pictures sit on top of your page. `start` again
(say, to add the camera to a voice-only call) swaps the tracks without a gap; `mute({video, audio})` darkens or
silences them; `stop()` closes both. A pair that cannot connect directly (state `room`) gets no media unless your
builder has leased a relay: say "could not connect directly".

## How the tab ranks games

* **Popular** weighs what people said: likes, minus dislikes, plus half a point
  per comment, with newer games given a head start that fades over time. It is
  the same ranking the Exchange uses for mintpads.
* **Latest** is simply the newest first.

Likes, dislikes, comments and tips are ordinary feed actions aimed at your
game's txid, the same transactions people use on posts. Nothing about a game's
reputation lives anywhere but the chain.

## Tips

A tip on a game pays **the address that inscribed it**, in the same
transaction that says which game it was for. While the arcade runs on testnet,
that is the inscription's creator address on testnet. When games are listed on
a chain other than the one names and feed actions live on, a tip goes to the
address your own key announcement binds to the creator; if you never published
one, nobody can tip you there, so publish your key from the account you
inscribe games with.

## Recipes

### A game that is one page

Inscribe the game itself as the HTML page, with the `game` JSON. This suits
anything that fits in a single inscription: the page loads in the player's
viewer, saves with `arcade.storage`, and can pay prizes through prize pools
(see the inscription API, prize pools and referee guides).

### A card for a game you already inscribed

A game inscribed before the Apps tab existed has no `game` JSON, and JSON can
never be added afterwards. Give it a card by inscribing a tiny page, from the
**same address** that inscribed the game, whose JSON names the game in `play`:

```json
{"game": {"name": "Moon Miner", "play": "<txid of the game you inscribed>",
          "description": "Dig, upgrade the drill, reach the core."}}
```

*Play* and *Details* then open your original game directly, as itself. That
matters: everything the viewer does for a page is done for the page it shows,
so the game keeps its own `arcade.storage` saves, any prize pools bound to it,
and its referee claims, exactly as before. Likes, comments and tips go to the
card.

The card page itself can be anything small; somebody who opens it directly
can be sent on to the game:

```html
<!doctype html><meta charset="utf-8">
<p><a href="/content/<txid of the game>">Play Moon Miner</a></p>
```

Avoid a page that only redirects to the game with `location.replace` and no
`play` field: the game would run inside the card's frame, so the viewer would
answer for the card, not the game. Saves would be kept under the card's id,
and anything bound to the game page (prize pools, claims) would not find it.

### A big game made of several inscriptions

Inscribe the front page as the game, with the `game` JSON, and have it load
its parts from `/content/<txid>`. The front page is the game's identity, so
bind pools and keep saves under it.

### One page for several games

A page that offers several games behind its own menu gets **one** card,
because it is one inscription. That is a fine way to publish a collection of
small games together. If each game should have its own likes, comments, tips
and place in *Popular*, give each one its own card with `play` instead.

## Checklist before you inscribe

* The content type is `text/html`.
* The JSON is valid and has `"game": {"name": ...}`.
* The `cover`, if you use one, is the txid of a picture that is already
  inscribed (inscribe the picture first).
* You are inscribing from the address you want tips to reach, and that address
  has published its key.
* For a card with `play`: inscribed from the same address as the game it names.
* For a new version: the same `name`, a new `version`.

After the inscription's block lands, the game is on the *Games* tab of every
arcade. Press *Latest* to find it straight away.
