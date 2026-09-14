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
        self._confirm_posts()
        self._check(self.state.messaging, public_only=False)
        # The public board's mainnet half needs its own scan, and a public-only
        # scanner decrypts nothing, so it is safe there (D-014).
        self._check(self.state.ledger, public_only=True)
        self._sync_ledgers()
        self._keep_shop()

    _shopkeeper = None

    def _keep_shop(self) -> None:
        """Answer orders at this wallet's shops (arcade/shopkeeper.py).

        After the scan and the ledger sync on purpose: an order is in a
        message the scan just found, and it is about items and balances the
        sync just brought up to date. A wallet that cannot spend (D-021)
        keeps no shop.
        """
        if not self.state.messaging.can_spend:
            return
        if self._shopkeeper is None:
            from ..shopkeeper import Shopkeeper
            self._shopkeeper = Shopkeeper(self.state)
        if self._shopkeeper.tick():
            self.state.bump_generation()

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
                        done, first = self._count_confirmed(rpc, store, row)
                        if done is None:
                            continue
                        if done < len(store.txid_list(row, row["txid"])):
                            continue          # some of it is still in the mempool
                        store.mark_sent_confirmed(
                            row["txid"], int(first.get("blocktime") or 0),
                            self._height_of(rpc, first.get("blockhash")))
                        self.state.bump_generation()
        except Exception:
            log.debug("could not confirm sent messages", exc_info=True)

    def _confirm_posts(self) -> None:
        """Notice when a post of ours reaches a block.

        The same job `_confirm_sent` does for private messages, which public
        posts never had. Our own copy is written when it is sent and its height
        only arrived when the scanner read the WHOLE post back off the chain --
        every chunk of it. A picture is eighteen transactions spread over ten
        blocks, so a post whose first transaction confirmed within a minute sat
        marked pending for ten, with every byte of it already paid for and in a
        block. Reported from a phone: "it hasn't shown up and it still says
        pending". It had; the pending was about assembly, not about the chain.
        """
        for chain, network in ((self.state.messaging, self.state.messaging.network),
                               (self.state.ledger, self.state.ledger.network)):
            try:
                with self.state.store() as store:
                    pending = store.unconfirmed_posts(network)
                    if not pending:
                        continue
                    with chain.rpc() as rpc:
                        for row in pending:
                            done, first = self._count_confirmed(
                                rpc, store, row, table="group_post")
                            if done is None:
                                continue
                            if done < len(store.txid_list(row, row["txid"])):
                                continue      # some of it is still in the mempool
                            store.mark_post_confirmed(
                                row["id"],
                                self._height_of(rpc, first.get("blockhash")),
                                int(first.get("blocktime") or 0))
                            self.state.bump_generation()
            except Exception:
                log.debug("could not confirm posts on %s", network, exc_info=True)

    def _count_confirmed(self, rpc: Any, store: Any, row: Any,
                         table: str = "sent") -> tuple[int | None, dict]:
        """How many of a send's transactions are in blocks, and the first one.

        A picture is dozens of transactions and they are independent of each
        other -- a split wallet funds each from its own output -- so the miner
        takes them in whatever order it likes and the FIRST one can be the last
        to land. Asking only about that one made a message with forty of its
        fifty transactions in blocks read "unconfirmed", with nothing to say
        how far along it was. This counts them, so the interface can.
        """
        txids = store.txid_list(row, row["txid"])
        first_raw: dict = {}
        done = 0
        for index, txid in enumerate(txids):
            try:
                raw = rpc.call("getrawtransaction", txid, 1)
            except Exception:
                return (None, {})            # node busy; leave it for next tick
            if index == 0:
                first_raw = raw
            if int(raw.get("confirmations") or 0) >= 1:
                done += 1
        if done != (row["confirmed_count"] if "confirmed_count" in row.keys() else 0):
            store.record_send_progress(table, row["id"], done)
            self.state.bump_generation()
        return (done, first_raw)

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

        # Pieces of an unfinished post mean a scan still has work to find, so
        # keep looking even at a tip already seen. A chunk that landed in a
        # block the scan had just passed, or one missed because a pass failed,
        # otherwise waited for the NEXT block to be noticed -- and a post is
        # only assembled once every one of its pieces has been.
        waiting = 0
        try:
            with self.state.store() as store:
                waiting = store.waiting_chunks(name)
        except Exception:
            pass
        if seen == tip and not waiting:
            return

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

        # Recorded only once a scan has actually reached it. Marking the tip
        # seen before scanning meant a pass that failed -- a busy node, a
        # timeout -- skipped those blocks until the next one arrived, and
        # nothing ever went back for them.
        self.state.tips[name] = tip

        found = result.opened + result.announcements + result.group_posts
        if found:
            # Only a change anyone can see bumps the generation. A block with
            # nothing in it for us must not make every open page reload.
            self.state.bump_generation()
            log.info("new on %s at height %d: %s", name, tip, result)
