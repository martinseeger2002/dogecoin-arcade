# MoonBrix 2

MoonBrix is tbuuol's protocol on Kotia. It keeps everything in OP_RETURN outputs:

- **TLDs**, with a white- or blacklist that decides who may claim a space in them.
- **Spaces** inside a TLD: four bytes, written like an IP (`19.55.66.132.moonbrix`).
- **Inscriptions**: files of any type, up to about 90 kB in one transaction, or put together from parts.
- **Folders and files** in a space, each file pointing at an inscription.
- **Properties**: fungible tokens of fixed or managed supply, and NFTs.
- **A DEX**: tokens and NFTs offered for KOT. A buyer reserves first and then pays, as on Omni's DEX.

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
| `ref` | a pointer: a `str` chain, then an `id`. The chain is the target chain's ticker (`kot`, `ltc`, `doge`, `btc` ...), or empty for this chain |
| `refs` | a `var` count (1 to 10,000), then that many `ref`s |
| `data` | `00` empty, `01` followed by a `str` text, or `02` followed by a `ref` |
| `rest` | every byte left, at least one |

Every field must be there and well formed, and nothing may follow the last field. Otherwise the command is rejected.

### Names

| Name | Rule |
|---|---|
| TLD | 1 to 16 of `a-z`, `0-9` and `-`, not starting or ending with `-` |
| Ticker | 3 to 8 of `A-Z`, letters only, so `G0LD` can never pass for `GOLD` |
| Space name | 1 to 32 of `a-z`, `0-9` and `-`, not starting or ending with `-`. It is unique within its TLD |
| Path | 1 to 255 bytes, names joined by `/`. Each name is 1 to 64 of `a-z`, `0-9`, `.`, `_` and `-`, and not `.` or `..` |
| Text: a property's or an inscription's name | valid UTF-8 with no control characters (U+0000 to U+001F, U+007F to U+009F), at most 32 bytes (an inscription's name 64) |
| Type | 3 to 100 bytes of printable ASCII (space to `~`) with a `/` in it, such as `image/png` |
| A TLD's message | text of at most 80 bytes, may be empty |
| A chain in a `ref` | 2 to 8 of `a-z` and `0-9`, or empty |
| Text in a `data` | text of at most 1,024 bytes. Line breaks and tabs are allowed. More goes into an inscription it points to |

### Pointers to other chains

A `ref` names its chain, so MoonBrix on another chain can point at Kotia's content (`kot:<txid>`), and Kotia's at
another's.
- **Here:** a pointer to this chain (empty, or `kot` on Kotia) must name an inscription this reader has, or the command
  is rejected.
- **Another chain:** the pointer is kept as written and marked unchecked, until a reader that reads both chains checks
  it.
- **Parts:** an assemble's parts must be on this chain for now. Only there are their size and SHA-256 checked.

A name is never changed to make it fit: a name that breaks the rule rejects the command.

**Names that address something are lowercase only:** TLDs, space names, folders and files. So
`rpg.lwq/www/index.html` means one thing, however it is typed, and two names can never differ only in case. A ticker
is uppercase only. The name of a property or an inscription is free text, because it is shown and never looked up.

### Addresses

| Address | Names |
|---|---|
| `lwq` | the TLD, with all its spaces |
| `rpg.lwq` | the space named `rpg` in `lwq` |
| `19.55.66.132.lwq` | the same space, by its number |
| `rpg.lwq/www/index.html` | a folder or file in it |

The arcade's explorer takes these, a ticker, a transaction id or an address. It comes in two forms:
- **`/moonbrix?q=...`** in the arcade's own look.
- **`/moonbrix/explorer`** as a page of its own in tbuuol's Syfy-UI look, Atlantis or OASIS. An address is dialled like
  a gate address: each part locks one chevron, and the gate opens on what it names.

## Commands

`→` means the command needs a target. `→?` means a target is used when there is one, and the sender is used when there
is none.

### TLDs

| Op | Command | Fields | Rule |
|---|---|---|---|
| `01` | claim TLD | `str` tld, `u8` mode, `str` message | The TLD is free. Mode `00` whitelist, `01` blacklist. The sender owns it. The message is kept and never changes. |
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
| `12` | name space | `str` tld, `u32` space, `str` name | The sender owns the space, and no other space of the TLD has the name. An empty name clears it. |

### Inscriptions

| Op | Command | Fields | Rule |
|---|---|---|---|
| `20` | inscribe | `str` type, `str` name, `rest` data | The file is the data. Its id is this transaction's id. |
| `21` | part | `rest` data | Data for a later `22`. A part on its own is no file. |
| `22` | assemble | `str` type, `str` name, `var` size, `id` sha256, `refs` parts | Every part is a `21` confirmed before this command: in an earlier block, or earlier in the same block. A part may be named more than once. The parts' data joined in order is `size` bytes long and its SHA-256 is `sha256`. The file's id is this transaction's id. |

An inscription cannot be changed or taken over. The sender is written down as the one who inscribed it.

### Folders and files

Every command here names a space (`str` tld, `u32` space) first, and its sender must own that space. The root folder
is the empty path, and it is always there.

| Op | Command | Fields | Rule |
|---|---|---|---|
| `30` | make folder | path | The parent folder exists, and nothing is at the path yet. |
| `31` | put file | path, `ref` inscription | The parent folder exists, nothing is at the path yet, and the pointer is good (see *Pointers*). |
| `32` | replace file | path, `ref` inscription | A file is at the path, and the pointer is good. |
| `33` | remove | path | A file, or an empty folder, is at the path. |
| `34` | move | `str` from, `str` to | Something is at `from` and nothing at `to`. The folder `to` goes in exists, and it is not `from` or inside it. A rename is a move. |
| `35` | join | `str` from, `str` into | Both are folders, and `into` is not `from` or inside it. Everything in `from` goes into `into`, and `from` is gone. If a name is in both, the command is rejected. |

Nothing is overwritten except by `32`.

### Properties

| Op | Command | Fields | Rule |
|---|---|---|---|
| `40` | create property | `str` tld, `u32` space, `str` ticker, `str` name, `u8` kind, `u8` decimals, `var` supply, `data` grant | The sender owns the space, and the ticker is free (tickers are unique across MoonBrix). Kind `00` is **fixed**: the supply is 1 or more, and all of it goes to the sender. Kind `01` is **managed**: the supply is 0, and the owner mints. Kind `02` is an **NFT**: the supply and decimals are 0, and the owner mints items. Decimals are 0 to 8. **Grant data** is set here once and never changes. |
| `41` | give property | `str` ticker → | The sender owns the property. The target owns it now, and with it the right to mint. |
| `42` | lock property | `str` ticker | The sender owns a managed property or an NFT. Nothing more is ever minted of it. |
| `43` | mint | `str` ticker, `var` amount →? | Only for managed properties: the sender owns it, it is not locked, the amount is 1 or more, and the supply stays below 2^64. |
| `44` | send | `str` ticker, `var` amount → | A fungible property: the sender holds at least the amount, which is 1 or more. |
| `45` | burn | `str` ticker, `var` amount | A fungible property: the sender holds at least the amount, which is 1 or more. The supply goes down by it. |
| `46` | mint NFT | `str` ticker, `data` grant →? | The sender owns the NFT property and it is not locked. The item gets the next number, starting at 1. Its **grant data** is set here once and never changes. |
| `47` | send NFT | `str` ticker, `var` item → | The sender holds the item. Its data goes with it. |
| `48` | set issuer data | `str` ticker, `var` item, `data` | The sender owns the NFT property. The item's **issuer data** is now this (empty clears it). |
| `49` | set holder data | `str` ticker, `var` item, `data` | The sender holds the item. The item's **holder data** is now this (empty clears it). It stays when the item moves on. |

Amounts are in the smallest unit: with 8 decimals, `100000000` is one token. **A fixed property is never minted.**
**An NFT is never burnt.**

An NFT item has three data fields:

| Field | Set by | When |
|---|---|---|
| Grant data | the property's owner | once, when the item is minted |
| Issuer data | the property's owner | any time |
| Holder data | whoever holds the item | any time. It stays when the item is sent or sold. |

### DEX

The DEX works as Omni's does. A buyer first **reserves** part of an offer, then **pays** for it in a second
transaction. Once a reservation is on the chain, the reserved tokens are the buyer's to pay for until the offer's
window closes. Nobody else can reserve them, and the seller cannot take them back. A buyer only pays for tokens that
are certain to come.

| Op | Command | Fields | Rule |
|---|---|---|---|
| `50` | offer | `str` ticker, `var` amount or item, `var` price, `var` window, `var` deposit | The sender holds the tokens (for an NFT, the number is the item). They leave the sender and are held by the offer. **price**: koinu for the whole offer, at least 1. **window**: the blocks a reservation has to pay in, 1 to 10,000. **deposit**: koinu for the whole offer, 0 up to the price. The offer's id is this transaction's id. |
| `51` | cancel | `id` offer | The sender made the offer, and it is still open. What is not reserved goes back to the seller. What is reserved stays reserved: it is paid for, or it goes back to the seller when its window closes. |
| `52` | reserve | `id` offer, `var` amount | The offer is open, and the sender did not make it. 1 to what is left of it (an NFT: 1). The reservation's id is this transaction's id. This transaction pays the seller's address at least the deposit's share. That payment counts towards the price. If it pays the whole share of the price, the tokens go to the buyer at once. |
| `53` | pay | `id` reservation | The sender made the reservation, and its window is still open. This transaction pays the seller's address what is still due. The buyer gets the reserved tokens. |

- **Share of an amount:** its share of the price is `price × amount ÷ the offer's amount`, rounded up. The same goes
  for the deposit.
- **Payment:** the seller's payment in a transaction is the sum of every output that pays the seller's address.
- **The window:** a reservation made in block `h` can be paid up to and including block `h + window`. At the start of
  every block after that, before that block's commands, it ends. Its tokens go back to the offer, or to the seller if
  the offer was cancelled. The deposit stays with the seller. That is what makes it cost something to reserve without
  paying.
- **The arcade's view:** the arcade reads only blocks that carry data, so an ended reservation shows on its page from
  the next such block on. What counts is the block height, never when it was read.
- **What a wallet must check:** a payment after the window has closed is rejected, and the KOT stays paid. A wallet
  must check that the window is still open and that the payment reaches the chain in time.
- **Several buyers:** two buyers can reserve parts of one offer at the same time. The first in block order gets as
  much as is left. A reservation that asks for more than is left is rejected, and its deposit stays paid. A wallet
  checks what is left before it sends.

## Sending from the arcade's wallet

A signed-in account sends commands from its own Kotia address on **`/moonbrix`**, under *Send a command*. Pick the
command, fill in its fields, and send.
- **Who does what:** the node builds the transaction (`POST /account/moonbrix/offer`), and the browser writes the
  command's bytes itself (`wallet.js` `mbEncode`, the same table as `moonbrix.OPS`). The browser signs only when the
  transaction's OP_RETURNs are exactly those bytes and it pays nobody but the target and the account itself.
- **The outputs:** the command's OP_RETURNs come first, then the target, paid 0.001 KOT, Kotia's smallest relayed
  output. A payload of more than one OP_RETURN gets as many outputs back to the sender as Kotia needs. The change
  comes last.
- **A reserve or a pay:** it pays the offer's seller what is due: the deposit's share, the whole price with *pay the
  whole price now*, or what a reservation still owes.
- **Before it is built:** the node tries the command against the state it has read. If MoonBrix would reject it, the
  page says why and asks whether to send it anyway. It never refuses: on Kotia, what a person writes is theirs to send.
- **Inscribing:** a file of up to 80 kB goes in one transaction. A bigger one goes as parts (`21`), each sent on the
  change of the one before so they reach a block in order, then an assemble (`22`) with its size and SHA-256. That is
  up to 20 parts, about 1.5 MB, in one run.

## What the arcade keeps

`<net>-moonbrix.sqlite` holds the state as one JSON document: `tlds`, `spaces`, `inscriptions`, `parts`,
`properties`, `offers`, `reservations` and `log`. It also keeps the document as it was before each of the last 200 blocks that changed
it, so a reorg is rolled back. The arcade's unspent set (`data_blocks`) says which blocks carry an OP_RETURN, and only
those are read.

When a part's data cannot be fetched from the Core, that is the node's problem and not the command's. The reader then
stops at that block and tries again on the next pass. It never rejects a command for it.

## Still open

- **Claiming a TLD costs only the fee.** Somebody can claim many names. A price (a burn, or a payment to a fixed
  address) would be a change of the protocol, from a block on.
- **Bids.** A buy order that holds KOT until somebody sells cannot be done with OP_RETURN alone. KOT would have to be
  locked in a script.
