"""DogeFeedX: Mac OS X Aqua-style desktop theme for the Dogecoin Arcade.

This module contains standalone apps that register with the DogeFeedX shell's dock.
Each app is a self-contained module with its own JS, CSS and template.

To register an app with the dock, call its register function:

    from arcade.web.dogefeedx import network
    network.register(app, state)

The apps use the DogeFeed styling (Mac OS X Aqua look) defined in their templates.
"""
from __future__ import annotations

__all__ = ["network"]
