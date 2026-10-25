# Kotia on this arcade

Kotia (KOT) is an **add-on chain**, like Dogecoin and Litecoin. An operator turns it on in
**Admin → Chains**. It can run here, with a Kotia node on this machine, or be borrowed from an
arcade that shares its Kotia Core over the mesh (see *Shared Cores*). It is shown grayed out
until its node has synced.

**Status:**
- **In the wallet,** on the same key as PEP, DOGE and LTC: receive and send KOT.
- **Swaps, trades, listings, bids, sweeps and inscription sends are refused on Kotia for now.** They
  do not write Kotia's transaction time yet (below), and the arcade says so instead of building
  something Kotia's Core cannot read.

## Running the node

The arcade expects **Kotia 1.0.0**, from `github.com/eleonoraandrea/kotiacash`. It is a
proof-of-stake coin on Bitcoin 0.14's code. Proof of work ended at block 1,000,000. Its config lives
at `~/.kotia/kotia.conf`:

```
server=1
txindex=1
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcport=46173
port=46174
```

- **`txindex=1` is needed.** Kotia's Core reads a block only as raw bytes or with its transaction
  ids, so the arcade fetches each of its transactions on its own.
- **Its Core is older than the others.** It takes `getblock` with `true` or `false` rather than a
  verbosity number, and it does not report whether it is still downloading blocks. The arcade notices
  both by itself: it asks for blocks the way Kotia takes them, and counts the chain as synced by its
  verification progress.

## What is different about Kotia

- **Every transaction carries a time.** It sits right after the version, and it is part of the
  transaction's id and of every signature. The arcade writes the current time. Kotia's Core refuses
  a transaction more than 15 seconds ahead of its own clock, so keep this machine's clock right.
- **Blocks are staked.** Each block ends with the staker's signature. The arcade reads it but does
  not check proof of stake; it proves each block by its hash, merkle root and the block before it.

## Addresses

- **Ordinary addresses and script (P2SH) addresses both start with `K`.** Their version bytes are
  46 and 45, which no other chain here uses. A Luckycoin `K…` (version 47) is a different chain,
  and the arcade refuses it in a Kotia send.
- **Kotia has no SegWit.**

## Keys and fees

- **Keys:** coin type 3, the same key as PEP, DOGE and LTC. The **Backup** page hands out your
  Kotia private key (WIF) for importing into Kotia's own wallet.
- **Fees:** 0.0001 KOT per kB, and no soft-dust fee.
- **Change under 0.001 KOT is left to the staker.** Kotia's Core refuses a transaction with more
  than 10 outputs under 0.001 KOT, so the arcade never makes one that small.

## Data

Kotia relays data outputs (OP_RETURN) of up to 15,000 bytes, several per transaction, as long as no
more than half of a transaction's outputs carry data.

## The whole unspent set (Kotia only)

Kotia's Core has neither `scantxoutset` nor `importmulti`. That is how the arcade normally finds coins an address held
before it was watched. Instead, a node that runs Kotia here keeps **Kotia's whole unspent set**, built once from block 0,
in `<net>-utxoset.sqlite`:

- **Building** reads all of Kotia's 1.5 million blocks through its Core, at a pace that keeps the pages answering
  (about half an hour). Admin → Chains shows how far it has got. After that it follows the chain block by block.
- **The Wallet's Kotia coins and history come from it**, not from the arcade's index, so there is one source and no
  floor in between. Every pass the arcade makes the Wallet's rows what the set says.
- **Every account there when the set was built** gets its history from block 0, because building reads every block
  anyway. **An account added later** sees its coins at once, whatever their age, but its history only from when it
  was added. Nothing is read again for it, and nobody can set off such a read.
- History from before the arcade's own index has no time, so the Wallet files it under *Earlier* and gives its block.
- **What is kept:** each address's unspent coins (49 bytes a coin), the history of the arcade's own accounts, and, for
  every block that carries data (OP_RETURN), the ids of those transactions. Kotia's set is about 79,000 coins, a 5 MB
  file.
- **Only with Kotia's Core run here.** Building needs the Core on this machine: through its RPC, or straight from its
  block files, which only its own machine has. An arcade that **borrows** Kotia builds no set. It is **lent the
  sharer's set** instead, once a minute, through the same shared Core: the coins of its accounts, and their history
  from when the sharer began keeping it for them (the sharer keeps it from then on). That is the sharer's word, like
  everything else a borrowed Core says that the blocks cannot prove. If the sharer lends no set (an older arcade), the
  arcade's own index follows the coins as on any other chain. Admin → Chains says which.
- **Staking rewards** are paid to a public key, not an address. They count in the set, but the Wallet does not spend
  them, because it signs only ordinary address coins.
- **Kotia only for now.** Another chain would need adapting first: its size, how its headers are named, its
  block-file magic, and what its transactions carry.
