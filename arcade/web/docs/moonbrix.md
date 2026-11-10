# MoonBrix 2

MoonBrix is tbuuol's protocol on Kotia. It keeps everything in OP_RETURN outputs:

- **TLDs**, with a white- or blacklist that decides who may claim a space in them.
- **Spaces** inside a TLD: four bytes, written like an IP (`19.55.66.132.moonbrix`).
- **Inscriptions**: files of any type, up to about 90 kB in one transaction, or put together from parts.
- **Folders and files** in a space, each file pointing at an inscription.
- **Properties**: fungible tokens of fixed or managed supply, and NFTs.
- **A DEX**: tokens and NFTs offered for KOT and bought in one transaction.

This arcade reads it on **`/moonbrix`** (`arcade/moonbrix.py`). This page is the protocol: the arcade does exactly
what is written here and nothing else.

**MoonBrix 2 replaces the prototype (2026-11-10).** The prototype (`github.com/tbuuol/MoonBrix`, the C++ Kurator)
read one command a block, took a fixed output as the target, dropped letters it did not like from names, and let some
things through that it was meant to stop. Its commands on Kotia were tests. MoonBrix 2 starts from the same block
(1,236,675) and does not read them: none of them starts with MoonBrix 2's magic bytes.

## Which transaction is a command

1. **The data.** A transaction's first OP_RETURN output must start with the bytes `4d 42 02` (`MB`, then version 2).
   An OP_RETURN output counts when it is `OP_RETURN` followed by data pushes only. That output's data and the data of
   every OP_RETURN output after it, joined in output order, are the **payload**.
   - If any of those outputs holds something other than a data push (`OP_1` to `OP_16`, `OP_1NEGATE`), the command is
     rejected.
   - Another version byte is not this protocol's yet and is passed over, not rejected.
2. **Every command counts.** A block's commands are read in the order its transactions stand in the block. One
   transaction is one command.
3. **The sender** is the address of the coin the first input spends. That is the key that signed the command. A
   first input whose coin pays no address makes the command rejected.
4. **The target**, where a command needs one, is the **first output that pays an address other than the sender's**.
   The sender's own change outputs are passed over wherever they stand. A command that needs a target and has none is
   rejected.
5. **A rejected command changes nothing.** It is written into the event log with the reason. Nothing makes the reader
   stop: a command is either applied whole or rejected whole.

### Sizes on Kotia

Kotia's Core relays an OP_RETURN output of at most 15,000 bytes of script: 14,996 bytes of data in one push. It
relays a transaction of under 100,000 bytes, with **at most half of its outputs** being OP_RETURNs. A payload of about
89 kB therefore goes into six OP_RETURN outputs and needs six outputs that pay an address (change, split into six, is
enough). `moonbrix.data_scripts(payload)` cuts a payload into those outputs.

## The payload

```
4d 42 02   op   fields...
```

| Field | Bytes |
|---|---|
| `u8` | one byte |
| `u32` | four bytes, big-endian (a space) |
| `var` | an unsigned LEB128 number below 2^64, in its shortest form |
| `str` | a `var` length, then that many bytes of UTF-8 |
| `id` | 32 bytes: a transaction id, in the order it is printed |
| `ids` | a `var` count (1 to 10,000), then that many `id`s |
| `opt` | `00`, or `01` followed by an `id` |
| `rest` | every byte left, at least one |

Every field must be there and well formed, and nothing may follow the last field. Otherwise the command is rejected.

### Names

| Name | Rule |
|---|---|
| TLD | 1 to 16 of `a-z`, `0-9` and `-`, not starting or ending with `-` |
| Ticker | 3 to 8 of `A-Z` and `0-9`, starting with a letter |
| Text: a space's name, a property's or an inscription's name | valid UTF-8 with no control characters (U+0000 to U+001F, U+007F to U+009F), at most 32 bytes (an inscription's name 64) |
| Path | 1 to 255 bytes, names joined by `/`. Each name is text of 1 to 64 bytes, without `/` and not `.` or `..` |
| Type | 3 to 100 bytes of printable ASCII (space to `~`) with a `/` in it, such as `image/png` |

A name is never changed to make it fit: a name that breaks the rule rejects the command.

## Commands

`→` means the command needs a target. `→?` means a target is used when there is one, and the sender is used when there
is none.

### TLDs

| Op | Command | Fields | Rule |
|---|---|---|---|
| `01` | claim TLD | `str` tld, `u8` mode | The TLD is free. Mode `00` whitelist, `01` blacklist. The sender owns it. |
| `02` | give TLD | `str` tld → | The sender owns the TLD. The target owns it now. |
| `03` | set list mode | `str` tld, `u8` mode | The sender owns the TLD. |
| `04` | list add | `str` tld → | The sender owns the TLD. The target is not on the list yet. |
| `05` | list remove | `str` tld → | The sender owns the TLD. The target is on the list. |
| `06` | list clear | `str` tld | The sender owns the TLD. |

### Spaces

| Op | Command | Fields | Rule |
|---|---|---|---|
| `10` | claim space | `str` tld, `u32` space | The TLD exists and the space is free. The TLD's owner may always claim. Otherwise, under a whitelist only an address on the list may, under a blacklist every address that is not on it. The sender owns the space, with an empty root folder. |
| `11` | give space | `str` tld, `u32` space → | The sender owns the space. Its folders and files go with it. |
| `12` | name space | `str` tld, `u32` space, `str` name | The sender owns the space. An empty name clears it. |

### Inscriptions

| Op | Command | Fields | Rule |
|---|---|---|---|
| `20` | inscribe | `str` type, `str` name, `rest` data | The file is the data. Its id is this transaction's id. |
| `21` | part | `rest` data | Data for a later `22`. A part on its own is no file. |
| `22` | assemble | `str` type, `str` name, `var` size, `id` sha256, `ids` parts | Every part is a `21` confirmed before this command: in an earlier block, or earlier in the same block. A part may be named more than once. The parts' data joined in order is `size` bytes long and its SHA-256 is `sha256`. The file's id is this transaction's id. |

An inscription cannot be changed or taken over. The sender is written down as the one who inscribed it.

### Folders and files

Every command here names a space (`str` tld, `u32` space) first, and its sender must own that space. The root folder
is the empty path, and it is always there.

| Op | Command | Fields | Rule |
|---|---|---|---|
| `30` | make folder | path | The parent folder exists, and nothing is at the path yet. |
| `31` | put file | path, `id` inscription | The parent folder exists, nothing is at the path yet, and the inscription exists. |
| `32` | replace file | path, `id` inscription | A file is at the path, and the inscription exists. |
| `33` | remove | path | A file, or an empty folder, is at the path. |
| `34` | move | `str` from, `str` to | Something is at `from` and nothing at `to`. The folder `to` goes in exists, and it is not `from` or inside it. A rename is a move. |

Nothing is overwritten except by `32`.

### Properties

| Op | Command | Fields | Rule |
|---|---|---|---|
| `40` | create property | `str` tld, `u32` space, `str` ticker, `str` name, `u8` kind, `u8` decimals, `var` supply | The sender owns the space, and the ticker is free (tickers are unique across MoonBrix). Kind `00` is **fixed**: the supply is 1 or more, and all of it goes to the sender. Kind `01` is **managed**: the supply is 0, and the owner mints. Kind `02` is an **NFT**: the supply and decimals are 0, and the owner mints items. Decimals are 0 to 8. |
| `41` | give property | `str` ticker → | The sender owns the property. The target owns it now, and with it the right to mint. |
| `42` | lock property | `str` ticker | The sender owns a managed property or an NFT. Nothing more is ever minted of it. |
| `43` | mint | `str` ticker, `var` amount →? | Only for managed properties: the sender owns it, it is not locked, the amount is 1 or more, and the supply stays below 2^64. |
| `44` | send | `str` ticker, `var` amount → | A fungible property: the sender holds at least the amount, which is 1 or more. |
| `45` | burn | `str` ticker, `var` amount | A fungible property: the sender holds at least the amount, which is 1 or more. The supply goes down by it. |
| `46` | mint NFT | `str` ticker, `opt` inscription →? | The sender owns the NFT property and it is not locked. The item gets the next number, starting at 1. An inscription named here must exist, and it becomes the item's picture or data for good. |
| `47` | send NFT | `str` ticker, `var` item → | The sender holds the item. |

Amounts are in the smallest unit: with 8 decimals, `100000000` is one token. **A fixed property is never minted.**
**An NFT is never burnt.**

### DEX

| Op | Command | Fields | Rule |
|---|---|---|---|
| `50` | offer | `str` ticker, `var` amount or item, `var` price | The sender holds them. For an NFT the number is the item. The amount or item leaves the sender and is held by the offer. The price is in koinu, at least 1, for the whole offer. The offer's id is this transaction's id. |
| `51` | cancel | `id` offer | The sender made the offer. What it held goes back. |
| `52` | buy | `id` offer | The offer is still open, and the sender did not make it. This same transaction pays the seller's address at least the price, all outputs to that address added up. The buyer (the sender) gets what the offer held. |

**A buy and its payment are one transaction, so nobody can take the tokens without paying or the KOT without
delivering.** What the protocol cannot do is give KOT back. When two buys of one offer reach the chain, the first in
block order gets it. The second is rejected, and its payment to the seller stays paid. A wallet must check that the
offer is still open, and that no buy of it is waiting in the mempool, before it sends a buy. An offer is all or
nothing: a seller who wants to sell in pieces makes several offers.

## What the arcade keeps

`<net>-moonbrix.sqlite` holds the state as one JSON document: `tlds`, `spaces`, `inscriptions`, `parts`,
`properties`, `offers` and `log`. It also keeps the document as it was before each of the last 200 blocks that changed
it, so a reorg is rolled back. The arcade's unspent set (`data_blocks`) says which blocks carry an OP_RETURN, and only
those are read.

When a part's data cannot be fetched from the Core, that is the node's problem and not the command's. The reader then
stops at that block and tries again on the next pass. It never rejects a command for it.

## Still open

- **Claiming a TLD costs only the fee.** Somebody can claim many names. A price (a burn, or a payment to a fixed
  address) would be a change of the protocol, from a block on.
- **Bids.** A buy order that holds KOT until somebody sells cannot be done with OP_RETURN alone. KOT would have to be
  locked in a script.
