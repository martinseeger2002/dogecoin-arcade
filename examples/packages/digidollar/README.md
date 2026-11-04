# DigiDollar

A node module for ARCADE Lite: DigiByte's stablecoin, read through this arcade's own DigiByte Core, and minted from
the caller's own wallet. It holds no keys.

    arcade-cli checkmodule ~/digidollar     what it adds and what it may ask of the Core
    arcade-cli installmodule ~/digidollar   kept under its id
    arcade-cli install digidollar           switched on: its commands join the console, its page is /m/digidollar/

Commands:
- `ddstats`: the oracle's price, the supply, the collateral and the system's health.
- `ddcollateral <dollars> [tier]`: what a mint locks.
- `ddmint <dollars> [tier]`: a mint, paid from your own DGB. It comes back as a transaction your wallet shows and signs.

**Test chains only.** A mint spends ordinary DGB coins, which the arcade's wallet signs. Redeeming the vault, or
sending the DigiDollars, spends DigiDollar's own Taproot outputs and needs a BIP-340 (Schnorr) signature, which the
arcade's wallet cannot make yet. So on mainnet this module refuses to mint: the DGB would be locked in a vault that
the same wallet could not open.

It needs the arcade's DigiByte Core at 9.26.x, with `txindex=1`.
