# DogeFeedX

DogeFeed's look, the arcade's data (2026-11-03: "this is meant not to be reliant on any external API's or
external servers or services. Everything needs to work through arcade", and "Keep UI/UX elements from DogefeedX wire
them into the existing arcade architecture"). The dock is the navigation; each window is one module. No module asks
any other server anything: not dogefeed.app, not an explorer, not a font host. `tests/test_dogefeedx_local.py`
keeps it so.

The data comes from `apps.LOCAL`, the provider app.py hands `register_routes(app, render, local=dfx_local)`; every
endpoint answers empty (`source: "arcade"`) when the arcade has nothing to say.

Ownership indexes:

| Owns | Where |
|---|---|
| Balance | The watcher's coin index (`arcade/utxos.py` on the Dogecoin ledger). The sum only. Outpoints are not returned. While this Core is behind, or the chain is borrowed, the sum is the sharer's lent set (`utxoset.LentSet`, the same ask as `watcher._utxoset_lent_pass`), then `arcade_coinscan` when that chain has no set. |
| Doginals | The ledger inscription index (`held_collections` / `held_pieces`). Pictures are this node's `/content/`. While behind, inscriptions on the lent coins come from `DoginalIndex.lookup` (`what: on`). |
| DRC-20 | This arcade's ord index (`DoginalIndex.balances`). While behind, `DoginalIndex.lookup` (`what: balances`), the most complete linked walker. |
| Names | This arcade's ord index (`DoginalIndex.names_on` for coins the watcher already holds). While behind, the same question through `lookup`. The claim's outpoint is dropped. |
| Which node | Holdings carry `reading.via` (`local` or `peer`), `reading.node`, and `reading.sync` from `core_catchup.say` (this Core, not the peer's tip). Wallet, Files, NFTs, and Profile show that line. |

A pasted address that this node is not watching comes back empty. Import it watch-only on the node and let the
indexes catch up. No key is asked.

## Per-module data sources

| Dock module | What it shows | Source of truth |
|---|---|---|
| Home | The arcade's own feed, and Post | `/dogefeedx/api/feed` (the Feed's posts: For you = popular, Latest = newest). Post goes on the chain: an account signs in its browser (`wallet.post`), the operator's arcade posts from its wallet (`/dogefeedx/api/post`, as `/feed/post`). A picture from the Meme Library is linked (`/content/<id>`); a new one is inscribed (operator). Paid posts are not offered. |
| Post | One post and its replies; Like, Repost, Reply, Bookmark | `/dogefeedx/api/posts/<txid>`. Reactions are the Feed's (`wallet.react` / `/dogefeedx/api/react`, as `/feed/{txid}/{doing}`); `_arcade_act.html`. |
| Profile | An arcade account's posts; the selected watch wallet | `/dogefeedx/api/users/<tag>/posts`; the card is the asker's @tag, bio and website from the chain (`/dogefeedx/api/me`). The wallet switcher lists this node's watch list (`/dogefeedx/api/wallet/addresses`). |
| Files, Wallet, NFTs | Collections, DRC-20, names, balance | Node indexes via `/dogefeedx/api/wallet/holdings`, labeled from the arcade's collections. |
| Notifications | This arcade's notices | `/dogefeedx/api/local/notifications`. |
| Messages | The arcade's own sealed messages | `/messaging.js` (an account, in its browser) or `/node-mail.js` (the operator), as `/me/messages`. Groups and files: the full Messages page. |
| Explore, Search, Leaderboard | Tags, people, top posts, earners | The arcade's feed (`/dogefeedx/api/sidebar`, `/leaderboard`, `/search`). Top referrers stays empty: there is no referral system. |
| Market, Collections | Doginal collections, floors, covers; DRC-20 | The collection registry (collections described by an inscription: Doginal Frogs, Doginal Dogs ...) and the imported listing (`known_collections`); floors are the lowest open ask on this arcade's Doginal market. Tokens from the ord index. |
| Inscribe | Create an inscription, token, or name | This arcade (`/create/ord`, `/tokens`, `/create/name`). |
| Launchpad | Collections minting now | The arcade's mintpads (`ledger.launches`). |
| Arcade | Apps on this arcade | The Apps page's list (`_games_ranked`). |
| Bookmarks | Saved posts | This browser (`dogefeedx.bookmarks`). |
| Meme Library | Pictures on the chain | The ord index (`what: typed`, `image/`), paged by inscription number. |
| Settings | Your profile and notification switches | Bio and website published on the chain (`mail.publishProfile`, as Profile on `/me`); switches stay in this browser. |

There is no DOGE price: the arcade does not ask a price feed (`/dogefeedx/api/price` answers `price: null`).

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

