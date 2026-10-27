# Exchange safety: what may be signed ahead, and why

This page is for whoever changes the exchange next: Martin, SadFrog, or an AI agent working for either.
Every rule below exists because breaking it lets somebody take coins. Each rule names the code that
holds it and the test that proves it. **If you change one of those places, keep the rule, or change
this page and the test in the same commit and say why in COLLAB-NOTES.md.**

## What happened elsewhere (2026-10-26)

The Doginal Dogs marketplace was emptied. Six transactions moved 201 Dogs out of 194 wallets to one
address. All 264 inputs were signed **SIGHASH_NONE | ANYONECANPAY**:

* `ANYONECANPAY`: the signature covers only its own input, so the input can be added to anybody's transaction.
* `NONE`: the signature covers **no output at all**, so that transaction may pay anybody.

So each signature was a blank cheque for one Dog. Those signed inputs leaked from the market's side,
and one person put 264 of them into six transactions that paid only themself. Nothing on the chain was
broken. The market had asked its users for signatures that promised nothing, and then kept them where
someone else could read them.

The lesson for this arcade has two parts:

1. **Never ask for, sign, or keep a signature that promises less than the trade it is for.**
2. **A signature handed out is only undone when its coin moves.** Deleting a row, letting a listing
   "expire", or the seller changing their mind does not unmake it. Anything this node keeps is a copy
   that can leak: a backup, a database file, a disk.

## The sighash types and what each one is allowed for

A legacy (non-SegWit) signature ends in one byte that says what it covers:

| Byte | Name | Covers | Allowed here |
|---|---|---|---|
| `0x01` | ALL | every input and every output | Every ordinary send, every lock claim and refund, every escrow spend, watchtower jobs |
| `0x81` | ALL\|ANYONECANPAY | this input and **every** output | Pre-signed bids (`funding.build_bid`): the buyer's payment, with every output fixed |
| `0x83` | SINGLE\|ANYONECANPAY | this input and **the output at its own index** | Listings (`funding.build_leg`), Doginal listings (`doginal_market`), buy-order lots (`standing`) |
| `0x02`, `0x82` | NONE, NONE\|ANYONECANPAY | **no output** | **Never.** `funding.sighash` cannot compute it, and nothing files it |
| `0x03` | SINGLE (without ANYONECANPAY) | — | Never; `funding.sighash` refuses it |

Where it is enforced:

* **Making a digest:** `funding.sighash` only builds ALL, ALL|ANYONECANPAY and SINGLE|ANYONECANPAY, and
  it refuses SINGLE where the input's index is past the last output (the "SINGLE bug", which signs the
  number 1). The browser signs only digests that it has worked out itself from the bytes it was shown
  (`coins.js` `verifyLeg`, `checkLot`, `sighashWitness`).
* **Filing a signature someone sent:**
  * listings (`listings.py`, `Listings.register`) and Doginal listings (`doginal_market.Book.register`)
    refuse anything but `0x83`;
  * bids are filed as `0x81`;
  * watchtower jobs (`app.py`, `_signature_good`) as `0x01`;
  * buy-order lots (`/account/standing/sign`) as `0x83`, the buyer's own key, over that lot's own digest.
    That last check was added 2026-10-27; before it, any bytes were kept.

A SINGLE|ANYONECANPAY signature is only as strong as **the output it covers**. A listing's covered output
is the price paid to the seller, which is a real promise. A buy-order lot's covered output is only the
buyer's own change, so the lot's price is not in the signature. The buyer trusts this node with that lot
(see below).

## Rule 1: a listing can only get cheaper while it stands

*Code:* `app.py` `_no_dearer_relist` (exchange listings), `doginal_market.Book.register` (Doginal listings).
*Tests:* `test_account_list.py` `test_a_listed_piece_is_listed_again_only_for_less`,
`test_doginal_market.py` `test_a_listed_coin_is_listed_again_only_for_less_and_the_old_signature_is_not_kept`.

A listing is a signed trade: "this piece for that price". It stays valid until the coins it was signed
over are spent.

* **Exchange listings** are announced to every node (`Listings.has_leg`: "an announced listing is read by
  every node"). A second listing of the same piece is built over **other** coins, so the first one
  stays a trade any buyer can finish while the seller still holds the piece.
* Listing again for **more** would therefore leave the cheaper listing buyable. That is refused until the
  seller cancels, which spends the old listing's coins back to them (`/account/list/cancel`).
* Listing again for **less** is fine: the dearer signature can only ever be bought at the dearer price.
* **Doginal listings** never leave this node: `doginal_market.public` strips the signature from every page.
  The same rule holds anyway, because a database copy can leak.
* A replaced or expired Doginal row has its signature **blanked**. Nothing is sold from such a row, so
  keeping the signature would only be keeping something that can leak.

Do not add an "edit price" button that only updates a row. A new price means a new signature, and the
old one must be either more expensive or dead.

## Rule 2: what a buy-order lot trusts, said plainly, and undone on cancel

*Code:* `standing.py`, `app.py` `/account/standing/lots`, `/account/standing/sign`,
`/account/standing/cancel`, `pair.html`. *Test:* `test_standing.py`
`test_a_lot_is_kept_only_with_its_buyers_single_anyonecanpay_signature_and_cancel_moves_it_back`.

"Fill while away" buy orders work like this:

* The buyer pre-signs lots: one coin each, SINGLE|ANYONECANPAY over **their own change**.
* The price is not covered by the signature, so whoever holds a lot's signature can spend that lot.
  Most likely that is this node, filling the order. It could also be anyone who copies the node's
  `standing` database.
* That is the trust the buyer gives. The setup prompt says so, and must keep saying so.
* **Cancel** stops the order and blanks the signatures. It then **offers a sweep that moves the signed
  lots back** to the buyer, and pair.html asks the buyer to sign it. Only that sweep makes the old
  signatures useless.
* A lot's signature is filed only once it is the buyer's own key (checked against the order's address),
  `0x83`, and over that lot's own digest.

If you change how lots are filled, the lot coins must still go back on cancel. And never let the
prompt promise more than the signature enforces.

## Rule 3: a swap's secret hash is used once

*Code:* `trade.Book.knows_secret_hash`, `node_trader._consider`, `wallet_js.js` `answerProposals`.
*Test:* `test_trade.py` `test_the_node_trader_never_takes_a_secret_hash_twice`.

* A swap lock's script commits to the secret hash, the two keys and the lock time. **It does not commit
  to an amount.**
* So two proposals that share a hash, keys and lock time (against two of one lister's orders) have
  **the same lock script**.
* The taker could lock once, name that one lock for both trades, and claim both of the lister's locks.
* So a proposal whose hash appears in any trade, in any state, is refused, by the node trader and by
  the browser alike.

## Rule 4: lock times stay out of a miner's reach

*Code:* `trade.LOCK_SECONDS` (8 h), `trade.FUTURE_DRIFT` (2 h), `trade.next_step`, `trade.locktimes_fit`;
the same in `wallet_js.js` (`TRADE`, `nextTradeStep`, `locktimesFit`, the watchtower's `not_after`).
*Tests:* `test_trade.py`
`test_the_initiator_never_claims_while_an_early_block_could_open_the_responders_refund`,
`test_a_responder_accepts_only_lock_times_it_can_finish_under`; `test_trade_tab.py` (wallet.js and
Python agree).

Every lock here refunds through CHECKLOCKTIMEVERIFY with a **time**, not a height:

* On chains without BIP113 (Dogecoin, Pepecoin and their family; CSV never activated there), the time is
  checked against **the block's own timestamp**.
* A block may be stamped up to **2 hours ahead** of the real time and still be accepted.

With 2 h / 1 h locks, a party able to mine a single block on a cheap chain could:

1. watch the other side's claim show the secret in the mempool,
2. mine a block stamped about an hour ahead, holding **its own refund** in place of that claim,
3. claim the other lock with the secret.

It would end up with both sides. So:

* the locks are 8 h (initiator, T1) and 4 h (responder, T2);
* the initiator claims only while `T2 - now > CLAIM_MARGIN + FUTURE_DRIFT`. After that it waits and
  refunds at T1 instead;
* `locktimes_fit` refuses a proposal whose T2 leaves less than `LOCK_MARGIN + CLAIM_MARGIN + FUTURE_DRIFT`;
* the gap T1 − T2 (4 h) stays more than `FUTURE_DRIFT`, so the responder's claim is also out of reach of
  an early refund by the initiator.

Do not shorten the locks below about `2 × FUTURE_DRIFT + margins`. Do not move to height locks without
redoing this arithmetic per chain.

## Rule 5: no lock where it could not settle

*Code:* `config.require_cltv`, `config.require_p2sh_payable`, `config.require_untimed` (called by
`htlc.address_of`, `escrow.address_of` and `trade_orders.check_order`), `wallet_js.js` `NO_CLTV`,
`NO_LOCK`. *Tests:* `test_wojakcoin.py`, `test_kotia.py`
`test_no_swap_lock_or_escrow_is_made_where_it_could_not_settle`.

| Chain | Why no lock | Check |
|---|---|---|
| Wojakcoin | CHECKLOCKTIMEVERIFY never enforced: a refund opens at once | `require_cltv` |
| Bellscoin | its P2SH byte is Dogecoin's "D": a lock would be paid as a key hash and lost | `require_p2sh_payable` |
| Kotia | transactions carry `nTime`, which lock claims and refunds do not write yet: its Core would refuse them and the coins would stay locked | `require_untimed` |

An order that can never settle is refused **when it is listed**, not when somebody takes it.

When you add a chain, run it through all three checks. When you teach the lock builders (`htlc.claim`,
`htlc.refund`, `escrow`) to write `nTime`, lift `require_untimed` for Kotia in the same commit, with a
test that Kotia's Core takes the claim.

## Rule 6: every chain's script address is paid as a script, or refused

*Code:* `txbuild.SCRIPTHASH_VERSIONS`, `config.unpayable_versions`, `app._check_address`.
*Test:* `test_kotia.py` `test_every_chains_script_address_is_paid_as_a_script_or_refused`.

* `txbuild.p2pkh_script` picks the script by the address's version byte alone.
* A chain's P2SH byte that is missing from `SCRIPTHASH_VERSIONS` gets a **key-hash** script, and those
  coins are lost. Kotia's 45 was missing until 2026-10-27.
* A byte that is another chain's key-hash byte can't be both. Such a byte is refused as a payee
  (`unpayable_versions`).
* The test walks every chain in `NETWORKS`, so a new chain fails it until its byte is placed.

## Rule 7: the node signs only what was offered

*Code:* `swap.countersign`. *Test:* `test_swap.py` `test_the_seller_signs_nothing_it_did_not_offer`.

* `signrawtransaction` signs **every** input the node's wallet holds a key for.
* So before countersigning a buyer's half, every buyer input's address is checked with
  `validateaddress`.
* An input that belongs to the node's own wallet is refused; only the offered output may be the node's.
* Anything else that hands a half-signed transaction to the node's wallet needs the same check.

## Rule 8: the node key's own sends on a timed chain

*Code:* `keywallet.prepare`, `escrow.serialize` / `escrow.sighash` (`ntime`).
*Test:* `test_kotia.py` `test_the_node_keys_own_send_carries_the_time`.

The operator's sends from the node key (Admin, on a borrowed chain or a Core without a wallet) write
Kotia's `nTime`, both in the bytes and in each digest. The test checks the digest against a transaction
that Kotia's own Core signed.

## Known and left as decided

* **Zero-confirmation swaps** below the big-swap line (operator, 2026-10-17). Either side can double-spend
  an unconfirmed lock, up to that line. The node's own trades are protected (`OWN_REAL_COINS`); tab-to-tab
  swaps are not. To close it: the initiator claims only against a confirmed lock, and locks that signal
  RBF are refused.
* **Bids** are ALL|ANYONECANPAY. Misusing a stored bid needs the node's database *and* the seller, and
  even then the ledger refuses the transfer. The buyer would pay for nothing, which is not a theft by a
  third party.
* **The DigiByte branch** (team/digibyte): its "D" addresses are Dogecoin's byte. Coin choice must name
  its chain to the inscription guard (`funding.guard_for(address, network)`), never pick the guard by
  address alone.

## Checking a change against this page

1. Grep your change for `sighash_type`, `signrawtransaction`, `SINGLE_ANYONECANPAY`, `ALL_ANYONECANPAY`,
   `NONE`, `LOCK_SECONDS`, `scripthash_version`.
2. Run:
   `tests/test_account_list.py tests/test_doginal_market.py tests/test_standing.py tests/test_trade.py
   tests/test_trade_tab.py tests/test_kotia.py tests/test_wojakcoin.py tests/test_swap.py`.
3. If a rule above no longer holds, say so in COLLAB-NOTES.md before you push. Don't leave it for the
   other side to find.
