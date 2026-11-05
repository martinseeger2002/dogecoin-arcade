# DigiDollar

A node module for ARCADE Lite: DigiByte's stablecoin, read through this arcade's own DigiByte Core, and minted from
the caller's own wallet. It holds no keys.

    arcade-cli checkmodule ~/digidollar     what it adds and what it may ask of the Core
    arcade-cli installmodule ~/digidollar   kept under its id
    arcade-cli install digidollar           switched on: its commands join the console, its page is /m/digidollar/

Commands:
- `ddstats [main|test]`: the oracle's price, the supply, the collateral and the system's health.
- `ddcollateral <dollars> [tier] [main|test]`: what a mint locks.
- `ddmint <dollars> [tier]`: a mint, paid from your own DGB. It comes back as a transaction your wallet shows and signs.
- `ddvaults`: your DigiDollars and your vaults, and when each vault unlocks.
- `ddredeem <vault>`: once a vault unlocks, burn the DigiDollars it minted and take back all its DGB.

**DigiByte's test chain only (testnet26, `dgbtest` in the arcade).** A mint spends ordinary DGB coins, which the
wallet signs as any send. A redeem spends the vault by its script path and the DigiDollars by their key path, both
Taproot: the wallet signs those with Schnorr (BIP-340), after checking each coin is its own key's from the coin's own
script. That signing is new wallet code, so this module mints and redeems on the test chain until it has been
reviewed. `ddstats main` and `ddcollateral ... main` read mainnet.

A vault is found by its own DigiDollar output: a vault whose dollars were sent on is not listed by `ddvaults`. A
DigiDollar send is not here yet.

It needs the arcade's DigiByte Cores at 9.26.x, with `txindex=1`.
