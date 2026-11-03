"""DogeFeedX: Mac OS X Aqua-style desktop theme for the Dogecoin Arcade.

This module contains standalone apps that register with the DogeFeedX shell's dock.
Each app is a self-contained module with its own JS, CSS and template.

Apps included:
- Explorer: Finder-style browser with Collections, DRC-20 Tokens, and Names
- Terminal: Terminal.app-style command window
- Network: Node status, sync progress, and mesh peers

To register all DogeFeedX apps (`local` is the arcade's data; see apps.py):
    from arcade.web.dogefeedx import register_routes
    register_routes(app, render, local=...)

To register just the Network app (requires state):
    from arcade.web.dogefeedx import network
    network.register(app, state)
"""
from __future__ import annotations

from .apps import (
    router,
    register_routes,
    IMG_BASE,
    CACHE_TTL,
)
from . import network

__all__ = [
    "router",
    "register_routes",
    "network",
    "IMG_BASE",
    "CACHE_TTL",
]
