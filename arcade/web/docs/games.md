# Games: listing yours on the Games tab

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
    "family": "moon-miner",
    "cover": "<txid of a picture you inscribed>",
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

### A launcher for a game you already inscribed

A game inscribed before the Games tab existed has no `game` JSON, and JSON can
never be added afterwards. Give it a card by inscribing a tiny launcher page,
a few hundred bytes, that opens the game you already have:

```html
<!doctype html><meta charset="utf-8">
<script>location.replace('/content/<txid of the game you inscribed>')</script>
```

Inscribe that page with the `game` JSON. *Play* opens the launcher, which
opens your game in its place. This is also the way to build a big game out of
several inscriptions: the launcher is the front door, and it loads the rest
from `/content/<txid>`.

One thing to know: the game then runs as the launcher's inscription, so what
it saves with `arcade.storage` is kept under the launcher's id. A player's old
saves from opening the original inscription directly are not carried across.

### One page for several games

A page that offers several games behind its own menu gets **one** card,
because it is one inscription. That is a fine way to publish a collection of
small games together. If each game should have its own likes, comments, tips
and place in *Popular*, give each one its own launcher instead.

## Checklist before you inscribe

* The content type is `text/html`.
* The JSON is valid and has `"game": {"name": ...}`.
* The `cover`, if you use one, is the txid of a picture that is already
  inscribed (inscribe the picture first).
* You are inscribing from the address you want tips to reach, and that address
  has published its key.
* For a new version: the same `name`, a new `version`.

After the inscription's block lands, the game is on the *Games* tab of every
arcade. Press *Latest* to find it straight away.
