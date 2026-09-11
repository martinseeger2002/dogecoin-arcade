"""Encrypted messaging over Dogecoin-family chains.

Testnet only, permanently (docs/DECISIONS.md D-010). Every entry point that
touches the network calls `require_messaging_network()` first.

Design: docs/messaging/02-design.md
"""
