# How DogecoinArcade works

DogecoinArcade is a social network, messenger, token exchange and NFT arcade
that keeps everything that matters on a public blockchain. There is no
company database behind it: posts, names, tokens, NFTs and trades are
transactions, and any node that reads the same chain arrives at the same
arcade. This page is the overview. The other guides go deeper.

## What lives on the chain

| On the chain | Where it is read from |
|---|---|
| Coins, tokens, NFTs and inscriptions | Mainnet blocks |
| @tags (names), posts, likes and profiles | Blocks, and the mempool for speed |
| Offers, asks and order-book trades | Blocks; takes in flight are watched in the mempool |
| Private messages, sealed to one reader | Testnet, read from the mempool within seconds |

Tokens follow the Omni Layer rules, so anyone who has used Omni Core will
recognise the transaction types and the method names. Inscriptions (images,
text and interactive HTML pages) are stored in the transactions themselves.

The chain is **Pepecoin**, a Dogecoin Core fork. The protocol works the same
on Dogecoin, and the installer can set up either one.

## What a node does

A node is one machine running three things:

* **Pepecoin Core, mainnet**: the ledger.
* **Pepecoin Core, testnet**: the messenger's chain.
* **DogecoinArcade itself**: an indexer that replays blocks into a local
  database, and a web interface at `http://127.0.0.1:8420`.

The indexer holds no authority. It computes what the chain says. A node can
start from a downloaded index to save time, and the manifest beside the
download lets you re-read the chain yourself and prove the copy correct
instead of trusting it.

A node can run two ways:

* **As a wallet**, the default. One person's machine, with the node's own
  wallet, open only on that machine.
* **As a public arcade**, with `--public`. Other people can sign up for a
  seat, and strangers can see the public pages.

## What your browser does

When you join someone's public node, **your keys stay in your browser**:

* Your account is twelve words, made in the browser and never sent to the
  node. The node stores only your public key.
* To sign in, the browser signs a one-time challenge from the node.
* Each action (a post, a send, an order, a message) is built by the node,
  signed by your browser, and then broadcast. The node never sees a key it
  could spend with.
* Your address book and your copies of your sent messages stay in the
  browser. The node never learns who you talk to.

This makes a node a convenience, not a custodian. Your name, coins and posts
are on the chain, so you can sign in on another node, or run your own, and
they are all still there.

## How trading stays trustless

A trade between two people is an **atomic swap**. It is a single
transaction that either moves both sides or moves nothing. A seller signs
their half in advance, and the buyer completes it later. Neither side can
take the other's part without paying.

Standing buy orders can be filled while you are away, in one of two ways:

* The node fills them from lots you signed in advance, which can only be
  spent on the exact trade you agreed to.
* Or you get a notification and fill the order yourself the next time you
  open the app.

## Messages

Private messages are encrypted end to end, sealed to the recipient's key,
and carried as testnet transactions. They appear within seconds, straight
from the mempool. Nobody else can read them, and neither can the sender once
they are sent. Messaging runs on testnet only, by design: it uses no real
coins and puts no load on the main chain.

## Screening

A public node can screen what it shows. Each post and image is checked by a
language model the operator runs. Material marked sensitive is blurred until
a viewer chooses to reveal it, and illegal material is never shown. This
check happens on the node. It changes only what that node displays, and it
never alters the chain.

## Running your own node

On Linux:

```bash
curl -O https://dogecoinarcade.com/install.py
python3 install.py
```

The installer downloads and verifies Pepecoin Core, sets up both chains as
services, installs the latest release of the app and fetches the latest index
bootstrap. Every node also hands out its own copy of the program and index at
`/clone`, so the network does not depend on any one website.

## Where to read next

* [What it does](features.html): every feature, what it costs and its limits.
* [Node setup](00-node-setup.html): the installer, and the same steps by hand.
* [Remote access](remote-access.html): reaching your node from elsewhere.
* [Inscription API](inscription-api.html): building interactive NFT pages.
* [Bot RPC](bot-rpc.html): scripting against a node.
* [Messaging node](messaging/01-node.html) and
  [messaging design](messaging/02-design.html): how messages are carried.
