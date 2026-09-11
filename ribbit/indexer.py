"""The block handler: chain transactions in, protocol state out.

Plugs into `ChainFollower` from M0. For each block it walks the transactions in
order, extracts any that carry a Ribbit payload, and applies them.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import Params
from .db import StateDB
from .rpc import RpcClient
from .script import parse_output
from .state import Engine
from .tx import PrevOut, RibbitTransaction, TxError, extract

log = logging.getLogger(__name__)


class PrevOutCache:
    """Resolves transaction inputs to (address, value, type).

    Sender determination needs the *previous* output of each input, which a block
    fetch does not include. With `txindex=1` the node can serve any transaction,
    so we look them up and cache -- the same funding transaction is often spent by
    several transactions in one block.
    """

    def __init__(self, rpc: RpcClient, params: Params, max_entries: int = 100_000):
        self.rpc = rpc
        self.params = params
        self.max_entries = max_entries
        self._cache: dict[tuple[str, int], PrevOut] = {}
        self.hits = 0
        self.misses = 0

    def add_block(self, block: dict[str, Any]) -> None:
        """Pre-load every output of this block.

        Transactions frequently spend outputs created earlier in the same block,
        and those are already in hand -- fetching them over RPC would be pure
        waste.
        """
        for tx in block.get("tx", []):
            txid = tx.get("txid")
            if not txid:
                continue
            for vout in tx.get("vout", []):
                index = int(vout.get("n", 0))
                self._store(txid, index, vout)

    def _store(self, txid: str, index: int, vout: dict[str, Any]) -> None:
        script_hex = vout.get("scriptPubKey", {}).get("hex", "")
        value = int(round(float(vout.get("value", 0)) * 100_000_000))
        parsed = parse_output(script_hex, value, self.params)
        if len(self._cache) >= self.max_entries:
            # Simple bound rather than an LRU: block-local reuse is what matters,
            # and a stale entry is never wrong, only absent.
            self._cache.clear()
        self._cache[(txid, index)] = PrevOut(
            address=parsed.address, value=parsed.value, type=parsed.type
        )

    def lookup(self, txid: str, index: int) -> PrevOut:
        key = (txid, index)
        cached = self._cache.get(key)
        if cached is not None:
            self.hits += 1
            return cached

        self.misses += 1
        raw = self.rpc.call("getrawtransaction", txid, True)
        for vout in raw.get("vout", []):
            self._store(txid, int(vout.get("n", 0)), vout)
        result = self._cache.get(key)
        if result is None:
            raise TxError(f"input {txid}:{index} does not exist")
        return result


class RibbitHandler:
    """BlockHandler that decodes and applies Ribbit transactions."""

    def __init__(self, rpc: RpcClient, params: Params):
        self.params = params
        self.prevouts = PrevOutCache(rpc, params)
        self.stats = {"blocks": 0, "candidates": 0, "valid": 0, "invalid": 0, "unreadable": 0}

    def on_connect(self, state: StateDB, height: int, block: dict[str, Any]) -> None:
        if height < self.params.activation_height:
            return

        self.prevouts.add_block(block)
        engine = Engine(state, self.params)
        self.stats["blocks"] += 1

        for position, tx in enumerate(block.get("tx", [])):
            try:
                rtx = extract(tx, height, position, self.params, self.prevouts.lookup)
            except TxError as exc:
                # The transaction carried a marker but could not be read. That is
                # worth recording and worth noticing, but it is not fatal: a
                # malformed transaction is simply invalid under the protocol, and
                # every implementation must agree it does nothing.
                self.stats["unreadable"] += 1
                log.info("unreadable marked tx %s at height %d: %s", tx.get("txid"), height, exc)
                continue

            if rtx is None:
                continue

            self.stats["candidates"] += 1
            result = engine.process(rtx)
            self.stats["valid" if result.valid else "invalid"] += 1
            if not result.valid:
                log.info("invalid tx %s at height %d: %s", rtx.txid, height, result.reason)
