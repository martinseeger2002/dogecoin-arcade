# Litecoin on this arcade

Litecoin is an **add-on chain**, and so is Litecoin testnet. An operator turns either on in
**Admin → Chains**: run it here, with a Litecoin node on this machine, or borrow it from an arcade that
shares its Litecoin Core over the mesh (see *Shared Cores*), with no Litecoin node at all. This page
says what works, and what to know before you trade on Litecoin.

**Status:** Litecoin carries coins only: no tokens, NFTs, inscriptions or other arcade transactions
(see below). Lock-script coin swaps work. Litecoin is in the wallet (one key for PEP,
DOGE and LTC) and on the Exchange (PEP/LTC and DOGE/LTC).

## What works the same as on Pepecoin

- **Fees are Litecoin's own.** Litecoin relays at 1/1000 of Pepecoin's minimum fee. Here a Litecoin
  transaction pays 0.0001 LTC per kB, ten times Litecoin's minimum.
- **Lock-script swaps.** LTC for PEP uses the same lock scripts
  as PEP for DOGE (see *Swaps between chains*).

## Address types

On **Litecoin testnet** the Wallet's drop-down also offers a native SegWit (`tltc1…`) and a nested
SegWit (P2SH) wallet beside the legacy one, each its own entry, and every kind can pay every other. On
Litecoin mainnet SegWit wallets are not offered yet: the node refuses to register one on a real-coin
chain.

## No inscriptions or tokens on Litecoin

This arcade keeps no ord index on Litecoin or Litecoin testnet, as on Kotia (2026-11-05). It reads no
inscriptions, LTC-20 tokens or Omni there, and none of its own transactions either: no arcade tokens,
NFTs, names or posts (`Params.class_b`), and the Create, Tokens and Exchange pages do not switch to
Litecoin. Nothing is checked when you send: coins go back and forth as in any plain wallet. It holds no Litecoin coin
back as "held in inscriptions":
every coin in a Litecoin wallet is spendable. An inscription someone made elsewhere on a coin you hold
is not seen here, so keep such coins in a wallet that knows them.

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
