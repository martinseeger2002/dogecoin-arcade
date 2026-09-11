"""Following the chain: connecting blocks, and surviving reorgs.

The follower keeps Ribbit's view of the chain in step with the node's. Two things
can happen on each pass:

  * the node has blocks we have not seen  -> connect them, in order
  * the node has *replaced* blocks we hold -> disconnect ours, then connect theirs

The second case is the one that matters. A meta-layer that quietly keeps state
from an orphaned block is permanently wrong, with no symptom until someone
compares consensus hashes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .config import Params
from .db import Database, StateDB
from .rpc import RpcClient

log = logging.getLogger(__name__)


class BlockHandler(Protocol):
    """What a milestone plugs in to actually interpret a block.

    M0 ships `NullHandler`, which records nothing beyond the block itself. M1 adds
    payload extraction, M2 adds state transitions. The follower does not care.
    """

    def on_connect(self, state: StateDB, height: int, block: dict[str, Any]) -> None: ...


class NullHandler:
    """Records blocks without interpreting them. The M0 handler."""

    def on_connect(self, state: StateDB, height: int, block: dict[str, Any]) -> None:
        return None


@dataclass
class SyncResult:
    """What one `sync_once` pass did."""

    connected: list[int] = field(default_factory=list)
    disconnected: list[int] = field(default_factory=list)
    fork_height: int | None = None
    node_tip: int = 0

    @property
    def reorged(self) -> bool:
        return bool(self.disconnected)

    def __str__(self) -> str:
        parts = []
        if self.disconnected:
            parts.append(f"disconnected {len(self.disconnected)} (from {max(self.disconnected)})")
        if self.connected:
            parts.append(f"connected {len(self.connected)} ({min(self.connected)}..{max(self.connected)})")
        return ", ".join(parts) or "up to date"


class ReorgTooDeep(Exception):
    """A reorg went deeper than we are willing to handle automatically."""


class ChainFollower:
    """Keeps the database in step with the node's active chain."""

    def __init__(
        self,
        rpc: RpcClient,
        db: Database,
        params: Params,
        handler: BlockHandler | None = None,
        max_reorg_depth: int = 500,
        clock: Callable[[], int] = lambda: int(time.time()),
    ):
        if params.activation_height is None:
            raise ValueError(
                f"network {params.name!r} has no activation height set; "
                "mainnet's is chosen at launch (see docs/DECISIONS.md D-004)"
            )
        self.rpc = rpc
        self.db = db
        self.params = params
        self.handler = handler or NullHandler()
        self.max_reorg_depth = max_reorg_depth
        self.state = StateDB(db)
        self._clock = clock

    # --- fork detection -------------------------------------------------------

    def find_fork_height(self) -> int | None:
        """Return the highest height where our chain still agrees with the node.

        None means we hold no blocks. Walks backwards from our tip comparing
        hashes; the first agreement is the fork point.

        Raises ReorgTooDeep rather than walking back forever, because a
        disagreement that deep means something is wrong that a retry will not fix
        -- a different network, a corrupted database, or a genuinely catastrophic
        reorg. All three deserve a human.
        """
        tip = self.db.tip()
        if tip is None:
            return None

        height = tip["height"]
        floor = max(self.params.activation_height, height - self.max_reorg_depth)

        while height >= floor:
            ours = self.db.block_at(height)
            if ours is None:
                height -= 1
                continue
            try:
                theirs = self.rpc.get_block_hash(height)
            except Exception:
                # Node does not have this height at all (it is behind, or shorter).
                height -= 1
                continue
            if theirs == ours["hash"]:
                return height
            height -= 1

        raise ReorgTooDeep(
            f"our chain disagrees with the node for more than {self.max_reorg_depth} blocks "
            f"below height {tip['height']}; refusing to roll back automatically"
        )

    # --- one pass -------------------------------------------------------------

    def sync_once(self, max_blocks: int = 1000) -> SyncResult:
        """Bring the database forward by up to `max_blocks`.

        Any reorg is fully resolved before a single new block is connected, so the
        database is never a mixture of two chains.
        """
        result = SyncResult(node_tip=self.rpc.get_block_count())

        tip = self.db.tip()
        if tip is not None:
            fork = self.find_fork_height()
            result.fork_height = fork
            if fork is None or fork < tip["height"]:
                target = fork if fork is not None else self.params.activation_height - 1
                doomed = [
                    row["height"]
                    for row in self.db.conn.execute(
                        "SELECT height FROM block WHERE height > ? ORDER BY height DESC", (target,)
                    )
                ]
                if doomed:
                    log.warning(
                        "reorg: disconnecting %d block(s) %d..%d, forking at %s",
                        len(doomed), min(doomed), max(doomed), target,
                    )
                for height in doomed:
                    self.state.rollback_block(height)
                    result.disconnected.append(height)

        tip = self.db.tip()
        next_height = self.params.activation_height if tip is None else tip["height"] + 1

        for height in range(next_height, min(result.node_tip, next_height + max_blocks - 1) + 1):
            block_hash = self.rpc.get_block_hash(height)
            block = self.rpc.get_block(block_hash, 2)
            self._connect(height, block)
            result.connected.append(height)

        return result

    def _connect(self, height: int, block: dict[str, Any]) -> None:
        """Apply one block and everything it implies, atomically."""
        txs = block.get("tx") or []
        with self.state.block_context(
            height=height,
            block_hash=block["hash"],
            # The genesis block has no previousblockhash.
            prev_hash=block.get("previousblockhash", "0" * 64),
            block_time=int(block["time"]),
            tx_count=len(txs),
            processed_at=self._clock(),
        ) as state:
            self.handler.on_connect(state, height, block)
