# Bellscoin on this arcade

Bellscoin (BEL) is an **add-on chain**, like Dogecoin and Litecoin. An operator turns it on in
**Admin → Chains**. It can run here, with a Bells node on this machine, or be borrowed from an
arcade that shares its Bells Core over the mesh (see *Shared Cores*). It is shown grayed out until
its node has synced, and nothing is paid, held or signed on it before then.

**Status:**
- **In the wallet,** on the same key as PEP, DOGE and LTC.
- **Bellinals are indexed and shown,** and you can inscribe one from **Creator → Ords**.
- **Swaps between chains and Bellinal sends from the arcade are not open yet.**

## Running the node

The arcade expects **Bells Core 3.0.0** or newer. It is Bitcoin-Core-based, from
`github.com/BellsChain/bellscoin`. Its config lives at `~/.bells/bells.conf`:

```
server=1
txindex=1
prune=0
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcport=19918
port=19919
```

- **Keep `rpcport=19918`.** The build's own default is still Bitcoin's 8332, and the arcade looks on 19918.
- **`txindex=1` is needed** for the inscription index and the wallet's history.
- **RPC stays on 127.0.0.1.** Only the peer port (19919) needs to be reachable.

## Addresses: one thing to know

- **Ordinary Bellscoin addresses start with `B`.** Those are what the arcade gives you and pays.
- **A Bellscoin script (P2SH) address starts with `D`,** exactly like an ordinary *Dogecoin*
  address, with the same version byte. The arcade cannot tell the two apart from the text, so on
  Bellscoin it **refuses a `D…` payee** rather than risk paying it the wrong way. For the same reason
  it makes no swap lock, escrow or prize pool on Bellscoin yet. Ask for a `B…` address. A swap
  order with a Bellscoin side is refused when it is listed (2026-10-27; before, it was listed and failed
  only once someone took it). See *Exchange safety*.
- **`bel1…` (SegWit) and Taproot addresses are not paid yet.** The page says so if you paste one.

## Keys

Your arcade words recover your Bellscoin coins here. They use coin type 3, the same key as PEP,
DOGE and LTC. Other Bells wallets derive on a different path, so the same words show a different
address there. The **Backup** page hands out your Bellscoin private key (WIF) for importing
into Bells Core or another wallet.

## Fees

- **Bells relays like Litecoin, not like Dogecoin.** A transaction here pays 0.0001 BEL per kB,
  ten times Bells' minimum relay fee.
- **There is no soft-dust fee.**
- **Change under 0.0001 BEL goes to the miner** rather than becoming a coin of its own.

## Bellinals

Bellinals are Bellscoin's ord inscriptions. Bells has both kinds of envelope: the Doginals kind in
an ordinary input, and ord's Taproot witness kind. The arcade reads both.

- **The index:**
  - Admin → Ords → Bellscoin, set to *walk*, *help*, *read* or *off*. It is off until the operator turns it on.
  - It starts at block **22,470**, the first-inscription height of the official Bells indexer.
- **Numbering follows the official indexer, nintondo's ord,** including **reinscriptions**: an
  inscription made on a coin that already carries one gets its own number. Each one is **marked as a
  reinscription** (`reinscription: true` in a lookup) and counted in the index's status. That is so
  a page can show or hide them.
- **It is a big index.** Bells' early blocks are full of inscriptions; block 30,000 alone holds 779,
  and the chain passed a hundred million by 2024. Expect it to take many hours and a lot of disk.
- **In the wallet:**
  - The NFTs tab shows your Bellinals, for viewing only for now.
  - Sending, listing and swapping a Bellinal from the arcade come later.
  - An ordinary BEL send never spends a coin that carries one.
- **Inscribing:** **Creator → Ords** inscribes a Bellinal from your own wallet, by the chain's own
  rules (see *Inscribing ords from your wallet*).
