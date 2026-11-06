# DigiDollar on DigiByte: where it stands, and the Lite pilot

DigiDollar is DigiByte's stablecoin: a person locks DGB in a time-locked vault and mints dollars against it, and gets
the DGB back by burning those dollars after the lock. This note says where it stands (read 2026-11-04 from the
DigiByte Core source at tag v9.26.7 and its release notes), and how the ARCADE Lite node-module pilot uses it
(`examples/packages/digidollar`, design in `PACKAGES.md`).

Sources: R = https://github.com/DigiByte-Core/digibyte, RAW = https://raw.githubusercontent.com/DigiByte-Core/digibyte/v9.26.7

## Which Core, which networks

- **DigiByte Core 9.26.x has it; 8.26.x does not.** v9.26.2 (2026-06-30) was the activation release; the newest is
  v9.26.7 (2026-10-06), the version the arcade already installs (`arcade/core_install.py`). Linux x86_64:
  `R/releases/download/v9.26.7/digibyte-9.26.7-x86_64-linux-gnu.tar.gz`, sha256 `85f1a587…cd8d6` (R/releases).
- **Mainnet: live** since block 23,869,440 (2026-07-17), a buried deployment since v9.26.5 (RAW/src/kernel/chainparams.cpp,
  `DigiDollarHeight`; R/releases/tag/v9.26.5). "Thaw Day" rules (mint volatility, vault health) at 24,490,000.
- **Testnet: a reset chain, "testnet26"** (not testnet4): DigiDollar from block 600, its own folder `testnet26/`, P2P
  12033, RPC 14026. Test DGB comes from the project's chat room. Its size and sync time are not published.
- **Regtest: active from the first block**, with a **mock oracle** (`setmockoracleprice`, regtest only). Oracle and
  P2P gates open at height 650. `-digidollar=1` only makes `txindex=1` required (RAW/src/init.cpp).
- `txindex=1` is required on every network unless the node is pruned. The arcade's Cores already run with it.

## Its RPCs (RAW/src/rpc/digidollar.cpp)

- **Wallet RPCs, needing the Core's own wallet and its keys:** `mintdigidollar`, `senddigidollar`,
  `sendmanydigidollar`, `redeemdigidollar`, `listdigidollarpositions`, `getdigidollarbalance`,
  `getdigidollaraddress`, `getredemptioninfo`, `listdigidollartxs` and the oracle key commands.
- **Node RPCs, no wallet:** `getdigidollarstats`, `getoracleprice`, `getalloracleprices`, `estimatecollateral`,
  `calculatecollateralrequirement`, `getdcamultiplier`, `getdigidollardeploymentinfo`, `getprotectionstatus`,
  `validateddaddress`.
- **There is no unsigned-transaction or PSBT path.** Mint, send and redeem are built, signed and broadcast by the
  Core wallet only (RAW/DIGIDOLLAR_WALLET_INTEGRATION.md). A wallet outside the Core must build the transaction itself.

## How a mint, a send and a redeem work

- **Mint** (transaction version `0x01000770`): a P2TR vault holding the DGB, with an unspendable internal key and two
  script paths that both wait for the lock (normal; and emergency, below 100% system health); a 0-DGB P2TR output
  holding the dollars, keyed to the owner; and an `OP_RETURN "DD" 1 <cents> <lock height> <tier> <owner key>`
  record. Change in plain DGB. Fees at 0.35 DGB a kB, at least 0.1 DGB. The mint must be mined within 100 blocks of
  being built. (RAW/src/digidollar/scripts.cpp, txbuilder.cpp, validation.cpp)
- **Tiers:** 0 is one hour (240 blocks, for testing) at 1000%; 1 to 9 run from 30 days at 500% to ten years at 200%.
  The ratio rises when the system's health falls. Mainnet mints are $100 to $100,000.
- **Send:** spends DigiDollar outputs, which are Taproot key-path outputs: a **BIP-340 Schnorr signature** by the
  owner's key. Fees in DGB.
- **Redeem:** only after the lock, and only the whole vault: burn the minted dollars and the DGB comes back.
  Spends the vault by a Taproot script path, signed by the owner (Schnorr). No partial redeems, no liquidations.
- **Addresses:** `DD…` (mainnet), `TD…` (testnet), `RD…` (regtest): base58 with a two-byte version around a Taproot
  key. Not bech32m.

## The pilot: what works

- **A node module package, `digidollar`,** runs confined in the arcade's home, with no keys: `ddstats` and
  `ddcollateral` for anyone; `ddmint`, `ddvaults` and `ddredeem` for a signed-in user; and a page at `/m/digidollar/`.
- **No Core wallet is needed for a mint or a redeem.** The module builds each exactly as the Core's builder does, from
  the user's own coins, and the arcade checks every coin is the user's before the user's wallet signs.
  - The module names the user's key as the vault's owner. The key comes from the user's wallet, and the arcade passes
    it on only when it hashes to the user's address.
  - **Mint:** ordinary DGB coins in, signed with ECDSA like any send.
  - **Redeem** (version `0x03000770`, lock time = the vault's lock height): the vault first, by its normal script path
    (witness: signature, leaf, control block with the NUMS key and the emergency leaf's hash), then the DigiDollars to
    burn, by their key path (one signature by the tweaked key), then a DGB coin for the fee. Both Taproot inputs wait
    (sequence `0xFFFFFFFE`). Every DGB in the vault comes back; dollars over the vault's come back as DigiDollars
    (`OP_RETURN "DD" 3 <cents>`). Signed with Schnorr (BIP-340, SIGHASH_DEFAULT) by the wallet.
- **The wallet's Schnorr signing (coins.js):** `taprootMine` checks a Taproot coin is this key's from its own script
  (the key path of this key, or a leaf ending in this key and OP_CHECKSIG that opens the coin); `sighashTaproot` works
  out BIP-341's digest from the transaction and every input's value and script; `signSchnorr` signs. `arcade/taproot.py`
  is the arcade's half (the same checks in `modhost.prepare`, and the digests it offers).
- **DigiByte's test chain in the arcade:** `dgbtest` (testnet26), an add-on chain like Litecoin testnet: P2PKH 126,
  P2SH 140, RPC 14026, P2P 12033, its node in `~/.digibyte-testnet/` with its RPC address under `[test]`.
- **Tested:**
  - The builder against BIP-341's vectors, the wallet's signer against BIP-340's first vector, and the browser's
    digests against the arcade's (`tests/test_lite_schnorr_browser.py`).
  - A mint and its redeem against a real DigiByte Core 9.26.7 on regtest, signed by a key no Core holds
    (`tests/test_digidollar_regtest.py`, skipped where `digibyted` is not installed). It passed on 2026-11-04: the node
    took the mint ($0 to $100 of supply), then 340 blocks later the redeem (back to $0, every DGB returned).

## Every step in the window (2026-11-05)

SadFrog: "Make sure every aspect of the Digibyte digidollar has ui for the whole process with the ability to also do it
in the console." The module's tab in the Lite window has a control for each step, drawn by the window from the
manifest's `form` (PACKAGES.md): DigiDollar now, Cost, Gather, Mint, My vaults (with Redeem beside an unlocked vault),
and, for the operator, Mainnet mints. Each runs the line the console takes (`ddstats`, `ddcollateral`, `ddgather`,
`ddmint`, `ddvaults`, `ddredeem`, `ddmainnet`) and shows it under its answer. The module's README has the table.

- **A mint spends at most about eight coins.** At 0.35 DGB a kB each coin adds about 0.05 DGB of fee, and the arcade
  caps a module's fee at 0.5 DGB (`max_fee`); before this, a mint of 9 to 50 coins was refused by the arcade with a
  fee message. `ddgather` sends up to 50 small coins to the caller's own address first.

## Mainnet: the groundwork (2026-11-05)

SadFrog: "lay the ground work for making this process work for digidollar on mainnet with mainnet Digibyte". Every
command takes `main` or `test`. On mainnet:
- **Mints are off until the operator turns them on** (`ddmainnet on yes`, kept in the module's own data folder). The
  module's README lists what to check first.
- **Mainnet's rules** are the module's too: $100 to $100,000, tiers 1 to 9 only, and Core's minting restrictions
  (`estimatecollateral`'s `minting_restricted`, which Thaw Day's rules feed) refuse a mint with Core's reason.
- **Each Core is checked to be the chain it is named** (`getblockchaininfo.chain`), both ways.
- **The wallet's question says "Real DGB"** for a spend on a chain of real coins (`_wallet.html` liteSign).
- **Redeem is open on both chains:** it only gives the owner's DGB back.

What mainnet still needs: the review of the wallet's Schnorr signing; a run of the regtest test against the Core the
arcade runs; a DigiDollar send (`0x02000770`) and the `DD...` address format, so dollars can be paid to someone.

## Send, receive and hold (2026-11-05)

SadFrog: "add capability to send and receive store digidollar test and digidollar". DigiDollar 0.4.0:
- **Receive** (`ddreceive`): the caller's DigiDollar address, `base58check(52 85 | b1 29, Taproot output key)` (DigiByte
  Core base58.cpp `CDigiDollarAddress`), and the DigiDollars they hold, each coin said as from a mint, a send or a
  redeem's change.
- **Send** (`ddsend <address> <dollars>`): as DigiByte Core's `BuildTransferTransaction` builds it (version
  `0x02000770`, lock time 0): the caller's DigiDollar coins by key path first, then a DGB coin for the fee; the payee's
  zero-value P2TR, the DigiDollar change to the caller's own, the DGB change, and `OP_RETURN "DD" 2 <cents>...` in the
  order of the zero-value outputs (validation.cpp `ValidateTransferTransaction`: $1 to $100,000 an output, conservation
  to the cent). Fees as a mint's: 0.35 DGB a kB, at least 0.1 DGB.
- **The wallet's question** (coins.js `verifyOffer`, `ddRecordAmounts`, `ddAddress`) reads the record from the
  transaction itself and says each zero-value Taproot output as "$40.00 in DigiDollars to TD...", or as the signer's own.
- **Vaults are kept** by the module when it prepares a mint (`module-data/digidollar/vaults.json`: the vault's script,
  dollars, lock and tier), so `ddvaults` finds a vault after its own DigiDollar output moved, and `ddredeem` burns any
  of the owner's DigiDollars for it.
- **Mainnet sends** wait for the same operator switch as mints (`ddmainnet`). Reads and `ddreceive main` are open.
- **Tested against a real DigiByte Core 9.26.7 on regtest** (`tests/test_digidollar_regtest.py`): a miner's coins
  gathered, $100 minted, $40 sent to a second key and back, the supply unchanged, Core's `validateddaddress` taking the
  address in regtest's version, and the vault redeemed with the two coins that came back.

## What does not work yet

- **Mainnet mints** wait for the operator's switch (above).
- **A vault minted outside this arcade** whose dollars were sent on is not found by `ddvaults`.

## Decisions for the user and for the operator

1. **Taproot signing in the wallet** (BIP-340 in coins.js, Taproot inputs in `funding` and `modhost.prepare`): written
   and tested on 2026-11-04 for the redeem. It is wallet code, so it is the operator's to review before mainnet.
2. **Prepared transactions from modules.** The arcade checks that every coin is the caller's and caps the fee, but a
   module chooses the outputs. The wallet shows the module's summary, the fee and the change. Is that enough, or
   should a module's transaction also list every output in plain words before signing?
3. **Coins that carry an inscription** (done, 2026-11-05). On a chain with an inscription guard (DigiByte mainnet has
   one; testnet26 has no ord index), the arcade leaves inscribed coins out of a module's `scantxoutset` and refuses a
   prepared spend of one (`modhost.without_inscriptions`, `_refuse_inscribed`), as an ordinary send never spends one.
   DigiAssets are not read by the arcade at all: a coin carrying one is spent as plain DGB by any send.
4. **Testnet26** is the arcade's DigiByte test chain (SadFrog, 2026-11-04). Its oracle is the real 7-of-35, so a mint or
   redeem waits for a signed price as on mainnet. Test DGB comes from DigiByte's community.
