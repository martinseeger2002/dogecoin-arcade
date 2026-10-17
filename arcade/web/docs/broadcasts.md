# Broadcasts: one node telling every node

A **broadcast** is public machine talk: a program on one arcade publishes a small
JSON message under a **topic**, and every arcade's index reads it. Nothing draws
it. It never shows up in a feed or a chat; programs read it by topic.

Use it for anything one node wants every node to hear: a game's announcement,
tournament results, a price list, a list of flagged content. It is the public
counterpart of node-to-node messages, which are sealed to one recipient.

* **Public.** Not encrypted; any node, any program, anyone can read it.
* **From an address the chain proves.** The sender is whoever paid for the
  transaction. An arcade broadcasts from its fee address, the same address its
  instance and mesh announcements come from, so readers know which arcade said it.
  What a sender's word is worth is up to each reader.
* **One transaction.** Up to 7,000 bytes; a larger message is refused, not
  split. Publish several instead.
* **Confirmed only.** A broadcast is read once its block arrives.
* **On the messaging chain** (testnet), like every other message. Older arcades
  simply skip a type they do not know.

## Topics

A topic is 1 to 64 lower-case letters, digits and `. _ : / -`. Name the format
and its version, for example `my-game/scores/v1`. A reader skips topics it does
not understand, so a new version can live beside the old one.

## From a program (bot RPC)

| Method | |
|---|---|
| `da_publish topic body [from]` | prepares a broadcast; `body` is any JSON. Send it with `da_broadcast txid` |
| `da_broadcasts [topic after count [senders]]` | confirmed broadcasts, oldest first: `id`, `txid`, `sender`, `topic`, `body`, `height`, `blocktime`. Pass the last `id` as `after` to read on from there |

With the `rpc()` helper from [The bot RPC](bot-rpc.md):

```python
prepared = rpc("da_publish", "my-game/scores/v1", {"week": 41, "top": ["@pip", "@vex"]})
rpc("da_broadcast", prepared["txid"])            # sent: every arcade reads it next block

after = 0
for b in rpc("da_broadcasts", "my-game/scores/v1", after, 50):
    print(b["sender"], b["body"])
    after = b["id"]
```

## Its first user: sensitive-material flags

An arcade whose content screening is on can tell every other arcade what its
model flagged. Turn it on under **Admin → Switches → Broadcast this arcade's
sensitive-material flags**. The flags go out under the topic `flags/v1`:

```json
{"f": [["<sha256 of the picture or words>", "sensitive"],
       ["<sha256>", "illegal"],
       ["<sha256>", "ok"]]}
```

* **Only a fingerprint and a verdict**, never the content. A picture or a post
  has the same fingerprint wherever it is posted.
* **`ok` takes a flag back**, for instance when the operator clears a post.
* **Many flags share one transaction**: they are gathered for half a minute and
  sent together, at most 200 broadcasts a day.
* **Only the model's own verdicts go out**, never flags taken from another
  arcade, so arcades that follow each other do not echo.

Every arcade lists the arcades that broadcast flags under **Admin →
Sensitive-material flags from other arcades**, by @name or domain, each with its
own switch. Turn one on and what it flags is covered (sensitive) or hidden
(illegal) on your arcade too, even with your own screening off. Another arcade's
flags can only make yours stricter, and anything you clear yourself stays
cleared.
