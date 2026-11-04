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
  `ddcollateral` for anyone, and `ddmint` for a signed-in user, plus a page at `/m/digidollar/`.
- **A mint needs no Core wallet.** Its inputs are ordinary DGB coins, and DigiByte's mint rules say nothing about
  them. So the module builds the mint itself, exactly as the Core's builder does, from the user's own coins. The arcade
  checks the coins are the user's and hands it to the user's wallet, which signs it like any send (ECDSA).
  - The module names the user's key as the vault's owner. The key comes from the user's wallet, and the arcade passes
    it on only when it hashes to the user's address.
- **Tested:** the builder against BIP-341's own vectors, and DigiByte's script encoding
  (`tests/test_module_packages.py`). A mint against a real DigiByte Core 9.26.7 on regtest, signed by a key no Core
  holds (`tests/test_digidollar_regtest.py`, skipped where `digibyted` is not installed). It passed on 2026-11-04
  against DigiByte Core 9.26.7: the node took the mint into a block, and its supply went from $0 to $100.

## What does not work yet

- **Send and redeem.** Both spend Taproot outputs, and the arcade's wallet (coins.js) signs ECDSA only. They need
  BIP-340 signing in the wallet and Taproot spends in the arcade's builder. Until then **the module refuses to mint on
  mainnet**: the DGB would sit in a vault that the same wallet cannot open.
- **A public test network.** Testnet26 needs a sync of unknown size and test DGB from the project's chat. Regtest has
  everything a test needs, the mock oracle included.

## Decisions for the user and for the operator

1. **Taproot signing in the wallet** (BIP-340 in coins.js, and Taproot inputs in the arcade's builder). Send, redeem
   and mainnet minting all need it. It is wallet code, so it is the operator's to review.
2. **Prepared transactions from modules.** The arcade checks that every coin is the caller's and caps the fee, but a
   module chooses the outputs. The wallet shows the module's summary, the fee and the change. Is that enough, or
   should a module's transaction also list every output in plain words before signing?
3. **Coins that carry an inscription.** The module picks coins from the user's address. The arcade's own sends avoid
   coins with inscriptions on them, but `modhost.prepare` does not check that yet.
4. **Testnet26 or regtest only** for the arcade's DigiByte test chain. A DigiByte test chain in the arcade itself (its
   Params, keys and coin type) is not part of this pilot.
