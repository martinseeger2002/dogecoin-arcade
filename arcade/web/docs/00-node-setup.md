# Phase 0 — Node setup

Status: **machine survey done. Target chain changed to Pepecoin (see docs/DECISIONS.md D-002).**
Litecoin sections below are retained as historical record and are superseded.
Last updated: 2026-09-10

---

## Step 1 — Machine survey

### Which machine is this?

The project brief describes an NVIDIA DGX Spark (GB10), DGX OS / Ubuntu 24.04, ARM64.
**This machine is not that machine.** the operator confirmed mid-session: *"this pc is not the spark it's the mini pc"*.

Everything below was measured on this host, not assumed.

| Property | Value | How verified |
|---|---|---|
| Hostname | `robin-a second test machine` | `uname -a` — *verified on host* |
| Product name | `a second test machine` (generic mini PC) | `/sys/devices/virtual/dmi/id/product_name` — *verified on host* |
| Architecture | **x86_64 / amd64** | `uname -m`, `dpkg --print-architecture` — *verified on host* |
| OS | **Linux Mint 22.3 "Zena"** (`ID_LIKE=ubuntu debian`, `UBUNTU_CODENAME=noble`) | `/etc/os-release` — *verified on host* |
| Kernel | `6.17.0-29-generic #29~24.04.1-Ubuntu` | `uname -a` — *verified on host* |
| CPU | **Intel Celeron N5095A @ 2.00 GHz**, 4 cores / 4 threads, 1 socket (Jasper Lake) | `lscpu` — *verified on host* |
| RAM | **15 GiB** total, ~13 GiB available, 2 GiB swap | `free -h` — *verified on host* |
| GPU | Intel UHD (Jasper Lake). No NVIDIA GPU, no `nvidia-smi`. | `lspci`, `nvidia-smi` — *verified on host* |

> Note: it is an Ubuntu 24.04 (noble) userland under Mint branding, so Ubuntu-targeted
> binaries and systemd units apply normally.

### Storage — the constraint that matters

**There is exactly one drive.**

| Device | Size | Type | Model | Mount | Free |
|---|---|---|---|---|---|
| `sda1` | 512 M | vfat | — | `/boot/efi` | 505 M |
| `sda2` | 465.3 G | ext4 | — | `/` | **415 G** |

- `/sys/block/sda/queue/rotational` = **1** → *verified on host*: this is a **spinning hard disk**, not an SSD.
- Model `ST9500325AS` = Seagate Momentus 5400.6, 500 GB, **5400 RPM 2.5" SATA laptop drive** — *stated in Seagate product documentation* (model-number identification; the on-host evidence is the model string + `rotational=1`).
- Measured sequential write: **63.1 MB/s** (`dd bs=1M count=512 conv=fdatasync`) — *verified on host*. The read figure was served from page cache and is not meaningful; I could not drop caches without root.

### Current Litecoin mainnet storage requirement

| Component | Size | Basis |
|---|---|---|
| Raw block data (`blocks/`) | **~216.8 GiB** (232,812,927,925 bytes) | *verified via Blockchair API* `api.blockchair.com/litecoin/stats`, 2026-09-11, at height 3,175,747 |
| Undo/rev files | ~30 GiB | **assumption** — undo data historically runs ~15 % of block data in Bitcoin Core–derived nodes |
| `chainstate/` (UTXO set) | ~4–6 GiB | **assumption** |
| `indexes/txindex/` | ~18–25 GiB | **assumption** — Litecoin has 413,359,859 txs (*verified via Blockchair*); at ~45–60 bytes/entry incl. LevelDB overhead |
| `indexes/blockfilter/` | ~5–8 GiB | **assumption** — see note below, this is **on by default** in Litecoin Core |
| **Total with `txindex=1`** | **~275–290 GiB** | sum of the above |

Chain stats at survey time (*verified via Blockchair API*):
height **3,175,747**, 413,359,859 transactions, 855 reachable nodes.

**Verdict on capacity:** 415 GiB free vs ~275–290 GiB needed → it fits, with roughly
125–140 GiB of headroom. Capacity is **not** the problem. Speed is.

### Finding: `blockfilterindex` defaults to ON in Litecoin Core

*Verified on host* from `litecoind -help` in v0.21.5.6:

```
-blockfilterindex=<type>
     Maintain an index of compact filters by block (default: 1, values: basic).
```

This differs from Bitcoin Core, where the default is `0`. It is presumably there to
serve MWEB/light clients. It costs disk and IBD time we do not need for this project,
so I recommend `blockfilterindex=0` unless we later want BIP157/158 support.

### Finding: `rpcserialversion` defaults to 2, not 1

*Verified on host* from `litecoind -help` in v0.21.5.6:

```
-rpcserialversion
     Sets the serialization of raw transaction or block hex returned in
     non-verbose mode, non-segwit(0) or segwit(1) (default: 2)
```

The help text documents only 0 and 1 but the **default is 2** — an undocumented third
value that does not exist in Bitcoin Core. This is a strong early signal that MWEB
added a serialization level. Full analysis is Phase 0 step 5; flagged here because it
directly affects how the Phase 2 extractor parses raw blocks.

### Pre-existing install (disclosure)

Before this project brief was given, earlier in the same session I had already
installed Litecoin Core v0.21.5.6 on this host at the operator's request:

- x86_64 tarball from the official GitHub release, SHA-256 **matched**
  (`3c0a217651a431ef446641669a0b74ce7dbcd9b9ed1a118fc830b8f6779ee83f`),
  GPG signature on `SHA256SUMS.asc` **good** (David Burkett, key
  `D356 21D5 3A1C C6A3 4567 58D0 3620 E9D3 87E5 5666`).
- Binaries in `~/.local/bin`, config at `~/.litecoin/litecoin.conf` with
  `txindex=1`, `daemon=1`, `server=1`, RPC bound to 127.0.0.1, cookie auth.
- Daemon started; at the time of writing it is at block 0 / ~262k headers,
  3 peers, 288 bytes on disk — i.e. **essentially nothing has been downloaded yet.**

This does not satisfy the Phase 0 requirements (`/usr/local/bin`, systemd unit,
`dbcache`, ZMQ, `disablewallet`), so the plan is to stop it, migrate, and restart
cleanly. Discarding the current state costs nothing.

### Release check

*Verified via GitHub API* (`/repos/litecoin-project/litecoin/releases`), 2026-09-10:

| Tag | Published | Prerelease |
|---|---|---|
| **v0.21.5.6** | 2026-08-02 | no |
| v0.21.5.5 | 2026-05-06 | no |
| v0.21.5.4 | 2026-04-26 | no |
| v0.21.4 | 2024-11-07 | no |

**v0.21.5.6 is the newest release; nothing newer exists**, including prereleases.
This confirms the operator's expectation.

---

## Open questions for the operator

See the session summary. Blocking item: the 5400 RPM HDD.

---

## Open decision: Pepecoin instead of Litecoin?

Raised by the operator mid-Phase-0, 2026-09-10.

### Findings

| Claim | Evidence | Label |
|---|---|---|
| Pepecoin (PEP) is a **Dogecoin Core** fork, Scrypt PoW, AuxPoW merged mining since block 42,000 | `github.com/pepecoinppc/pepecoin` README; ViaBTC writeup | *stated in documentation* |
| Latest release is **Pepecoin Core v1.1.0, published 2024-12-18** — ~21 months stale as of today | *verified via GitHub API* `/repos/pepecoinppc/pepecoin/releases/latest` | verified |
| x86_64 Linux build exists (`pepecoin-1.1.0-x86_64-linux-gnu.tar.gz`), with `SHA256SUMS.asc` | *verified via GitHub API* release asset list | verified |
| Windows/macOS builds are marked `-unsigned` | *verified via GitHub API* asset names | verified |
| **No Omni Layer implementation exists on Pepecoin.** Omni Core forks Bitcoin Core; OmniLite forks Litecoin. "OMNIPEPE" in search results is an unrelated Solana memecoin. | web search; `github.com/OmniLayer/omnicore` is "forked from bitcoin/bitcoin" | *verified by absence* — no port found; would need deeper search to prove a negative |

### Analysis

**Phases 0-2 cannot move to Pepecoin.** the operator's files were inscribed via Omni **on Litecoin**
in 2021-2023. Those bytes exist in Litecoin blocks and nowhere else. Recovering them requires
a Litecoin node reading Litecoin history. No other chain substitutes, at any price.

**Phase 3 cannot move to Pepecoin either,** as currently scoped. The companion app is an *Omni*
app — indexer, consensus engine, consensus hash, property/NFT rules. On Pepecoin there is no
Omni protocol to index, so there would be nothing for it to do.

**Phase 4 is where Pepecoin genuinely fits,** and the fit is good:

- Because Pepecoin is Dogecoin-derived, **Doginals-style inscriptions transfer almost directly** —
  Dogecoin-lineage chains inscribe via P2SH redeem-script envelopes rather than Taproot witness
  envelopes. The operator already has `doginals` and `dogcoin_ordinal_auto_inscriber` repos, so the
  tooling is largely written. (*assumption* pending Phase 4 verification of Pepecoin's script rules.)
- The Pepecoin chain is small and young (launched 2024), so it would sync quickly even on this
  5400 RPM disk — unlike Litecoin's 217 GiB.
- Cost per KB on a low-value chain is far below Litecoin's.

**Risks if Pepecoin is chosen for Phase 4:** a ~21-month-stale node release, a much smaller
network (fewer nodes = weaker permanence guarantee for inscribed data), and unsigned
binaries on some platforms.

### Status: awaiting the operator's answer. Litecoin install is staged and ready to proceed.

---
---

# PEPECOIN CORE INSTALL (current target)

## Software verification — Pepecoin Core v1.1.0

Downloaded from the official GitHub release
`github.com/pepecoinppc/pepecoin/releases/download/v1.1.0/`.

| Check | Result | Label |
|---|---|---|
| SHA-256 of `pepecoin-1.1.0-x86_64-linux-gnu.tar.gz` | `9d7ef948e5726c9941cbc5307b4a0b725edc715bc10ed5515154485faecd710b` | *verified on host* |
| Matches entry in `SHA256SUMS.asc` | ✅ **identical** | *verified on host* |
| GPG signature on `SHA256SUMS.asc` | ✅ **Good signature**, RSA key `18250EC9 2E527E97 23E49116 FD415169 D2691927` | *verified on host* |
| Signer identity | David Eichel (email redacted; identity is pinned by the fingerprint below) | *verified on host* |
| Key provenance | ✅ fingerprint **matches `contrib/gitian-keys/david2278-key.pgp`** committed in the Pepecoin repo at tag v1.1.0 | *verified in source code* |
| Binary ↔ source correspondence | `pepecoind --version` reports build `v1.1.0.0-4fb5a0cd9`; the cloned `v1.1.0` tag is commit `4fb5a0cd930c0df82c88292e973a7b7cfa06c4e8` — **same commit** | *verified on host* |

### ⚠️ Two caveats, stated honestly

1. **The signing key expired 2026-01-02.** The signature was made 2024-12-16, i.e. while the
   key was still valid, so the signature itself is sound — expiry prevents *new* signatures,
   it does not retroactively invalidate old ones. But nobody has re-signed or rotated since.

2. **Single-channel trust.** With Litecoin we had two independent distribution channels
   (`download.litecoin.org` and GitHub) publishing the same hash, plus a key on public
   keyservers. For Pepecoin, the tarball, the checksums, the signature, **and** the signing
   key all come from the same GitHub organisation, and the key is **not on
   `keyserver.ubuntu.com`** (*verified on host*: "keyserver receive failed: No data").
   `pepecoin.com` only links back to the same GitHub releases (*verified on host*).
   So the trust root is "whoever controls the pepecoinppc GitHub org". A compromise there
   would defeat every check simultaneously.

   This is weaker than Litecoin's chain of custody. It is not a reason to stop — it is the
   normal state of a small project — but it should be a conscious acceptance, not an
   oversight. Mitigation available if wanted: build from source and compare against the
   published binary.

## Feature availability check (*verified on host*, `pepecoind -help`)

| Option | Present | Note |
|---|---|---|
| `-txindex` `-prune` `-dbcache` | ✅ | |
| `-rpcbind` `-rpcallowip` | ✅ | |
| `-disablewallet` | ✅ | |
| `-datacarriersize` `-permitbaremultisig` | ✅ | Omni Class C / Class B depend on these |
| `-dustlimit` `-harddustlimit` | ✅ | Pepecoin-specific, inherited from Dogecoin |
| `-rpcserialversion` | ✅ | |
| `-blockfilterindex` | ❌ | Bitcoin 0.19+ feature; this codebase is 0.13-era |
| ZMQ: `hashblock` `hashtx` `rawblock` `rawtx` | ✅ | compiled in |
| ZMQ: `sequence` | ❌ | **reorgs must be detected via `previousblockhash`** |

## Install layout

| Path | Contents |
|---|---|
| `/usr/local/bin/pepecoind`, `pepecoin-cli`, `pepecoin-tx` | binaries, root-owned, 0755 |
| `/etc/pepecoin/pepecoin.conf` | config, `pepecoin:pepecoin` 0640 |
| `/var/lib/pepecoind/` | datadir, `pepecoin:pepecoin` 0710 |
| `/etc/systemd/system/pepecoind.service` | service unit |

Runs as a dedicated `pepecoin` system user with a hardened unit (`ProtectSystem=full`,
`NoNewPrivileges`, `PrivateDevices`, `MemoryDenyWriteExecute`, restricted syscalls and
address families). `Restart=on-failure`, `RestartSec=30`. `robin` is added to the
`pepecoin` group so tooling can read the RPC cookie without root.

`pepecoin-qt` and `test_pepecoin` are deliberately **not** installed to `/usr/local/bin`
(no GUI needed on a headless node; the test binary stays in the staging dir).

## Service commands

```bash
sudo systemctl status pepecoind          # state
sudo systemctl start|stop|restart pepecoind
sudo systemctl enable|disable pepecoind  # start at boot
sudo journalctl -u pepecoind -f          # service log
sudo tail -f /var/lib/pepecoind/debug.log

pepecoin-cli -conf=/etc/pepecoin/pepecoin.conf -datadir=/var/lib/pepecoind getblockchaininfo
```
