# Swaps between chains: how a trade works here

The Exchange lets you swap coins, tokens and NFTs between two different chains
(Pepecoin for Dogecoin, for example) with somebody on any arcade, without
trusting them, the arcade you use, or theirs. Nobody ever holds your coins:
every swap is settled by **lock scripts** on both chains.

Swaps on a **single** chain (a token for PEP, an NFT priced in a token) do not
need any of this. They settle instantly in one transaction on that chain's own
market: the seller signs their half ahead of time and the buyer completes it.

## The short version

* Either **both** sides get paid, or **both** get their own coins back. There is
  no outcome where one side pays and the other keeps everything.
* A swap normally takes **two or three minutes**: one confirmation on each chain.
  A big swap waits for three confirmations each, for extra safety.
* You do **not** have to keep the page open. Your own node finishes your side
  with transactions you signed in your browser in advance.
* If the other side disappears part-way, your coins come back to you
  automatically after at most **two hours**.

## Step by step

Say Alice lists 1,000 PEP for 300 DOGE and Bob takes it.

* **1. Bob makes a secret** in his browser and keeps it. Only its fingerprint
  (a hash) is shared.
* **2. Bob locks his 300 DOGE** on the Dogecoin chain in a lock script that says:
  *Alice may take this if she shows the secret; otherwise Bob may take it back
  after two hours.*
* **3.** Once that lock has a confirmation, **Alice's browser locks her 1,000 PEP** on
  the Pepecoin chain: *Bob may take this with the same secret; otherwise Alice
  may take it back after one hour.*
* **4.** Once that lock has a confirmation, **Bob claims the PEP**. To do it he has to
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

* About **0.01** of the coin to put your side in its lock (the network fee).
* When you claim coins out of a lock, **0.01** comes off what you receive (the
  fee for that transaction).
* A token or NFT goes into its lock with **0.02** of that chain's coin, which
  pays for moving it out again, whoever ends up with it.
* Listing a swap writes a small record on the chain so other arcades can show
  it: about **0.01**.

You see these amounts before you say yes. Saying yes to Take, or to List, also
approves locking your side when the time comes; you are not asked again.

## Listing your own swap

On the Exchange, under **Swaps between arcades** (or on a coin pair's market,
such as PEP/DOGE), choose what you offer and what you want. Your listing shows
on other arcades while an arcade page is open in your browser; close them all
and it stops being shown within the hour. Withdraw it whenever you like.

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
