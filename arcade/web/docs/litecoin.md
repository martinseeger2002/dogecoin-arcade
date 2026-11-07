# Litecoin on this arcade

Litecoin is an **add-on chain**, and so is Litecoin testnet. An operator turns either on in
**Admin → Chains**: run it here, with a Litecoin node on this machine, or borrow it from an arcade that
shares its Litecoin Core over the mesh (see *Shared Cores*), with no Litecoin node at all. This page
says what works, and what to know before you trade on Litecoin.

**Status:** Litecoin carries coins and Litecoin ordinals (see below). It carries none of this arcade's own
transactions: no arcade tokens, NFTs, names or posts. Lock-script coin swaps work. Litecoin is in the wallet (one
key for PEP, DOGE and LTC) and on the Exchange (PEP/LTC and DOGE/LTC). Litecoin testnet carries coins only.

## What works the same as on Pepecoin

- **Fees are Litecoin's own.** Litecoin relays at 1/1000 of Pepecoin's minimum fee. Here a Litecoin
  transaction pays 0.0001 LTC per kB, ten times Litecoin's minimum.
- **Lock-script swaps.** LTC for PEP uses the same lock scripts
  as PEP for DOGE (see *Swaps between chains*).

## Address types

The Wallet's address type offers **Legacy** (`L…`, the default), **P2SH (Nested SegWit)** (`M…`) and
**Native SegWit** (`ltc1q…`) on Litecoin and Litecoin testnet (2026-11-07). Each is its own address from the
same twelve words (paths m/44', m/49' and m/84'), and every kind can pay every other. A send pays any of
them, and an old `3…` script address too. MWEB (`ltcmweb1…`) is a separate, private layer this arcade does
not pay.

## Litecoin ordinals

Litecoin ordinals are back on Litecoin mainnet (2026-11-07). They had been switched off on 2026-11-05, with
LTC-20, Omni and this arcade's own transactions; only the ordinals came back. Litecoin testnet has none.

Litecoin has two kinds of inscription, and they are not interchangeable.

- **Made elsewhere: Taproot ordinals.** Most Litecoin ordinals tools write the data into a Taproot
  witness and keep the coin on a Taproot address (`ltc1p…`).
- **Made here: the Doginals format.** The inscription's data rides in an ordinary (legacy) input, the
  same way Doginals work on Dogecoin. Its coin is an ordinary address, so the wallet can hold, send and
  trade it.

What that means for you:

- **The arcade's ord index for Litecoin reads both kinds.** It starts at block 2,424,429, as
  ord-litecoin does, and reads Taproot envelopes as well as the Doginals kind. Pick how this arcade
  gets it in **Admin → Ords**: walk it here, or read it from an arcade that shares one.
- **Coins carrying an inscription are held,** as on every chain with ords: the Wallet says so beside
  the balance and never spends one by accident. Until an arcade has the index up to date, a small
  coin waits as "unknown" rather than risk spending an inscription.
- **Litecoin's Taproot ordinals indexers and marketplaces do not show Doginals-format
  inscriptions.** An inscription made here is seen by this arcade and by Doginals-style indexers
  pointed at Litecoin, but not by Taproot-only sites.
- **Taproot inscriptions show here.** `/content/<id>` reads a Taproot inscription's content from the
  reveal transaction's witness.
- **Nothing is converted.** Content already on the chain never changes.

## Exchanging across chains

A swap between Litecoin and Pepecoin (or Dogecoin) is two lock-script trades, one on each chain,
tied together by one secret:

1. Each side locks their coins in a script on their own chain.
2. One side claims the other side's lock, and that claim publishes the secret.
3. The other side uses that secret to claim theirs.

If nobody finishes, each lock **refunds its owner** after its time runs out.

Before you trade:

- **You pay a network fee on each chain you lock on.** A swap costs a little LTC on the Litecoin side
  and a little PEP on the Pepecoin side.
- **Refunds take time.** A lock refunds only after its time has passed, and Litecoin measures that
  time by its blocks. They come every 2½ minutes instead of Pepecoin's 1, so a Litecoin refund can
  land up to about 15 minutes after the clock time shown.
- **Each chain confirms at its own pace.** A trade is done when both claims are in blocks on both
  chains, not when the first page says "sold".
- **Never send to an MWEB address (`ltcmweb1…`).** The arcade cannot see or spend Litecoin's
  private extension blocks.
- **Lock-script swaps are not the same as a node-facilitated swap.** A lock-script swap is
  trustless: no node ever holds your coins. Some node-facilitated trades, like the mainnet PEP
  swap, ask you to trust the operator, and the page always says which kind you are using.
