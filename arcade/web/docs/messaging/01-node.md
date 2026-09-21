# Step 1 — Pepecoin node for encrypted messaging

Status: **1.1 complete. 1.2–1.4 await the operator's approval.**
Date: 2026-09-11

Docs for this workstream live in `docs/messaging/` to avoid colliding with the
Ribbit numbering already in `docs/` (00-node-setup, 03-companion-app-design, M0–M2).

---

## 1.1 — Binary

### Machine

The brief again describes an NVIDIA DGX Spark (GB10), aarch64. **This is not that
machine** — same discrepancy as at the start of the Ribbit work, which the operator
resolved then with *"this pc is not the spark it's the mini pc"*.

*Verified on host:* `uname -m` → **x86_64**, product name `a second test machine`.
Intel Celeron N5095A, 4 cores, 15 GiB RAM, one 5400 RPM HDD, 415 GiB free.

**Consequence: we need the x86_64 build, not aarch64.** The aarch64 tarball would
not execute here.

### Release check

*Verified via GitHub API*, 2026-09-11 — the three most recent releases:

| Tag | Published |
|---|---|
| **v1.1.0** | 2024-12-18 |
| v1.0.1 | 2024-05-14 |
| v1.0.0 | 2024-01-30 |

**v1.1.0 is still the latest.** Nothing newer, confirming the brief.

### Already installed and verified

Pepecoin Core v1.1.0 was installed during the Ribbit work and does not need
redoing. From `docs/00-node-setup.md`:

| Check | Result |
|---|---|
| SHA-256 | `9d7ef948…f710b`, matches `SHA256SUMS.asc` |
| GPG signature | **Good** — key `18250EC9 2E527E97 23E49116 FD415169 D2691927` |
| Signer | David Eichel, fingerprint **matches `contrib/gitian-keys/david2278-key.pgp`** in the repo |
| Binary ↔ source | `pepecoind` reports `v1.1.0.0-4fb5a0cd9` = the `v1.1.0` tag commit |
| Installed at | `/usr/local/bin/{pepecoind,pepecoin-cli,pepecoin-tx}` |

Two caveats stand from that verification: the **signing key expired 2026-01-02**
(the signature predates expiry, so it is sound, but nothing has been re-signed),
and **trust is single-channel** — tarball, checksums, signature and key all come
from the same GitHub org, and the key is not on public keyservers.

**No new download was needed, so nothing new to verify.**

---

## Re-verification of the facts in the brief

All *verified in source code* at `/home/you/reference/pepecoin`, tag v1.1.0
(commit `4fb5a0cd`), except where noted.

| Claim | Verdict | Evidence |
|---|---|---|
| pepecoinppc/pepecoin, Dogecoin-derived | ✅ | binary banner: "Bitcoin Core, Dogecoin Core, and Pepecoin Core" |
| Latest release v1.1.0 | ✅ | GitHub API |
| Testnet P2P 44874 | ✅ | `chainparams.cpp:282` |
| Testnet RPC 44873 | ✅ | `chainparamsbase.cpp:48` |
| Testnet addresses start with `n` | ✅ | `PUBKEY_ADDRESS = 113`; encoding a test hash160 gives `nhixVBZHWLHo9YEm6AbZre38n7EaMi9UNw` |
| OP_RETURN script ≤ 83 bytes | ✅ | `script/standard.h:30` — `MAX_OP_RETURN_RELAY = 83` |
| ~80 bytes of data | ✅ | 83 − 1 (OP_RETURN) − 2 (OP_PUSHDATA1 + len) = **80** |
| **Only one OP_RETURN per standard tx** | ✅ | `policy/policy.cpp:115-119` — `if (nDataOut > 1) reason = "multi-op-return"` |
| Mainnet `fRequireStandard = true` | ✅ | `chainparams.cpp:177` |
| Testnet / regtest `fRequireStandard = false` | ✅ | `chainparams.cpp:309`, `:408` |
| `-acceptnonstdtxn` toggles it | ✅ | `init.cpp:1057`, and it is **refused on mainnet** (`init.cpp:1059`) |
| `signrawtransaction`, not `…withwallet` | ✅ | present / absent in `src/rpc`, `src/wallet/rpcwallet.cpp` |
| `validateaddress`, not `getaddressinfo` | ✅ | same |
| `generate` and `generatetoaddress` both exist | ✅ | same |
| `fundrawtransaction` exists | ✅ | same |
| Recommended min fee 0.01 PEP/kB | ✅ | `policy/policy.h:23` — `RECOMMENDED_MIN_TX_FEE = COIN/100` |
| Dust limit 0.01 PEP | ✅ | `policy/policy.h:70` — and note the **hard** dust limit is `DUST/10` = **0.001 PEP** (`:81`), which is what standardness actually enforces |
| Mempool ancestor/descendant limits 25 | ✅ | `validation.h:74`, `:78` |
| aarch64 gitian target exists | ✅ | release assets include `pepecoin-1.1.0-aarch64-linux-gnu.tar.gz` — irrelevant here, this host is x86_64 |

**Every claim in the brief holds.** One refinement worth carrying into the design:
there are *two* dust limits, and the one that decides standardness is the **hard**
limit of 0.001 PEP, not the 0.01 PEP soft limit.

---

## ⚠️ Finding that changes Step 2 materially

The brief assumes messages are chunked across **80-byte OP_RETURN** outputs, with
transactions chained through change outputs and a 25-transaction ceiling.

**Ribbit already has a much larger carrier.** From M1, *verified by test*:

| Carrier | Payload bytes per transaction |
|---|---|
| Class C (OP_RETURN) | **72** after the 4-byte marker and 4-byte type header |
| **Class B (bare multisig)** | **7,646** |

Class B is standard on Pepecoin mainnet — *verified in source code*:
`DEFAULT_PERMIT_BAREMULTISIG = true` (`validation.h:143`) and x-of-3 bare multisig
is standard (`policy/policy.cpp:41-48`).

### What that does to the chunking problem

Sealed-box overhead is 48 bytes (32-byte ephemeral public key + 16-byte MAC).
Assuming a 12-byte chunk header, OP_RETURN carries ~68 ciphertext bytes per
transaction:

| Message | Ciphertext | OP_RETURN path | **Class B path** |
|---|---|---|---|
| 100 chars | 148 B | 3 transactions | **1** |
| 500 chars | 548 B | 9 transactions | **1** |
| 1,000 chars | 1,048 B | **16 transactions** | **1** |
| 7,598 chars | 7,646 B | 113 tx — **over the 25 limit** | **1** |

*(assumption — exact figures recomputed in Step 2 once the header is fixed)*

Class B also appears **cheaper**, not merely fewer transactions: 1,048 bytes needs
35 packets → 18 multisig outputs → roughly a 2.2 KB transaction ≈ 0.022 PEP at
0.01 PEP/kB, against 16 OP_RETURN transactions of ~250 B each ≈ 0.04 PEP.

### Other consequences

- **The 25-transaction unconfirmed-chain limit stops being a constraint** for any
  message under ~7.6 KB, so change-chaining is unnecessary for ordering.
- **Sender binding comes free.** Class B's obfuscation keystream is seeded with
  the *sender's address* (`parsing.cpp:108-131`), so a payload only reassembles
  under the correct sender — the property the brief wanted change-chaining for.
- **Obfuscation is not encryption.** The Class B keystream is derived from a
  public address, so anyone can strip it. That is fine: our payload is already
  sealed-box ciphertext. It must be stated plainly in the threat model so nobody
  mistakes obfuscation for confidentiality.
- Class B costs **one extra marker output plus one dust output per two packets**,
  all at the 0.001 PEP hard dust limit, and every one of those outputs is
  **spendable by the sender** (the redeeming pubkey is included, `encoding.cpp:37`),
  so the dust is recoverable rather than burned.

**Recommendation:** Step 2 should design for Class B as the primary carrier and
keep Class C for short messages that fit in 72 bytes, rather than treating
80-byte OP_RETURN chunking as the only option. Both remain mainnet-standard.

---

## Open question before 1.2

Does this messaging feature live **inside Ribbit** (its own message type, sharing
the codec, indexer and database) or as a **separate tool**? The operator's instruction
was *"Add encrypted messaging to Ribbit"*, which suggests the former, but the
brief is written standalone. This decides whether messages get a Ribbit message
type or their own magic bytes.

---

## 1.2 — Testnet service: **done**

Installed alongside the mainnet node. Neither touches the other's data.

| | Mainnet | **Testnet** |
|---|---|---|
| Unit | `pepecoind.service` | `pepecoind-testnet.service` |
| Datadir | `/var/lib/pepecoind` | `/home/you/.pepecoin-testnet` |
| Runs as | `pepecoin` | **`robin`** |
| P2P / RPC | 33874 / 33873 | **44874 / 44873** |
| ZMQ | 28332/3/5 | **28432/3/5** |
| Auth | rpcauth | **cookie** |
| Wallet | disabled | **enabled** |
| dbcache | 4096 MiB | 512 MiB |

*Verified on host:* both services `active`, all six ZMQ ports listening, no collisions.

**Why testnet runs as `robin` and mainnet does not.** The brief requires cookie
auth. `pepecoind` always writes `.cookie` at mode 0600, and no group membership,
`-rpccookiefile` path or POSIX ACL can share a 0600 file across users — we proved
this the hard way on mainnet, which is why mainnet ended up on `rpcauth` instead.
Running the testnet daemon as `robin` makes cookie auth work with no credential
sharing at all. Testnet coins have no value, so nothing is at risk.

The `AF_NETLINK` fix was carried into this unit from the start, so it did not
repeat the mainnet ZMQ crash loop.

### Mainnet, incidentally: fully synced

Height **1,204,728**, tip block 48 seconds old, 8 peers, **12.7 GB on disk**.
The whole chain synced overnight on the 5400 rpm disk. Pepecoin is a young,
low-volume chain — far smaller than the Litecoin figures that framed the original
storage planning.

---

## 1.3 — Peers and sync: **confirmed**

Both testnet DNS seeds are compiled in (`chainparams.cpp:95-96`) and resolve;
2 of 4 seed IPs accepted connections on 44874. The node has **2 peers** and is
syncing at **~24 blocks/sec**.

The testnet chain is deeper than it first appeared — headers were still climbing
past 450,000 at the time of writing. At the observed rate this is a few hours.
A background watcher is running.

---

## 1.4 — Testnet coins: **CPU mining is realistic; no faucet needed**

### The decisive rule

*Verified in source code*, `pow.cpp:17-28` and `chainparams.cpp` (testnet):

```cpp
bool AllowMinDifficultyForBlock(...)
{
    if (!params.fPowAllowMinDifficultyBlocks) return false;
    if (pindexLast->nHeight < 1250)            return false;
    // Allow a minimum-difficulty block if more than 2*nTargetSpacing has elapsed
    return (pblock->GetBlockTime() > pindexLast->GetBlockTime() + params.nPowTargetSpacing*2);
}
```

The testnet ruleset progression is:

| Heights | Ruleset | Min-difficulty allowed |
|---|---|---|
| 0–999 | base | true |
| 1000–1249 | digishield | **false** |
| 1250–41,999 | minDifficulty | **true** |
| 42,000– | auxpow (`= minDifficultyConsensus`) | **true** (inherited) |

So at present heights the rule **is** active: if testnet has been quiet for more
than **120 seconds** (2 × 60 s spacing), the next block may be mined at
`powLimit` = `0x00000fff…` — roughly 2²⁰ expected Scrypt hashes.

**A 4-core Celeron N5095A should find such a block in on the order of a minute**
(*assumption* — order-of-magnitude only, to be measured with `generate` once the
sync completes). Coinbase maturity on testnet is 240 blocks
(`digishieldConsensus.nCoinbaseMaturity = 240`), so mined coins are spendable
after ~240 minutes of chain time, or immediately-ish if we mine a run of blocks.

**Conclusion: a faucet is a convenience, not a dependency.** We can mint our own
testnet coins with the built-in `generate` RPC.

### Faucets, for completeness

`pepeblocks.com/faucet` exists and `testnet.pepeblocks.com` resolves and returns
HTTP 200, but both are client-rendered SvelteKit apps, so it could not be
confirmed from the HTML whether the faucet dispenses **testnet** coins or only
mainnet. Worth a manual try once we have a testnet address; not worth blocking on.

---

## Future requirement: cross-platform installer

2026-09-11: *"Eventually I want an installer that will automatically set
up core and testnet. I will want one for Windows and Linux and an ARM variant."*

Recorded as a requirement, **not built yet**. Notes for when it is:

### Upstream ships the binaries we would need

*Verified via GitHub API*, v1.1.0 release assets:

| Target | Asset | Relevant |
|---|---|---|
| Linux x86_64 | `pepecoin-1.1.0-x86_64-linux-gnu.tar.gz` | ✅ |
| **Linux aarch64** | `pepecoin-1.1.0-aarch64-linux-gnu.tar.gz` | ✅ ARM variant |
| Linux armhf (32-bit) | `pepecoin-1.1.0-arm-linux-gnueabihf.tar.gz` | ✅ Raspberry Pi 32-bit |
| **Windows 64** | `pepecoin-1.1.0-win64.zip`, `…-win64-setup-unsigned.exe` | ✅ |
| macOS | `pepecoin-1.1.0-osx-unsigned.dmg` | (not requested) |

So the installer never needs to build from source on any requested platform.

### What differs per platform, and what does not

**Same everywhere:** download URL pattern, SHA-256 verification, GPG verification
against `contrib/gitian-keys/david2278-key.pgp`, and the testnet config file
contents.

**Differs:**

| | Linux | Windows |
|---|---|---|
| Service manager | systemd unit | Windows Service or Scheduled Task (**no systemd**) |
| Default datadir | `~/.pepecoin` | `%APPDATA%\Pepecoin` |
| Binary location | `/usr/local/bin` | `%ProgramFiles%\Pepecoin` |
| Privilege model | `sudo` | UAC elevation |
| Path separator, line endings | — | both differ |

### Lessons from doing it by hand that the installer must encode

Every one of these cost us a debugging cycle in this session:

1. **`RestrictAddressFamilies` must include `AF_NETLINK`**, or libzmq aborts at
   startup with `ip_resolver.cpp:542`.
2. **`-daemonwait` does not exist** in this 0.13/0.14-era codebase. Use
   `Type=simple` and run in the foreground.
3. **The rpcauth HMAC uses the salt as raw ASCII of the hex string**, not decoded
   hex (`httprpc.cpp:118`). Getting this wrong yields a silent auth failure.
4. **`.cookie` is always mode 0600**, so cookie auth only works if the client runs
   as the same user as the daemon. Plan the service user around that, not after it.
5. **The signing key is expired**, and is not on public keyservers — the installer
   must ship or fetch it from the repo and should warn rather than fail.
6. **Verify before executing**, always: hash *and* signature, and stop on failure.

### Open questions for that work

- Is the installer's job only Core + testnet, or DogecoinArcade itself too?
- Windows service, or a tray app / manual launch?
- Does it need to handle Dogecoin as well as Pepecoin, given D-011?
