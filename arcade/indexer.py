"""The block handler: chain transactions in, protocol state out.

Plugs into `ChainFollower` from M0. For each block it walks the transactions in
order, extracts any that carry a Arcade payload, and applies them.
"""

from __future__ import annotations

import logging
from typing import Any

from . import utxos
from .config import Params
from .db import StateDB
from .rpc import RpcClient
from .script import parse_output
from .state import Engine
from .tx import PrevOut, ArcadeTransaction, TxError, extract

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


def is_cancel(rtx) -> bool:
    """Whether a transaction takes orders off the book (types 26-28)."""
    from . import payload as P
    try:
        msg = P.decode(rtx.payload)
    except Exception:
        return False
    return isinstance(msg, (P.MetaDExCancelPrice, P.MetaDExCancelPair,
                            P.MetaDExCancelEcosystem))


class ArcadeHandler:
    """BlockHandler that decodes and applies Arcade transactions."""

    def __init__(self, rpc: RpcClient, params: Params):
        self.params = params
        self.prevouts = PrevOutCache(rpc, params)
        self.stats = {"blocks": 0, "candidates": 0, "valid": 0, "invalid": 0, "unreadable": 0}

    def on_connect(self, state: StateDB, height: int, block: dict[str, Any]) -> None:
        # Below the floor, a block is read for its @names alone -- if names start
        # earlier than the floor (config.Params.names_from) -- and otherwise not at
        # all. Coins are followed there too: a floor never touches coins.
        names_only = self.params.names_only(height)
        if height < self.params.activation_height and not names_only:
            return

        self.prevouts.add_block(block)
        engine = Engine(state, self.params, names_only=names_only)
        self.stats["blocks"] += 1

        # Coins first, and for every transaction in the block rather than
        # only the arcade's own: a watched address is paid by ordinary
        # sends far more often than by anything carrying a marker, and an
        # index that only saw its own transactions would show a balance
        # that is always too small (arcade/utxos.py).
        watched = utxos.watching(state.db)
        if watched:
            moved = utxos.on_block(state, height, block, self.params, watched)
            self.stats["coins_in"] = self.stats.get("coins_in", 0) + moved["added"]
            self.stats["coins_out"] = self.stats.get("coins_out", 0) + moved["spent"]

        # From `cancels_last_from`, a block's cancels wait for the rest of it:
        # see `later` below and config.Params.cancels_last_from.
        cancels_last = (self.params.cancels_last_from is not None
                        and height >= self.params.cancels_last_from)
        later: list = []
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
            if cancels_last and is_cancel(rtx):
                later.append(rtx)
                continue
            self._process(engine, rtx, height)
        for rtx in later:
            self._process(engine, rtx, height)

    def _process(self, engine, rtx, height: int) -> None:
        result = engine.process(rtx)
        self.stats["valid" if result.valid else "invalid"] += 1
        if not result.valid:
            log.info("invalid tx %s at height %d: %s", rtx.txid, height, result.reason)
