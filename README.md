# DogecoinArcade

An arcade for Dogecoin-family chains: a wallet, an exchange, tokens, NFTs and ord inscriptions,
games, and a Messenger, served by a node anyone can run.

It reads blocks from **unmodified** Core nodes and keeps its own protocol state in SQLite. It is not
a fork of any node. Arcades link to each other over a mesh: they share Cores, compare their
indexes, carry one swap order book and host game rooms.

## Chains

Each chain is data, its `Params` in `arcade/config.py`.

| Chain | Role |
|---|---|
| Pepecoin testnet | The Messenger, the @names and the mesh's own checks. Every arcade runs this Core itself. |
| Pepecoin mainnet | The ledger: the arcade's tokens, NFTs and inscriptions. Run here or borrowed, never off. |
| Dogecoin, Litecoin, Litecoin testnet, Bellscoin, Luckycoin, Kotia, Wojakcoin, DigiByte | Add-on chains. The operator runs each one here, borrows it from an arcade that shares its Core, or leaves it off (**Admin → Chains**). |

Each add-on chain has its own page in the docs, with what works on it and what does not yet.
Swaps between chains are refused on Wojakcoin, which never enforces lock times (see the swaps page).

## Install

```bash
curl -O https://dogecoinarcade.com/install.py
python3 install.py
```

The installer needs Python 3.10 or newer and only the standard library. It downloads and verifies
(SHA-256 and GPG) Pepecoin Core for mainnet and testnet and Dogecoin Core for mainnet, installs
DogecoinArcade into `~/.dogecoinarcade`, fetches the latest index bootstrap, and registers everything
as user services. `python3 install.py --help` lists its options; `--dry-run` changes nothing. The
other chains' Cores are not installed by it: run them yourself, or borrow them.

Once installed:

| Command | What it does |
|---|---|
| `dogecoinarcade-update` | Update to the latest published code (`--check`, `--dry-run`) |
| `arcade-web` | The indexer and web interface, on `http://127.0.0.1:8420` |
| `arcade-msg` | The Messenger from a shell (keys, send, scan, inbox) |
| `arcade-rpc` | The bot RPC from a shell, like `bitcoin-cli` |
| `arcade-publish` | Publish an inscription straight through a node |
| `arcade-admin-window` | The window that opens the admin panel when the machine starts |

## Documentation

The manual is in `arcade/web/docs/`, and every arcade serves it at `/docs`. Start with
`00-node-setup.md` (what a node is and what the installer does), `how-it-works.md`,
`shared-cores.md` and `remote-access.md`.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,web]"
.venv/bin/python -m pytest
```

The `web` extra is needed for the web tests. Tests that need a node spawn a throwaway
`pepecoind -regtest` in a temporary directory as the current user, and skip when there is none. No
root, and the system's nodes and mainnet data are never touched.

Before changing anything, read `AGENTS.md` and `COLLAB-NOTES.md`.

## Hard rules

1. No floats in the consensus engine. Amounts are integers; prices are exact rationals.
2. An unknown transaction type stops the indexer with a loud error. It is never skipped.
3. Blocks are processed in order, each with an undo journal entry for every mutation.
4. The node never holds a person's keys. Accounts are sealed with their own password and sign in the
   browser. The node's own coins are signed by its Core wallet, or, on a chain with no Core wallet
   here, by the arcade's identity key.
5. Nothing is signed on a real-coin chain before the person has seen its fees and costs and said yes.
   One yes can cover a run, such as a whole collection or a swap's lock. What a node sends later on a
   person's behalf, such as a swap's claim or refund, that person signed in advance.
6. **The Messenger is testnet-only**, enforced in code (`require_messaging_network`).
7. No swap or escrow lock on a chain that does not enforce CHECKLOCKTIMEVERIFY (`require_cltv`).
