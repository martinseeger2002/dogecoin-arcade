# DogecoinArcade

A sidecar Omni-style meta-layer for **Dogecoin-family chains** — Pepecoin and
Dogecoin, with identical behaviour on both.

It reads blocks from an **unmodified** node and maintains protocol state in its
own SQLite database. It is not a fork of the node, and it never holds private
keys — transactions are funded and signed by the node's own wallet.

## Why both chains work identically

Pepecoin is a Dogecoin fork, and every constant this protocol depends on is the
same in both, down to the source line numbers:

| Constant | Value | Source (both) |
|---|---|---|
| `MAX_OP_RETURN_RELAY` | 83 | `script/standard.h:30` |
| `DEFAULT_PERMIT_BAREMULTISIG` | true | `validation.h:143` |
| x-of-3 bare multisig standard | yes | `policy/policy.cpp:41` |
| `RECOMMENDED_MIN_TX_FEE` | COIN/100 | `policy/policy.h:23` |
| hard dust limit | DUST/10 | `policy/policy.h:81` |
| scriptSig limit | 1650 | `policy/policy.cpp:86` |
| block spacing | 60s | `chainparams.cpp` |

Only chain identity differs — address versions and ports — and those live in
`arcade/config.py` as data, not code.

## Status

**M2 complete.** Block reader, payload codec, core state, consensus hash.
See `../docs/` for the design plan and decision log.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

Tests spawn a throwaway node in a temp directory as the current user. No root,
and the system node and mainnet data are never touched.

## Hard rules

1. No floats in the consensus engine. Amounts are `u64`; prices are exact rationals.
2. An unknown transaction type stops the indexer with a loud error. Never skipped.
3. Blocks are processed in order, each with an undo journal entry for every mutation.
4. The app never holds private keys.
5. No mainnet broadcast without explicit per-transaction confirmation.
6. **The Messenger is testnet-only**, enforced in code (`require_messaging_network`).
