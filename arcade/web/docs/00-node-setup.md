# Node setup

This page explains what a DogecoinArcade node is made of, what the installer
does, and how to do the same thing by hand. If you just want a working node,
the installer is the quickest route; the manual steps are here so you can see
exactly what ends up on your machine, or reproduce it on a system the
installer does not handle.

## What a node is

A DogecoinArcade node is three things running on one machine:

| Part | What it does |
|---|---|
| **Pepecoin Core, mainnet** | The ledger: tokens, NFTs, inscriptions and coins |
| **Pepecoin Core, testnet** | The Messenger's chain (messaging is testnet-only) |
| **DogecoinArcade** | The indexer and web interface, served at `http://127.0.0.1:8420` |

Pepecoin is a Dogecoin Core fork (Scrypt proof of work, AuxPoW merged mining
since block 42,000). The protocol DogecoinArcade uses is identical on
Dogecoin, so the installer can set up a Dogecoin node instead of, or as well
as, a Pepecoin one. Everything below uses Pepecoin; the Dogecoin differences
are listed at the end.

## Quick install (Linux)

```bash
curl -O https://dogecoinarcade.com/install.py
python3 install.py
```

Check the installer before you run it: the site's homepage publishes the
expected `sha256sum install.py` value next to the command.

Requirements: Python 3.10 or newer (3.12 is what gets tested), `gpg` for the
signature check (strongly recommended), and `git` if you have it (otherwise
the installer fetches a source archive instead).

When it finishes:

```
Start it with:   dogecoinarcade
Then open:       http://127.0.0.1:8420
```

### Installer options

| Flag | Effect |
|---|---|
| `--dry-run` | Show what would happen, change nothing |
| `--coin pepecoin\|dogecoin` | Scripted or partial installs only: install just that coin's nodes. Without it the installer installs both, with no question |
| `--skip-core` | Install only the application, not the node |
| `--force-core` | Install the pinned Core even if one is already installed or running |
| `--no-services` | Do not register services |
| `--no-browser` | Do not open a browser at the end |
| `--no-bootstrap` | Build the index from the chain instead of downloading the latest bootstrap |
| `--update` | Update an existing installation to the latest code |

After installation, `dogecoinarcade-update` (in `~/.local/bin`) does the same
as `--update`; it also accepts `--dry-run` and `--check`.

### Platform support

| Platform | Status |
|---|---|
| Linux x86_64 | tested |
| Linux aarch64 / arm64, armv7l | supported, untested |
| macOS (Intel and Apple silicon) | supported, untested |
| Windows 64-bit | supported, untested (`curl.exe -O ...` then `py install.py` in PowerShell) |

## What the installer does

1. **Checks the machine** — operating system, architecture, Python version.
2. **Downloads Pepecoin Core** for your platform from the official GitHub
   release, unless a Pepecoin Core is already installed (then it is left
   alone; `--force-core` overrides that).
3. **Verifies it** — SHA-256 against the release's `SHA256SUMS.asc` *and*
   against a hash pinned inside the installer, then the GPG signature on
   `SHA256SUMS.asc`. It stops on any mismatch.
4. **Installs the binaries** (`pepecoind`, `pepecoin-cli`, `pepecoin-tx`) into
   `~/.local/bin`. No root needed.
5. **Writes two configs**: mainnet and testnet. An existing config is never
   overwritten.
6. **Registers both nodes as user services** that start at login and keep
   running after logout.
7. **Installs DogecoinArcade** into its own virtual environment under
   `~/.dogecoinarcade`.
8. **Fetches the latest index bootstrap** for each chain, so a new node starts
   near the chain tip instead of indexing from block zero.
9. **Registers the web interface** as a user service, writes the
   `dogecoinarcade` launcher, adds `~/.local/bin` to your PATH if needed, and
   opens the browser.

The nodes still have to sync their block chains after installation. Testnet
is small; mainnet is larger. The application shows progress while they do.

## Doing it by hand

The steps below reproduce the installer on Linux with systemd.

### 1. Download and verify Pepecoin Core

```bash
mkdir -p ~/arcade-install && cd ~/arcade-install
BASE=https://github.com/pepecoinppc/pepecoin/releases/download/v1.1.0
curl -LO $BASE/pepecoin-1.1.0-x86_64-linux-gnu.tar.gz   # pick your architecture
curl -LO $BASE/SHA256SUMS.asc

sha256sum pepecoin-1.1.0-x86_64-linux-gnu.tar.gz
grep pepecoin-1.1.0-x86_64-linux-gnu.tar.gz SHA256SUMS.asc
```

The two hashes must be identical. The installer additionally pins these
hashes:

| Asset | SHA-256 |
|---|---|
| `pepecoin-1.1.0-x86_64-linux-gnu.tar.gz` | `9d7ef948e5726c9941cbc5307b4a0b725edc715bc10ed5515154485faecd710b` |
| `pepecoin-1.1.0-aarch64-linux-gnu.tar.gz` | `c0abd1451e978a171ca5a7d37a57ecb79cc70c6755acf473018acbcd8d31083c` |
| `pepecoin-1.1.0-arm-linux-gnueabihf.tar.gz` | `42394118ecabdb25894a69650f3981fe6ca13eeb23ce7847974437a66a770832` |
| `pepecoin-1.1.0-win64.zip` | `0df90ce84518f1bd827f67fb4900785ce4bfa422304f1a0bc768c0d2489fdf63` |
| `pepecoin-1.1.0-osx-unsigned.dmg` | `9c8cb2c59d96e7db95ca1e6d19ae31de5e81ba326be1b6065882c239a6220c32` |

Then check the signature. The signing key is not on public keyservers, so it
comes from the Pepecoin source tree at the release tag:

```bash
curl -L -o signer.pgp \
  https://raw.githubusercontent.com/pepecoinppc/pepecoin/v1.1.0/contrib/gitian-keys/david2278-key.pgp
export GNUPGHOME=$(mktemp -d)
gpg --batch --import signer.pgp
gpg --batch --verify SHA256SUMS.asc
```

Expect **Good signature** from key
`18250EC9 2E527E97 23E49116 FD415169 D2691927`. Stop if either check fails.

Two things to be aware of:

- **The signing key expired on 2026-01-02.** The v1.1.0 signature was made on
  2024-12-16, while the key was valid, so it still stands; expiry only
  prevents new signatures. gpg will mention the expiry.
- **Trust is single-channel.** The tarball, checksums, signature and signing
  key all come from the same GitHub organisation, and `pepecoin.com` links
  back to the same releases. A compromise there would defeat every check at
  once. If that matters to you, build from source and compare: the release
  binary reports `v1.1.0.0-4fb5a0cd9`, and the `v1.1.0` tag is commit
  `4fb5a0cd930c0df82c88292e973a7b7cfa06c4e8`.

### 2. Install the binaries

```bash
tar xzf pepecoin-1.1.0-x86_64-linux-gnu.tar.gz
mkdir -p ~/.local/bin
install -m 0755 pepecoin-1.1.0/bin/{pepecoind,pepecoin-cli,pepecoin-tx} ~/.local/bin/
```

`pepecoin-qt` and the test binary are not needed on a node.

### 3. Write the configs

Mainnet goes in `~/.pepecoin/pepecoin.conf`:

```ini
# Pepecoin Core -- MAINNET (the ledger: tokens, NFTs and coins)
server=1
txindex=1
prune=0

rpcbind=127.0.0.1
rpcallowip=127.0.0.1

# Cookie authentication: the daemon regenerates .cookie at every start,
# so no password is written to a file. This works because the node runs as you.

dbcache=512

zmqpubhashblock=tcp://127.0.0.1:28332
zmqpubrawblock=tcp://127.0.0.1:28333
zmqpubrawtx=tcp://127.0.0.1:28335
```

Testnet goes in `~/.pepecoin-testnet/pepecoin.conf`: the same file with
`testnet=1` added and the ZMQ ports moved to **28432 / 28433 / 28435**.

Make both files mode `0600`.

### 4. Register the nodes as user services

Create `~/.config/systemd/user/pepecoind-mainnet.service`:

```ini
[Unit]
Description=Pepecoin mainnet Core for DogecoinArcade
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/pepecoind -datadir=%h/.pepecoin
Restart=always
RestartSec=5
TimeoutStopSec=600
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictRealtime=true

[Install]
WantedBy=default.target
```

and `pepecoind-testnet.service`, identical except for
`-datadir=%h/.pepecoin-testnet`. Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now pepecoind-mainnet.service pepecoind-testnet.service
loginctl enable-linger "$USER"     # keep them running after you log out (may need sudo)
```

Why the unit looks like this:

- **`Type=simple`, foreground.** This codebase has no `-daemonwait`, so
  `Type=forking` would race against the PID file.
- **`Restart=always`.** The application restores a wallet by shutting the node
  down cleanly and swapping `wallet.dat`; the node has to come back afterwards.
  An explicit `systemctl stop` is still honoured.
- **`AF_NETLINK` must be allowed.** libzmq opens a netlink socket to enumerate
  interfaces; without it the daemon aborts at startup.
- **It runs as you.** `.cookie` is always written with mode `0600`, so cookie
  authentication only works when the application runs as the same user as the
  daemon.

### 5. Install the application

```bash
git clone https://dogecoinarcade.com/repo ~/.dogecoinarcade/src
python3 -m venv ~/.dogecoinarcade/venv
~/.dogecoinarcade/venv/bin/pip install --upgrade pip
~/.dogecoinarcade/venv/bin/pip install --upgrade "$HOME/.dogecoinarcade/src[web]"
~/.dogecoinarcade/venv/bin/python -c "import arcade"      # should print nothing
```

Without git, download `https://dogecoinarcade.com/source.tar.gz`, check it
against `https://dogecoinarcade.com/source.tar.gz.sha256`, and unpack it into
`~/.dogecoinarcade/src`. `https://dogecoinarcade.com/source.rev` names the
revision it contains.

A dedicated virtual environment keeps the pinned dependencies away from the
rest of your system.

### 6. Fetch the index bootstrap

The application keeps its own index of each chain in
`~/.dogecoinarcade/<test|main>-ledger.sqlite`. Building it from block zero is
slow, so a live node publishes a recent copy. For each of `test` and `main`,
**and only if you have no index yet**:

```bash
NET=main   # then repeat with NET=test
curl -O https://app.dogecoinarcade.com/bootstrap/$NET.json
curl -O https://app.dogecoinarcade.com/bootstrap/$NET.sqlite.gz
cat $NET.json                      # shows bytes, sha256 and height
stat -c %s $NET.sqlite.gz          # must equal "bytes"
sha256sum $NET.sqlite.gz           # must equal "sha256"
gunzip -c $NET.sqlite.gz > ~/.dogecoinarcade/$NET-ledger.sqlite.partial
mv ~/.dogecoinarcade/$NET-ledger.sqlite.partial ~/.dogecoinarcade/$NET-ledger.sqlite
```

The node carries on indexing from the bootstrap's height. An existing index is
never replaced. If you skip this step (or the installer's `--no-bootstrap`),
the node builds its index from the chain itself, which just takes longer.

### 7. Run the web interface

Create `~/.config/systemd/user/arcade-web.service`:

```ini
[Unit]
Description=DogecoinArcade web interface
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/.dogecoinarcade/venv/bin/arcade-web
Restart=always
RestartSec=10
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now arcade-web.service
```

Then open `http://127.0.0.1:8420`.

`arcade-web` binds to `127.0.0.1` by default, on purpose: whoever can reach the
port controls the node's wallet. See [remote-access.md](remote-access.md) for
reaching it from elsewhere.

## Everyday commands

```bash
systemctl --user status pepecoind-mainnet pepecoind-testnet arcade-web
systemctl --user restart pepecoind-mainnet
journalctl --user -u pepecoind-mainnet -f
tail -f ~/.pepecoin/debug.log

pepecoin-cli -datadir=$HOME/.pepecoin getblockchaininfo
pepecoin-cli -datadir=$HOME/.pepecoin-testnet getblockchaininfo
```

## Ports

| | Mainnet | Testnet |
|---|---|---|
| P2P | 33874 | 44874 |
| RPC (loopback only) | 33873 | 44873 |
| ZMQ hashblock / rawblock / rawtx | 28332 / 28333 / 28335 | 28432 / 28433 / 28435 |
| Web interface | 8420 (both chains, one application) | |
| Realtime mesh (TCP) | 8421 (one port for both chains) | |
| STUN for fast games (UDP) | 3478 | |

## The realtime mesh

Game rooms (`arcade.realtime`) run over a mesh of arcade nodes linked to each
other directly, with no server in between. It is on by default, and needs
nothing set up to work: a node dials out to the nodes announced on the chain.

**Letting other nodes reach yours** helps the mesh and makes your node a place
others can join through. Forward TCP **8421** on your router to the machine
running the node, and open it in its firewall:

```bash
sudo ufw allow 8421/tcp
```

That is all. A listening node works out its own public address from what its
Pepecoin peers see, dials itself there to check it really leads back, and then
announces it on the chain. It does that again whenever the address changes,
and once a day to stay fresh. It looks every ten minutes. Nothing is announced
while the port is not forwarded, and the node still plays by dialling out.
An announcement is a small transaction paid from the node's fee address.

| Option | |
|---|---|
| `--no-mesh` | do not join: games on this node play solo |
| `--mesh-port N` | listen on another port (default 8421); `0` only dials out |
| `--mesh-public-port N` | the port the router forwards, if it is not the same number |
| `--mesh-peer IP:PORT` | link to a node directly, such as one on the same network, which the chain cannot carry; may be repeated |
| `--mesh-announce IP:PORT` | announce this address instead of working it out |
| `--mesh-inbound N` | how many other nodes may link to this one (32 by default; a server everyone can reach can take hundreds) |
| `--mesh-tunnel auto/off` | when no address of this node can be proved, open a free Cloudflare quick tunnel (if `cloudflared` is installed) and announce that, so every node can still dial this one (`auto` by default) |
| `--mesh-core on/off` | let every Core that runs on this node peer with the same chain's Core on other arcades through the mesh, so two nodes behind routers still sync with each other (on by default) |
| `--stun-port N` | UDP port of this node's STUN server (default 3478); `0` runs none |

**STUN for fast games.** Fast games connect players' browsers directly to each
other (the fast lane, see the multiplayer guide). Before they can, each browser
asks a STUN server what address the internet sees it at. Every node runs one on
UDP **3478**. Another node checks that it can reach yours before it is offered to
any browser, so forwarding the port is optional:

```bash
sudo ufw allow 3478/udp
```

A STUN server only tells a browser its own address. It carries no game traffic
and keeps nothing. When no node's STUN is known, browsers use a public one
(Cloudflare's). To turn that off, set `"realtime_public_stun": false` in the
node's settings.

The web interface prints the node's mesh id when it starts, and says whether it
is listening.

## Pepecoin Core options that matter

Checked against `pepecoind -help` for v1.1.0:

| Option | Present | Note |
|---|---|---|
| `-txindex` `-prune` `-dbcache` | yes | |
| `-rpcbind` `-rpcallowip` | yes | |
| `-disablewallet` | yes | Do not use it: the application uses the node's wallet |
| `-datacarriersize` `-permitbaremultisig` | yes | Class C and Class B payloads depend on these |
| `-dustlimit` `-harddustlimit` | yes | Inherited from Dogecoin |
| `-rpcserialversion` | yes | |
| `-blockfilterindex` | no | Bitcoin 0.19+ feature; this codebase is 0.13/0.14-era |
| ZMQ `hashblock` `hashtx` `rawblock` `rawtx` | yes | |
| ZMQ `sequence` | no | Reorgs are detected via `previousblockhash` |

## Running Core as a system service instead

If you prefer a system-wide node (binaries in `/usr/local/bin`, config in
`/etc/pepecoin/pepecoin.conf`, datadir `/var/lib/pepecoind`, a dedicated
`pepecoin` user and a hardened system unit), note that cookie authentication
will not work for the application: `.cookie` is always `0600`, and no group
membership, `-rpccookiefile` path or ACL shares it across users. Use `rpcauth`
in the node's config and give the application a config with `rpcuser` /
`rpcpassword` (`arcade-web --ledger-conf ...` / `--msg-conf ...`).

When generating the `rpcauth` line yourself, the HMAC uses the salt as the
**raw ASCII of the hex string**, not decoded hex. Getting this wrong gives a
silent authentication failure.

## Dogecoin

The installer always installs both coins, with no question: Pepecoin Core
(a mainnet node and a testnet node) and Dogecoin Core (a mainnet node only).
They are the arcade's three networks: testnet PEP, mainnet PEP and DOGE.
There is no Dogecoin testnet node; the arcade's testnet is Pepecoin's.

| | Pepecoin | Dogecoin |
|---|---|---|
| Core version | 1.1.0 (`pepecoinppc/pepecoin`) | 1.14.9 (`dogecoin/dogecoin`) |
| Binaries | `pepecoind`, `pepecoin-cli`, `pepecoin-tx` | `dogecoind`, `dogecoin-cli`, `dogecoin-tx` |
| Signing key | `david2278-key.pgp`, `18250EC92E527E9723E49116FD415169D2691927` (expired) | `patricklodder-key.pgp`, `DC6EF4A8BF9F1B1E4DE1EE522D3A345B98D0DC1F` (valid) |
| Datadirs (Linux) | `~/.pepecoin`, `~/.pepecoin-testnet` | `~/.dogecoin` |
| ZMQ base port | mainnet 28332, testnet 28432 | mainnet 28532 |
| Services | `pepecoind-mainnet`, `pepecoind-testnet` | `dogecoind-mainnet` |

The Dogecoin mainnet chain is large: its first sync takes much longer than
Pepecoin's, and the arcade shows its progress.

### The other chains

Litecoin, Litecoin testnet, Bellscoin, Luckycoin, Kotia, Wojakcoin and DigiByte are add-on chains. The
installer does not install their Cores. In **Admin → Chains** each one is run here (with its node on
this machine), borrowed from an arcade that shares its Core, or left off. Dogecoin can be borrowed or
left off too. Pepecoin testnet always runs here, and at least one more Core must run here as well (see
*Shared Cores*). The Bellscoin, Luckycoin, Kotia, Wojakcoin and DigiByte pages give each node's version and
config.

### A node you already run

If a node's config file already exists, the installer adds the settings the
arcade needs that it does not have (`server`, `txindex`, `rpcbind`,
`rpcallowip`, the ZMQ lines), in one block marked
`# --- added by the DogecoinArcade installer ---`. Nothing you wrote is
changed or removed: a setting you already have, whatever its value, stays
yours. A running node picks the additions up when it restarts.

The one setting it will not add for you is `txindex=1` on a node that already
holds blocks: switching it on there stops the node until it is restarted once
with `-reindex`, a full rebuild. The installer says so; add it yourself when
you are ready for that.

## Other platforms

| | Linux | macOS | Windows |
|---|---|---|---|
| Binaries | `~/.local/bin` | `/Applications/Pepecoin-Qt.app` (copied from the `.dmg`) | `%LOCALAPPDATA%\DogecoinArcade\bin` |
| Datadirs | `~/.pepecoin`, `~/.pepecoin-testnet` | `~/Library/Application Support/Pepecoin`, `...Pepecoin-testnet` | `%APPDATA%\Pepecoin`, `%APPDATA%\Pepecoin-testnet` |
| Services | systemd user units | launchd agents | logon tasks, or Startup-folder entries |
