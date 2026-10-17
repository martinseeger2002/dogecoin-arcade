# Messaging node

The Messenger runs on **Pepecoin testnet**, always. This page covers the node
side: which binary, how the testnet node is configured next to the mainnet
one, the chain rules the message format relies on, and how to get testnet
coins. The message format itself is in [02-design.md](02-design.md).

## Why testnet

Messaging is testnet-only as a product rule, enforced in code
(`require_messaging_network()` in `arcade/config.py` refuses any other
network). Tokens, NFTs and coins live on mainnet; messages, profiles and the
feed live on testnet. One application talks to both nodes.

The format is still designed to **mainnet** standardness rules, so that it
would relay anywhere: testnet sets `fRequireStandard = false`, and a format
that only worked there would be silently unrelayable on a real network.

## The binary

Pepecoin Core **v1.1.0** (released 2024-12-18) is the current release, and
the same binaries serve mainnet and testnet. Download and verification are
described in [../00-node-setup.md](../00-node-setup.md). In short:

| Check | Expected |
|---|---|
| SHA-256 (Linux x86_64) | `9d7ef948…f710b`, matching `SHA256SUMS.asc` |
| GPG signature | **Good**, key `18250EC9 2E527E97 23E49116 FD415169 D2691927` |
| Signer | fingerprint matches `contrib/gitian-keys/david2278-key.pgp` in the Pepecoin repo |
| Binary ↔ source | `pepecoind` reports `v1.1.0.0-4fb5a0cd9`, the `v1.1.0` tag commit |

Two caveats apply: the **signing key expired 2026-01-02** (the signature
predates expiry, so it is sound), and **trust is single-channel** (tarball,
checksums, signature and key all come from the same GitHub organisation; the
key is not on public keyservers).

Upstream ships builds for every platform the installer supports, so nothing
needs to be built from source:

| Target | Asset |
|---|---|
| Linux x86_64 | `pepecoin-1.1.0-x86_64-linux-gnu.tar.gz` |
| Linux aarch64 | `pepecoin-1.1.0-aarch64-linux-gnu.tar.gz` |
| Linux armhf (32-bit, e.g. Raspberry Pi) | `pepecoin-1.1.0-arm-linux-gnueabihf.tar.gz` |
| Windows 64-bit | `pepecoin-1.1.0-win64.zip` |
| macOS | `pepecoin-1.1.0-osx-unsigned.dmg` |

## Testnet next to mainnet

The installer sets up both nodes side by side. Neither touches the other's
data.

| | Mainnet | **Testnet** |
|---|---|---|
| Service (systemd user unit) | `pepecoind-mainnet.service` | `pepecoind-testnet.service` |
| Datadir (Linux) | `~/.pepecoin` | `~/.pepecoin-testnet` |
| Config | `pepecoin.conf` | `pepecoin.conf` with `testnet=1` |
| Runs as | you | you |
| P2P / RPC | 33874 / 33873 | **44874 / 44873** |
| ZMQ hashblock / rawblock / rawtx | 28332 / 28333 / 28335 | **28432 / 28433 / 28435** |
| RPC auth | cookie | cookie |
| `dbcache` | 512 MiB | 512 MiB |

Both nodes run as your own user because `pepecoind` always writes `.cookie`
with mode `0600`, and no group membership, `-rpccookiefile` path or POSIX ACL
can share a `0600` file across users. Running as the same user as the
application makes cookie authentication work with no stored password at all.

The testnet node needs its wallet enabled: it pays message fees.

Check it:

```bash
systemctl --user status pepecoind-testnet
pepecoin-cli -datadir=$HOME/.pepecoin-testnet getblockchaininfo
pepecoin-cli -datadir=$HOME/.pepecoin-testnet getconnectioncount
```

### Peers and sync

Both testnet DNS seeds are compiled into the binary (`chainparams.cpp`), so
the node finds peers on its own over port 44874. Expect a small number of
peers; testnet is a quiet network. The testnet chain is deeper than its
activity suggests (well past 450,000 headers), so a first sync from scratch
takes a few hours. Mainnet Pepecoin is a young, low-volume chain and syncs
comfortably even on modest hardware.

The index bootstrap the installer downloads (see
[../00-node-setup.md](../00-node-setup.md)) covers the application's own
index; the nodes still download and verify blocks themselves.

## Chain rules the format relies on

All checked in the Pepecoin v1.1.0 source (commit `4fb5a0cd`):

| Rule | Value | Source |
|---|---|---|
| Dogecoin-derived | banner: "Bitcoin Core, Dogecoin Core, and Pepecoin Core" | binary |
| Testnet P2P / RPC ports | 44874 / 44873 | `chainparams.cpp:282`, `chainparamsbase.cpp:48` |
| Testnet addresses start with `n` | `PUBKEY_ADDRESS = 113` | `chainparams.cpp` |
| `OP_RETURN` script size | ≤ 83 bytes (`MAX_OP_RETURN_RELAY`), i.e. 80 bytes of data | `script/standard.h:30` |
| `OP_RETURN` outputs per standard tx | **one** (`multi-op-return` otherwise) | `policy/policy.cpp:115-119` |
| Bare multisig relayed | `DEFAULT_PERMIT_BAREMULTISIG = true`; x-of-3 is standard | `validation.h:143`, `policy/policy.cpp:41-48` |
| Mainnet `fRequireStandard` | `true` | `chainparams.cpp:177` |
| Testnet / regtest `fRequireStandard` | `false` | `chainparams.cpp:309`, `:408` |
| `-acceptnonstdtxn` | toggles it; **refused on mainnet** | `init.cpp:1057`, `:1059` |
| Recommended min fee | 0.01 PEP/kB (`RECOMMENDED_MIN_TX_FEE = COIN/100`) | `policy/policy.h:23` |
| Soft dust limit | 0.01 PEP | `policy/policy.h:70` |
| Hard dust limit (standardness) | 0.001 PEP (`DUST/10`) | `policy/policy.h:81` |
| Mempool ancestor / descendant limit | 25 | `validation.h:74`, `:78` |

RPC names differ from modern Bitcoin Core: use `signrawtransaction` (not
`…withwallet`) and `validateaddress` (not `getaddressinfo`). `generate`,
`generatetoaddress` and `fundrawtransaction` all exist.

There are three dust thresholds, and the wallet's own floor
(`DEFAULT_DISCARD_THRESHOLD`, 0.01 PEP) is the one that binds when funding
through the node; see [02-design.md](02-design.md).

## Carriers

Messages are not squeezed into 80-byte `OP_RETURN` outputs. The application
already has a much larger carrier:

| Carrier | Payload bytes per transaction |
|---|---|
| Class C (`OP_RETURN`) | **72**, after the 4-byte marker and 4-byte type header |
| **Class B (bare multisig)** | **7,646** |

Class B is standard on mainnet, so it is the primary carrier for messages;
Class C is used for small records such as key announcements. Consequences:

- A message of up to about 7,500 characters is **one transaction**, so the
  25-transaction unconfirmed-chain limit is not a constraint for ordinary
  messages.
- **Sender binding comes free.** Class B's obfuscation keystream is seeded with
  the sender's address (`parsing.cpp:108-131`), so a payload only reassembles
  under the correct sender.
- **Obfuscation is not encryption.** The keystream derives from a public
  address, so anyone can strip it. Confidentiality comes entirely from the
  encryption described in the design.
- Class B costs one marker output plus one dust output per two packets. The
  multisig outputs include the sender's redeeming pubkey (`encoding.cpp:37`),
  so that dust stays spendable by the sender rather than burned.

## Testnet coins

Every message is a transaction, so the testnet wallet needs coins.

### From the arcade's faucet

A new account on an arcade is given a one-time gift of test coins so it can
claim its name, post and message straight away. The faucet is testnet-only by
default.

### By mining

Testnet allows minimum-difficulty blocks, which makes CPU mining practical
(`pow.cpp:17-28`):

```cpp
bool AllowMinDifficultyForBlock(...)
{
    if (!params.fPowAllowMinDifficultyBlocks) return false;
    if (pindexLast->nHeight < 1250)            return false;
    // Allow a minimum-difficulty block if more than 2*nTargetSpacing has elapsed
    return (pblock->GetBlockTime() > pindexLast->GetBlockTime() + params.nPowTargetSpacing*2);
}
```

| Heights | Ruleset | Min-difficulty allowed |
|---|---|---|
| 0–999 | base | yes |
| 1000–1249 | digishield | no |
| 1250–41,999 | minDifficulty | yes |
| 42,000– | auxpow | yes (inherited) |

So at current heights, if testnet has been quiet for more than **120
seconds** (2 × 60 s spacing), the next block may be mined at `powLimit`
(`0x00000fff…`), roughly 2²⁰ expected Scrypt hashes. On a single CPU core
that typically takes a few minutes.

The command-line tool wraps this:

```bash
arcade-msg fund
```

It mines **one** block and stops. One block pays 10,000 PEP, enough for tens of
thousands of messages. Coinbase maturity on testnet is **240 blocks**, so
mined coins become spendable about four hours later; mining more does not
shorten that, because maturity is measured in chain height. Start it when you
set up your key, not when you first want to send.

## Pitfalls when setting up nodes by hand

Each of these is handled by the installer:

1. **`RestrictAddressFamilies` must include `AF_NETLINK`**, or libzmq aborts at
   startup.
2. **`-daemonwait` does not exist** in this 0.13/0.14-era codebase. Use
   `Type=simple` and run in the foreground.
3. **The `rpcauth` HMAC uses the salt as the raw ASCII of the hex string**, not
   decoded hex (`httprpc.cpp:118`). Getting this wrong yields a silent
   authentication failure.
4. **`.cookie` is always mode `0600`**, so cookie auth only works if the client
   runs as the same user as the daemon. Choose the service user around that.
5. **The signing key is expired and not on public keyservers.** Fetch it from
   the source tree at the release tag.
6. **Verify before executing**: hash and signature, and stop on failure.

## Platform differences

| | Linux | Windows |
|---|---|---|
| Service manager | systemd user units | logon tasks (no systemd) |
| Default datadir | `~/.pepecoin`, `~/.pepecoin-testnet` | `%APPDATA%\Pepecoin`, `%APPDATA%\Pepecoin-testnet` |
| Binary location | `~/.local/bin` | `%LOCALAPPDATA%\DogecoinArcade\bin` |
