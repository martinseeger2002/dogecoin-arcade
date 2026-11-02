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
