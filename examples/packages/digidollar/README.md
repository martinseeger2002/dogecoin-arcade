# DigiDollar

A node module for ARCADE Lite: DigiByte's stablecoin, read through this arcade's own DigiByte Cores, and minted and
redeemed from the caller's own wallet. It holds no keys.

    arcade-cli checkmodule ~/digidollar     what it adds and what it may ask of the Core
    arcade-cli installmodule ~/digidollar   kept under its id
    arcade-cli install digidollar           switched on: its tab, its commands in the console, its page

## Every step, by its control or by its line

Its tab in the arcade's window has a control for each step, above the module's own page. Each runs the very line the
console takes, and shows that line under its answer:

| Step | In the window | In the console |
|---|---|---|
| The price, supply and health | DigiDollar now | `ddstats [main\|test]` |
| What a mint locks | Cost | `ddcollateral <dollars> [tier] [main\|test]` |
| Many small coins into one | Gather | `ddgather [main\|test]` |
| Mint | Mint… | `ddmint <dollars> [tier] [main\|test]` |
| Your vaults, and when each unlocks | My vaults | `ddvaults [main\|test]` |
| Take the DGB back | Redeem, beside an unlocked vault | `ddredeem <vault> [main\|test]` |
| Mainnet mints on or off (operator) | Mainnet mints | `ddmainnet [status\|on\|off] [yes]` |

The chain is the test chain (testnet26, `dgbtest`) unless `main` is said. A mint or a redeem comes back as a
transaction your wallet shows, every payment listed, and signs.

- **Mint** spends ordinary DGB coins, signed as any send. At DigiDollar's fee (0.35 DGB a kB) each coin adds about
  0.05 DGB, and the arcade lets the module leave at most 0.5 DGB to fees, so a mint spends at most about eight coins.
  A wallet of many small coins (a miner's) is gathered first.
- **Gather** is an ordinary send to yourself of up to 50 coins, at twice DigiByte Core's relay fee (about 0.015 DGB
  for 50).
- **Redeem** spends the vault by its script path and the DigiDollars by their key path, both Taproot: the wallet signs
  those with Schnorr (BIP-340), after checking each coin is its own key's from the coin's own script.

## Mainnet

Reads (`ddstats main`, `ddcollateral ... main`) are open. **Mints on mainnet are off until the arcade's operator turns
them on** (`ddmainnet on yes`, or the Mainnet mints control), because a vault holds real DGB until its time is up, 30
days at the least. Before turning them on, the operator checks:

1. **The wallet's Schnorr signing** (`coins.js` taprootMine, sighashTaproot, signSchnorr; `arcade/taproot.py`;
   `modhost.prepare`) has been reviewed. A mistake there makes a redeem fail, not lose coins; the vault waits.
2. **The vault's scripts** match DigiByte Core's own. `tests/test_digidollar_regtest.py` mints and redeems against a
   real DigiByte Core; run it against the Core version the arcade runs.
3. **This arcade runs its own DigiByte mainnet node**, synced, with `txindex=1`. A borrowed one does not answer
   `scantxoutset`.
4. **Minting is open on the chain:** `ddstats main` says `minting: open`. Since Thaw Day (block 24,490,000) Core
   restricts mints while the price moves fast or the system's health is low, and says why.

Mainnet's own rules hold either way: $100 to $100,000 a mint, and tiers 1 to 9 (30 days to ten years); tier 0, one
hour, is the test chain's. Redeeming a mainnet vault is never switched off: it only gives the owner's DGB back. The
wallet's question says "Real DGB" for a mainnet spend.

A vault is found by its own DigiDollar output: a vault whose dollars were sent on is not listed by `ddvaults`. A
DigiDollar send is not here yet. It needs the arcade's DigiByte Cores at 9.26.x, with `txindex=1`.
