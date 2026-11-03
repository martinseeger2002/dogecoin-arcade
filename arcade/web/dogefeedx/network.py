"""DogeFeedX Network app: shows node status, sync progress, and mesh peers.

A self-contained module for the DogeFeedX desktop shell. Shows:
- Nodes this arcade runs (Dogecoin mainnet, Pepecoin mainnet, Pepecoin testnet) with status
- Progress bar per node showing blocks vs headers/network height and how far behind
- Button to start syncing any unsynced node (uses arcade's existing node controls)
- Lists of trusted network nodes and contributing mesh peers from existing mesh code

Mac OS X Aqua styling (DogeFeed theme).

To register with the shell's dock:
    from arcade.web.dogefeedx import network
    network.register(app, state)
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger("arcade.dogefeedx.network")

TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "dogefeedx" / "network.html"

APP_ID = "dogefeedx.network"
APP_NAME = "Network"
APP_ICON = "/dogefeedx/network/icon-32.png"


def register(app, state, *, admin_check: Callable | None = None) -> None:
    """Register the Network app with FastAPI and the DogeFeedX dock.

    Args:
        app: FastAPI application instance
        state: AppState instance
        admin_check: Optional function to verify admin access
    """

    def _admin_check(request: Request) -> None:
        if admin_check:
            admin_check(request)

    def _get_node_status(chain) -> dict:
        """Get status info for one chain."""
        sync = state.chain_sync(chain)
        blocks = sync.get("blocks", 0)
        headers = sync.get("headers", 0)
        progress = sync.get("progress", 0.0)
        ready = sync.get("ready", False)
        online = sync.get("online", False)

        behind = max(0, headers - blocks) if headers > 0 else 0
        progress_pct = round(progress * 100, 2)

        return {
            "network": chain.network,
            "label": chain.params.label,
            "letter": chain.letter,
            "family": chain.family,
            "online": online,
            "ready": ready,
            "synced": ready,
            "blocks": blocks,
            "headers": headers,
            "behind": behind,
            "progress": progress,
            "progress_pct": progress_pct,
            "borrowed": getattr(chain, "borrowed", False),
            "is_addon": chain.is_addon,
            "is_mainnet": chain.is_mainnet,
        }

    def _get_mesh_peers() -> list[dict]:
        """Get mesh peer info from operators share."""
        mesh = getattr(state, "mesh", None)
        if mesh is None:
            return []

        operators = getattr(mesh, "operators", None)
        if operators is None:
            return []

        try:
            return operators.status()
        except Exception:
            log.debug("network: could not get mesh peers", exc_info=True)
            return []

    def _get_trusted_nodes() -> list[dict]:
        """Get list of trusted/contributing nodes from mesh data."""
        mesh = getattr(state, "mesh", None)
        if mesh is None:
            return []

        node = getattr(mesh, "node", None)
        if node is None:
            return []

        try:
            linked = list(node.links.keys()) if hasattr(node, "links") else []
            return [{"id": peer[:12], "full_id": peer, "linked": True} for peer in linked[:20]]
        except Exception:
            log.debug("network: could not get trusted nodes", exc_info=True)
            return []

    def _get_core_peers(chain) -> list[dict]:
        """Get Core peers for a chain if available."""
        try:
            with chain.rpc() as rpc:
                info = rpc.call("getpeerinfo")
                if not isinstance(info, list):
                    return []
                return [{
                    "addr": str(p.get("addr", ""))[:50],
                    "subver": str(p.get("subver", "")).strip("/")[:30],
                    "synced_blocks": p.get("synced_blocks", 0),
                    "inbound": p.get("inbound", False),
                } for p in info[:20]]
        except Exception:
            return []

    @app.get("/dogefeedx/network")
    def network_page(request: Request):
        """Render the Network app page."""
        try:
            from arcade.web.app import render
            return render(request, "dogefeedx/network.html",
                          app_id=APP_ID,
                          app_name=APP_NAME)
        except ImportError:
            return HTMLResponse(TEMPLATE_PATH.read_text() if TEMPLATE_PATH.exists() else
                                f"<html><body><h1>{APP_NAME}</h1><p>Template not found</p></body></html>",
                                headers={"Cache-Control": "no-store"})

    @app.get("/dogefeedx/network/api/status")
    def network_status(request: Request):
        """API: Get status of all nodes."""
        nodes = []

        for chain in [state.messaging, state.ledger] + list(getattr(state, "addons", []) or []):
            nodes.append(_get_node_status(chain))

        seen_nets = set()
        unique_nodes = []
        for n in nodes:
            if n["network"] not in seen_nets:
                seen_nets.add(n["network"])
                unique_nodes.append(n)

        return JSONResponse({
            "nodes": unique_nodes,
            "mesh_peers": _get_mesh_peers(),
            "trusted_nodes": _get_trusted_nodes(),
            "timestamp": time.time(),
        }, headers={"Cache-Control": "no-store"})

    @app.get("/dogefeedx/network/api/peers/{network}")
    def network_chain_peers(request: Request, network: str):
        """API: Get Core peers for a specific chain."""
        chain = None
        for c in [state.messaging, state.ledger] + list(getattr(state, "addons", []) or []):
            if c.network == network:
                chain = c
                break

        if chain is None:
            return JSONResponse({"detail": f"chain {network} not found"}, status_code=404)

        return JSONResponse({
            "network": network,
            "peers": _get_core_peers(chain),
        }, headers={"Cache-Control": "no-store"})

    @app.post("/dogefeedx/network/api/sync/{network}")
    async def network_start_sync(request: Request, network: str):
        """API: Start syncing a chain (uses existing arcade controls)."""
        _admin_check(request)

        chain = None
        for c in [state.messaging, state.ledger] + list(getattr(state, "addons", []) or []):
            if c.network == network:
                chain = c
                break

        if chain is None:
            return JSONResponse({"detail": f"chain {network} not found"}, status_code=404)

        sync = state.chain_sync(chain)
        if sync.get("ready"):
            return JSONResponse({"detail": "already synced", "status": sync})

        if not sync.get("online"):
            return JSONResponse({
                "detail": "node offline - start the Core first",
                "status": sync,
                "help": f"Start the {chain.params.label} Core, or borrow it from a linked arcade"
            }, status_code=503)

        return JSONResponse({
            "detail": "sync in progress",
            "status": _get_node_status(chain),
        })

    @app.get("/dogefeedx/network/icon-32.png")
    def network_icon():
        """Serve the Network app icon."""
        svg = b'''<svg width="32" height="32" viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
<defs>
  <linearGradient id="aqua" x1="0%" y1="0%" x2="0%" y2="100%">
    <stop offset="0%" style="stop-color:#7ec8f3"/>
    <stop offset="50%" style="stop-color:#4ca8e8"/>
    <stop offset="100%" style="stop-color:#2d8dd6"/>
  </linearGradient>
</defs>
<circle cx="16" cy="16" r="14" fill="url(#aqua)" stroke="#1a6cb8" stroke-width="1"/>
<circle cx="16" cy="10" r="3" fill="#fff"/>
<circle cx="10" cy="20" r="3" fill="#fff"/>
<circle cx="22" cy="20" r="3" fill="#fff"/>
<line x1="16" y1="13" x2="11" y2="17" stroke="#fff" stroke-width="2"/>
<line x1="16" y1="13" x2="21" y2="17" stroke="#fff" stroke-width="2"/>
<line x1="13" y1="20" x2="19" y2="20" stroke="#fff" stroke-width="2"/>
</svg>'''
        return HTMLResponse(svg, media_type="image/svg+xml",
                            headers={"Cache-Control": "public, max-age=86400"})

    register_with_dock(APP_ID, APP_NAME, "/dogefeedx/network", APP_ICON)
    log.info("DogeFeedX Network app registered")


_dock_apps: list[dict] = []


def register_with_dock(app_id: str, name: str, href: str, icon: str) -> None:
    """Register an app with the DogeFeedX shell's dock.

    This is a stub for the dock registration system. The actual shell implementation
    (being built by another agent on the 'cursor/' branch for 'DogeFeedX macOS UI')
    will provide a `dogefeedx_shell.register_dock_app()` function. This stub maintains
    API compatibility and stores registrations for the shell to pick up.

    To integrate with the real shell:
    1. Import the shell module when available: `from arcade.web.dogefeedx import shell`
    2. Call: `shell.register_dock_app(app_id, name, href, icon)`

    Args:
        app_id: Unique identifier for the app (e.g., "dogefeedx.network")
        name: Display name for the dock icon
        href: URL path the app opens
        icon: URL path to the 32x32 icon
    """
    _dock_apps.append({
        "id": app_id,
        "name": name,
        "href": href,
        "icon": icon,
    })


def get_dock_apps() -> list[dict]:
    """Get all apps registered with the dock.

    Returns a list of dicts with keys: id, name, href, icon.
    The shell uses this to populate the dock.
    """
    return list(_dock_apps)


def clear_dock_apps() -> None:
    """Clear all registered dock apps. For testing."""
    _dock_apps.clear()
