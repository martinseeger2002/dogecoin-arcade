# A game's bank on your own node

`bank.py` sits in your game's realtime room as your bank's own address, hears what players are owed, and pays them in
your game's tokens straight through your node. No browser, no web API in the write path, no offer waiting in anybody's
memory: the transaction is built here from the arcade's own payload code, read back with the index's own reader,
journaled, and handed to the node.

It is the pattern a live testnet game's bank runs on, cut down to what any game needs. Read it, then make it yours.

## What you need

- An arcade node on testnet, with its ledger index (`~/.dogecoinarcade/test-ledger.sqlite`) and its Pepecoin Core
  RPC cookie. The bank only READS the index; it writes nothing of the arcade's.
- A key for the bank: 64 hex digits in a file only you can read (`chmod 600`). Its address is the bank. Fund it with a
  few coins (every payout spends a small confirmed coin) and give it the tokens it pays: either hold a balance of
  them (it sends from custody first) or issue them as managed tokens from this address (it grants what custody cannot
  cover).
- Your node's public name (`node` in the config): the room is joined through it, the way a player's game joins.

## Run it

    cp examples/rpc_bank/config.example.json bank.json     # then edit it
    python examples/rpc_bank/bank.py --config bank.json --dry    # builds and checks payouts, sends nothing
    python examples/rpc_bank/bank.py --config bank.json
    python examples/rpc_bank/bank.py --config bank.json --status
    python examples/rpc_bank/bank.py --config bank.json --retry 12 13

Run it under systemd (or anything that restarts it): every promise is in SQLite, so a restart loses nothing.

## The protocol your game speaks

A player's game, in the room:

    {"t": "dep", "id": "<unique per request>", "items": {"gold": 30, "wood": 4}}

The bank, at once (the same `id` asked again gets the same answer and nothing more):

    {"t": "dep", "id": "...", "to": "<player address>", "ok": true, "paid": {"gold": 30, "wood": 4}, "refused": [], "note": "..."}

and when each payout has gone out:

    {"t": "paid", "to": "<player address>", "item": "gold", "units": 30, "txid": "..."}

Messages from guests (players who are not signed in) are ignored: a payout needs an address.

## Never twice, never wrong

- **Intents.** Every payout is named by its job ids. The signed bytes are written and fsynced before the broadcast;
  asked again, an intent returns its first txid, or sends the very same bytes if the node lost them. A job that
  already went out under another intent is HELD for a person.
- **Read back.** Before anything is journaled the transaction is decoded with the index's own reader and must say:
  this bank sent it, the player is the recipient, these exact payload bytes, every input the bank's own, a sane fee.
- **Confirmed coins only.** A payout never spends change still waiting for a block.
- **Coalescing.** Jobs of the same player and item that arrive together (a player mining one unit at a time) wait up
  to `hold_s` and go as one transaction.
- **Caps.** `per_day` units per player and item, `per_minute` payouts, `batch_cap` units in one payout (over it: held).

## Sweep back

- Every 10 minutes, payouts from the last two days are checked against the ledger index and the node. One the chain
  refused (invalid: nobody was paid) is paid once more; a second refusal is HELD. One the node lost is sent again as
  the same bytes. One that cannot be accounted for is HELD.
- Every hour the bank's small coins (under `sweep_below`) are swept into one.

## Trust: testnet only

The bank believes what a game says it is owed, within the caps. That is fine for a testnet game and nothing else, and
`bank.py` refuses to run with `"network"` other than `"test"`. A mainnet bank must prove what it pays for -- a referee
that has seen the play -- before it pays.
