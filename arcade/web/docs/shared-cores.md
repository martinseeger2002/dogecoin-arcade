# Shared Cores: running some chains, borrowing the rest

An arcade does not have to run a Core node for every chain it offers. Any operator can share a Core
that runs on their machine with every other arcade, and any arcade can borrow a chain from arcades
that share it. Two arcades that each run two Cores can both offer every chain.

## The rule

- **Pepecoin testnet always runs on your own machine.** The Messenger, the @names and the mesh's own
  checks live there.
- **Run at least one more Core yourself.** Every arcade brings a Core to the network as well as
  borrowing.
- **Every other chain is your choice:** run it here, borrow it from an arcade that shares it, or (for
  a chain the arcade can do without, such as Dogecoin) leave it off.

For example, one operator runs Pepecoin testnet, Pepecoin mainnet and Dogecoin, and shares the two
mainnet Cores. Another runs Pepecoin testnet and Litecoin, and shares Litecoin. Both arcades then
offer every chain.

## Setting it up

Open **Admin → Chains**. Each chain has:

- **Core:** *Runs here*, *Borrow a shared Core*, or *Off*. Changing this takes effect when the arcade
  restarts; at the node machine the panel offers **Restart now** when a service manager runs the
  arcade.
- **Share:** let other arcades use this chain's Core. **On by default:** every Core that runs here is
  shared unless you untick it. Only a Core that runs here can be shared. A change takes effect within
  a minute, with no restart.

The same choices are kept in `settings.json` as `core:<chain>` (`here`, `borrow` or `off`) and
`share:<chain>` (`true` or `false`; `true` when it is not set).

## What sharing lets other arcades do

A shared Core answers a short list of requests and nothing else:

- reading blocks, transactions, block headers and the chain tip;
- reading the mempool and whether a coin is still unspent;
- fee estimates, the relay fee, and checking a signed message;
- broadcasting a transaction that somebody already signed.

**It never answers a wallet request.** Sharing can never spend, sign or reveal your node's coins. Your
Core works on at most two borrowed requests at a time, so your own pages never wait behind other
arcades, and each borrowing arcade is limited in how fast it can ask.

A sharing arcade also sends each new mempool transaction, as it arrives, to the arcades borrowing
that chain, so their wallets see incoming payments as quickly as yours does.

A Core that is still syncing or rescanning is not offered to other arcades until it has caught up.

## Which sharer is asked

When several linked arcades share a chain, the one with the fastest ping is asked first. A sharer that
has failed three times lately goes behind every one that answers, whatever its ping. Among the ones
failing, the fewest failures come first, then ping. A sharer caught sending a wrong answer counts as
ten failures. Each answer it gives counts one failure off again, so a sharer that recovers moves back up.

## Every broadcast goes to every sharer

When your arcade sends a signed transaction on a chain, its own Core (or the borrowed one) takes it
first. The same transaction is then also sent to every linked arcade that shares that chain, each at
once, and each of them passes it on once more. In several mempools at once it reaches the miners sooner,
and one slow or badly connected node no longer leaves it unseen. Sending a transaction twice is
harmless: a node that already has it says so.

## What a borrowing arcade checks

A borrowed chain's answers come from somebody else's node, so the borrowing arcade proves what it can
from the bytes:

- **Transactions** are fetched raw. A transaction whose bytes hash to the id that was asked for is that
  transaction; nothing in it can have been changed.
- **Blocks** are fetched raw. The block's hash must be the hash of its header, its transactions must
  hash up to the header's merkle root, and the header must carry the proof of work its difficulty
  asks for: scrypt (SHA-256 on Wojakcoin), or for a merge-mined chain (Dogecoin, Pepecoin mainnet,
  Bellscoin, Luckycoin) a parent block that commits to it. A block whose difficulty falls more than
  fourfold from the block under it is refused. DigiByte's work is not checked: it is mined with five
  algorithms, so its borrowed blocks are proved by hash, merkle root and linkage only, and the work is
  the sharer's word.
- **Kotia's blocks** (staked since block 1,000,000) are not checked for work or stake. Their hash,
  merkle root and link to the block under them are proved; the rest is taken on the sharer's word.
  **Admin → Chains** says so on a borrowed chain whose work is not checked.
- **Mempool transactions** pushed by a sharer are checked the same way as any other transaction.

What cannot be proved from bytes is taken on the sharer's word: which block is at a given height,
whether a coin is still unspent, and what the mempool holds. When two sharing arcades are linked, their
chain tips are compared, and the admin panel says so if they disagree.

An answer that fails a check is refused. The sharer that sent it goes to the back of the line, and the
next sharing arcade is asked instead.

A borrowed chain has no node wallet. Accounts work as usual, because people sign in their own browsers.
What needs the node's own coins on that chain does not: the faucet, the node's own trading, and the node
paying for inscriptions.

## A Core that is behind borrows until it catches up

A chain you run here can still be read from a sharer for a while. When its Core is behind (still
syncing, loading its block index, or rescanning its wallet) and a linked arcade shares the chain, reads
and broadcasts go through that sharer, proved the same way as on a borrowed chain. Everything else,
above all the node's own wallet, stays on your Core, and a broadcast is handed to your Core as well so
its wallet knows what it spent.

The arcade checks every 30 seconds and goes back to your own Core after two caught-up checks in a row.
**Admin → Chains** says on the chain's row which arcade it is borrowing from and how far your Core has
got. With no linked arcade sharing the chain, your Core is used as it is.

## Ord-index lanes on lending arcades

An ord index (**Admin → Ords**) reads many blocks at once, each lane fetching its own next block. Beside
the lanes on your own Core, it also gets lanes on every linked arcade that shares the chain's Core. An
arcade with no Core of its own for a chain walks its ord index on these borrowed lanes alone.

- **Each lender says how many lanes it lends** one borrower at a time: 4 on a machine with 8 or more
  CPU cores, 1 on a smaller one, so a small server's disk is never asked for more than one block at a
  time per borrower. An arcade that does not say lends one.
- **Lanes are dealt round the lenders:** each lender's first lane, then each one's second, up to the
  number of cores you give the ord index in **Admin → Ords**.
- **A lender that misses three times in a row rests for five minutes,** and the chunk it was reading
  is let go for another lane to take.

## Every arcade's index, compared

Every arcade builds its index of tokens, inscriptions, names and trades from the chain alone, so honest
arcades on the same rules hold the same index. Linked arcades check that they do:

- Every minute, each arcade sends a fingerprint of its index up to a checkpoint (a multiple of 100
  blocks, at least 6 blocks under the tip, so a reorganisation cannot move it), and the consensus hash
  of its balances.
- Two arcades whose fingerprints differ halve the range of blocks, again and again, until they find the
  first block they read differently, and both arcades' rows for that block are shown in
  **Admin → Chains**.

This applies to borrowed chains too: an index built from a borrowed Core's blocks is compared with the
indexes other arcades built from their own Cores.

## A tenth to the arcade that shares

An arcade that borrows a chain pays the arcade whose Core it uses a tenth of what it earns on that chain:

- **Operator fees.** The operator's fee on a borrowed chain is split in the same transaction: nine tenths to
  this arcade and one tenth to the sharer. A tenth too small for the network to carry (under the dust
  limit) is not paid; the whole fee then stays with this arcade.
- **Mined blocks.** A block mined with a borrowed Core's work pays the sharer a tenth of its reward in its
  own coinbase.

Each sharing arcade names the address it is paid at for every chain it shares, and a borrowing arcade is
paid its fees and mined coins in the same way. It's the operator account's own address on that chain when
one is named, so the coins show up in that account's Wallet. Otherwise it's the Core wallet's address, or,
on a chain with no Core wallet here (a borrowed one), the arcade's own key's address there.
**Admin → The node's coins** shows and spends those coins, signed by the arcade itself with its identity
key. Nothing has to be imported into another wallet.

## Mining test coins

**Admin → Chains → Mine test coins** mines blocks on a test-coin chain with this machine's CPU, using work
from that chain's Core, whether it runs here or is borrowed. Chains of real coins and merge-mined chains are
never mined this way. A testnet lets anyone mine its next block at the lowest difficulty once none has been
found for a while. That is when a CPU wins a block.

A sharing arcade answers mining calls (`getblocktemplate`, `submitblock`) for test-coin chains only, and no
more often than once every couple of seconds per borrower.

To mine on another machine, run the miner there against this arcade's `/rpc/work/<chain>`, with the
arcade's RPC cookie (`~/.dogecoinarcade/rpc.cookie`):

```
python -m arcade.blockminer --url http://127.0.0.1:8420/rpc/work/ltctest \
    --cookie ~/.dogecoinarcade/rpc.cookie --chain ltctest --address <yours> --workers 16
```

The arcade listens on its own machine only, so reach it from another one through an SSH tunnel
(`ssh -L 8420:localhost:8420 <arcade machine>`).
