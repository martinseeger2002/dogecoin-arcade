# Ribbit

A sidecar Omni-style meta-layer for Pepecoin.

Ribbit reads blocks from an **unmodified** `pepecoind` and maintains protocol state
in its own SQLite database. It is not a fork of the node, and it never holds
private keys — transactions are funded and signed by the node's own wallet.

See `../docs/` for the design plan and decision log.

## Status

**M0 complete.** Block reader, regtest harness, schema, undo journal, reorg rollback.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

Tests spawn a throwaway `pepecoind -regtest` in a temp directory as the current
user. No root, and the system node and mainnet data are never touched.

## Hard rules

1. No floats in the consensus engine. Amounts are `u64`; prices are exact rationals.
2. An unknown transaction type stops the indexer with a loud error. Never skipped.
3. Blocks are processed in order, each with an undo journal entry for every mutation.
4. The app never holds private keys.
5. No mainnet broadcast without explicit per-transaction confirmation.
