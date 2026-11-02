# Wojakcoin on this arcade

Wojakcoin (WJK) is an **add-on chain**, like Dogecoin and Litecoin. An operator turns it on in
**Admin → Chains**. It can run here, with a Wojakcoin node on this machine, or be borrowed from an
arcade that shares its Wojak Core over the mesh (see *Shared Cores*). It is shown grayed out until
its node has synced.

**Status:**
- **In the wallet,** on the same key as PEP, DOGE and LTC.
- **Wojakinals are indexed and shown,** and you can inscribe one from **Creator → Ords**.
- **Wojakinals can be sent, listed and bought** like Doginals.
- **Swaps between chains and escrows are closed.** Wojakcoin never enforces lock
  times: its chainparams set BIP65 (CHECKLOCKTIMEVERIFY), BIP66 and CSV to never activate. Every swap lock and escrow
  this arcade makes refunds through CHECKLOCKTIMEVERIFY, so on Wojakcoin whoever locked could take the coins back at
  any moment, even after the other side paid. The arcade refuses such a lock, in your browser and on every node. Ordinary
  sends are fine.

## Running the node

The arcade expects **WojakCoin 1.21.3** or newer (`wojakcoind`). It is Bitcoin Core's code, mined
with SHA-256, not merged-mined. Its config lives at `~/.wojakcoin/wojakcoin.conf`:

```
server=1
txindex=1
prune=0
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcport=20760
port=20759
```

- **`txindex=1` is needed** for the inscription index and the wallet's history.
- **RPC stays on 127.0.0.1.** Only the peer port (20759) needs to be reachable.
- **Its sync figure never reaches 100%.** Wojak Core's built-in estimate of the chain's traffic is a
  placeholder, so its verification progress sits near 0.9 even at the tip. The arcade counts the chain
  as synced once its blocks have caught up with its headers.
- **The node's Core wallet address.** Wojak Core has no accounts, so the arcade keeps one legacy `W…`
  address under a label in its wallet and uses that for the node's own coins.

## Addresses

- **Ordinary Wojakcoin addresses start with `W`.** Their version byte is 73, which no other chain
  here uses.
- **Script (P2SH) addresses start with `3`,** the same byte as Luckycoin's script addresses and
  Litecoin's older `3…` script addresses. That byte is not any chain's ordinary address, so a
  Wojakcoin send can pay one.
- **Wojakcoin has no SegWit.** The `wj1…` prefix is in the node's source, and SegWit and Taproot
  are compiled in, but neither is ever activated. The arcade does not pay a `wj1…` address.

## Keys and fees

- **Keys:** coin type 3, the same key as PEP, DOGE and LTC. The **Backup** page hands out your
  Wojakcoin private key (WIF, version 201) for importing into Wojak Core.
- **Fees:** 0.0001 WJK per kB, ten times the node's 0.00001 WJK/kB minimum relay fee, and no
  soft-dust fee. Change under 0.0001 WJK is left to the miner.

## Wojakinals

Wojakinals are Wojakcoin's ord inscriptions, in the Doginals format: the data rides in an ordinary
input, the way `wojakinals` writes it. SegWit is not active, so there is no Taproot envelope to read.

- **The index:**
  - Admin → Ords → Wojakcoin, set to *Keep the full index* (walk), *Help scan* (help), *Watch only* (read) or *Off* (off).
    It is Watch only until the operator picks another.
  - It starts at block **1**, which is `ord-wojakcoin`'s `first_inscription_height`.
- **Numbering follows wonky-ord, as on Litecoin:** a new inscription is looked for only when no
  inscription already sits on the coin it is made on.
- **In the wallet:**
  - The NFTs tab shows your Wojakinals, and they can be sent, listed and bought like Doginals.
  - Swapping one is closed, as on the rest of Wojakcoin.
  - An ordinary WJK send never spends a coin that carries one.
- **Inscribing:** **Creator → Ords** inscribes a Wojakinal from your own wallet, by the chain's own
  rules (see *Inscribing ords from your wallet*).
