"""Following the chain: connecting blocks, and surviving reorgs.

The follower keeps Arcade's view of the chain in step with the node's. Two things
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
from .rpc import RpcClient, RpcError

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


class IndexFromAnotherFloor(Exception):
    """This index was built for a different floor than the chain now has.

    Not a disagreement with anybody: it is what moving a floor MEANS, and it
    is the routine operation this project has performed four times in two
    days. Its own class because the answer is its own -- the index cannot be
    unwound to meet the floor, it is set aside and rebuilt from it (D-123).

    Three shapes, and the rule is one line: an index starts at the floor it
    was built for, so one that starts anywhere else was built for another one
    (D-130).

    * The floor was RAISED past everything the index holds: every block in it
      is unreadable.
    * The floor was RAISED past where the index starts, so it holds blocks
      below the floor -- a whole era of tokens and inscriptions that no node
      reads any more, while it keeps up with the tip and looks healthy. This
      is the one that made every reset manual, and the one a test machine was
      sitting on while the two nodes disagreed about what existed.
    * The floor was LOWERED below where the index starts, so the blocks
      between are missing and nothing ever goes back for them: an index only
      extends forward.
    """


#: The name this was published under when it only knew one direction.
IndexBelowFloor = IndexFromAnotherFloor


class NodeUnreachable(Exception):
    """The node could not be asked, so the chain said nothing either way."""


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
        start = self.params.index_start
        bottom = self.db.conn.execute(
            "SELECT MIN(height) AS low FROM block").fetchone()
        low = bottom["low"] if bottom and bottom["low"] is not None else height
        if low != start and height >= start:
            # An index starts at the floor it was built for. Anything else --
            # blocks below the floor that no node reads now, or a bottom that
            # was never fetched -- is an index built for another floor, and
            # neither can be fixed by going forward.
            raise IndexFromAnotherFloor(
                f"this index was built for a floor of {low:,} and the chain "
                f"now starts at {start:,}, so it "
                + (f"holds {start - low:,} block(s) below the floor that no "
                   f"node reads any more" if low < start else
                   f"is missing the {low - start:,} block(s) between, which "
                   f"nothing would ever go back for")
                + ". Nothing is wrong with the chain. The index is being set "
                  f"aside and rebuilt from {start:,}.")
        # The index begins where names do (Params.index_start), which a floor move
        # leaves behind -- so a MOVE shows only in the content floor it was built
        # for, recorded beside it. An index from before this record existed was
        # built for the floor it finds, and adopts it rather than rebuilding.
        content = self.params.activation_height
        built_for = self.db.get_meta("content_floor")
        if built_for is None:
            self.db.set_meta("content_floor", str(content))
            self.db.conn.commit()
        elif int(built_for) != content and height >= start:
            raise IndexFromAnotherFloor(
                f"this index was built for a floor of {int(built_for):,} and the "
                f"floor is now {content:,}. Nothing is wrong with the chain. The "
                f"index is being set aside and rebuilt: @names are read from "
                f"{start:,}, everything else from {content:,}.")
        if height < start:
            # The floor was raised above everything this index holds. No hash
            # is compared, and none should be: every block in here is below
            # the floor and unreadable by definition. Said before the loop
            # because the loop would not run -- `floor` would exceed `height`
            # and the function would fall through to a reorg it never looked
            # for. Three floors in two days each reported as a catastrophic
            # reorg, and the hashes agreed perfectly every time (a test machine, D-123).
            raise IndexFromAnotherFloor(
                f"this index was built for an older floor: it ends at "
                f"{height:,} and the chain now starts at {start:,}, so "
                f"everything in it is below the floor and cannot be read. "
                f"Nothing is wrong with the chain. Move the index file aside "
                f"and let it rebuild from {start:,}.")
        floor = max(start, height - self.max_reorg_depth)

        asked = 0
        while height >= floor:
            ours = self.db.block_at(height)
            if ours is None:
                height -= 1
                continue
            try:
                theirs = self.rpc.get_block_hash(height)
            except RpcError:
                # The node answered and does not have this height: it is
                # behind, or on a shorter chain. That IS evidence about the
                # chain, so keep walking back.
                asked += 1
                height -= 1
                continue
            except Exception as exc:
                # It did not answer -- a timeout, a refused connection, wrong
                # credentials. That is evidence about the node and none at all
                # about the chain, and walking on regardless is how a wallet
                # that briefly cannot reach its node reports a catastrophic
                # reorg (D-123).
                raise NodeUnreachable(
                    f"the node could not be asked for the hash at {height:,}: "
                    f"{exc}") from exc
            asked += 1
            if theirs == ours["hash"]:
                return height
            height -= 1

        raise ReorgTooDeep(
            f"our chain disagrees with the node for more than {self.max_reorg_depth} blocks "
            f"below height {tip['height']}; refusing to roll back automatically "
            f"({asked} heights compared)"
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
                target = fork if fork is not None else self.params.index_start - 1
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
        next_height = self.params.index_start if tip is None else tip["height"] + 1

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
