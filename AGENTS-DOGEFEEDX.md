# DogeFeedX Theme App Registration API

This document describes how to register custom apps into the DogeFeedX desktop shell's dock.

## Overview

The DogeFeedX theme provides a macOS-style desktop with a dock at the bottom. Apps can be registered
dynamically via `window.DogeFeedX.registerApp()`. Registered apps appear in the dock and can be
opened by clicking their icons.

**Shell-owned apps** (Profile, Market): Built into the shell, not replaceable via this API.

**External apps** (Terminal, Network, Explorer): These have placeholder stubs in the shell. Other
agents should register their own implementations to override the placeholders.

## API Reference

### `window.DogeFeedX.registerApp(key, config)`

Register an app for the DogeFeedX dock.

**Parameters:**

- `key` (string): Unique app key. Use one of the reserved keys to override a placeholder:
  - `'dfxterminal'` - Terminal app
  - `'dfxnetwork'` - Network status panel
  - `'dfxexplorer'` - Explorer (DogeFeed collections, blocks, transactions)
  
- `config` (object):
  - `name` (string): Display name shown in dock tooltip
  - `glyph` (array): Icon as `[fillColor, strokeColor, svgPath]`
    - Example: `['#28c840', '#1c1c1e', 'M4 17l6-6-6-6M12 19h8']`
  - `opener` (function): Called when app icon is clicked. Receives a helpers object.

### Helpers Object

The `opener` function receives a helpers object with these properties:

```javascript
{
  dialog: function(key, title, item, html, width, height) { ... },  // Create a dialog window
  openWin: function(opts) { ... },   // Open an iframe window: {key, url, name, item, w, h}
  byKey: { ... },                    // Registry of all app items (for icons)
  SYSTEM: { version, node, chains }, // System info
  esc: function(s) { ... },          // HTML escape utility
  WHO: string,                       // Current user's handle or 'guest'
  OPERATOR: boolean,                 // Whether user is the node operator
  focus: function(win) { ... },      // Focus a window
  wins: [ ... ]                      // Array of open windows
}
```

### `window.DogeFeedX.listApps()`

Returns array of registered app keys.

### `window.DogeFeedX.getApp(key)`

Returns the config object for a registered app, or undefined.

### `window.DogeFeedX.version`

API version string (currently `'1.0.0'`).

## Example: Registering a Terminal App

```javascript
window.DogeFeedX.registerApp('dfxterminal', {
  name: 'Terminal',
  glyph: ['#28c840', '#1c1c1e', 'M4 17l6-6-6-6M12 19h8'],
  opener: function(helpers) {
    var open = helpers.wins.filter(function(w) { return w.key === 'dfxterminal'; })[0];
    if (open) { helpers.focus(open); return; }
    
    var d = helpers.dialog('dfxterminal', 'Terminal', helpers.byKey.dfxterminal,
      '<div class="my-terminal">' +
      '<div class="output" id="term-out"></div>' +
      '<input type="text" id="term-in" autocomplete="off">' +
      '</div>', 700, 450);
    
    // Set up terminal behavior...
    var out = d.q('#term-out'), inp = d.q('#term-in');
    inp.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') {
        // Execute command via /api/terminal/exec
        fetch('/api/terminal/exec', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({command: inp.value.split(' ')[0], args: inp.value.split(' ').slice(1).join(' ')})
        }).then(function(r) { return r.json(); }).then(function(j) {
          // Display output
        });
        inp.value = '';
      }
    });
  }
});
```

## Example: Registering a Network Panel

```javascript
window.DogeFeedX.registerApp('dfxnetwork', {
  name: 'Network',
  glyph: ['#5856d6', '#1c1c1e', 'M12 2a10 10 0 1 0 0 20...'],
  opener: function(helpers) {
    var open = helpers.wins.filter(function(w) { return w.key === 'dfxnetwork'; })[0];
    if (open) { helpers.focus(open); return; }
    
    var d = helpers.dialog('dfxnetwork', 'Network', helpers.byKey.dfxnetwork,
      '<div class="network-panel">...</div>', 600, 500);
    
    // Fetch network status from /api/network
    fetch('/api/network', {credentials: 'same-origin'})
      .then(function(r) { return r.json(); })
      .then(function(data) {
        // data.chains - array of chain sync status
        // data.mesh - mesh connection status
        // data.tunnel - tunnel status
        // data.arcades - connected arcades
      });
  }
});
```

## File Organization

External apps should be organized as self-contained modules:

```
arcade/web/templates/apps/
  dfxterminal.js      # Terminal app module
  dfxnetwork.js       # Network panel module
  dfxexplorer.js      # Explorer module
```

Each module should:
1. Check if DogeFeedX is available: `if (window.DogeFeedX)`
2. Register the app: `window.DogeFeedX.registerApp('dfx...', { ... })`
3. Be loaded after desktop.html (add a script tag in the theme's section)

## Backend APIs

The following API endpoints are available for DogeFeedX apps:

### `GET /api/terminal/commands`
Returns the whitelist of available terminal commands.

### `POST /api/terminal/exec`
Execute a terminal command. Body: `{command: string, args: string}`

Allowed commands: `help`, `status`, `balance`, `peers`, `sync`, `chains`, `block`, `tx`, `address`,
`inscription`, `collection`, `clear`

### `GET /api/network`
Returns network status including:
- `chains`: Array of chain sync status with progress
- `mesh`: Mesh connection status, peer count
- `tunnel`: Tunnel status
- `arcades`: Connected arcades list

## CSS Classes

DogeFeedX provides these CSS classes for app windows:

- `.dfx-*` - DogeFeedX-specific styles
- `.w98-btn` - Button styling
- `.w98-field` - Input field styling
- `.dlg-acts` - Dialog action button row

## Notes

- All input fields must have `autocomplete="off"` to pass tests
- Apps should handle the case where the user is not signed in (`WHO === 'guest'`)
- The `dialog()` helper creates a panel window; use `openWin()` for iframe-based windows
- Registered apps override the placeholder implementations when both exist
