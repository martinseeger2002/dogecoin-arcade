# Inscribing ords from your wallet

**Creator → Ords** inscribes a file as an ord on Dogecoin (a Doginal), Pepecoin (a Pepinal), Bellscoin (a Bellinal),
Luckycoin (a Luckinal), Wojakcoin (a Wojakinal) or DigiByte (a DigiByte ordinal). Your own wallet pays and signs, and the ord lands on your own address on that chain.

## How it works

1. Pick the chain and the file, and press **Inscribe**. The page tells you how many transactions it takes and what the
   fees come to. On a real-coin chain you confirm once, for the whole ord.
2. The first transaction pays from your coins into a lock that only your key can spend. It holds the postage and every
   fee still to come. Each next transaction carries about 1,500 bytes of the file and pays the next lock. The last one
   pays the ord's own coin (0.001 of the coin) to your address.
3. Your browser checks every transaction before it signs it. Each one spends only the coin the transaction before it
   made, and pays only your own next lock or your own address. No other coin of yours is spent once the first
   transaction has gone out.

The inscription id is the second transaction's id followed by `i0`.

The chains take only about 25 unconfirmed transactions in a row. So a big file sends 20, waits for a block and carries
on; the page says when it is waiting. Keep the tab open until it finishes. If the tab closes or the arcade restarts,
the inscription is listed under **Not finished yet**: choose the same file again and it carries on where it stopped.
Until then, the coins for the rest wait in your own lock.

## Each chain's rules

Every chain is written the way its own official indexer reads it. Before anything is sent, the page's node reads the
planned transactions back under those rules.

| Chain | Called | Read the way of | Parts | Fees | Empty file |
| --- | --- | --- | --- | --- | --- |
| Dogecoin | Doginal | wonky-ord / ord-dogecoin | 240 bytes | 0.01 DOGE a kB, plus the soft-dust fee on coins under 0.01 | not an inscription |
| Pepecoin | Pepinal | ord-pepecoin | 240 bytes | 0.01 PEP a kB, plus the soft-dust fee | allowed |
| Bellscoin | Bellinal | Nintondo ord (the Doginals kind) | 240 bytes | 0.0001 BEL a kB, no soft-dust fee | not an inscription |
| Luckycoin | Luckinal | Doginals-kind indexers | 240 bytes | 0.01 LKY a kB, plus the soft-dust fee | not an inscription |
| Wojakcoin | Wojakinal | wojakinals (the Doginals kind) | 240 bytes | 0.0001 WJK a kB, no soft-dust fee | not an inscription |
| DigiByte | DigiByte ordinal | wonky-ord-digibyte (the Doginals kind) | 240 bytes | 0.01 DGB a kB, no soft-dust fee | not an inscription |

Files up to 400 kB are taken.

## Inscribing a whole collection

**Inscribe a collection** takes a HashLips Art Engine `build` folder (its `images` and `json`, or `json/_metadata.json`)
or any folder of pictures:

1. Choose the chain and the folder. The page reads each picture's JSON the way the NFT collection run does (by
   `edition`, or by the file its `image` names), fills in the collection's name, and says how many transactions it
   takes and about what it costs.
2. Approve once. Every picture then goes on as its own ord, one after another, each with its own transactions, all
   signed in the tab. When a chain has as many unconfirmed transactions in a row as it takes, the page waits for a block
   and carries on.
3. When the last picture is in, the collection index -- each piece's name, number and traits from its JSON -- is
   inscribed on Pepecoin test, where the arcades index collections.

If the tab closes, choose the folder again: pictures already inscribed are skipped, a half-done one carries on where it
stopped, and the same index is not written twice. **Stop after this piece** ends a run cleanly between pictures.

## Making a collection by hand

Under **Make a collection index**, group your ords into a collection. It uses the same index the Doginal Dogs, the
Doginal Frogs and the Anti Doginal Dogs are published in.

- Ords you inscribe on the page are listed for you. You can add others by their inscription id.
- Give each one a number and, if you like, traits (`Background=Blue, Eyes=Red`). You can also paste a doggy.market
  trait file instead.
- **Inscribe the index** writes the parts and then the root, inscribed by your account on this arcade's own chain.
  Every arcade then shows the collection on the Exchange.

The first index to name an ord owns it, and only the account that published an index can replace it.

### The trait file

The trait file is a JSON list with one entry per ord, in doggy.market's layout. The number comes from the end of
`name` (`#12`):

```json
[
  {"inscriptionId": "<txid>i0", "name": "My Frogs #1", "attributes": {"Background": "Blue", "Eyes": "Red"}},
  {"inscriptionId": "<txid>i0", "name": "My Frogs #2", "attributes": {"Background": "Green", "Eyes": "Laser"}}
]
```

Each `inscriptionId` is the ord's inscription id on the chain its items live on: a txid of 64 hex characters followed by
`i0` (or `i1`, ...). Every id and every number may appear only once.

### Making the trait file with the arcade helper

The robot in the corner of every page can write the trait file for you. Collect each ord's inscription id, name and
traits from wherever the collection is listed, then paste them to the helper in batches of about 50 and ask, for
example: "Turn these into a trait file for the collection index: inscriptionId, name and attributes." Put the batches
together into one list (one `[` at the start and one `]` at the end), check the count, and paste it under **Or a
trait file** in **Make a collection index**. Choose where the items live (for example **Pepecoin** for Pepinals),
give the collection its name and press **Inscribe the index**.

