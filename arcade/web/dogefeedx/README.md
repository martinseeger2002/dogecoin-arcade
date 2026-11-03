# DogeFeedX

The dock is the navigation. Each window is one module. What a wallet **owns**
is read from this Dogecoin node and this arcade's indexes. [dogefeed.app](https://dogefeed.app)
is read-only, and only to enrich a label: a collection's public name, cover,
and floor, or a public social page. No module asks dogefeed.app, dogecoinnames,
or explorer.dogesoft.io who holds a coin, a doginal, a DRC-20, or a name.

Ownership indexes:

| Owns | Where |
|---|---|
| Balance | The watcher's coin index (`arcade/utxos.py` on the Dogecoin ledger). The sum only. Outpoints are not returned. While this Core is behind, or the chain is borrowed, the sum is the sharer's lent set (`utxoset.LentSet`, the same ask as `watcher._utxoset_lent_pass`), then `arcade_coinscan` when that chain has no set. |
| Doginals | The ledger inscription index (`held_collections` / `held_pieces`). Pictures are this node's `/content/`. While behind, inscriptions on the lent coins come from `DoginalIndex.lookup` (`what: on`). |
| DRC-20 | This arcade's ord index (`DoginalIndex.balances`). While behind, `DoginalIndex.lookup` (`what: balances`), the most complete linked walker. |
| Names | This arcade's ord index (`DoginalIndex.names_on` for coins the watcher already holds). While behind, the same question through `lookup`. The claim's outpoint is dropped. |
| Which node | Holdings carry `reading.via` (`local` or `peer`), `reading.node`, and `reading.sync` from `core_catchup.say` (this Core, not the peer's tip). Wallet, Files, NFTs, and Profile show that line. |

A pasted address that this node is not watching comes back empty. Import it
watch-only on the node and let the indexes catch up. No key is asked, and
nothing is written to dogefeed.app.

## Per-module data sources

| Dock module | What it shows | Source of truth |
|---|---|---|
| Home | Public social feed, and a local composer | dogefeed.app `/api/feed` is read-only. The composer (image, meme, emoji, paywall lock, `/send` and `/pay`) saves the bark in this browser as the wallet selected on Profile. It does not post or pay on dogefeed.app. |
| Profile | The selected Core wallet, as a compact card | Wallet switcher lists addresses from `/dogefeedx/api/wallet/addresses` (this node's watch list). The choice is stored in this browser and is the address Wallet, Files, NFTs, and Profile read. A public handle's posts still come from dogefeed.app. |
| Files | Collections, DRC-20, names | Node indexes via `/dogefeedx/api/wallet/holdings`. Catalog names and covers from dogefeed.app `/market`. |
| Notifications | This arcade's notices | Local `/dogefeedx/api/local/notifications`. |
| Messages | This arcade's messages | Local arcade account. No dogefeed auth. |
| Explore | Hashtags, people, collections, earners, top posts | dogefeed.app public pages. Not a wallet. |
| Market | Public Doginal and DRC-20 listings, floors, covers | dogefeed.app market. Not what any wallet owns. |
| Inscribe | Create an inscription, token, or name | This arcade (`/create/ord`, `/tokens`, `/create/name`). Does not embed inscribe.doginals.org. |
| Wallet | Balance, doginals, DRC-20, names | Node indexes. dogefeed.app only labels collections the index already holds. |
| Launchpad | Public mint listings, plus this module's Top Launches panel | dogefeed.app launch listings. Applying to launch uses this arcade's wallet. |
| NFTs | Doginals at the node's addresses | Node indexes, labeled from the public catalog. |
| Arcade | Free-chip games, and games inscribed here | dogefeed.app's public game list, and this node's own games. |
| Bookmarks | Saved posts on this arcade | Local. |
| Meme Library | Public meme pictures, and pictures picked in this browser | dogefeed.app `/api/memes` for the public grid. Local picks stay in this browser. |
| Leaderboard | Public boards | dogefeed.app. Top referrers stays empty: there is no referral system. |
| Settings | This arcade's account | Local `/account`. Not a DogeFeed login. |

Routes that used to ask explorer.dogesoft.io for a wallet (`/api/balance`,
`/api/address/inscriptions`, `/api/address/tokens`, `/api/lookup`) now read
the same indexes. `/api/wallet/holdings` is the one Files, NFTs, Profile, and
Wallet call.

# DogeFeedX Network App

A Mac OS X Aqua-styled network status viewer for the DogeFeedX desktop shell.

## Features

- **Node Status Display**: Shows all blockchain nodes (Dogecoin mainnet, Pepecoin mainnet, Pepecoin testnet) with current status
- **Sync Progress Bars**: Aqua-style progress bars showing blocks vs headers/network height
- **Behind Indicator**: Shows how many blocks behind each node is
- **Sync Button**: Start syncing any unsynced node using existing arcade controls
- **Mesh Peers**: Lists connected mesh peers with operator tags and hosts
- **Trusted Nodes**: Shows linked trusted nodes from the mesh

## Installation

The Network app registers itself with the DogeFeedX shell's dock:

```python
from arcade.web.dogefeedx import network
network.register(app, state, admin_check=your_admin_check_function)
```

## API Endpoints

### GET /dogefeedx/network
Renders the Network app page with Mac OS X Aqua styling.

### GET /dogefeedx/network/api/status
Returns JSON with:
- `nodes`: Array of node status objects with network, blocks, headers, progress, etc.
- `mesh_peers`: Array of connected mesh peers
- `trusted_nodes`: Array of trusted linked nodes
- `timestamp`: Unix timestamp of the response

### GET /dogefeedx/network/api/peers/{network}
Returns Core peers for a specific blockchain network.

### POST /dogefeedx/network/api/sync/{network}
Starts syncing the specified network's node.

### GET /dogefeedx/network/icon-32.png
Returns the app's SVG icon (network nodes graphic).

## Dock Registration

The Network app uses a dock registration stub that maintains compatibility with the
shell being built on the `cursor/` branch for "DogeFeedX macOS UI":

```python
from arcade.web.dogefeedx.network import get_dock_apps, register_with_dock

# Get all registered apps
apps = get_dock_apps()

# Manually register an app
register_with_dock("my.app.id", "My App", "/path", "/icon.png")
```

When the shell is available, it will call `get_dock_apps()` to populate its dock.

## Styling

The app uses Mac OS X Aqua styling including:
- Brushed metal titlebar with traffic light buttons
- Aqua progress bars with glossy finish
- Rounded corners and subtle shadows
- Gradient backgrounds on buttons and cards
- Full dark mode support

## Testing

Run tests with:
```bash
pytest tests/test_dogefeedx_network.py -v
```

## Screenshots

See `/opt/cursor/artifacts/screenshots/dogefeedx_network_mock.png` for a demo with mock data.
