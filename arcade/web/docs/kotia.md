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
