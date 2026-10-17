# Litecoin on this arcade

Litecoin is an **add-on chain**. An operator hosts it by running a Litecoin node and switching it on
in Admin → "Face and chains". This page says what works today, and what to know before you trade or
inscribe on Litecoin.

**Status:** being added. The protocol is proven on a test chain: the arcade's own inscriptions,
Doginals-format inscriptions, and lock-script swaps all work on Litecoin. Litecoin is not yet shown
in the wallet or on the Exchange. That comes with the next update, and this page grows with it.

## What works the same as on Pepecoin

- **The arcade's own tokens, NFTs and inscriptions.** These use the arcade's Class B transactions
  (bare multisig plus OP_RETURN), and Litecoin relays them under the same rules as Pepecoin and
  Dogecoin. Nothing is different about them on Litecoin except the coin and the fee.
- **Fees are Litecoin's own.** Litecoin relays at 1/1000 of Pepecoin's minimum fee. Here a Litecoin
  transaction pays 0.0001 LTC per kB, ten times Litecoin's minimum.
- **Lock-script swaps.** LTC for PEP, and a Litecoin inscription for PEP, use the same lock scripts
  as PEP for DOGE (see *Swaps between chains*).

## Inscriptions on Litecoin, and Taproot

Litecoin has two kinds of inscription, and they are not interchangeable.

- **Made here: the Doginals format.** The inscription's data rides in an ordinary (legacy) input, the
  same way Doginals work on Dogecoin. Its coin is an ordinary address, so this arcade's wallet can
  hold, send and trade it.
- **Made elsewhere: Taproot ordinals.** Most Litecoin ordinals tools write the data into a Taproot
  witness and keep the coin on a Taproot address (`ltc1p…`).

What that means for you:

- **Litecoin's Taproot ordinals indexers and marketplaces do not show Doginals-format
  inscriptions.** An inscription made here is seen by this arcade and by Doginals-style indexers
  pointed at Litecoin, but not by Taproot-only sites.
- **This arcade cannot yet move inscriptions on Taproot (`ltc1p…`) addresses.** Use a Taproot
  wallet for those. Reading them here is planned; moving them may come later.
- **Nothing is converted.** Content already on the chain never changes. An inscription made here is
  never presented as the same item as one made elsewhere.

## Exchanging across chains

A swap between Litecoin and Pepecoin (or Dogecoin) is two lock-script trades, one on each chain,
tied together by one secret:

1. Each side locks their coins (or their inscription) in a script on their own chain.
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
- **Inscriptions travel by position.** An inscription sits on its coin's first litoshi. Send or lock
  the exact coin it sits on, and nothing else in front of it, or it can move to the wrong place.
  This arcade does that for you; another wallet might not.
- **Never send to an MWEB address (`ltcmweb1…`).** The arcade cannot see or spend Litecoin's
  private extension blocks.
- **Lock-script swaps are not the same as a node-facilitated swap.** A lock-script swap is
  trustless: no node ever holds your coins. Some node-facilitated trades, like the mainnet PEP
  swap, ask you to trust the operator, and the page always says which kind you are using.
