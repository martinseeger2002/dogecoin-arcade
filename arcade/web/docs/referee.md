# The referee: prizes for a verified win

A plain [prize pool](prize-pools.md) pays whoever gives its phrase, and the
phrase is in the game's own code. Anybody who reads it can claim without
playing. A pool with a **referee** pays only for a win that really happened:
the game plays from a seed the referee hands out, records what the player did,
and sends the recording with the claim. The referee runs the recording through
the game's own rules, and signs the claim only if it wins.

It works for any game whose rules can run without graphics and give the same
answer every time: puzzles, card and board games, shooters, racers, anything
turn-based or frame-based.

## What is inscribed, and what is not

* **The judge is an inscription**: your game's rules, as plain JavaScript.
* **The pool is an inscription**, like any prize pool, and its JSON names the
  judge and the referee.
* **The referee is not an inscription.** It is an arcade node, named in the
  pool by its address (`https://app.dogecoinarcade.com` unless you name
  another). It holds the key that signs winning claims, which is why it cannot
  be on the chain: a key written on the chain would sign for anybody.

Deleting the pool is one more inscription, as for any pool.

## How it works

1. **You inscribe a judge**: your game's rules as plain JavaScript, with no
   drawing, that replays a run and says whether it won.
2. **You make a pool that names a referee** (an arcade node) and the judge.
   The pool's tokens, coins or pieces go to a two-key address.
3. **Before a run, the game asks for a seed.** The referee issues it to that
   player's wallet, for that pool, for one use.
4. **The game plays from the seed and records the inputs.**
5. **On a win, the game claims with the recording.** Any arcade node takes the
   claim and passes the recording to the referee.
6. **The referee replays it.** If the judge says it won, the referee signs the
   claim, and the prize moves in one transaction.

### Why the phrase is no longer enough

A plain pool's prizes are signed ahead of time. A refereed pool's address
needs two signatures to pay a prize: the pool's, made ahead of time as usual,
and the referee's, which it gives only after a winning replay. The referee's
signature covers the whole claim, so the prize can only go to the wallet that
played. A claim built by hand from the phrase alone is refused by the network
itself, not just by arcade nodes.

Closing the pool needs neither: it takes your own wallet's key, so what is in
the pool is always yours to take back, even if the referee is gone.

## Writing a judge

A judge is one JavaScript function:

```js
function judge(seed, inputs, params) {
  // replay the run from the seed and the recorded inputs
  return {won: true, score: 1234};
}
```

| argument | |
|---|---|
| `seed` | 64 hex characters, issued by the referee. All randomness comes from it |
| `inputs` | whatever your page recorded, as JSON, up to 1 MB |
| `params` | what the pool says, so one judge can serve several pools: `{"stage": 10}`. The referee adds `params.claimer`, the address the claim pays, and `params.facts` when the pool asks for them |

It returns an object of your own design, up to 2 KB: whatever your game or
use case needs to say about a run — a time, a score, the order of finishers,
a level reached. The referee keeps it exactly as returned. Two fields are read
only when a pool asks for them: `won` (true) for a pool that requires a win,
and `score` (a number) for one that requires `score_min`. A judge that never
feeds a pool can leave both out.

### The rules that make a judge agree with the game

The referee must reach exactly the result the player's browser reached. So the
game and the judge share one simulation, and that simulation is
**deterministic**:

* **Random numbers come from the seed**, never `Math.random`. A small seeded
  generator is enough:

  ```js
  function rng(seed) {                 // mulberry32 over the seed's first word
    let a = parseInt(seed.slice(0, 8), 16) >>> 0;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  ```

* **A fixed timestep.** Advance the world one tick at a time, never by how
  long a frame took. Record inputs per tick.
* **No clock.** `Date` and `performance` do not exist in the judge.
* **Your own trigonometry.** `+ - * /` and `Math.sqrt` give the same answer in
  every engine; `Math.sin`, `cos`, `atan2`, `exp` and `pow` may not. Write your
  own (a lookup table or a short series) and use it in the game too.
* **No drawing, no page, no network.** The judge is the rules, not the game.

The simplest way to get all of this right is to write the game's rules as one
file that both the page and the judge load, and keep drawing separate.

### Recording inputs

Record what the player did, per tick, as compactly as you like. It is your
format; the judge reads it. For example:

```js
{"v": 1, "stage": 10, "moves": [[120, 0, 1], [3, 2, -1]]}
```

Run-length encoding (how many ticks, then the move) keeps a long run small.
Cap in the judge anything a player could inflate: the stage, the loadout, the
run's length.

### Testing a judge

The referee runs judges in **QuickJS 1.19.4**, with 64 MB and 5 seconds. Test
yours in exactly that before you inscribe it:

```bash
pip install quickjs==1.19.4
```

```python
import json, quickjs
ctx = quickjs.Context()
ctx.set_memory_limit(64 << 20); ctx.set_time_limit(5)
ctx.eval("delete globalThis.Date; Math.random = undefined;")
ctx.eval(open("judge.js").read())
ctx.set("s", seed); ctx.set("i", json.dumps(inputs)); ctx.set("p", json.dumps(params))
print(ctx.eval("JSON.stringify(judge(s, JSON.parse(i), JSON.parse(p)))"))
```

Play a run in the browser, save its seed and inputs, and check the judge says
the same thing the game did. Then do it for a loss.

### Inscribing it

On the NFTs page, *Inscribe one thing*, choose the `.js` file. It must be
inscribed as `text/javascript` and be at most 256 KB.

## Making a refereed pool

Everything in the [prize pools](prize-pools.md) guide applies. Add `referee`:

```json
{"name": "Final stage prize",
 "prizepool": {"token": 19, "lot": "250", "lots": 10, "price": "0.01",
               "game": "#226", "once": true, "phrase": "<your phrase>",
               "referee": {"node": "https://app.dogecoinarcade.com",
                           "judge": "#<your judge>",
                           "require": {"won": true},
                           "params": {"stage": 10},
                           "seed_hours": 2}}}
```

| field | |
|---|---|
| `node` | the arcade that referees, by its address. Leave it out to make the node you are signed in on the referee |
| `judge` | your judge inscription, `#number` or id |
| `require` | `{"won": true}`, or `{"score_min": N}` |
| `params` | optional, up to 4 KB: handed to the judge |
| `seed_hours` | optional: how long a player has from starting a run to claiming its prize. 2 unless you say, up to 720 (30 days). Give a game whose runs are long, or that lets a player claim later, more |

### What the player holds: facts

A game where levels are token balances and gear is NFTs needs the judge to
know what the player really holds, or a replay could claim the best gear.
Name what to read, and the referee passes it to the judge as `params.facts`:

```json
"facts": {"tokens": [21, 22],
          "collections": [{"creator": "<address>", "collection": "Arsenal"}]}
```

```js
params.facts = {tokens: {"21": "1500", "22": "0"},        // balances, in units
                pieces: [{id, number, collection, json}]}  // held pieces of those collections
```

It is what the player held when the seed was issued **and** still holds when
the claim is judged: the smaller balance of each token, and only the pieces
held at both moments. Gear handed to another wallet after a run starts counts
for neither run. Up to 20 tokens and 10 collections.

## The page's side

### Asking for a seed

Before a run that can win a prize:

```js
parent.postMessage({arcade: "seed", seq: 1, pool: "<pool_id>"}, "*");
```

The answer comes back as a message with the same `seq`:

* `{arcade: "seed", seq: 1, seed, expires}`: play the run from `seed`.
* `{arcade: "seed", seq: 1, error}`: no seed. `"this pool has no referee"`
  means a plain pool, so claim the plain way.

A seed belongs to the wallet that is looking and to that pool. It works once,
and lasts as long as the pool's `seed_hours` says (two hours unless it says):
a run has to be claimed within that long of starting. `expires` says when, in
seconds since 1970. Only seeds expire. The judge is an inscription and never does, and the pool
pays out until it is empty or you delete it. `pool_id` comes from `GET /r/claimpools/<your game>`.

### One run, several prizes

A session that can pay more than one prize (experience from one pool, an item
from another) asks for a seed for the whole game instead of one pool:

```js
parent.postMessage({arcade: "seed", seq: 1, game: true}, "*");
// answers {seed, expires, pools: [pool ids]}
```

Every refereed pool of this page takes that seed **once each**, with the same
replay; each pool's judge and `params` decide what it pays for. The pools must
share a referee. The seed lasts as long as the longest `seed_hours` among them.

### Claiming with the run

When the player wins:

```js
parent.postMessage({arcade: "claim", seq: 2, secret: "<phrase>",
                    pool: "<pool_id>", replay: {seed, inputs}}, "*");
```

The player sees the arcade's claim card, which says the referee checks their
run before it pays. The answers are those of any claim: `heard`, then
`{ok, txid, piece}` or `{error}`.

### When the pool pays for the claim

A pool with `"fee": "pool"` (see *Prize pools*) needs nothing of the player but
their run: the claim is made of the lot's own coins, so there is no card and no
key to unlock, and the prize is in the player's wallet as soon as the block
lands. Use it for prizes a game pays while it is being played.

### Refusals

| error | what to tell the player |
|---|---|
| `the replay did not win` | the run did not win under the judge. If it did in the game, the game and the judge disagree |
| `the judge did not say whether the replay won` / `the judge gave no score` | the pool requires `won` or `score_min` and the judge's result has no such field |
| `the replay scored N, under M` | a `score_min` pool, and the score was short |
| `that seed was used; a new run needs a new seed` | ask for a new seed and play again |
| `that seed expired; ...` | the same |
| `that seed was not issued for this pool` | the seed came from another pool |
| `this prize pays only for a replay of a win ...` | the claim came without `replay` |
| `this wallet has already claimed from this prize pool` | a `once` pool |
| `the judge ran too long` / `used too much memory` / `failed: ...` | the judge is broken for this run: a bug for the game's author |
| `that wallet has had its limit of replays judged this minute ...` | wait a minute |
| `this pool's referee did not answer ...` | the referee node is offline. The prize is still there |

## Verified results

A game can also keep its results on the chain — times, scores, a finished
level — with the referee's word that each one really happened. Nobody has to
run a score keeper: the result is an inscription, and any node, prize pool or
leaderboard page can check it.

### Recording a verified result

1. Ask for a seed bound to the judge (and the player):
   `parent.postMessage({arcade: "seed", seq, judge: "<judge id>"}, "*")`.
   Name another arcade as `referee: "https://…"` to use its referee.
2. Play from the seed and record the inputs.
3. Ask to inscribe the result with `verify`:

```js
parent.postMessage({arcade: "inscribe", seq: 2,
                    data: JSON.stringify({track: "Harbour"}),
                    contenttype: "application/json", json: {game: "racecondition"},
                    verify: {judge: "<judge id>", seed, inputs, params: {laps: 3}}}, "*");
// answers {ok, txid, result}
```

The referee runs the judge and signs **what it said** — the whole result
object, up to 2 KB — for **this player's address** over **this content**. The
signature goes into the inscription's JSON as `attested`. A copy inscribed by
anybody else, or over other content, does not check out. Verified means "this
is what the judge said", not "this was good": every run the judge scores is
verified, so a game keeps every result and decides for itself what counts.

A seed works once. If the player cancels the inscription card after the
referee has signed, that run is spent; play again with a new seed.

### Reading them: a leaderboard with no server

`GET /r/verified/<judge>` lists every inscription carrying a result that
judge verified, newest first, from every player:

```json
{"judge": "…", "referee": "02…", "next": 212,
 "results": [{"id": "…", "number": 214, "creator": "n…", "creatortag": "pip",
              "block": 1518123, "result": {"won": true, "score": 120.4, …},
              "json": {"game": "racecondition"}}]}
```

* `referee=<key>` names the referee the list trusts; this node's own unless
  given. An attestation signed with any other key is left out — without that,
  anybody could sign results with a key of their own.
* `after=<number>` pages on (pass `next`); `limit` up to 200.
* A leaderboard page reads this, sorts `result` its own way, and shows
  `creatortag`. Names come from the chain, never from the result.

### Paying on them: facts

A pool's `facts` can ask for the claimer's own verified results:

```json
"facts": {"attested": ["<judge id>"]}
```

and its judge gets `params.facts.attested`: every result of that judge the
claimer inscribed, checked against this referee's key —
`[{id, number, judge, block, result, json}]`. A 16-race tour pays its
trophies this way: the trophy pool's judge totals the claimer's verified races.
`params.claimer` is the address the claim pays.

## The referee node

`GET /r/referee` on any arcade says whether and how it referees:

```json
{"pubkey": "02…", "engine": "quickjs==1.19.4", "cpu_seconds": 5,
 "memory_bytes": 67108864, "inputs_bytes": 1048576, "judge_bytes": 262144,
 "seed_seconds": 7200, "judged_per_minute": 3}
```

* **Every node takes claims**; only the referee node runs judges. Other nodes
  forward seed requests and replays to it.
* **Its key** is made the first time it is needed, and kept in the node's own
  folder. It signs nothing but claims its judges passed.
* **If it is offline**, claims wait. Nothing is lost, and the pool's creator
  can close the pool at any time with their own wallet.
* **To referee on your own node**, install its extra: `pip install
  "dogecoin-arcade[referee]"`, which adds QuickJS and the signing library.

## What a referee proves, and what it does not

* **It proves** that a winning run was played from a fresh seed, and that the
  prize goes to the wallet that played it.
* **It does not prove a person played.** A program that can win your game can
  win your prize.
* **The seed is what stops a run being reused.** The same moves on another
  seed may still win an easy stage. Pay for hard things (a final stage, a high
  score), not for the first level.
* **`once` is per wallet**, not per person.
* **The referee node is trusted** to judge honestly and to stay online. Name a
  node you trust.
