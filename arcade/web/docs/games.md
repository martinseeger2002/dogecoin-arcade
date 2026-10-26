# Apps: making one and listing it on the Apps tab

The **Apps** tab (it was the *Games* tab until 2026-10-23; old `/games` links still land on it) lists every app
anyone has inscribed, on every arcade and every chain the arcade reads. An app is a game, a video call, a voice room, a
shop, a tool: any HTML page with a card. Players press **Open** to run it full screen, star it, like or dislike it,
comment on it and tip its maker. On a phone with the arcade installed, the same list is the home screen's **App Store**,
and an app added from there gets its own icon.

In full screen the arcade keeps one thin strip of its own under your app (22 pixels, 32 on a phone), saying that it
never asks for a password inside a game; your frame is that much shorter. The arcade's own cards (a send, a trade,
Wallet access) always come up over your app with Cancel ready, and their yes works a moment after they appear.

An app gets onto the tab by saying so in its own JSON. There is nothing to register, nobody to ask, and no fee beyond
the inscription itself: every arcade reads the same chain, so an app you inscribe appears on all of them.

This page starts with a walkthrough, then the full reference. What your page can ask the arcade for (saving, the
wallet, rooms, contacts) is in *The inscription API* and *Multiplayer and trades*.

## Your first app, step by step

You need an account on an arcade (press **Join** on any arcade; where the operator has the faucet on, a new account gets
Pepecoin test coins from it) and a text editor. Start on testnet: test coins cost nothing, and a mistake costs nothing either. Everything
you inscribe stays on the chain for good, so test there before you inscribe on a real coin.

1. **Write the page.** One HTML file, with everything inside it: its styles, its script, its pictures as `data:`
   URLs. An inscribed page can load nothing from the internet (no CDN, no web font, no analytics), only other
   inscriptions (`/content/<txid>`) and the arcade's own scripts (`/r/*.js`). A minimal app:

   ```html
   <!doctype html>
   <html lang="en"><head>
   <meta charset="utf-8">
   <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
   <title>Tap Counter</title>
   <style>
     html,body{margin:0;height:100%;display:grid;place-items:center;background:#101418;color:#eef;font:18px system-ui}
     button{font:inherit;padding:16px 28px;border-radius:12px;border:0;background:#f5b400;color:#111}
   </style>
   </head><body>
   <div style="text-align:center">
     <p id="best">Best: 0</p>
     <button id="tap">Tap</button> <p id="n">0</p>
   </div>
   <script src="/r/storage.js"></script>
   <script>
   var n = 0, best = 0, save = null;
   arcade.storage.ready.then(function (s) { save = s; best = Number(s.getItem('best') || 0); show(); });
   function show() { document.getElementById('n').textContent = n;
                     document.getElementById('best').textContent = 'Best: ' + best; }
   document.getElementById('tap').onclick = function () {
     n++; if (n > best) { best = n; if (save) save.setItem('best', String(best)); } show();
   };
   </script>
   </body></html>
   ```

   `arcade.storage` keeps the player's best score for them, in their wallet, the same on every device they use it
   from. A page that does not need it can leave the script out.

2. **Inscribe a cover picture (optional).** A square or 4:3 picture fills the card; without one, the card shows the
   app's name. Go to **Creator → NFTs**, choose the picture under **Inscribe one thing** and press **Inscribe it**.
   When it is done, copy its id (the 64-character txid on its page). Small pictures are quicker and cheaper: the page
   offers Medium and Small sizes.

3. **Write the card.** The card is a little JSON that goes on the chain with the page:

   ```json
   {"app": {"name": "Tap Counter", "description": "Tap. Beat your best.", "version": "1.0",
            "players": "1", "genre": "arcade", "family": "tap-counter",
            "cover": "<txid of the picture from step 2>"}}
   ```

   Only `name` is required. Every field is in the table below. Check that the JSON is valid (a missing comma or quote
   means no card): paste it into any JSON checker, or ask the robot helper in the corner of the page.

4. **Inscribe the page with its card.** Still under **Creator → NFTs → Inscribe one thing**, choose your `.html` file,
   open **JSON beside the file (optional)** and paste the card into it, then press **Inscribe it**. The page shows how
   many transactions it takes and what it costs, and asks once. The file must end in `.html` so that it is inscribed
   as `text/html`: only a page can be an app.

5. **Find it.** When the inscription's block lands (about a minute), open **Apps** and press **Latest**. Your card is
   there, on every arcade. Press **Open** to play it. If it is not there, see *When the card does not show* below.

6. **Release a new version.** Change the page, inscribe it again with the same `name` and a new `version`. The new
   card replaces the old one, and it keeps the stars, likes, comments and tips the old one had.

The JSON can never be edited after it is inscribed, and neither can the page. A mistake is fixed by inscribing a new
version, never by changing the old one.

### From the command line

A developer can inscribe the same thing without a browser, with `arcade-publish` (it ships with the arcade). It signs
with a key kept in a file (64 hex digits, mode 0600) and asks the node for the coins:

```
arcade-publish -testnet --key ~/.arcade/publish.key --datadir ~/.pepecoin-testnet \
    --index ~/.dogecoinarcade/test-ledger.sqlite \
    --name tap-counter.html --json "$(cat card.json)" tap-counter.html
```

`--whoami` says the key's address and balance and sends nothing. Every transaction is written to a journal beside the
file before it is sent, so a run that stops carries on where it stopped and nothing is paid for twice. Wait for one
publish to be in a block before you start the next from the same key: two at once can try to spend the same coin.

## An app made of several inscriptions

One page is fine for a small app. A bigger one (a video call, an RPG, a voice room) is better as a small **launcher**
plus **parts**: the style, the screens and the program, each its own inscription. A new version is then new parts, not
a new launcher, and the card keeps its id. The arcade's own Video Call and Arcade Spaces apps are built this way.

1. **Inscribe the parts.** For example `style.css`, `body.html` and `app.js`, each under **Inscribe one thing**, with
   no JSON. Note each one's txid.
2. **Inscribe a registry** (optional, but it is what makes updates easy): a small JSON inscription, from the same
   account, that names the parts and a version number:

   ```json
   {"myapp": "registry", "version": 2,
    "modules": {"style": {"id": "<txid>"}, "body": {"id": "<txid>"}, "app": {"id": "<txid>"}}}
   ```

3. **Inscribe the launcher** with the card in its JSON. It finds its creator's newest registry, then loads the parts:

   ```html
   <!doctype html><html><head><meta charset="utf-8">
   <meta name="viewport" content="width=device-width,initial-scale=1"></head>
   <body><p id="boot">Loading…</p>
   <script>
   var CREATOR = '<your address>', reg = /* the registry, baked in as a fallback */ {"version": 1, "modules": {}};
   function text(id) { return fetch('/content/' + id).then(function (r) { return r.text(); }); }
   fetch('/r/inscriptions?creator=' + CREATOR + '&limit=300').then(function (r) { return r.json(); })
     .then(function (list) {
       list.forEach(function (p) {                         // the newest registry by this creator wins
         var j = p.json;
         if (j && j.myapp === 'registry' && p.creator === CREATOR && j.version > reg.version) reg = j;
       });
     }).catch(function () {})                             // offline index: use the one baked in
     .then(function () {
       var m = reg.modules;
       return Promise.all([text(m.style.id), text(m.body.id), text(m.app.id)]);
     }).then(function (parts) {
       var st = document.createElement('style'); st.textContent = parts[0]; document.head.appendChild(st);
       document.getElementById('boot').remove();
       document.body.insertAdjacentHTML('afterbegin', parts[1]);
       var s = document.createElement('script'); s.textContent = parts[2]; document.body.appendChild(s);
     });
   </script></body></html>
   ```

   Check `p.creator` as above: anybody can inscribe a JSON that calls itself your registry, and only yours counts.

4. **Updating it** is now: inscribe the changed parts, then a registry with a higher `version`. Every player gets the
   new version the next time they open the app; the launcher and the card stay as they are.

Keep these in mind:

* **Rooms belong to a page.** A room an app joins is the app's own, named from the page the player opened, so two
  launchers would be two sets of rooms. To keep players of every version together, give the card a `family` and join
  with it: `arcade.realtime.join('lobby', {game: 'my-app'})`.
* **Saves and switches belong to the page *Open* runs** (the launcher). Keep that page, and every player keeps their
  saves, their Wallet access and their other switches across updates.
* **Load the arcade's scripts** (`/r/realtime.js`, `/r/storage.js` and the rest) with a `<script src>` tag. Only the
  arcade's own paths work; anything else is refused by the page's sandbox.

## What makes an inscription an app

Two things, both decided when you inscribe it:

1. **It is a page.** The inscription's content type is `text/html`. That page
   is what *Open* runs, so it is either the whole app or a small launcher
   that loads it (see above).
2. **Its JSON has an `app` object with a name.** Cards inscribed before the
   tab was renamed say `game` instead, and still count: `{"game": {...}}` and
   `{"app": {...}}` are the same card.

```json
{
  "app": {
    "name": "Moon Miner",
    "description": "Dig, upgrade the drill, reach the core.",
    "version": "1.0",
    "players": "1",
    "multiplayer": false,
    "wallet": true,
    "family": "moon-miner",
    "cover": "<txid of a picture you inscribed>",
    "play": "<txid of the app page, if this page is only its card>",
    "genre": "arcade"
  }
}
```

Only `name` is required. Everything else is optional, and anything that is the
wrong shape is simply left out rather than refusing the app.

| Field | What it is | Limit |
|---|---|---|
| `name` | The app's name, shown on its card. **Required.** | 60 characters |
| `description` | One or two lines about it. | 280 characters |
| `version` | Shown as `v1.0` on the card. | 20 characters |
| `players` | How many can use it, in your own words: `"1"`, `"2-4"`, `"1-50"`. | 20 characters |
| `multiplayer` | `true` puts a *multiplayer* badge on the card. Only a real `true` counts. | — |
| `family` | A short id your versions share, such as `moon-miner`: letters, digits, `.` `_` `:` `-`. Rooms can be shared under it, and `/play/<family>` always opens your newest card. | 64 characters |
| `cover` | The txid of a picture you inscribed. It fills the card; without one, the card shows the name. | a 64-character txid |
| `play` | The txid of the page *Open* runs, when this inscription is only the app's card. Counts only if you inscribed both. | a 64-character txid |
| `genre` | One word or two: `rpg`, `racing`, `puzzle`, `voice`. Shown as a tag. | 24 characters |
| `wallet` | `true`: the app asks the player's wallet to trade, pay, send, mint or sign. Its card shows **Wallet access**. See below. | — |
| `media` | `true`: the app uses the camera or microphone. Its card shows **Camera & microphone**. | — |
| `contacts` | `true`: the app reads the player's address book (`arcade.contacts.list`). Its card shows **Contacts**. | — |
| `notify` | `true`: people can ring the player in this app while it is closed (`arcade.realtime.ring`). Its card shows **Notifications**. | — |
| `minimized` | How long your app keeps running after a player minimizes it on the installed app's home screen: `{"timeout": 300}` is 300 seconds, then it is closed; `0` is no limit; left out, 15 minutes. See *Minimized* below. | up to 86400 seconds |

The JSON is inscribed with the page and can never be edited, so check it
before you inscribe. Text is trimmed to its limit and extra spaces are folded,
and a `cover` that is not a txid is ignored. `wallet`, `media`, `contacts`,
`notify` and `multiplayer` count only as a real `true`, not `"true"`.

## One card per app, and how to update it

The tab shows **one card per maker and name**: the newest inscription by the
same creator with the same `name` (capital letters do not matter). To release
a new version, inscribe the new page with the same name and a new `version`.
Its card replaces the old one.

* Only you can replace your app. The card belongs to the address that
  inscribed it, so somebody else's page with the same name is a different app
  with a card of its own.
* Likes, dislikes, comments and tips carry over: whatever was said about an
  older version counts for the card that is shown now, and a player's last word
  counts (an unlike on the new card undoes a like on the old one). Stars carry
  over too, because a star is kept under the maker and the name.
* An older version is not deleted. It is still on the chain and still runs
  from its own link; it just is not the card on the tab any more.
* **A link that always opens the newest version:** `/play/<family>` (or
  `/play/<the name, in lower case with dashes>`). Put that on your website.
  Somebody who is not signed in gets a sign-up first, then the app.

## Removing an app

The maker of an app can take its card off the tab, the same way a post's
author removes a post from the feed: press *Remove* on your own card. Only the
address that inscribed the card sees the button, and only its removal counts.

* The card disappears from the *Apps* tab on every arcade, and so do older
  versions with the same name: removing an app does not bring an older one back.
* The inscription itself stays on the chain, as everything on a chain does.
  It still runs from its own link; it is just no longer listed.
* To list the app again, inscribe a new version with the same name.

## When the card does not show

* **Not in a block yet.** Wait a minute and look under **Latest**.
* **Not a page.** The file was not inscribed as `text/html` (it did not end in `.html`). Inscribe it again as a page.
* **No card in the JSON.** The JSON was not valid, or the object is not called `app` (or `game`), or it has no
  `name`. Open the inscription's **Details** to see the JSON as it went on the chain.
* **A `play` that does not count.** `play` names a page inscribed by somebody else: *Open* then runs the card itself.
* **Held for a look.** A card that reads like another publisher's app, or uses its cover, waits for the operator
  (see *lookalike cards* below).

## Wallet access, and lookalike apps

An app can ask a player's wallet to trade, pay, put things in escrow or sign. Anybody can inscribe an app,
including a copy of yours under a lookalike name (an extra letter, a digit for a letter), so **no app gets
wallet access until the player turns it on**:

* Every card on the Apps tab has a **Wallet access** switch, off until the player turns it on, next to
  whether their wallet is unlocked in that browser. Turning it on names the app's publisher (its @name and
  address) and asks the player to check that it is the real one.
* **An app that uses the wallet does not play until the switch is on.** Say so on your card with
  `"wallet": true`, and Open waits for the switch, and so does the app's own page. An app that asks for the
  wallet while its switch is off (to trade, pay, send, mint, claim, put into escrow or sign) is stopped
  there: its page is replaced by a word pointing to its card, nothing is asked, and from then on that
  browser holds the app until the switch is on. So an app that uses the wallet should say `"wallet": true`,
  or its players meet the stop mid-game.
* An app that never asks for the wallet plays without the switch: it saves (`arcade.storage`) and joins rooms as
  the player (`arcade.realtime`), and that signature only proves who the player is and moves nothing.
* A page that is not an app (a shop, a mintpad) has no card to switch, and asks the player once when it first
  needs the wallet.

The switch belongs to the page *Open* runs (`play`, or the card itself), and the player's browser keeps it.

**Lookalike cards are hidden.** Each arcade checks new cards against the apps already listed. A card by a
different publisher with a name that reads the same once the lookalike letters are taken out, or with the same
cover, goes to the operator's model; if it looks made to be mistaken for the other app, it is left off the
Apps tab and its `/play` link until the operator has looked. The first publisher of a name counts as the real
one, unless the operator trusts another. Pick a name of your own and nothing here affects you.

## Camera and microphone (video calls, voice)

An app can show players to each other and let them talk: a video call, a table with voices. The camera and
microphone are the player's, so they work like Wallet access:

* Say `"media": true` on your card. The card then shows a **Camera & microphone** switch, off until the player
  turns it on; turning it on names the publisher and asks the player to check it is the real one.
* An app whose switch is off and that asks anyway is told `off`, and the arcade says over the app where to turn
  it on (the app is not stopped: it can still play without them). A page that is not an app asks the player once.
* Your page never touches the camera (a sandboxed page cannot). The arcade opens it, sends it to the players you
  name, and draws the pictures over your page where you say. While the camera or microphone is on, the arcade shows
  so in the top-right corner of your page, with a **Stop** button you cannot cover; Stop tells your app `off`.

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
silences them; `stop()` closes both. A pair that cannot connect directly (state `room`) gets no media unless the
arcade's operator has turned on the call relay (Admin → Network → Call relay) for your app: say "could not connect
directly". The full `room.media` reference, and the relay's rules, are in *Multiplayer and trades*.

## Minimized: what your app does in the background

On a phone with the arcade installed (the PWA), every app opens full screen over the home screen, and the player can
pull down from the top edge to **Minimize** it. A minimized app keeps running: its page, its sockets and its seat in a
room all stay. You decide for how long, on your card:

```json
{"app": {"name": "Moon Miner", "minimized": {"timeout": 300}}}
```

- `{"timeout": 300}` -- keep running five minutes after it is minimized, then the arcade closes it. Restoring it before
  then stops the clock. At most 86400 (a day).
- `{"timeout": 0}` -- no limit: keep running until the player closes it.
- no `minimized` (or no `timeout`) -- 15 minutes.

Your page is told what is happening, so it can pause, save, or keep its seat warm. Load `/r/app.js`:

```html
<script src="/r/app.js"></script>
<script>
arcade.app.on(function (s) {
  // s.state: "minimized", "restored" or "closing"; s.timeout: your seconds (null: none);
  // s.closesAt: when it will be closed, in ms since 1970 (null: never); s.inShell: on the home screen.
  if (s.state === 'minimized') pauseTheMusic();
  if (s.state === 'restored') resumeTheMusic();
  if (s.state === 'closing') saveTheGame();      // the player closed it, or your timeout ran out
});
arcade.app.state().then(function (s) { /* {state: "open", ...} until something happens */ });
</script>
```

"closing" comes a moment before the page is removed: save quickly. The same thing is sent as a window event,
`arcade:app`. In a browser tab nothing is ever minimized (the browser has tabs of its own), so only `"open"` is heard.
A phone may still pause the whole arcade when the player leaves it for another app; when they come back, an app whose
timeout ran out meanwhile is closed then.

## How the tab ranks apps

* **Popular** weighs what people said: likes, minus dislikes, plus half a point
  per comment, with newer apps given a head start that fades over time. It is
  the same ranking the Exchange uses for mintpads.
* **Latest** is simply the newest first.
* **Starred** apps come first, then the rest, each in that order. A star is the
  player's own, kept on their arcade.

Likes, dislikes, comments and tips are ordinary feed actions aimed at your
app's txid, the same transactions people use on posts. Nothing about an app's
reputation lives anywhere but the chain.

## Tips

A tip on an app pays **the address that inscribed it**, in the same
transaction that says which app it was for. While the arcade runs on testnet,
that is the inscription's creator address on testnet. When apps are listed on
a chain other than the one names and feed actions live on, a tip goes to the
address your own key announcement binds to the creator; if you never published
one, nobody can tip you there, so publish your key from the account you
inscribe apps with.

## Contacts and notifications

Two more switches work the same way as Wallet access, each shown on a card that says it needs it:

* **Contacts** (`"contacts": true`): your app may read the player's address book with `arcade.contacts.list()`,
  names and addresses only. Until the player turns it on, the list is empty. Adding or removing a person
  (`arcade.contacts.add`, `arcade.contacts.remove`) asks the player each time, switch or not.
* **Notifications** (`"notify": true`): the people in the player's address book can ring them in your app while it is
  closed, and their phone shows a notification. Nobody else can ring them. Ring with
  `arcade.realtime.ring(address, {line: 'Video call', link: 'call=x7'})`; when the player taps the notification,
  your app opens and `arcade.realtime.ringed()` says who rang and the `link` you sent. The switch is the account's,
  so every phone the player is signed in on rings. It needs the arcade installed on the phone (on an iPhone: **Share →
  Add to Home Screen**) and an arcade that sends notifications.

The player sees every app they allowed, and can turn each off, under **Game access** (`/me/access`).

## On the phone: the App Store and the home screen

On a phone with the arcade installed, the home screen has an **App Store**: the same list as the Apps tab. Adding an
app puts its icon on the home screen, and the switches its card says it needs (wallet, camera, contacts, notifications)
are asked for as it is added. The icon is your `cover`. Players can arrange icons over several pages and into folders,
and hold an icon for **Open**, **Close** and **Remove**. An app opens full screen; pulling down from the top edge
minimizes it (see *Minimized* above).

So for the phone, a good cover is a square picture that still reads when it is small, like a phone app icon.

## On the T-Deck: a build for the LoRa handheld

The DogecoinArcade T-Deck firmware has an App Store of its own. It lists every app whose card names a **T-Deck build**,
downloads it over Wi-Fi, checks it against the sha256 the arcade lists, keeps it in the T-Deck's flash and runs it there.
A T-Deck has no web browser, so the build is not your page: it is a small JavaScript file.

1. Inscribe the script as `text/javascript`, at most 64 KB.
2. Name it on your card: `{"app": {"name": "Tic-Tac-Toe", "tdeck": "<the script's txid>"}}`. Only a script **you**
   inscribed counts, the same rule as `play`.
3. Optionally, pick its home-screen icon from the T-Deck's own: `"tdeck_icon": "globe"`. Without one, the tile shows the
   app's first letter.

The arcade lists them at `/r/tdeck/apps`. The script runs in a sandbox: it can draw on its own screen, hear keys, keep a
few strings, set timers, chime, and read `.mesh` pages (`mesh.get`, see [the .mesh web](mesh-web.md)). It has no radio,
no wallet and no files.

```js
ui.title("Hello");
ui.text("Press a button", {center: true});
ui.row(function () { ui.button("Yes", function () { chime("coins"); }, {primary: true}); ui.button("No", app.close); });
var c = ui.canvas(120, 60); c.fill(0x141310); c.rect(10, 10, 20, 20, 0xd9a520); c.text(40, 20, "hi", 0xffffff);
onKey(function (k) { return k === "enter"; });           // "up" "down" "left" "right" "enter" "backspace" or a character
ui.link("Read the about page", function () { /* ... */ }); // underlined, wrapping text to pick
mesh.get("me-seeks.mesh/about", function (p) { ui.text(p.ok ? p.body : p.why); });
every(500, function () { /* ... */ });                   // after(ms, fn), cancel(id)
store.set("best", "12"); store.get("best");              // strings, 4 KB per app
```

An app may use 512 KB of memory, and one call may run for a quarter of a second; past either, the T-Deck stops it and
says why. A full example is `apps/tictactoe.js` in github.com/dogecoinarcade/arcade-lora.

## Recipes

### An app that is one page

Inscribe the app itself as the HTML page, with the `app` JSON (the walkthrough at the top). This suits anything that
fits in a single inscription: the page saves with `arcade.storage`, meets other players with `arcade.realtime`, and can
pay prizes through prize pools (see the inscription API, prize pools and referee guides).

### A card for a page you already inscribed

A page inscribed without a card has no `app` JSON, and JSON can never be added afterwards. Give it a card by
inscribing a tiny page, from the **same address** that inscribed the app, whose JSON names it in `play`:

```json
{"app": {"name": "Moon Miner", "play": "<txid of the page you inscribed>",
         "description": "Dig, upgrade the drill, reach the core."}}
```

*Open* and *Details* then run your original page directly, as itself. That matters: everything the viewer does for a
page is done for the page it shows, so the app keeps its own `arcade.storage` saves, any prize pools bound to it, and
its referee claims, exactly as before. Likes, comments and tips go to the card.

The same trick gives an app a new card (a new cover, a new description) without inscribing the app again: a new card
with the same `name` and `play` naming the same page replaces the old card, and every player keeps their saves and
switches, because those belong to the page.

The card page itself can be anything small; somebody who opens it directly can be sent on to the app:

```html
<!doctype html><meta charset="utf-8">
<p><a href="/content/<txid of the app>">Open Moon Miner</a></p>
```

Avoid a page that only redirects to the app with `location.replace` and no `play` field: the app would run inside the
card's frame, so the viewer would answer for the card, not the app. Saves would be kept under the card's id, and
anything bound to the app's page (prize pools, claims) would not find it.

### A big app made of several inscriptions

A launcher and parts, with a registry for updates: see *An app made of several inscriptions* above. The launcher is
the app's identity, so bind pools and keep saves under it.

### One page for several games

A page that offers several games behind its own menu gets **one** card, because it is one inscription. That is a fine
way to publish a collection of small games together. If each game should have its own likes, comments, tips and place
in *Popular*, give each one its own card with `play` instead.

## Checklist before you inscribe

* The file ends in `.html`, so its content type is `text/html`.
* It loads nothing from the internet: only `/content/<txid>` and `/r/*.js`.
* The JSON is valid and has `"app": {"name": ...}` (or `"game"`).
* The `cover`, if you use one, is the txid of a picture that is already inscribed (inscribe the picture first).
* `wallet`, `media`, `contacts` and `notify` are `true` for each thing your app uses: an app that asks for the wallet
  without saying so is stopped mid-game.
* You are inscribing from the address you want tips to reach, and that address has published its key.
* For a card with `play`: inscribed from the same address as the page it names.
* For a new version: the same `name`, a new `version`.
* You tried it on testnet first.

After the inscription's block lands, the app is on the *Apps* tab of every arcade. Press *Latest* to find it straight
away.
