"""Watch for new blocks and scan when one arrives.

Why this exists
---------------
Messages and public posts only become visible once they are in a block, and
until now the only way to see them was to press "scan". That makes the
application feel broken in a specific way: a correspondent says "I sent it", the
recipient is looking straight at the conversation, and nothing is there. The
chain is doing exactly what it should and the interface is simply not looking.

So it looks, once per block.

Why polling rather than ZMQ
---------------------------
The node publishes `zmqpubhashblock`, which would be instant. This polls
`getblockcount` every few seconds instead, because:

  - it needs no extra dependency in the web process, and no socket to keep alive
    across a node restart -- a dropped ZMQ subscription fails silently, and a
    notifier that has quietly stopped is worse than one that is a few seconds
    late;
  - it works on an installation whose node config this application did not
    write, where there may be no ZMQ endpoint at all;
  - block times are around a minute, so a few seconds of latency is not
    perceptible.

Failure behaviour
-----------------
Never raises into the application, and never stops. A node that is down, syncing,
restarting for a wallet switch, or mid-reorg simply means the next poll finds
nothing; the thread keeps going and picks up when the node returns. The interface
shows when the last successful check was, so "nothing new" and "not looking" are
distinguishable -- silently doing nothing is the failure this whole module exists
to prevent.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ..messaging.scanner import Scanner, repair_announcement_names

log = logging.getLogger(__name__)

#: How often to ask the node for its height. Cheap: one RPC call per chain.
POLL_SECONDS = 5.0

#: Scanning is bounded per pass so a long catch-up cannot hold the lock for
#: minutes. The next poll continues from the cursor.
BLOCKS_PER_PASS = 500


class BlockWatcher:
    """Scans each chain when its tip moves, on a daemon thread."""

    def __init__(self, state: Any, poll_seconds: float = POLL_SECONDS):
        self.state = state
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="arcade-blockwatch",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:            # never let the watcher die
                log.debug("block watcher tick failed", exc_info=True)
            self._stop.wait(self.poll_seconds)

    def _tick(self) -> None:
        self._repair_once()
        self._confirm_sent()
        self._check(self.state.messaging, public_only=False)
        # The public board's mainnet half needs its own scan, and a public-only
        # scanner decrypts nothing, so it is safe there (D-014).
        self._check(self.state.ledger, public_only=True)
        self._sync_ledgers()

    def _sync_ledgers(self) -> None:
        """Keep the token indexes in step with their chains.

        Each runs when its tip moves or the index is behind it, a bounded
        number of blocks at a time, so a fresh install catches up over
        successive ticks without ever holding the node for minutes. A stopped
        index is retried only when a new block arrives: the block it stopped
        on will not read differently five seconds later.
        """
        for chain in self.state.token_chains:
            index = self.state.token_index(chain)
            if not index.enabled:
                continue
            try:
                with chain.rpc() as rpc:
                    tip = rpc.get_block_count()
            except Exception:
                continue
            moved = tip != self.state.ledger_tips.get(chain.network)
            self.state.ledger_tips[chain.network] = tip
            height = index.indexed_height()
            behind = height is None or height < tip
            if not moved and (not behind or index.stopped is not None):
                continue

            before = (index.stats.get("candidates", 0), index.stopped)
            result = index.sync(max_blocks=BLOCKS_PER_PASS)
            after = (index.stats.get("candidates", 0), index.stopped)
            # Only something a token page would show bumps the generation: a
            # transaction indexed, or the index stopping or resuming.
            if after != before or (result is not None and result.reorged):
                self.state.bump_generation()

    _repaired = False

    def _repair_once(self) -> None:
        """Correct stored names an older parser cut, once, on the first tick.

        Here rather than at startup because it needs the node, and a node that
        is not up yet must not delay the interface -- the next tick tries again.
        """
        if self._repaired:
            return
        try:
            with self.state.messaging.rpc() as rpc, self.state.store() as store:
                fixed = repair_announcement_names(
                    rpc, self.state.messaging.params, store)
        except Exception:
            return                      # node not ready; try on the next tick
        self._repaired = True
        if fixed:
            log.info("repaired %d stored announcement name(s)", fixed)
            self.state.bump_generation()

    def _confirm_sent(self) -> None:
        """Notice when a message we sent reaches a block.

        A sent bubble says "unconfirmed" until this finds it, because until then
        the only timestamp available is when this computer pressed send -- which
        is not when the message exists for anybody else. Nothing marked a send
        confirmed at all before; the column existed and no code ever set it.
        """
        try:
            with self.state.store() as store:
                pending = store.unconfirmed_sent()
                if not pending:
                    return
                with self.state.messaging.rpc() as rpc:
                    for row in pending:
                        try:
                            raw = rpc.call("getrawtransaction", row["txid"], 1)
                        except Exception:
                            continue          # not ours, or not indexed yet
                        if int(raw.get("confirmations") or 0) < 1:
                            continue
                        store.mark_sent_confirmed(
                            row["txid"], int(raw.get("blocktime") or 0),
                            self._height_of(rpc, raw.get("blockhash")))
                        self.state.bump_generation()
        except Exception:
            log.debug("could not confirm sent messages", exc_info=True)

    @staticmethod
    def _height_of(rpc: Any, blockhash: str | None) -> int:
        if not blockhash:
            return 0
        try:
            return int(rpc.call("getblock", blockhash).get("height") or 0)
        except Exception:
            return 0

    def _check(self, chain: Any, public_only: bool) -> None:
        name = chain.network
        try:
            with chain.rpc() as rpc:
                tip = rpc.get_block_count()
        except Exception:
            return                        # node down or restarting; try again later

        seen = self.state.tips.get(name)
        self.state.last_checked[name] = time.time()
        if seen == tip:
            return
        self.state.tips[name] = tip

        # An identity is only needed to open sealed messages. Without one this
        # still collects public posts, which is the whole point on mainnet.
        identity = None
        if not public_only:
            try:
                identity = self.state.ensure_identity()
            except Exception:
                identity = None

        try:
            with chain.rpc() as rpc, self.state.store() as store:
                scanner = Scanner(rpc, chain.params, store, identity=identity,
                                  public_only=public_only)
                result = scanner.scan(max_blocks=BLOCKS_PER_PASS)
        except Exception:
            log.debug("scan failed for %s", name, exc_info=True)
            return

        found = result.opened + result.announcements + result.group_posts
        if found:
            # Only a change anyone can see bumps the generation. A block with
            # nothing in it for us must not make every open page reload.
            self.state.bump_generation()
            log.info("new on %s at height %d: %s", name, tip, result)
