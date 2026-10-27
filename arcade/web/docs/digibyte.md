# DigiByte on this arcade

DigiByte (DGB) is an **add-on chain**, like Dogecoin and Litecoin. An operator turns it on in
**Admin → Chains**. It can run here, with a DigiByte node on this machine, or be borrowed from an
arcade that shares its DigiByte Core over the mesh (see *Shared Cores*). It is shown grayed out until
its node has synced.

**Status:**
- **In the wallet,** on the same key as PEP, DOGE and LTC.
- **Swaps between chains and escrows** work as on Litecoin: DigiByte enforces lock times
  (CHECKLOCKTIMEVERIFY and CSV have been buried soft forks since block 4,394,880).
- **DigiByte ordinals are indexed and shown,** and you can inscribe one from **Creator → Ords**.

## Running the node

The arcade expects **DigiByte Core 9.26.7** or newer (`digibyted`). It is Bitcoin Core's code. Its
config lives at `~/.digibyte/digibyte.conf`:

```
server=1
txindex=1
prune=0
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcport=14022
port=12024
```

- **`txindex=1` is needed** for the wallet's history.
- **RPC stays on 127.0.0.1.** Only the peer port (12024) needs to be reachable, and only if you want
  peers to connect to you.
- **A big chain.** Blocks come every 15 seconds, so the chain has over 24 million of them. A first sync
  takes days on a small machine. A larger `dbcache` while it syncs helps a lot.
- **Its testnet** keeps its data in `testnet26/` since DigiByte's 2025 testnet reset. The arcade does not
  run DigiByte testnet.

## Proof of work: the sharer's word

DigiByte is mined with **five algorithms** (SHA-256, Scrypt, Skein, Qubit and Odocrypt), chosen per
block by bits in the block's version. This arcade does not check DigiByte's work. A block borrowed
from another arcade is still proved by its hash, its merkle root and its link to the block before it,
and **Admin → Chains** says that the work is the sharer's word. On a node of your own, DigiByte Core
checks everything as usual.

## Addresses

- **Ordinary DigiByte addresses start with `D`.** Their version byte is 30, **the same as Dogecoin's.**
  The same key gives the same `D…` string on both chains, and a coin sent to it belongs to that key on
  either chain, so pasting one for the other loses nothing. The arcade still names the chain on every
  send, so check which coin you picked.
- **Script (P2SH) addresses start with `S`** (version 63). No other chain here uses that byte.
- **Older `3…` script addresses are refused.** DigiByte's source still lists version 5, but DigiByte
  Core 9.26.7 does not accept a `3…` address, so neither does the arcade.
- **SegWit (`dgb1…`)** is active on DigiByte, and Taproot since block 21,168,000. The arcade pays
  DigiByte's legacy `D…` and `S…` addresses for now. Ask for one of those.

## Keys and fees

- **Keys:** coin type 3, the same key as PEP, DOGE and LTC. The **Backup** page hands out your
  DigiByte private key (WIF, version 128) for importing into DigiByte Core.
- **Fees:** 0.01 DGB per kB, ten times the node's 0.001 DGB/kB minimum relay fee and the same as its
  wallet's fallback fee. There is no soft-dust fee. Change under 0.0001 DGB is left to the miner (the
  node's dust line for an ordinary output is 5,460 satoshis).
- **Big swaps:** a lock asks for one confirmation from 1,000 DGB. That is low on purpose until the
  operator sets `swap_big:dgb` to what the coins are worth.

## DigiByte ordinals

DigiByte ordinals are DigiByte's ord inscriptions, in the Doginals format: the data rides in an ordinary
input, the way `digibyteordinals` writes it and `wonky-ord-digibyte` reads it. Taproot is active on
DigiByte, but its ordinals are not Taproot envelopes, so the index reads the Doginals kind only.

- **The index:**
  - Admin → Ords → DigiByte, set to *walk*, *help*, *read* or *off*. It is off until the operator turns it on.
  - It starts at block **0**, which is `wonky-ord-digibyte`'s `first_inscription_height`. That is a long
    walk: over 24 million blocks. Borrowed lanes and helpers share it.
- **Numbering follows wonky-ord, as on Dogecoin's Doginals kind:** a new inscription is looked for only
  when no inscription already sits on the first unit of the coin it is made on, and an envelope of 0 parts
  is not an inscription.
- **Tokens are `dgb-20`:** DRC-20's rules under DigiByte's own `"p":"dgb-20"`, as `wonky-ord-digibyte` reads
  them. A `drc-20` inscription on DigiByte is not a token there.
- **In the wallet:** your DigiByte ordinals are shown, and they can be sent, listed and bought like
  Doginals. An ordinary DGB send never spends a coin that carries one.
- **Inscribing:** **Creator → Ords** inscribes one from your own wallet in 240-byte parts, as
  `digibyteordinals` does (see *Inscribing ords from your wallet*).

## What DigiByte 9 added

DigiByte Core 9 activated two soft forks at block 23,869,440: **DigiDollar** and **AlgoLock**. The
arcade reads DigiByte blocks from block 24,462,000, after both. It pays and reads ordinary outputs
only and does not make or read DigiDollar transactions.
