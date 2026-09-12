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
        self._check(self.state.messaging, public_only=False)
        # The public board's mainnet half needs its own scan, and a public-only
        # scanner decrypts nothing, so it is safe there (D-014).
        self._check(self.state.ledger, public_only=True)

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
