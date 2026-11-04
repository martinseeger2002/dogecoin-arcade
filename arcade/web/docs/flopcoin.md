# Flopcoin on this arcade

Flopcoin (FLOP) is an **add-on chain**, like Dogecoin and Litecoin. An operator turns it on in
**Admin → Chains**. It can run here, with a Flopcoin node on this machine, or be borrowed from an
arcade that shares its Flopcoin Core over the mesh (see *Shared Cores*). It is shown grayed out
until its node has synced.

**Status:**
- **In the wallet,** on the same key as PEP, DOGE and LTC.
- **Flopinals,** Flopcoin's ords, are indexed, shown, sent, listed, bought and swapped like Doginals,
  and inscribed from **Creator → Ords** (see *Flopinals* below).
- **Swaps between chains** follow the same setting as Dogecoin's. Flopcoin enforces
  CHECKLOCKTIMEVERIFY (since block 111,000), so a lock can be made there.

## Running the node

The arcade expects **Flopcoin Core v2.0.0.0** or newer, from `github.com/Flopcoin/Flopcoin`.
**Admin → Coins & chains → Install a node** downloads it and writes the config for you; it is only
offered on x86_64 Linux, the one Linux build the project publishes.

v2.0.0.0 is a **mandatory** update. It hard-forked at block 125,000 and changed the network's
message-start bytes, so older nodes are not peers. It is Dogecoin 1.14's code, with one
`wallet.dat`. Its config lives at `~/.flopcoin/flopcoin.conf`:

```
server=1
txindex=1
prune=0
listen=1
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcport=32551
port=32552
```

**Peers are scarce.** On 2026-11-03, only one of the addresses its DNS seeds (`seed.flopcoin.net`,
`seed2.flopcoin.net`) handed out answered: `216.45.61.204`. The release has no fixed seeds to fall
back on. If a new node finds no peers, add one by hand:

```
addnode=216.45.61.204:32552
```

## Addresses

- **Ordinary Flopcoin addresses start with `F`** (version 35, no other chain's).
- **Script (P2SH) addresses use version 22,** the same byte as Dogecoin's and Pepecoin's script
  addresses. No chain here uses 22 for an ordinary address, so a Flopcoin script address is always
  paid as a script.
- **Flopcoin has no SegWit.**

## Keys and fees

- **Keys:** coin type 3, the same key as PEP, DOGE and LTC. The **Backup** page hands out your
  Flopcoin private key (WIF). Flopcoin's WIF byte is Dogecoin's (0x9E), so it is the same text as
  your Pepecoin mainnet and Dogecoin key.
- **Fees are Dogecoin's:** 0.01 FLOP per kB, and the 0.01 FLOP soft-dust line. An output under it
  pays that fee again.
- **Mining:** since block 125,000, every block's coinbase pays 10% to the FlopFund, until block
  600,000. That only touches miners: no send or lock made here is changed by it.

## Flopinals

Flopinals are Flopcoin's ord inscriptions, in the Doginals format: the data rides in an ordinary
input, in 240-byte parts. Flopcoin is Dogecoin 1.14's code and has no SegWit, so there is no Taproot
envelope to read.

- **The index:**
  - Admin → Ords → Flopcoin, set to *Keep the full index* (walk), *Help scan* (help), *Watch only* (read) or *Off* (off).
  - It starts at block **18,295**, the first Doginals-style inscription found by scanning the whole chain
    from block 0 (Flopcoin has no official ord indexer). It is a long walk, like the other chains': the
    arcade's own coins and sends start at block 914,000.
- **Numbering follows wonky-ord, as on DigiByte:** a new inscription is looked for only when no
  inscription already sits on the coin it is made on (a reinscription is not numbered).
- **In the wallet:** your Flopinals are shown on the NFTs tab, and they can be sent, listed, bought and
  swapped like Doginals. An ordinary FLOP send never spends a coin that carries one.
- **Inscribing:** **Creator → Ords** inscribes a Flopinal from your own wallet, by the chain's own
  rules (see *Inscribing ords from your wallet*). Fees are Dogecoin's, 0.01 FLOP a kB plus the
  soft-dust fee.
