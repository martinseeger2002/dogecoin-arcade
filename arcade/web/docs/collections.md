# Collections: putting a set of inscriptions on the arcade

A collection is a set of inscriptions that belong together: *Space Frogs #1* to *#100*, each with its own picture,
number and traits. The arcade lists collections on the **Exchange**, in the **Wallet**, and under **Collections**, with
each piece's traits counted across the set (*Blue background: 3 of 100*).

There are three ways to get a collection onto the arcade. Pick the one that fits:

| You have | Use | Where |
|---|---|---|
| Pictures, and you want them inscribed on the arcade's own chain (Pepecoin test, or a real coin chosen at the top of the page) | **Run a collection** | **Creator → NFTs** |
| Pictures, and you want them inscribed as ords (Doginals, Pepinals, Bellinals, Luckinals, Wojakinals, DigiByte ordinals) | **Inscribe a collection** | **Creator → Ords** |
| Ords that are already inscribed (yours, or a collection inscribed elsewhere) | **Make a collection index** | **Creator → Ords** |

Each is described step by step below. Everything here is signed in your own browser, by your own account, and
paid from your own coins: the node never holds your key. Try a small set on testnet first. An inscription cannot be
taken back.

**Pictures made with an AI model on the LLM tab** need no folder: tick **For a collection** on them under **Your
pictures** and press **Inscribe as NFTs** or **Inscribe as ords**. The pictures arrive already chosen, and a details
form writes their JSON (see [the LLM service](llm.md), "Inscribing a picture you made").

## The operator's inscription fee

On a mainnet chain an arcade may charge an **inscription fee** (Admin -> Trading -> Inscription fee). It is the larger
of a rate (0.5% of the network fees by default, or Off) and an amount the operator sets for each chain, for example
about 2 cents' worth; unset, that amount is the chain's dust line. It is paid once per inscription, in its first
transaction: once per piece of a collection, once per ord (on its commit, never on a reveal). The wallet shows it on its
own line, and the collection's estimate counts it. Testnet never pays it, and neither does the operator's own account.

## Getting the folder ready

All three start from the same kind of folder. The easiest is a **HashLips Art Engine** `build` folder, used as it
is: an `images` folder (`1.png`, `2.png` ...) and a `json` folder with one `<number>.json` per picture and all of them
together in `json/_metadata.json`. Each piece's JSON looks like this:

```json
{"name": "Space Frogs #12", "edition": 12, "description": "100 hand drawn space frogs",
 "attributes": [{"trait_type": "Background", "value": "Blue"}, {"trait_type": "Eyes", "value": "Laser"}]}
```

* `name` is the collection's name, then `#` and the number. That is how every node files the piece into its set.
* `attributes` are the traits: what collectors filter and compare by.
* HashLips writes `"image": "ipfs://NewUriToReplace/12.png"`. That is dropped before it goes on the chain: the picture
  *is* the inscription, so the link would point to nowhere and cost bytes.

**A folder of your own pictures works too**, with no JSON at all. The arcade sees that the JSON is missing and offers
**Make the JSON with the wizard**: give the collection a name and a description, and choose the traits:

* **None for now**,
* **The same traits on every piece**, or
* **Each piece its own, from a spreadsheet (CSV)**: one row per picture, the first column the picture's file name or
  number, each other column a trait, with the trait names in the first row:

  ```
  file,Background,Eyes
  sunset.png,Blue,Laser
  moon.png,Green,Red
  ```

The pieces are numbered in the order of their file names, and files called `1.png`, `2.png` ... keep their own
numbers. The wizard shows what piece #1 will read before it writes anything. **Keep the JSON that is already there**
fills in only what is missing, if some pieces have JSON and some do not.

Smaller pictures are cheaper and quicker: every byte is paid for. Shrink them before you start if they do not need to
be big.

## On the arcade's own chain: Run a collection

**Creator → NFTs → Run a collection** (also on **My NFTs**).

1. **Pick the chain** at the top of the Creator page: Pepecoin test to try it, a real coin when you are ready.
2. **Choose the folder** and press **Write it down**. The page reads every picture and its JSON, says how many
   transactions the run takes and what it costs, and how long your node expects it to take. If the JSON is missing
   or incomplete, it offers the wizard (above).
3. **Set the collection's rules**, for a new collection:
   * **Maximum supply**: `0` means no limit, so you can add more pieces later. A number seals the set at that size
     for good: nothing joins after, and nothing numbered past it joins at all.
   * **The collection's details, written on #1**: a description, a website, the artist, and a thumbnail (the id of an
     inscription of yours to use as the set's face; without one, #1 is its face).
4. **Press Inscribe them all.** Each picture goes on the chain as its own inscription, signed in this tab, one after
   another. The page says where it is. **Stop** pauses between pieces.
5. **Keep the tab open** while it runs. If it closes, open the page again in the same browser and the run is still
   there, at the piece it stopped on. In another browser, choose the folder again and it carries on. Pieces already
   on the chain are never paid for twice.

When the blocks land, the collection is on **Collections**, the **Exchange** and in your **Wallet**, on every arcade.
**Every run this account has written down** (`/me/runs`) lists each run: its pieces, which one stopped it, and what it
cost.

**Adding more pieces later.** Run another folder with the same collection name, from the same account. The page says
the set is already on the chain and that the new pieces take its rules. **Continue the numbering after the pieces
already on the chain** is ticked: untick it only if your new build carries its own numbers that do not clash with the
old ones. A set with a maximum supply that is full takes nothing more.

**The rules, in short.** A set is one creator's: somebody else using the same name makes a different collection. Each
number is taken once, and the first piece to claim it wins, so a build inscribed twice does not double the set. The
same collection is not inscribed twice from one account by accident: pieces of the build that are already on the chain
from that address are left out of the run, and the page says how many. The full rules are under *Collections* in *What it does*.

**Selling it.** A collection and a shop are two decisions. To sell random pieces at one price, make a mintpad with the
**Launchpad wizard** (Creator → Launchpad wizard), or design your own (see *Mintpads*).

## As ords: Inscribe a collection

**Creator → Ords → Inscribe a collection** inscribes every picture as an ord on Dogecoin, Pepecoin, Bellscoin,
Luckycoin or Wojakcoin, from your own wallet on that chain, and then indexes the set so the arcades show it.

1. **Choose the chain and the folder.** The page reads each picture's JSON (by `edition`, or by the file its `image`
   names), fills in the collection's name, and says how many transactions it takes and about what it costs.
2. **Check the collection name** and press **Inscribe the collection**. You approve once for the whole run.
3. Every picture goes on as its own ord, one after another, each signed in the tab. Ord chains take only about 25
   unconfirmed transactions in a row, so the page waits for a block when it has to and carries on by itself.
4. When the last picture is in, the **collection index** (each piece's name, number and traits) is inscribed on
   Pepecoin test, where the arcades index collections. The collection then shows on the Exchange.

If the tab closes, choose the folder again: pictures already inscribed are skipped, a half-done one carries on where it
stopped, and the index is not written twice. **Stop after this piece** ends a run cleanly between pictures. The
fees, and how each chain's indexer reads an ord, are in *Inscribing ords from your wallet*.

## Ords already inscribed: Make a collection index

For ords that are already on their chain (inscribed with this arcade, with another wallet, or years ago), **Creator →
Ords → Make a collection index** groups them into a collection. It is the same index the Doginal Dogs, the Doginal
Frogs and the Anti Doginal Dogs are published in.

1. **Name the collection**, choose where its items live (**Items live on**: Dogecoin for Doginals, Pepecoin for
   Pepinals, and so on), and write a description.
2. **Add the ords.** Ords you inscribed on this page are listed for you. Add others by their inscription id (the txid
   followed by `i0`) and press **Add**. Give each one a number and, if you like, traits: `Background=Blue, Eyes=Red`.
3. **Or paste a trait file** instead, under **Or a trait file**. Two layouts are read:
   * doggy.market's: `[{"inscriptionId": "<txid>i0", "name": "My Frogs #1", "attributes": {"Background": "Blue"}}]`
   * the OW layout: `[{"id": "<txid>i0", "meta": {"name": "My Frogs #1", "attributes": [{"trait_type": "Background", "value": "Blue"}]}}]`

   The number comes from the end of `name` (`#12`). Every id and every number may appear only once.
4. **Use number 1 as the cover** is ticked: the set's face is its #1. Untick it and the index names no cover.
5. **Press Inscribe the index.** It writes the index's parts and then its root, inscribed by your account on Pepecoin
   test, so it costs test coins only. Once its blocks are mined and the arcades have read it, the collection is on
   the Exchange of every arcade.

**Who owns an index.** The first index to name an ord owns it, so a collection cannot be taken over by somebody who
indexes it later. Only the account that published an index can replace it.

**Making a trait file with the helper.** The robot in the corner of every page can write the trait file. Collect each
ord's inscription id, name and traits from wherever the collection is listed, paste them to the helper in batches of
about 50, and ask: "Turn these into a trait file for the collection index: inscriptionId, name and attributes." Join
the batches into one list (one `[` at the start, one `]` at the end), check the count, and paste it under **Or a trait
file**.

## When something goes wrong

* **"The JSON is missing or incomplete."** Use the wizard, or fix the JSON files and choose the folder again.
* **A piece is "not admitted" to the set.** Its number was already taken, or the set was full. It is still an
  inscription you own and can sell; it just is not a member of that set.
* **The run is waiting.** It waits for blocks, not minutes. It says which of two things happened: blocks came and the
  pieces are still not in one (the fee is too low for the mempool right now), or no block came at all (the chain is
  stalled, and there is nothing to fix but wait).
* **The collection does not show yet.** Collections appear when their blocks are mined and the arcade has read them.
  Give it a few minutes, then look under **Collections** or on the Exchange.
* **The tab closed.** Open the same page again and choose the same folder: nothing already sent is paid for again.
