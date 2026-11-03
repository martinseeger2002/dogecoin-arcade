"""DogeFeedX: Mac OS X Aqua-style desktop theme for the Dogecoin Arcade.

This module contains standalone apps that register with the DogeFeedX shell's dock.
Each app is a self-contained module with its own JS, CSS and template.

Apps included:
- Explorer: Finder-style browser with Collections, DRC-20 Tokens, and Names
- Terminal: Terminal.app-style command window
- Network: Node status, sync progress, and mesh peers

To register all DogeFeedX apps:
    from arcade.web.dogefeedx import register_routes
    register_routes(app, render)

To register just the Network app (requires state):
    from arcade.web.dogefeedx import network
    network.register(app, state)
"""
from __future__ import annotations

from .apps import (
    router,
    register_routes,
    DOGEFEED_APP_BASE,
    DOGEFEED_EXPLORER_BASE,
    DOGEFEED_IMG_BASE,
    CACHE_TTL,
)
from . import network

__all__ = [
    "router",
    "register_routes",
    "network",
    "DOGEFEED_APP_BASE",
    "DOGEFEED_EXPLORER_BASE",
    "DOGEFEED_IMG_BASE",
    "CACHE_TTL",
]
