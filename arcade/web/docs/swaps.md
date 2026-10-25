# Swaps between chains: how a trade works here

The Exchange lets you swap coins, tokens and NFTs between two different chains
(Pepecoin for Dogecoin, for example) with somebody on any arcade, without
trusting them, the arcade you use, or theirs. Nobody ever holds your coins:
every swap is settled by **lock scripts** on both chains. The one exception is a
swap you **list** for your arcade to fill with your pages closed: see
[Listing your own swap](#listing-your-own-swap).

Swaps on a **single** chain (a token for PEP, an NFT priced in a token) do not
need any of this. They settle instantly in one transaction on that chain's own
market: the seller signs their half ahead of time and the buyer completes it.

## The short version

* Either **both** sides get paid, or **both** get their own coins back. There is
  no outcome where one side pays and the other keeps everything.
* A swap normally takes **seconds**: each side acts as soon as the other's lock is in the
  mempool and is exactly the lock the terms say. A swap with a coin side at or above the
  arcade's big-swap line (1,000,000 PEP or 1,000 DOGE unless its operator sets another) waits
  for one confirmation on each chain instead. A token, NFT or Doginal side counts from the
  mempool too, once its lock transaction plainly moves exactly that asset in from its owner
  and nothing else in the mempool is moving it; if that cannot be told yet, it waits for the
  block.
* You do **not** have to keep the page open. Your own node finishes your side
  with transactions you signed in your browser in advance.
* If the other side disappears part-way, your coins come back to you
  automatically after at most **two hours**.

## Which chains can swap

A swap between chains needs a lock script on each chain, and every lock this arcade makes refunds
through CHECKLOCKTIMEVERIFY: the owner may take it back only after its time has passed.

* **Pepecoin, Dogecoin and Litecoin** (and their test chains) swap with each other.
* **Doginals, Pepinals and DRC-20 / PRC-20 tokens** swap from Dogecoin and Pepecoin mainnet.
* **Wojakcoin cannot swap.** Its chain never turns on CHECKLOCKTIMEVERIFY (BIP65), so there a lock's
  refund opens at once. Whoever locked could take their coins back after the other side had claimed, and
  keep both. Your browser and every node refuse a swap order, a swap lock or an escrow with a Wojakcoin
  side. Swaps on one chain, which need no lock, are not affected.
* **Bellscoin cannot swap yet.** Its script addresses share their version byte with ordinary Dogecoin
  addresses, so the arcade makes no lock there.
* **Kotia cannot swap yet.** Every Kotia transaction carries a time, and the arcade writes it only for
  ordinary sends so far.
* **Luckycoin swaps are not open yet** (see its own page).

## Step by step

Say Alice lists 1,000 PEP for 300 DOGE and Bob takes it.

* **1. Bob makes a secret** in his browser and keeps it. Only its fingerprint
  (a hash) is shared.
* **2. Bob locks his 300 DOGE** on the Dogecoin chain in a lock script that says:
  *Alice may take this if she shows the secret; otherwise Bob may take it back
  after two hours.*
* **3.** As soon as that lock is in the mempool, **Alice's browser locks her 1,000 PEP** on
  the Pepecoin chain: *Bob may take this with the same secret; otherwise Alice
  may take it back after one hour.*
* **4.** As soon as that lock is in the mempool, **Bob claims the PEP**. To do it he has to
  put the secret on the Pepecoin chain, where anyone can read it.
* **5. Alice claims the DOGE** with the secret she just read off the chain.

Because Bob can only take the PEP by revealing the secret, and the secret is all
Alice needs to take the DOGE, the two claims stand or fall together. Alice's
lock runs out before Bob's, so she always has time to claim after he does, and
neither side can take the other's coins and keep their own.

## Why it is safe to close the page

When your browser locks your coins it also signs, right away, the two
transactions that finish your side: the **claim** of the other lock and the
**refund** of your own. It hands them to your arcade node, which sends:

* the **claim** as soon as the secret shows up on the chain (or, if you made the
  secret, as soon as the other lock is confirmed), and
* the **refund** once your lock's time has run out, if nobody claimed it.

Your node cannot change either one: your signature covers where the coins go,
and they can only go to you. All it adds is the secret, which by then is public.

## What it costs

Whoever locks a side pays its fees, on top, so the other side receives exactly
what was agreed:

* About **0.01** of the coin to put your side in its lock (the network fee); about 0.0001 on Litecoin.
* A coin lock also holds the network fee of the other side's claim (**0.01**;
  about 0.0001 on Litecoin), so the coins come out of it whole.
* A token or NFT goes into its lock with **0.02** of that chain's coin, which
  pays for moving it out again, whoever ends up with it.
* Your arcade's operator fee, if it charges one (Admin, Trading): a per cent of
  the coins you lock, at least the chain's smallest payment (0.01). It waits in
  the lock and is paid to the operator when the other side claims; if your lock
  comes back to you, so does the fee. A token or NFT lock pays only that
  smallest amount, unless its price is coin on the same chain.
* Trading with an arcade that has not updated yet, its claim fee still comes off
  what you receive from it, and its claim keeps the operator fee in your lock.
* Listing a swap writes a small record on the chain so other arcades can show
  it: about **0.01**.

You see these amounts before you say yes. Saying yes to Take, or to List, also
approves locking your side when the time comes; you are not asked again.

## Listing your own swap

On the Exchange, under **Swaps between arcades** (or on a coin pair's market,
such as PEP/DOGE), choose what you offer and what you want. A Doginal or
DRC-20 / PRC-20 listing shows on other arcades while an arcade page is open in
your browser; close them all and it stops being shown within the hour. Withdraw
it whenever you like.

A coin, token or NFT you list is filled by your arcade's node, with every page
closed: what you offer moves to the listing's own address, and **your arcade
holds that listing's key** while it trades for you. That is not trustless: its
operator could spend what you deposit. Only list amounts you would trust that
arcade with. So each listing is capped: at most **1,000 DOGE**, **100,000 PEP**
or **1 LTC** (on Bellscoin, Luckycoin, Kotia and Wojakcoin, the big-swap line),
unless the operator sets other caps (Admin, Trading). Test coins have no cap.
Withdraw sends it straight back.

What you list is held for the listing until you withdraw it or it is taken. A listed DRC-20 / PRC-20
amount is off your balance, so it cannot be sent or listed again meanwhile, and a listed Doginal cannot
be sent or listed twice.

When somebody takes it, your browser checks that their terms match yours
exactly, locks your side once theirs is confirmed, and hands your node the claim
and refund. You can close the page after that.

## Taking a swap

Press **Take** (or **Buy** / **Sell** on a market). Your browser makes the
secret and proposes the swap to the lister. Nothing is locked until they accept.
Then your side is locked, and the swap finishes by itself.

## Limits worth knowing

* A **phone** stops a browser in the background, so a swap waiting for your
  side to lock only moves while the arcade is on screen. Once your side is
  locked, your node finishes it either way.
* Your wallet has to be **unlocked** in some tab for anything to be signed.
* Swaps between chains move whole amounts: a listing is taken in full.
* A **DRC-20 / PRC-20** side is locked as a transfer note on the lock itself. The other side counts it
  only once the ord index has read that note as valid, so it waits for the index, not only for the
  mempool.
* The swap book is shared by every arcade. When two people take the same listing, the first one wins.
