# Bare-address coins from a synced arcade in the mesh

## Why
- **Borrow only inside the shared arcade network.** A user may borrow chain data only from a node in the shared arcade mesh, never a third-party site. This endpoint is how they do it, and it serves **only** known mesh arcades: each ask is signed with the caller's mesh key (Ed25519), which must be one announced on chain within 3 days (`store.mesh_peers`). Unsigned, stale, wrongly signed or unknown-key asks get a 403.
- **An unsynced node showed 0.** While a Core catches up (Billy's PC was ~29% synced), the wallet window read its own incomplete index and showed a balance of 0.
- **`/signin/{tag}/coins` can't help with a bare address.** It lends coins only for an *arcade account* (by tag). A Core-wallet receive address or a watched address (e.g. Apple's `DCHxodk…`) has no tag.
- **Speed first.** The UTXO scan is what tells holdings (small coins carry inscriptions). Collection JSONs and the CDN only fill names and images in afterwards. So the answer is coin rows only, with inscription marks already known (no new reads), cached 30 s on the server and 60 s on the asker.

## What
- `arcade/peer_address.py`: ask signing and checking, `synced()`, the answer shape (`coins`, `balance_koinu`, `height`, `ins` marks, `complete`), and `ask_peers()` (first synced peer wins, up to 6 hosts).
- `GET /peer/address/{address}/coins` (web/app.py): rate-limited like the other peer reads (`_peer_data_limited`). Validates the address, requires a signed mesh ask, and answers only while this Core is at the tip (`headers - blocks <= 2`, not IBD). Otherwise 503 "ask another arcade". Listed in `door.PUBLIC_TREES`. The handler itself enforces the mesh signature.
- `wallet_assets.index_owned`: if the linked sharer lent no coins and this Core is behind (or the chain is borrowed), ask synced mesh arcades. The reading line shows `via: peer`, the peer's host, and **this** Core's own sync line (`core_catchup.say`).

## Tests
`tests/test_peer_address.py` (6 new tests): signed ask accepted; unsigned, unknown-key, stale and wrong-address asks refused; sync rule; answer sum, marks and cache; unsynced peer skipped and source labelled; wallet_assets fallback. These and `tests/test_dogefeedx_wallet_assets.py` pass (16 passed). `test_dogefeedx_local.py` errors identically on unmodified main (a pydantic `__pydantic_core_schema__` issue), so the error isn't from this change.

## For a serving arcade (e.g. Node 1)
Run this code with a synced Dogecoin Core and its token index, and be reachable at an announced host (domain or wss tunnel). The asking arcade's mesh key must be announced on chain. No config is needed.
