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

import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .. import release as releaselib
from .. import update
from ..messaging.scanner import (Scanner, repair_announcement_heights,
                                 repair_announcement_names)

log = logging.getLogger(__name__)

#: How often to ask the node for its height. Cheap: one RPC call per chain.
POLL_SECONDS = 5.0

#: Blocks between one gather pass and the next. A wallet that has just walked
#: something home should let it confirm before deciding what is still stray,
#: or it sends the same thing twice.
GATHER_EVERY = 2

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
        """One pass. Every phase in its own hands.

        They ran in a bare sequence, so the first one to raise took every
        phase after it -- and the outer catch logs at debug, so the effect was
        a subsystem silently switching itself off. That is how release
        announcements stopped: something upstream began raising and
        `_announce_release`, ninth in the list, simply never ran again. Nothing
        said so, on a machine watching itself closely (D-089).

        The update went first for the same reason, back when it was the only
        one protected. The right answer is that none of them can take the
        others down.
        """
        for phase in (self._auto_update,
                      self._repair_once,
                      self._confirm_sent,
                      self._confirm_posts,
                      lambda: self._check(self.state.messaging, public_only=False),
                      # The public board's mainnet half needs its own scan, and
                      # a public-only scanner decrypts nothing (D-014).
                      lambda: self._check(self.state.ledger, public_only=True),
                      self._sync_ledgers,
                      self._check_pending_offers,
                      self._check_pending_feed,
                      self._backfill_history_once,
                      self._check_release_notices,
                      self._announce_release,
                      self._keep_shop,
                      self._run_xchain,
                      self._import_listings,
                      self._walk_home):
            try:
                phase()
            except Exception:
                name = getattr(phase, "__name__", "a watcher phase")
                log.warning("watcher: %s failed", name, exc_info=True)

    _shopkeeper = None
    _gathered_at = 0

    #: Mempool transactions already read, per chain. Kept on the watcher so
    #: it survives the Scanner being built fresh every pass.
    _pools: dict = {}

    #: The offer transactions seen in the pool, per chain, so a new one bumps
    #: the generation exactly once.
    _pending_offers: dict = {}

    #: The same for the feed: posts and reactions waiting for a block.
    _pending_feed: set = frozenset()

    def _walk_home(self) -> None:
        """Bring what this wallet holds elsewhere back to one address.

        A pass at most once every few blocks, and only on a chain where a
        transaction costs nothing real: sweeping somebody's mainnet coins
        without asking is spending their money for them, so there the same
        work waits behind a button (D-046). A wallet that cannot spend
        (D-021) gathers nothing.
        """
        chain = self.state.messaging
        if not chain.can_spend or chain.is_mainnet:
            return
        gather = getattr(self.state, "gather_once", None)
        if gather is None:
            return
        try:
            height = self.state.ledger_tips.get(chain.network) or 0
        except Exception:
            height = 0
        if height and height - self._gathered_at < GATHER_EVERY:
            return
        self._gathered_at = height
        try:
            if gather(chain):
                self.state.bump_generation()
        except Exception:
            log.debug("gather pass failed", exc_info=True)


    _listings_at = 0.0

    def _import_listings(self) -> None:
        """Listings other nodes' sellers put on the chain, filed here too, so a
        mintpad inscription sells on every node (arcade/listing_announce.py)."""
        from .. import listing_announce
        if time.time() - self._listings_at < listing_announce.EVERY:
            return
        self._listings_at = time.time()
        filed = 0
        for chain in self.state.token_chains:
            try:
                filed += listing_announce.import_announced(self.state, chain)
            except Exception:
                log.debug("listing import on %s failed", chain.network, exc_info=True)
        if filed:
            self.state.bump_generation()

    def _run_xchain(self) -> None:
        """The cross-chain book (arcade/xchain_node.py): deposits that have their
        confirmations open their orders, and what is owed is paid. Every 20 s;
        only when this node has opened the book (a setting the operator turns on)."""
        if not self.state.setting("xchain:enabled"):
            return
        now = time.monotonic()
        if now - getattr(self, "_xchain_at", 0.0) < 20.0:
            return
        self._xchain_at = now
        self.state.xchain.tick()

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
        # And what this wallet has already promised in public: an offer that
        # meets a standing ask is a sale the owner agreed to when they said
        # the price (D-101).
        self._shopkeeper.sell_at_asking_price()
        # And a book that crosses itself is not a market: if this wallet's own
        # bid is above somebody's ask, take it (D-102).
        self._shopkeeper.fill_what_crosses()

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

    def _check_pending_feed(self) -> None:
        """Notice a post or a reaction in the pool, so open pages refresh.

        The same job `_check_pending_offers` does for the marketplace: those
        transactions are not messages, so nothing else notices them, and a
        feed that only moves when a block lands is a feed that looks broken
        for a minute at a time (D-141, D-142).
        """
        from ..messaging import mempool as mempoollib

        chain = self.state.messaging
        try:
            with chain.rpc() as rpc:
                waiting = mempoollib.read(rpc, chain.params, chain.network)
        except Exception:
            return
        now = ({post["txid"] for post in waiting.posts}
               | {act["txid"] for act in waiting.acts})
        if now != self._pending_feed:
            self._pending_feed = now
            self.state.bump_generation()

    def _check_pending_offers(self) -> None:
        """Notice an offer or an order in the pool, so the page refreshes.

        The scanner bumps the generation for a message in the pool; neither
        of these is a message, they are arcade transactions, so nothing
        noticed them and the Exchange sat unchanged until the block landed.
        That is the wait the mempool read was meant to remove (D-058, D-061).

        Only the sets of ids are compared. Reading them is cached by txid in
        the index, so a pass with nothing new costs one getrawmempool.
        """
        for chain in self.state.token_chains:
            index = self.state.token_index(chain)
            if not index.enabled:
                continue
            try:
                orders, cancels = index.pending_orders()
                now = ({offer["txid"] for offer in index.pending_offers()}
                       | {order["txid"] for order in orders} | cancels)
            except Exception:
                continue
            if now != self._pending_offers.get(chain.network, set()):
                self._pending_offers[chain.network] = now
                self.state.bump_generation()

    #: When the automatic update last looked, and how often it may.
    #:
    #: Fifteen minutes, not six hours. Six was chosen against a picture of
    #: releases arriving weekly; on the day it shipped, nine went out in two
    #: hours and every machine sat on the first one it happened to have --
    #: including a node holding a consensus rule that had since moved, while
    #: its owner was told twice to update by hand. An automatic update nobody
    #: notices is not automatic, it is slow (D-078).
    #:
    #: It costs one small signed file per quarter hour per machine, and the
    #: board notice (D-068) is still the fast path: seconds, when there is
    #: something to hear.
    _update_checked = 0.0
    UPDATE_EVERY = 15 * 60

    #: The last release this node announced or acted on, so a notice that
    #: stays on the board does not start an update every pass.
    _release_seen = ""

    #: The newest board post this node has considered, and it only ever moves
    #: forward. `_release_seen` alone could not do this job: it holds ONE
    #: revision while a board holds many, so walking newest-first and stopping
    #: at the first unseen one alternated between two notices for ever --
    #:每 tick "new", every tick a reset, twelve fetches a minute from one node.
    #: A high-water mark cannot alternate, cannot grow, and cannot be confused
    #: by a re-read (D-093).
    _release_after = 0

    def _check_release_notices(self) -> None:
        """Somebody published; look now rather than in six hours.

        Automatic updates poll, and a poll is a compromise: often enough to
        matter, rare enough not to hammer the site. A consensus rule starting
        at a height does not care about that compromise -- the machines that
        have not looked yet are exactly the ones that will read the block
        wrong (D-062).

        So a release is announced on the public board, and a node that sees
        one checks immediately. The notice carries no authority: it names a
        revision and nothing else, and what gets installed is still decided
        by the signature on the manifest (D-065). The worst a forged notice
        can do is make a node fetch a manifest it would have fetched anyway
        -- which is why the check below is worth having but is not what makes
        this safe.
        """
        try:
            # The TAG TABLE, which is the claim on the chain, not the tag an
            # announcement states. They can disagree -- an announcement is a
            # statement by the key holder and goes stale the moment they claim
            # a different name -- and the two sides of this feature were
            # reading different ones. The publisher asked the tag table and
            # announced; every receiver asked its announcements, found the old
            # name, and answered "nobody holds that" before it looked at a
            # single notice. Invisible from the publishing machine, because
            # the half that works is the half it runs (D-086).
            who = self._release_publisher()
            if not who:
                return                  # nobody holds that tag on this chain
            with self.state.store() as store:
                said = store.get_meta("release_notice")
        except Exception:
            return
        if not said:
            return
        try:
            notice = json.loads(said)
        except ValueError:
            return
        revision = str(notice.get("revision") or "")
        # Anybody may broadcast. Only the node that holds the release tag is
        # telling us about a release -- and the notice carries no authority
        # even then: it says "look now", and what installs is decided by the
        # signature on the manifest (D-065, D-147).
        if not revision or notice.get("from") != who:
            return
        if revision == self._release_seen:
            return                      # already acted on this one
        self._release_seen = revision
        # A warning rather than info, deliberately. It fires once per
        # release, and info from this service does not reach the journal at
        # all on a test machine -- so the one event worth diagnosing from outside
        # was the one leaving no trace (D-093).
        log.warning("release notice from @%s: %s -- checking now",
                    releaselib.RELEASE_TAG, revision)
        self._update_checked = 0.0

    def _release_publisher(self) -> str:
        """The address holding the release tag, as the chain has it."""
        for chain in self.state.token_chains:
            if chain.network != self.state.messaging.network:
                continue
            try:
                return self.state.token_index(chain).address_of(
                    releaselib.RELEASE_TAG) or ""
            except Exception:
                return ""
        return ""

    def _announce_release(self) -> None:
        """Tell everyone, if this is the node that may.

        Self-selecting rather than configured: the node that holds the tag a
        notice must come from is the node that publishes releases, and no
        other machine can usefully claim otherwise -- everyone else checks
        the sender against the same tag.
        """
        if self.state.setting("announce_releases", True) is False:
            return
        try:
            if (self.state.my_tag() or "").lstrip("@") != releaselib.RELEASE_TAG:
                return
            installed, published, _ = update.check()
        except Exception:
            return
        if not published or published == self._release_seen:
            return
        # Only what this machine is actually running. Announcing a release
        # this node has not installed is telling other people to run
        # something nobody has run.
        if installed != published:
            return
        self._release_seen = published
        try:
            self.state.broadcast_release(published)
            log.info("announced release %s", published)
        except Exception as exc:
            log.warning("could not announce release %s: %s", published, exc)

    def _auto_update(self) -> None:
        """Install a newer published release, if the machine is allowed to.

        Off by a checkbox on the Overview, on by default, because a node that
        is behind is not merely missing features: a consensus rule starts at a
        height, and a node still running last week's code reads the same block
        differently from everybody else (D-062). The people most likely to be
        behind are the ones least likely to be watching for a release.

        What makes this safe to do unattended is the signature, not the
        schedule: the manifest is checked against a key pinned in this code
        before anything is downloaded, and the archive must hash to what the
        manifest says (D-065). What makes it polite is the rest of this
        function -- nothing is replaced while this wallet is in the middle of
        sending something.
        """
        if not self.state.setting("auto_update", True):
            return
        now = time.time()
        if now - self._update_checked < self.UPDATE_EVERY:
            return
        self._update_checked = now
        if self.state.live_progress():
            return                      # a send is in flight; next time
        try:
            installed, published, available = update.check()
        except Exception as exc:
            self._said("could not reach the site", exc=str(exc))
            return
        if not published:
            self._said("the site published no revision")
            return
        if installed == published or not available:
            self._said("up to date", installed=installed, published=published)
            return
        # Written down BEFORE the subprocess, because the subprocess restarts
        # this service and systemd stops the parent with it. Everything after
        # this line may simply never run (D-088).
        self._said("installing", installed=installed, published=published)
        log.info("automatic update: %s -> %s", installed or "unknown", published)
        # In a subprocess, and not in this thread: the update replaces the code
        # this process is running out of, restarts the interface, and prints as
        # it goes. The watcher's job is to decide that it should happen.
        venv = Path(sys.executable).parent
        result = subprocess.run([str(venv / "python"), "-m", "arcade.update"],
                                capture_output=True, text=True, timeout=900)
        # The return code is not the outcome. The updater restarts arcade-web,
        # systemd stops this process in the same cgroup, and the subprocess is
        # killed mid-flight -- so a SUCCESSFUL update returns non-zero, logs
        # "automatic update failed", and the benign progress output ("already
        # there") is printed as the error.
        # A test machine caught it on the one run that actually worked.
        #
        # So the outcome is read from the installation, not from the exit
        # status: what is on disk now is what happened (D-088).
        try:
            now_installed, _, _ = update.check()
        except Exception:
            now_installed = None
        if now_installed and published and now_installed.startswith(published[:7]):
            log.info("automatic update installed %s", published)
            self._said("installed", installed=published, published=published)
            return
        trouble = (result.stderr or result.stdout).strip()[-600:]
        log.warning("automatic update did not take (%s): %s",
                    now_installed or "revision unknown", trouble)
        self._said("the update did not take", installed=now_installed or installed,
                   published=published, exc=trouble[-200:])

    def _said(self, what: str, installed=None, published=None, exc: str = "") -> None:
        """Record what the last update check decided, for the Overview.

        The operator asked twice why updates were not automatic while they were
        running exactly as written. Neither of us could answer it from
        outside, because the only evidence was a debug log nobody reads. A
        thing that runs on its own has to say when it last ran (D-084).
        """
        self.state.set_update_status({
            "at": time.time(), "what": what, "installed": installed,
            "published": published, "error": exc,
        })

    _backfilled = False

    def _backfill_history_once(self) -> None:
        """Rebuild inscription history from transactions already indexed.

        Kept out of the schema install because a swap's buyer needs the node,
        and a node that is not up yet must not delay the interface -- the next
        tick tries again. Costs one pass over a few hundred stored rows, and
        only when the table is empty (D-071).
        """
        if self._backfilled:
            return
        for chain in self.state.token_chains:
            index = self.state.token_index(chain)
            if not index.enabled:
                continue
            try:
                index.backfill_moves()
            except Exception:
                log.debug("history backfill failed on %s", chain.network, exc_info=True)
                return                  # try again next tick
        self._backfilled = True

    _repaired = False

    def _repair_once(self) -> None:
        """Correct what an older reader stored, once, on the first tick.

        Here rather than at startup because it needs the node, and a node that
        is not up yet must not delay the interface -- the next tick tries again.
        """
        if self._repaired:
            return
        try:
            with self.state.messaging.rpc() as rpc, self.state.store() as store:
                fixed = repair_announcement_names(
                    rpc, self.state.messaging.params, store)
                fixed += repair_announcement_heights(
                    rpc, self.state.messaging.params, store)
        except Exception:
            return                      # node not ready; try on the next tick
        self._repaired = True
        if fixed:
            log.info("repaired %d stored announcement row(s)", fixed)
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

    def _check_mempool(self, chain: Any, public_only: bool) -> None:
        """What is on its way but not yet in a block.

        Nothing here touches a balance, a book or the ledger -- those are
        read from blocks and only from blocks. This is carriage: messages,
        the answers a shop sends, the halves of a swap. A transaction that
        never confirms leaves a row that says so, and is promoted in place
        when its block does arrive.
        """
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
                pool = self._pools.setdefault(chain.network, set())
                scanner._pool_seen = pool
                result = scanner.scan_mempool()
                self._pools[chain.network] = scanner._pool_seen
        except Exception:
            log.debug("mempool scan failed for %s", chain.network, exc_info=True)
            return
        if result.opened + result.announcements + result.group_posts:
            self.state.bump_generation()
            log.info("in the pool on %s: %s", chain.network, result)

    def _check(self, chain: Any, public_only: bool) -> None:
        name = chain.network
        try:
            with chain.rpc() as rpc:
                tip = rpc.get_block_count()
        except Exception:
            return                        # node down or restarting; try again later

        seen = self.state.tips.get(name)
        self.state.last_checked[name] = time.time()

        # The mempool first, and every pass, whether or not the tip has moved.
        # A message costs a block to arrive and a swap costs several; reading
        # what is in the pool turns that into seconds (D-050).
        self._check_mempool(chain, public_only)

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
