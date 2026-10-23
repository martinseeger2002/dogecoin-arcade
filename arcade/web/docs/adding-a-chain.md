# Adding a chain: the checklist

For whoever adds the next coin, person or agent. Most of a chain is data, its `Params` in
`arcade/config.py`: the launcher flags, `core:` / `share:`, mesh Core peering, borrowing, discovery,
fees, dust and the ord index all follow from them. This page lists what still needs a hand, learned
from adding Bellscoin and Luckycoin (2026-10-22). Use Dogecoin (`doge`), Bellscoin (`bel`) and
Luckycoin (`lky`) as the worked examples.

## 1. Read the coin's own source, at the release tag the arcade will run

From its `chainparams.cpp` and `chainparamsbase.cpp`:
- genesis hash
- base58 versions: P2PKH, P2SH and WIF
- bech32 HRP, and whether SegWit or Taproot are active
- RPC and P2P ports
- AuxPoW chain id and start height, and how the header holds the id
- From `policy.h` and `validation.h`: the minimum relay fee, dust, soft dust, the OP_RETURN size
  (80 bytes must be standard) and bare multisig (must relay)

Then check every value against a node of that chain:
- `getblockhash 0`
- `validateaddress` on an address of each kind
- `signmessagewithprivkey` with a throwaway key in its WIF

## 2. Write its `Params`

Copy the nearest relative: `DOGECOIN` for a Dogecoin-code chain, `LITECOIN` for a Bitcoin-Core-based
one. Set:
- `name`, `coin`, `label`
- the ports, the versions, `genesis`
- `auxpow_chain_id`, and `auxpow_chain_mask` when the chain keeps versionbits beside its id
- `min_fee_per_kb`, `soft_dust`, `dust_limit`
- `letter` (one not already used for a badge), `family`, `net_word`
- `node_dir`, `node_conf`
- `real_coins`, `bech32_hrp`

Add it to `NETWORKS` and `CHAIN_NAMES`. Set the floor at the tip rounded up about two days (D-004).
The other arcade must take the same floor. Mainnets keep swaps, takes and the rest at `None`.

## 3. Check its address bytes against every other chain

`config.unpayable_versions` finds a P2SH byte that is another chain's P2PKH byte. Bellscoin's 30 is
Dogecoin's "D", and `txbuild` reads a version byte the same on every chain. Such a chain:
- refuses that payee (`_check_address`)
- makes no arcade lock there (`require_p2sh_payable`)

The inscription guard watches a script byte only where no other chain uses it. Look for the
quieter clashes too: Luckycoin's P2SH 5 is Litecoin's legacy "3".

## 4. The places that still name a chain

- **`coins.js`:** `COIN_TYPE` (3 for a real-coin chain, 1 for a test chain), `WIF`, `WIF_CHAIN`.
- **`wallet_js.js`:** `TRADE.BIG`, `CLAIM_FEE`, `FEE_FLOOR`, `PAYEE_VERSIONS`, `P2SH_VERSION`, `COIN_WORD`,
  `REAL_COINS`. `tests/test_trade_fees.py` holds the fee tables to Python's.
- **`trade.py`:** `BIG`. Set it low until the operator sets `swap_big:<net>`.
- **`txbuild.py`:** `SCRIPTHASH_VERSIONS`, only for a byte that clashes with nothing.
- **`doginal_index.py`:** `STARTS` (the official indexer's `first_inscription_height`, or the first
  envelope found by scanning the chain), `SOURCES`, `TAPROOT`.
- **`ord_rules.py`:** `RULES_BY_NET` and `NUMBERS_REINSCRIPTIONS`. Follow the chain's official
  indexer, and bump the chain's rules when its numbering changes so its indexes are cut again.
- **Templates:**
  - `my_wallet.html`: `ORD_WORDS` and the inscription word.
  - `my_backup.html`: the key list.
  - `_tab_swaps.html`: the coin names.
  - `base.html`: the real-coin review card.
  - `base_css.css`: the chain's colours and tags.
  - `coin-<net>.png`: its icon.
- **This docs page list:** `arcade/web/guide.py ORDER`, and a page of its own like this one's neighbours.

## 5. Prove it

- **Tests:**
  - a test file like `tests/test_bells_luckycoin.py`: Params, titles, address refusals, twins,
    the guard, tiers, the browser tables and the Admin opt-ins
  - real blocks for `rawchain` (read, work, Core's own decoding)
  - the ord dialect tests
- **The whole suite,** compared with `main` on the same machine.
- **A scratch walk of its ord index** against its own node. Then compare numbers and blocks with the
  chain's public index: #0, the newest, and a random spread. A drift finds its first differing number
  by bisection. That is how Bellscoin's reinscription rule was found.
