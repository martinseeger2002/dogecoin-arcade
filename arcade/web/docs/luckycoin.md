# Luckycoin on this arcade

Luckycoin (LKY) is an **add-on chain**, like Dogecoin and Litecoin. An operator turns it on in
**Admin → Chains**. It can run here, with a Luckycoin node on this machine, or be borrowed from an
arcade that shares its Luckycoin Core over the mesh (see *Shared Cores*). It is shown grayed out
until its node has synced.

**Status:**
- **In the wallet,** on the same key as PEP, DOGE and LTC.
- **Luckinals are indexed and shown.**
- **Swaps between chains and Luckinal sends from the arcade are not open yet.**

## Running the node

The arcade expects **Luckycoin Core 5.0.1** or newer, from `github.com/LuckycoinFoundation/Luckycoin`.
5.0.1 is a **mandatory** update: it no longer talks to 4.0.x nodes. It is Dogecoin 1.14's code, with
one `wallet.dat`. Its config lives at `~/.luckycoin/luckycoin.conf`:

```
server=1
txindex=1
prune=0
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcport=9918
port=9917
```

## Addresses

- **Ordinary Luckycoin addresses start with `K` or `L`.** A Luckycoin `L…` is not a Litecoin `L…`:
  the version bytes differ (47 against 48). The arcade checks them, so a Litecoin address pasted into
  a Luckycoin send is refused, and the other way round.
- **Script (P2SH) addresses start with `3`,** the same byte as Litecoin's older `3…` script addresses.
- **Luckycoin has no SegWit.**

## Keys and fees

- **Keys:** coin type 3, the same key as PEP, DOGE and LTC. The **Backup** page hands out your
  Luckycoin private key (WIF) for importing into Luckycoin Core.
- **Fees are Dogecoin's:** 0.01 LKY per kB, and the 0.01 LKY soft-dust line. An output under it pays
  that fee again.

## Luckinals

Luckinals (also called luckyscriptions) are Luckycoin's ord inscriptions, in the Doginals format:
the data rides in an ordinary input.

- **The index:**
  - Admin → Ords → Luckycoin, set to *walk*, *help*, *read* or *off*. It is off until the operator turns it on.
  - It starts at block **88,554**, the first Doginals-style envelope on the chain (August 2024).
    No official indexer height was found, so the whole chain was scanned for it.
- **Numbering follows wonky-ord, as on Litecoin:** a new inscription is looked for only when no
  inscription already sits on the coin it is made on. Reinscriptions are still **marked** wherever
  they are numbered.
- **In the wallet:**
  - The NFTs tab shows your Luckinals, for viewing only for now.
  - Sending, listing and swapping one come later.
  - An ordinary LKY send never spends a coin that carries one.
