"""The ledger: the token index the application keeps, and how it reads it.

M2 built the engine -- properties, balances, sends -- and M0 built the chain
follower it runs under. Until now nothing in the product ran either: the
consensus code was exercised by the test suite and by nobody else. This module
is the piece that puts them to work.

One `LedgerIndex` per ledger chain. It owns nothing long-lived: every sync and
every query opens its own connection to the index database, because sqlite
connections are per-thread and the block watcher syncs on a thread of its own
while requests read on theirs. WAL mode means the readers never block the
writer.

Stopping is a feature
---------------------
The engine raises on a message type it does not implement (hard rule #2), and
the follower refuses a reorg deeper than it can unwind. Both stop the index on
purpose: any balance produced after such a block is a guess. The stop is kept
here, with the height and the reason, and `status()` puts it first so the
interface cannot show a stale balance as if it were current. It is retried on
every new block -- a transient node error clears itself, a real one keeps
saying the same thing at the same height.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ContextManager, Sequence

from . import payload as P
from .chain import (ChainFollower, IndexBelowFloor, NodeUnreachable,
                    ReorgTooDeep, SyncResult)
from .config import Params
from .db import Database
from .indexer import ArcadeHandler
from .rpc import RpcClient
from .state import ECOSYSTEM_TEST, PROPERTY_DIVISIBLE, install_schema

log = logging.getLogger(__name__)

COIN = 100_000_000

#: What the payload types are called on screen. Only the ones the engine
#: applies; anything else stops the index before it could be shown.
TYPE_NAMES = {
    0: "send",
    4: "send all",
    50: "create (fixed supply)",
    54: "create (managed supply)",
    55: "grant",
    56: "revoke",
    70: "change issuer",
    200: "data",
    65533: "deactivate feature",
    65534: "activate feature",
    65535: "alert",
}


@dataclass
class Stopped:
    """Why the index is not moving, and where it stopped."""

    height: int
    reason: str
    at: float


#: Every listing of inscriptions reads the same columns, never the content:
#: a page of a hundred files would be a hundred files. The collection columns
#: come along by a join, NULL for an inscription that is in no set.
#: `held` is not "kept here" any more but "this node can produce it": from
#: its own copy if it kept one, or off the chain, because `inscription_piece`
#: says which transactions carry the bytes (D-113).
_INSCRIPTION_SELECT = (
    "SELECT i.txid, i.number, i.creator, i.owner, i.block_height, i.position, "
    "i.content_type, i.content_len, i.sha256, i.json, i.chunks, "
    "(i.content IS NOT NULL OR EXISTS (SELECT 1 FROM inscription_piece p WHERE p.inscription = i.txid)) AS held, c.collection, c.edition "
    "FROM inscription i LEFT JOIN collection_item c ON c.txid = i.txid")

#: How many offers are standing against a piece, as a column beside it. A
#: correlated count rather than a join, because a marketplace ORDERS on it
#: and the ordering has to happen before the page is cut.
_OFFERS_ON_IT = ("(SELECT COUNT(*) FROM nft_offer o "
                 "WHERE o.inscription = i.txid)")


class LedgerIndex:
    """The token index for one chain: sync it, ask it questions."""

    def __init__(self, path: Path | str, params: Params,
                 rpc_factory: Callable[[], ContextManager[RpcClient]]):
        self.path = Path(path)
        self.params = params
        self._rpc = rpc_factory
        #: Only one sync at a time. The watcher is the only caller in
        #: practice, but a manual "sync now" must not race it.
        self._sync_lock = threading.Lock()
        #: Files read back off the chain: txid -> bytes, oldest first,
        #: trimmed by total size. PER INDEX, not per class: two chains share
        #: this class, txids are only unique within a chain, and a shared
        #: cache would hand one chain's bytes to the other.
        self._content_cache: dict[str, bytes] = {}
        self.stopped: Stopped | None = None
        self.last_sync: float | None = None
        self.last_result: SyncResult | None = None
        self.stats: dict[str, int] = {}

    # --- storage --------------------------------------------------------------

    def open(self) -> Database:
        """A fresh connection with the protocol tables installed.

        Per call, per thread: see the module docstring.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = Database(self.path)
        install_schema(db)
        return db

    @property
    def enabled(self) -> bool:
        """False when the chain has no start block yet (Dogecoin mainnet)."""
        return self.params.activation_height is not None

    # --- syncing --------------------------------------------------------------

    def sync(self, max_blocks: int = 500) -> SyncResult | None:
        """Bring the index up to the node, up to `max_blocks` at a time.

        Returns None when the index is disabled or another sync holds the lock.
        Never raises: a failure is recorded in `stopped` for the interface.
        """
        if not self.enabled:
            return None
        if not self._sync_lock.acquire(blocking=False):
            return None
        try:
            with self._rpc() as rpc, self.open() as db:
                handler = ArcadeHandler(rpc, self.params)
                follower = ChainFollower(rpc, db, self.params, handler)
                result = follower.sync_once(max_blocks=max_blocks)
                for key, value in handler.stats.items():
                    self.stats[key] = self.stats.get(key, 0) + value
        except IndexBelowFloor as exc:
            self._start_again(exc)
        except Exception as exc:
            self._stop(exc)
            return None
        else:
            self.stopped = None
            self.last_sync = time.time()
            self.last_result = result
            if result.connected or result.disconnected:
                log.info("ledger %s: %s", self.params.name, result)
            return result
        finally:
            self._sync_lock.release()

    def _start_again(self, exc: IndexBelowFloor) -> None:
        """Move this index aside and rebuild it from the new floor.

        The move is automatic; the delete never is (D-123). An index is
        derived from the chain and can be rebuilt, so keeping it is not the
        decision -- but a program that DISCARDS on a config change discards on
        a mistaken one, and four floors have been published in two days with
        two of them moved after publishing. A floor a digit too high would
        wipe every updated node the moment it started, silently, before
        anybody read the number. A copy costs a file; the alternative costs
        everything, everywhere, at once, by way of the thing everybody trusts.

        So it is renamed after the FLOOR that displaced it, which is what
        makes it obvious which one to rename back, and the log says where it
        went -- an automatic move that does not say leaves somebody with six
        copies and no idea which is which (a test machine).

        The messaging store next to it is never touched. It holds an address
        book that was typed rather than scanned: losing it is losing work, not
        losing test data, which is precisely the thing a program cannot decide
        for somebody.
        """
        floor = self.params.activation_height
        kept = self.path.with_name(f"{self.path.name}.before-{floor}")
        count = 1
        while kept.exists():
            count += 1
            kept = self.path.with_name(f"{self.path.name}.before-{floor}.{count}")
        moved = False
        try:
            for suffix in ("", "-wal", "-shm"):
                source = Path(str(self.path) + suffix)
                if source.exists():
                    source.rename(str(kept) + suffix)
                    moved = moved or not suffix
        except OSError as exc2:
            self._stop(IndexBelowFloor(f"{exc}\n(and it could not be moved "
                                       f"aside automatically: {exc2})"))
            return
        if not moved:
            self._stop(exc)
            return
        log.warning("ledger %s: the floor is now %s and every block this index "
                    "held was below it, so it has been moved to %s and is being "
                    "rebuilt from %s. Nothing is wrong with the chain.",
                    self.params.name, f"{floor:,}", kept.name, f"{floor:,}")
        self.stopped = None
        self.stats = {}
        self._content_cache = {}

    def _stop(self, exc: Exception) -> None:
        """Record why the index halted, at the height it was working on."""
        height = self.indexed_height()
        working_on = (height + 1) if height is not None else (self.params.activation_height or 0)
        if isinstance(exc, (P.UnknownMessageType, P.OutOfScopeMessageType)):
            reason = (f"a transaction uses Omni message type {exc.message_type}, which "
                      f"this version does not implement. Balances after this block "
                      f"cannot be trusted until a version that does is installed.")
        elif isinstance(exc, IndexBelowFloor):
            # Its own sentence, because the old one named a catastrophic
            # external event for what is a config value somebody changed on
            # purpose ten minutes earlier. A wrong diagnosis in an error
            # message costs more than no message: it aims the investigation
            # (D-123).
            reason = str(exc)
        elif isinstance(exc, NodeUnreachable):
            reason = str(exc)
        elif isinstance(exc, ReorgTooDeep):
            reason = f"the chain reorganised further back than the index can unwind: {exc}"
        else:
            reason = str(exc) or exc.__class__.__name__
        if self.stopped is None or self.stopped.reason != reason:
            log.warning("ledger %s stopped at block %d: %s", self.params.name, working_on, reason)
        self.stopped = Stopped(height=working_on, reason=reason, at=time.time())

    # --- status ---------------------------------------------------------------

    def indexed_height(self) -> int | None:
        try:
            with self.open() as db:
                tip = db.tip()
        except Exception:
            return None
        return int(tip["height"]) if tip is not None else None

    def status(self, node_tip: int | None = None) -> dict[str, Any]:
        """Where the index stands, in the terms a page needs.

        `behind` is measured against `node_tip` when the caller has it (the
        watcher keeps one per chain) and otherwise left unknown rather than
        asked for -- status must be cheap and must not touch the node.
        """
        height = self.indexed_height()
        start = self.params.activation_height
        behind = None
        if node_tip is not None and start is not None:
            behind = max(0, node_tip - (height if height is not None else start - 1))
        return {
            "enabled": self.enabled,
            "activation_height": start,
            "indexed_height": height,
            "node_tip": node_tip,
            "behind": behind,
            # Below the start block there is nothing to read, so "current" is
            # honest there even though the index holds no blocks yet.
            "current": (self.stopped is None and behind == 0),
            "stopped": self.stopped,
            "last_sync": self.last_sync,
            "stats": dict(self.stats),
        }

    # --- queries --------------------------------------------------------------

    def properties(self) -> list[dict[str, Any]]:
        """Every token, main ecosystem first, then test, oldest first."""
        with self.open() as db:
            rows = db.conn.execute(
                "SELECT * FROM property ORDER BY ecosystem, property_id"
            ).fetchall()
            return [self._property_row(db, row) for row in rows]

    def property(self, property_id: int) -> dict[str, Any] | None:
        with self.open() as db:
            row = db.conn.execute(
                "SELECT * FROM property WHERE property_id = ?", (property_id,)
            ).fetchone()
            return self._property_row(db, row) if row is not None else None

    def _property_row(self, db: Database, row: Any) -> dict[str, Any]:
        prop = dict(row)
        prop["divisible"] = prop["property_type"] == PROPERTY_DIVISIBLE
        prop["test_ecosystem"] = prop["ecosystem"] == ECOSYSTEM_TEST
        prop["total_display"] = format_amount(prop["total_tokens"], prop["divisible"])
        prop["holder_count"] = db.conn.execute(
            "SELECT COUNT(*) AS n FROM balance WHERE property_id = ? AND balance > 0",
            (prop["property_id"],),
        ).fetchone()["n"]
        return prop

    def holders(self, property_id: int) -> list[dict[str, Any]]:
        """Who holds a token, largest first."""
        with self.open() as db:
            prop = db.conn.execute(
                "SELECT property_type FROM property WHERE property_id = ?", (property_id,)
            ).fetchone()
            if prop is None:
                return []
            divisible = prop["property_type"] == PROPERTY_DIVISIBLE
            rows = db.conn.execute(
                "SELECT address, balance FROM balance WHERE property_id = ? AND balance > 0 "
                "ORDER BY balance DESC, address",
                (property_id,),
            ).fetchall()
            return [
                {"address": r["address"], "balance": r["balance"],
                 "display": format_amount(r["balance"], divisible)}
                for r in rows
            ]

    def balances(self, addresses: list[str]) -> list[dict[str, Any]]:
        """What a set of addresses holds, one row per (address, token), non-zero only."""
        if not addresses:
            return []
        marks = ",".join("?" * len(addresses))
        with self.open() as db:
            rows = db.conn.execute(
                f"SELECT b.address, b.property_id, b.balance, p.name, p.property_type, "
                f"p.ecosystem, p.issuer FROM balance b JOIN property p USING (property_id) "
                f"WHERE b.address IN ({marks}) AND b.balance > 0 "
                f"ORDER BY p.property_id, b.address",
                tuple(addresses),
            ).fetchall()
        result = []
        for r in rows:
            divisible = r["property_type"] == PROPERTY_DIVISIBLE
            result.append({
                "address": r["address"],
                "property_id": r["property_id"],
                "name": r["name"],
                "issuer": r["issuer"],
                "divisible": divisible,
                "test_ecosystem": r["ecosystem"] == ECOSYSTEM_TEST,
                "balance": r["balance"],
                "display": format_amount(r["balance"], divisible),
            })
        return result

    def balance(self, address: str, property_id: int) -> int:
        with self.open() as db:
            row = db.conn.execute(
                "SELECT balance FROM balance WHERE address = ? AND property_id = ?",
                (address, property_id),
            ).fetchone()
        return int(row["balance"]) if row is not None else 0

    # --- @tags ----------------------------------------------------------------

    def tag_of(self, address: str) -> str | None:
        """The name this address answers to, if it has claimed one."""
        with self.open() as db:
            row = db.conn.execute(
                "SELECT tag FROM tag WHERE address=?", (address,)).fetchone()
            return row["tag"] if row else None

    def address_of(self, tag: str) -> str | None:
        """Who holds a name. None means nobody does -- never "not yet indexed",
        which is why anything spending on the strength of this has to show the
        address it resolved to before it spends."""
        from .tags import normalise

        with self.open() as db:
            row = db.conn.execute(
                "SELECT address FROM tag WHERE tag=?", (normalise(tag),)).fetchone()
            return row["address"] if row else None

    def tags(self, limit: int = 200) -> list[dict[str, Any]]:
        """Every claimed name, newest first."""
        with self.open() as db:
            return [dict(row) for row in db.conn.execute(
                "SELECT * FROM tag ORDER BY block_height DESC, position DESC "
                "LIMIT ?", (max(1, min(limit, 1000)),))]

    def search_tags(self, text: str, limit: int = 25) -> list[dict[str, Any]]:
        """Claimed names containing `text`, the closest first.

        What the address book searches. Prefixes rank above the middle of a
        word, because somebody typing "mar" is far more often looking for
        @robin than for @postmaster, and exact wins outright.
        """
        wanted = str(text or "").strip().lstrip("@").lower()
        if not wanted:
            return []
        like = wanted.replace("%", "").replace("_", "")
        with self.open() as db:
            return [dict(row) for row in db.conn.execute(
                "SELECT * FROM tag WHERE tag LIKE ? "
                "ORDER BY CASE WHEN tag = ? THEN 0 "
                "              WHEN tag LIKE ? THEN 1 ELSE 2 END, "
                "         LENGTH(tag), tag LIMIT ?",
                (f"%{like}%", wanted, f"{like}%", max(1, min(limit, 100))))]

    def tags_for(self, addresses: list[str]) -> dict[str, str]:
        """Names for a batch of addresses: one query for a page full of posts."""
        if not addresses:
            return {}
        marks = ",".join("?" * len(addresses))
        with self.open() as db:
            return {row["address"]: row["tag"] for row in db.conn.execute(
                f"SELECT address, tag FROM tag WHERE address IN ({marks})",
                addresses)}

    # --- inscriptions ---------------------------------------------------------

    def inscriptions(self, owner: str | None = None, creator: str | None = None,
                     limit: int = 100, after: int = -1,
                     offset: int = 0, owners: list[str] | None = None) -> list[dict]:
        """One page, newest first. Never the content: a listing of a hundred
        files would be a hundred files.

        `offset` rather than a cursor here, because these are NUMBERED: a page
        of inscriptions has a page number people can jump to, and jumping to
        page 40 with a cursor means walking 39 pages to find it.
        """
        sql = _INSCRIPTION_SELECT
        where, args = [], []
        if owner:
            where.append("i.owner = ?"); args.append(owner)
        if owners is not None:
            # A wallet is many addresses: what it holds is asked for in one
            # query, not page by page (D-028).
            if not owners:
                return []
            where.append("i.owner IN (%s)" % ",".join("?" * len(owners)))
            args.extend(owners)
        if creator:
            where.append("i.creator = ?"); args.append(creator)
        if after >= 0:
            where.append("i.number > ?"); args.append(after)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY i.number DESC LIMIT ? OFFSET ?"
        args.append(max(1, min(limit, 500)))
        args.append(max(0, offset))
        with self.open() as db:
            return [dict(row) for row in db.conn.execute(sql, args)]

    #: A swap, as it sits in `arcade_tx`: the AnyData type, then INSC, then
    #: version 1 and kind 5 (inscriptions.KIND_SWAP). Matching on the prefix
    #: is what makes a price history cheap -- messages are type 200 too, and
    #: there are thousands of them for every trade.
    SWAP_PREFIX = "000000c8494e53430105"

    def trades(self, limit: int = 500, since_height: int = 0) -> list[dict]:
        """Every swap this chain has read, newest first, as two legs and a time.

        The chart is drawn from these and from the gaps between them: a day
        with no trade is a day with no trade, not a straight line to the next
        one (D-039).
        """
        from . import inscriptions as I

        sql = ("SELECT a.txid, a.block_height, a.sender, a.payload_hex, b.time "
               "FROM arcade_tx a JOIN block b ON b.height = a.block_height "
               "WHERE a.valid = 1 AND a.payload_hex LIKE ? AND a.block_height >= ? "
               "ORDER BY a.block_height DESC, a.position DESC LIMIT ?")
        out = []
        with self.open() as db:
            rows = db.conn.execute(sql, (self.SWAP_PREFIX + "%", int(since_height),
                                         max(1, min(limit, 5000)))).fetchall()
        for row in rows:
            try:
                swap = I.parse(bytes.fromhex(row["payload_hex"])[4:])
            except Exception:
                continue          # a payload that starts like a swap and is not
            if not isinstance(swap, I.Swap):
                continue
            out.append({"txid": row["txid"], "height": row["block_height"],
                        "when": row["time"], "seller": row["sender"],
                        "give": swap.give, "take": swap.take})
        return out

    def shops(self, limit: int = 200) -> list[dict]:
        """Every inscription on this chain whose JSON names a shop.

        Only those still held by whoever created them: a shop is its
        creator's word about their own things, and sending the inscription
        away closes it (swap.py). Read here rather than filtered in the page
        so that the Exchange asks the chain, not a list somebody keeps
        (D-037).
        """
        sql = (_INSCRIPTION_SELECT
               + " WHERE i.creator = i.owner AND i.json LIKE '%\"shop\"%'"
                 " ORDER BY i.number DESC LIMIT ?")
        with self.open() as db:
            return [dict(row) for row in db.conn.execute(sql, (max(1, min(limit, 500)),))]

    def inscription(self, key: str | int) -> dict | None:
        """By txid or by number -- a number is what people say out loud."""
        with self.open() as db:
            column = "number" if isinstance(key, int) else "txid"
            row = db.conn.execute(
                f"{_INSCRIPTION_SELECT} WHERE i.{column}=?", (key,)).fetchone()
            return dict(row) if row else None

    def moves(self, key: str, limit: int = 100) -> list[dict]:
        """Every hand an inscription has changed in, oldest first.

        The inscription row holds only the current owner -- it is overwritten
        on each transfer -- so this is the only place that remembers. What a
        page usually wants from it is the first row whose sender is the
        creator: the moment a piece left the wallet that made it, which is
        what people mean by a mint date when they are asking about provenance
        (D-071).
        """
        with self.open() as db:
            return [dict(r) for r in db.conn.execute(
                "SELECT * FROM inscription_move WHERE inscription=? "
                "ORDER BY block_height, position LIMIT ?",
                (str(key), max(1, min(limit, 500))))]

    def left_the_creator(self, key: str) -> dict | None:
        """The first move out of the wallet that made it, if it has made one."""
        row = self.inscription(str(key))
        if row is None:
            return None
        for move in self.moves(str(key)):
            if move["from_address"] == row["creator"]:
                return move
        return None

    def backfill_moves(self) -> int:
        """Rebuild an inscription's history from transactions already indexed.

        Every transfer and every swap is on the chain as its own message, and
        this node has already read and judged each one -- `arcade_tx` keeps
        the payload, the sender, the reference and the height of all of them,
        valid or not. So the history that was never written down can be
        replayed from what is here, with no rescan of blocks and no trust in
        anything new.

        Ownership is tracked as the replay goes: a piece starts with its
        creator, and each accepted move hands it on. That is what makes a
        transfer's `from` recoverable at all -- the row records who the
        reference was, never who the owner had been.

        A swap needs one thing `arcade_tx` does not keep: the buyer, which is
        the first input that is not the seller's. The node has `txindex=1`
        (the installer writes it), so that is a lookup rather than a rescan.

        Runs once, when the table is empty. Returns how many rows it wrote.
        """
        from . import inscriptions as I
        from . import payload as P

        with self.open() as db:
            if db.conn.execute("SELECT 1 FROM inscription_move LIMIT 1").fetchone():
                return 0
            rows = [dict(r) for r in db.conn.execute(
                "SELECT txid, sender, reference, block_height, position, payload_hex "
                "FROM arcade_tx WHERE valid=1 AND message_type=200 "
                "ORDER BY block_height, position")]
            owner = {r["txid"]: r["creator"] for r in db.conn.execute(
                "SELECT txid, creator FROM inscription")}

        written: list = []
        skipped: list = []
        for row in rows:
            try:
                data = getattr(P.decode(bytes.fromhex(row["payload_hex"])), "data", None)
                if not data or not I.is_inscription(data):
                    continue
                parsed = I.parse(data)
            except Exception:
                continue
            if isinstance(parsed, I.Transfer):
                item = parsed.txid.hex()
                if item not in owner or not row["reference"]:
                    continue
                written.append((row["txid"], item, owner[item], row["reference"],
                                row["block_height"], row["position"], "transfer"))
                owner[item] = row["reference"]
            elif isinstance(parsed, I.Swap):
                for leg, giver_is_sender in ((parsed.give, True), (parsed.take, False)):
                    if leg.kind != I.LEG_INSCRIPTION:
                        continue
                    item = leg.txid.hex()
                    if item not in owner:
                        continue
                    try:
                        other = self._buyer_of(row["txid"], row["sender"])
                    except Exception as exc:
                        # Loudly. This swallowed an AttributeError from a
                        # broken rpc factory on its first live run, skipped
                        # every one of nine swaps, and reported success: four
                        # moves written and no sign that nine were missing.
                        # A partial history that says nothing is worse than a
                        # refusal, because the gap looks like "it never moved"
                        # -- which for provenance is the wrong answer rather
                        # than a missing one (D-073).
                        other = None
                        skipped.append((row["txid"], f"{type(exc).__name__}: {exc}"))
                    if other is None:
                        if not skipped or skipped[-1][0] != row["txid"]:
                            skipped.append((row["txid"], "no input but the seller's"))
                        continue
                    giver = row["sender"] if giver_is_sender else other
                    taker = other if giver_is_sender else row["sender"]
                    written.append((row["txid"], item, giver, taker,
                                    row["block_height"], row["position"], "swap"))
                    owner[item] = taker
        if skipped:
            # Not an exception: the transfers that WERE recovered are worth
            # keeping, and the next run will try the rest. But it has to be
            # visible, and the table stays incomplete rather than pretending.
            log.warning("history backfill could not resolve %d swap%s: %s",
                        len(skipped), "" if len(skipped) == 1 else "s",
                        "; ".join(f"{t[:12]} ({why})" for t, why in skipped[:5]))
        if not written:
            return 0
        with self.open() as db:
            db.conn.executemany(
                "INSERT OR IGNORE INTO inscription_move(txid, inscription, "
                "from_address, to_address, block_height, position, how) "
                "VALUES(?,?,?,?,?,?,?)", written)
            db.conn.commit()
        log.info("rebuilt %d inscription moves from indexed transactions", len(written))
        return len(written)

    def _buyer_of(self, txid: str, seller: str) -> str | None:
        """The first input of a swap that is not the seller's.

        One connection for the whole lookup, because a PrevOutCache holds the
        client it was built with and a cache whose node has been closed under
        it is a cache that raises on its first miss.
        """
        from .indexer import PrevOutCache

        with self._rpc() as rpc:
            cache = PrevOutCache(rpc, self.params)
            tx = rpc.call("getrawtransaction", txid, True)
            for vin in tx.get("vin", []):
                prev = cache.lookup(vin["txid"], int(vin["vout"]))
                if prev.address and prev.address != seller:
                    return prev.address
        return None

    #: Files read back off the chain, newest first, so a page of a hundred
    #: tiles does not ask the node a hundred times for the same picture. Small
    #: on purpose: it is a convenience, not a store, and the point of D-113 is
    #: that nothing here is where the file lives.
    CACHE_BYTES = 64 * 1024 * 1024

    def inscription_content(self, key: str | int) -> tuple[str, bytes] | None:
        """(content type, bytes), read back off the chain if need be.

        The bytes are not kept in this index (D-113). What is kept is which
        transactions carried them, so this fetches those, reassembles, and
        checks the result against the manifest's own sha256 before handing it
        over -- which is a stronger guarantee than a stored copy ever was: a
        stored blob is trusted, a reassembled one is proved.

        None means the file cannot be produced -- no such inscription, or a
        node that cannot serve those transactions -- never "does not exist".
        """
        with self.open() as db:
            column = "number" if isinstance(key, int) else "txid"
            row = db.conn.execute(
                f"SELECT txid, content_type, content, content_len, sha256, chunks "
                f"FROM inscription WHERE {column}=?", (key,)).fetchone()
            if row is None:
                return None
            if row["content"] is not None:
                return row["content_type"], bytes(row["content"])
            pieces = [dict(r) for r in db.conn.execute(
                "SELECT countdown, txid FROM inscription_piece "
                "WHERE inscription=? ORDER BY countdown DESC", (row["txid"],))]
        if not pieces:
            return None                   # indexed before pieces were recorded
        found = self._cached(row["txid"])
        if found is not None:
            return row["content_type"], found
        try:
            content = self._from_chain(row, pieces)
        except Exception as exc:
            log.warning("could not read %s back off the chain: %s",
                        row["txid"][:12], exc)
            return None
        self._remember(row["txid"], content)
        return row["content_type"], content

    def _from_chain(self, row: dict, pieces: list[dict]) -> bytes:
        """Fetch the transactions that carry a file, and prove what comes back."""
        from hashlib import sha256
        from . import inscriptions as I
        from .indexer import PrevOutCache
        from .tx import extract

        assembly = None
        with self._rpc() as rpc:
            cache = PrevOutCache(rpc, self.params)
            for piece in pieces:
                tx = rpc.call("getrawtransaction", piece["txid"], 1)
                # `extract` wants the lookup ITSELF, not the cache around it.
                read = extract(tx, 0, 0, self.params, cache.lookup)
                if read is None:
                    raise ValueError(f"{piece['txid'][:12]} carries no payload")
                body = P.decode(read.payload).data
                chunk = I.parse(body)
                if not isinstance(chunk, I.Chunk):
                    raise ValueError(f"{piece['txid'][:12]} is not a chunk")
                if assembly is None:
                    assembly = I.Assembly(inscription_id=chunk.inscription_id)
                assembly.pieces[chunk.countdown] = chunk.body
        if assembly is None or not assembly.complete():
            raise ValueError("the chain did not give back every piece")
        manifest, content = assembly.join()
        # Proved, not trusted. The index says what the file hashes to and the
        # chain says what it is; a mismatch means one of them is lying and
        # neither answer is safe to serve.
        if sha256(content).hexdigest() != row["sha256"]:
            raise ValueError("what came back does not match the manifest")
        return content


    def _cached(self, txid: str) -> bytes | None:
        found = self._content_cache.get(txid)
        if found is not None:
            # Newest last, so trimming takes the least recently wanted.
            self._content_cache[txid] = self._content_cache.pop(txid)
        return found

    def _remember(self, txid: str, content: bytes) -> None:
        cache = self._content_cache
        cache[txid] = content
        held = sum(len(v) for v in cache.values())
        while held > self.CACHE_BYTES and len(cache) > 1:
            held -= len(cache.pop(next(iter(cache))))

    def inscription_count(self, owner: str | None = None,
                          creator: str | None = None,
                          owners: list[str] | None = None) -> int:
        sql, args = "SELECT COUNT(*) FROM inscription", []
        where = []
        if owner:
            where.append("owner = ?"); args.append(owner)
        if owners is not None:
            if not owners:
                return 0
            where.append("owner IN (%s)" % ",".join("?" * len(owners)))
            args.extend(owners)
        if creator:
            where.append("creator = ?"); args.append(creator)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self.open() as db:
            return int(db.conn.execute(sql, args).fetchone()[0])

    def chunks_seen(self, sender: str, inscription_id: str) -> list[dict]:
        """Pieces of one unfinished set already on the chain, by countdown.

        What a collection run asks before sending the rest after a crash: a
        piece that went out and was never written down is still a piece, and
        the index is the one place it can be found again.
        """
        with self.open() as db:
            return [dict(row) for row in db.conn.execute(
                "SELECT countdown, txid, block_height FROM inscription_chunk "
                "WHERE sender = ? AND inscription_id = ? ORDER BY countdown DESC",
                (sender, inscription_id))]

    # --- collections ----------------------------------------------------------

    def collections(self, limit: int = 100, offset: int = 0,
                    creator: str | None = None) -> list[dict]:
        """Every collection, newest first, with how many it holds and a cover.

        The cover is the lowest edition -- #1 of a HashLips set -- so a wall
        of collections shows each one's first face rather than whatever
        happened to confirm last.
        """
        sql = ("SELECT c.creator, c.collection, COUNT(*) AS count, "
               "MIN(i.number) AS first_number, MAX(i.number) AS last_number, "
               "MIN(c.edition) AS first_edition, MAX(c.edition) AS last_edition "
               "FROM collection_item c JOIN inscription i ON i.txid = c.txid")
        args: list = []
        if creator:
            sql += " WHERE c.creator = ?"
            args.append(creator)
        sql += (" GROUP BY c.creator, c.collection ORDER BY MIN(i.number) DESC "
                "LIMIT ? OFFSET ?")
        args += [max(1, min(limit, 500)), max(0, offset)]
        with self.open() as db:
            out = []
            for row in db.conn.execute(sql, args):
                entry = dict(row)
                cover = db.conn.execute(
                    "SELECT i.txid, i.content_type FROM collection_item c "
                    "JOIN inscription i ON i.txid = c.txid "
                    "WHERE c.creator = ? AND c.collection = ? "
                    "ORDER BY c.edition IS NULL, c.edition, i.number LIMIT 1",
                    (entry["creator"], entry["collection"])).fetchone()
                entry["cover_txid"] = cover["txid"] if cover else None
                entry["cover_type"] = cover["content_type"] if cover else None
                out.append(entry)
            return out

    def collection_count(self, creator: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM (SELECT 1 FROM collection_item"
        args: list = []
        if creator:
            sql += " WHERE creator = ?"
            args.append(creator)
        sql += " GROUP BY creator, collection)"
        with self.open() as db:
            return int(db.conn.execute(sql, args).fetchone()[0])

    def collection(self, creator: str, name: str) -> dict | None:
        """One collection's summary, or None if nobody has inscribed it."""
        rows = self._collection_rows(creator, name)
        return rows[0] if rows else None

    def _collection_rows(self, creator: str, name: str) -> list[dict]:
        with self.open() as db:
            row = db.conn.execute(
                "SELECT c.creator, c.collection, COUNT(*) AS count, "
                "MIN(i.number) AS first_number, MAX(i.number) AS last_number, "
                "MIN(c.edition) AS first_edition, MAX(c.edition) AS last_edition "
                "FROM collection_item c JOIN inscription i ON i.txid = c.txid "
                "WHERE c.creator = ? AND c.collection = ? "
                "GROUP BY c.creator, c.collection", (creator, name)).fetchone()
            if row is None or row["count"] == 0:
                return []
            entry = dict(row)
            cover = db.conn.execute(
                "SELECT i.txid, i.content_type FROM collection_item c "
                "JOIN inscription i ON i.txid = c.txid "
                "WHERE c.creator = ? AND c.collection = ? "
                "ORDER BY c.edition IS NULL, c.edition, i.number LIMIT 1",
                (creator, name)).fetchone()
            entry["cover_txid"] = cover["txid"] if cover else None
            entry["cover_type"] = cover["content_type"] if cover else None
            return [entry]

    def collection_items(self, creator: str, name: str, limit: int = 100,
                         offset: int = 0) -> list[dict]:
        """A collection's inscriptions in edition order -- #1 first."""
        with self.open() as db:
            return [dict(row) for row in db.conn.execute(
                f"{_INSCRIPTION_SELECT} WHERE c.creator = ? AND c.collection = ? "
                f"ORDER BY c.edition IS NULL, c.edition, i.number LIMIT ? OFFSET ?",
                (creator, name, max(1, min(limit, 500)), max(0, offset)))]

    def collection_editions(self, creator: str, name: str) -> set[int]:
        """Which numbers of a set are already on the chain from that address.

        What a second run has to leave out. A collection admits one piece per
        edition (D-120), so re-sending these would pay for inscriptions that
        join nothing.
        """
        with self.open() as db:
            return {row["edition"] for row in db.conn.execute(
                "SELECT edition FROM collection_item WHERE creator = ? "
                "AND collection = ? AND edition IS NOT NULL", (creator, name))}

    def collection_cover(self, creator: str, name: str) -> dict | None:
        """The face of a collection: its #1, if this node can draw it.

        People know a set by its first piece -- #1 is the one on the flyer --
        so a marketplace row shows that, where a shop card shows a random
        member to say "a set of different things" (D-041). The lowest edition
        whose bytes are here and whose type a browser renders wins; when none
        of them can be drawn the true #1 comes back anyway, so the caller can
        say what it is holding instead of drawing a broken image.
        """
        with self.open() as db:
            row = db.conn.execute(
                "SELECT i.txid, i.number, i.content_type, i.json, c.edition, "
                "       ((i.content IS NOT NULL OR EXISTS (SELECT 1 FROM inscription_piece p WHERE p.inscription = i.txid)) "
                "        AND i.content_type LIKE 'image/%') AS drawable "
                "FROM collection_item c JOIN inscription i ON i.txid = c.txid "
                "WHERE c.creator = ? AND c.collection = ? "
                "ORDER BY drawable DESC, c.edition IS NULL, c.edition, i.number "
                "LIMIT 1", (creator, name)).fetchone()
            return dict(row) if row else None

    def collection_offers(self) -> dict[tuple[str, str], int]:
        """How many pieces of each collection have an offer standing on them.

        One query for a whole page of collections: asked per collection this
        is a query per row, and the marketplace draws a hundred rows.
        """
        with self.open() as db:
            return {(row["creator"], row["collection"]): int(row["pieces"])
                    for row in db.conn.execute(
                        "SELECT c.creator, c.collection, "
                        "       COUNT(DISTINCT o.inscription) AS pieces "
                        "FROM nft_offer o "
                        "JOIN collection_item c ON c.txid = o.inscription "
                        "GROUP BY c.creator, c.collection")}

    def collection_held_by(self, creator: str, name: str, owners: list[str],
                           limit: int = 100) -> list[dict]:
        """The pieces of one collection these addresses hold, edition first.

        Asked of the whole set rather than filtered out of the page on show:
        a wallet holding #400 of a collection of five hundred would be told
        it holds none of it, because everything of its own was on page
        seventeen (D-028).
        """
        if not owners:
            return []
        marks = ",".join("?" * len(owners))
        with self.open() as db:
            return [dict(row) for row in db.conn.execute(
                f"{_INSCRIPTION_SELECT} WHERE c.creator = ? AND c.collection = ? "
                f"AND i.owner IN ({marks}) "
                f"ORDER BY c.edition IS NULL, c.edition, i.number LIMIT ?",
                (creator, name, *owners, max(1, min(limit, 500))))]

    def collection_owners(self, creator: str, name: str) -> int:
        """How many addresses hold a piece of this set.

        What a marketplace means by "owners": the piece count is the set's
        size, and the two are the same number only when nobody has sold one.
        """
        with self.open() as db:
            return int(db.conn.execute(
                "SELECT COUNT(DISTINCT i.owner) FROM collection_item c "
                "JOIN inscription i ON i.txid = c.txid "
                "WHERE c.creator = ? AND c.collection = ?",
                (creator, name)).fetchone()[0])

    def collection_market(self, creator: str, name: str,
                          for_sale: Sequence[str] = (), limit: int = 100,
                          offset: int = 0) -> list[dict]:
        """A collection in market order: what can be bought now, then the rest.

        Somebody opening a collection is looking for what they can act on --
        a piece with a price on it, then a piece somebody has already offered
        for -- and after that for the collection itself, BY EDITION: #1, #2,
        #3. The chain's order was the first answer and it was the wrong one
        (D-096 revised in D-116): a set is known by its own numbering, that is
        how people ask for a piece and how every other marketplace lists one,
        and the order a miner happened to confirm things in is a fact about
        mining rather than about the set.

        The ordering is done in SQL because it has to happen BEFORE the page
        is cut: sorting one page's worth puts the for-sale pieces first only
        on the page they already fell on.

        `for_sale` is the txids a shop is selling right now, which is not in
        the ledger at all -- a shop is an inscription whose JSON says so, read
        afresh every time (D-037) -- so the caller passes what it read, in
        the order it wants them shown: cheapest first makes the first card
        the floor.
        """
        # The for-sale group keeps the ORDER IT WAS GIVEN IN -- cheapest
        # first, which is what a floor price means -- by asking where each
        # txid falls in one string. Commas separate them, so nothing can
        # match across the join of two.
        order = "," + ",".join(for_sale) + "," if for_sale else ""
        sql = _INSCRIPTION_SELECT.replace(
            "SELECT ", f"SELECT {_OFFERS_ON_IT} AS offers, ", 1)
        sql += (f" WHERE c.creator = ? AND c.collection = ?"
                f" ORDER BY CASE WHEN instr(?, i.txid) > 0 THEN 0"
                f"               WHEN {_OFFERS_ON_IT} > 0 THEN 1"
                f"               ELSE 2 END,"
                f"          instr(?, i.txid),"
                f"          c.edition IS NULL, c.edition, i.number"
                f" LIMIT ? OFFSET ?")
        args = [creator, name, order, order,
                max(1, min(limit, 500)), max(0, offset)]
        with self.open() as db:
            return [dict(row) for row in db.conn.execute(sql, args)]

    def book(self, property_id: int, limit: int = 50,
             pool: bool = True) -> dict[str, list[dict]]:
        """One pair's book: asks cheapest first, bids dearest first.

        Price is worked out from two integers and kept as a Fraction until
        the last moment, because a book sorted on floats puts orders in an
        order nobody can reproduce (D-048).

        `pool` includes what has been broadcast and not yet mined, marked
        `pending`, and drops what the pool cancels. An order is a public
        statement of a price, and a book that is ten minutes behind the
        prices people are actually offering is a book nobody can trade on
        (D-061). Nothing read from the pool is written down: the ledger is
        built from blocks, and the reserve an order holds moves only when its
        block lands.
        """
        from fractions import Fraction

        with self.open() as db:
            rows = [dict(r) for r in db.conn.execute(
                "SELECT * FROM book_order WHERE sale_property = ? OR want_property = ?",
                (property_id, property_id))]
        if pool:
            fresh, cancelled = self.pending_orders()
            rows = [r for r in rows if r["txid"] not in cancelled]
            rows += [r for r in fresh
                     if property_id in (r["sale_property"], r["want_property"])]
        asks, bids = [], []
        for row in rows:
            selling_token = row["sale_property"] == property_id
            tokens = row["sale_amount"] if selling_token else row["want_amount"]
            coins = row["want_amount"] if selling_token else row["sale_amount"]
            if not tokens:
                continue
            row["tokens"] = tokens
            row["coins"] = coins
            row["price"] = Fraction(coins, tokens)
            (asks if selling_token else bids).append(row)
        # Price first, then time: at the same price the order that was placed
        # first is filled first, which is what every order book means by fair
        # and is the rule a maker is entitled to rely on when they queue
        # behind somebody (D-083).
        #
        # `pending` sorts LAST within a price rather than first. An unmined
        # order has block_height 0, so a naive time sort put the newest thing
        # on the book ahead of orders that had been standing for hours --
        # time priority exactly backwards. It is the newest by definition:
        # it has not been mined yet.
        def when(row):
            return (1, 0, 0) if row.get("pending") else (0, row["block_height"],
                                                         row["position"])

        asks.sort(key=lambda r: (r["price"], when(r)))
        bids.sort(key=lambda r: (-r["price"], when(r)))
        return {"asks": asks[:limit], "bids": bids[:limit]}

    def pending_orders(self) -> tuple[list[dict], set[str]]:
        """(orders broadcast and not yet mined, txids the pool cancels).

        The same rule as `pending_offers`: read fresh on every call, written
        down nowhere. A pool order is a claim, not a settled one -- the tokens
        it sells are reserved when its block lands and not before, so what is
        shown from here is what somebody has said, exactly as the chain will
        read it, one confirmation early.

        Validated as the indexer validates it, minus what only a block can
        answer: two different sides, one of them the coin, amounts in range,
        and the property exists. Whether the seller still holds what it is
        selling is settled by the block, and by the swap that fills it.
        """
        from .indexer import PrevOutCache
        from .tx import extract

        try:
            with self._rpc() as rpc:
                ids = list(rpc.call("getrawmempool") or [])
                if not ids:
                    self._pool_orders = {}
                    return [], set()
                known = getattr(self, "_pool_orders", {})
                cache = PrevOutCache(rpc, self.params)
                found: dict[str, Any] = {}
                for txid in ids:
                    if txid in known:
                        found[txid] = known[txid]
                        continue
                    try:
                        tx = rpc.call("getrawtransaction", txid, True)
                        rtx = extract(tx, 0, 0, self.params, cache.lookup)
                    except Exception:
                        found[txid] = None
                        continue
                    found[txid] = self._order_row(rtx)
                self._pool_orders = found
        except Exception as exc:
            log.debug("mempool orders unavailable: %s", exc)
            return [], set()

        orders, cancels = [], []
        for row in self._pool_orders.values():
            if row is None:
                continue
            (cancels if row.get("cancels") else orders).append(row)
        if not cancels:
            return orders, set()

        # A cancel in the pool takes an order off the book now. Applied to
        # both books -- what is already mined and what is beside it in the
        # pool -- because a price somebody has withdrawn is not a price.
        standing = {o["txid"]: o for o in orders}
        with self.open() as db:
            for mined in db.conn.execute("SELECT * FROM book_order"):
                standing.setdefault(mined["txid"], dict(mined))
        gone = set()
        for cancel in cancels:
            for txid, order in standing.items():
                if order["address"] != cancel["address"]:
                    continue
                if cancel["cancels"] == "ecosystem":
                    gone.add(txid)
                    continue
                if (order["sale_property"] != cancel["sale_property"]
                        or order["want_property"] != cancel["want_property"]):
                    continue
                if cancel["cancels"] == "price":
                    # a/b == c/d without dividing anything, as the engine does
                    if (cancel["want_amount"] * order["sale_amount"]
                            != cancel["sale_amount"] * order["want_amount"]):
                        continue
                gone.add(txid)
        return [o for o in orders if o["txid"] not in gone], gone

    def _order_row(self, rtx) -> dict | None:
        """One mempool transaction as a book row, a cancel, or None."""
        if rtx is None or not rtx.payload:
            return None
        try:
            msg = P.decode(rtx.payload)
        except (P.PayloadError, P.UnknownMessageType, P.OutOfScopeMessageType):
            return None

        base = {"txid": rtx.txid, "block_height": 0, "position": 0,
                "address": rtx.sender, "reserved": 0, "pending": True}
        if isinstance(msg, P.MetaDExCancelEcosystem):
            return {**base, "cancels": "ecosystem", "sale_property": 0,
                    "want_property": 0, "sale_amount": 0, "want_amount": 0}
        if isinstance(msg, (P.MetaDExCancelPrice, P.MetaDExCancelPair)):
            price = isinstance(msg, P.MetaDExCancelPrice)
            return {**base, "cancels": "price" if price else "pair",
                    "sale_property": msg.property_id_for_sale,
                    "want_property": msg.property_id_desired,
                    "sale_amount": getattr(msg, "amount_for_sale", 0),
                    "want_amount": getattr(msg, "amount_desired", 0)}
        if not isinstance(msg, P.MetaDExTrade):
            return None

        sale, want = msg.property_id_for_sale, msg.property_id_desired
        if sale == want or 0 not in (sale, want):
            return None
        if not 0 < msg.amount_for_sale < 2 ** 63 or not 0 < msg.amount_desired < 2 ** 63:
            return None
        if self.property(want if sale == 0 else sale) is None:
            return None
        return {**base, "cancels": "", "sale_property": sale,
                "sale_amount": msg.amount_for_sale, "want_property": want,
                "want_amount": msg.amount_desired}

    def book_pairs(self, pool: bool = True) -> list[int]:
        """Every token with an order standing against the coin.

        The pool counts: a pair whose first order is still unmined has to
        appear, or there is nothing to click on and the order is invisible
        until its block (D-061).
        """
        with self.open() as db:
            rows = db.conn.execute(
                "SELECT DISTINCT CASE WHEN sale_property = 0 THEN want_property "
                "ELSE sale_property END AS pid FROM book_order").fetchall()
        pairs = {int(r["pid"]) for r in rows if r["pid"]}
        if pool:
            fresh, _ = self.pending_orders()
            pairs |= {o["want_property"] if o["sale_property"] == 0
                      else o["sale_property"] for o in fresh}
        return sorted(p for p in pairs if p)

    def order(self, txid: str) -> dict | None:
        """One standing order, by the transaction that placed it.

        Mined only, deliberately: an order in the pool holds no reserve yet,
        so a swap filling it would be refused by the engine for want of a
        balance. It becomes fillable when its block lands (D-062).
        """
        with self.open() as db:
            row = db.conn.execute("SELECT * FROM book_order WHERE txid=?",
                                  (str(txid),)).fetchone()
            return dict(row) if row else None

    def orders_of(self, addresses: list[str], pool: bool = True) -> list[dict]:
        """This wallet's own standing orders, newest first.

        The pool first: an order you have just placed is yours whether or not
        a miner has got to it, and a list that says "you have no orders" the
        moment after you placed one is the bug this removes (D-061).
        """
        if not addresses:
            return []
        marks = ",".join("?" * len(addresses))
        with self.open() as db:
            mine = [dict(r) for r in db.conn.execute(
                f"SELECT * FROM book_order WHERE address IN ({marks}) "
                f"ORDER BY block_height DESC, position DESC", tuple(addresses))]
        if not pool:
            return mine
        fresh, cancelled = self.pending_orders()
        here = set(addresses)
        return ([o for o in fresh if o["address"] in here]
                + [o for o in mine if o["txid"] not in cancelled])

    def offers_on(self, owners: list[str], limit: int = 100) -> list[dict]:
        """Offers standing against inscriptions these addresses hold.

        Read from the chain, so an offer reaches whoever holds the piece
        whether or not they have ever published a key, and whichever of
        their addresses it sits on (D-042). An offer whose item has since
        moved is not theirs to accept and does not appear.
        """
        if not owners:
            return []
        marks = ",".join("?" * len(owners))
        with self.open() as db:
            rows = db.conn.execute(
                f"SELECT o.*, i.number, i.owner, i.content_type, "
                f"       c.collection, c.edition, b.time AS when_ "
                f"FROM nft_offer o "
                f"JOIN inscription i ON i.txid = o.inscription "
                f"LEFT JOIN collection_item c ON c.txid = o.inscription "
                f"LEFT JOIN block b ON b.height = o.block_height "
                f"WHERE i.owner IN ({marks}) "
                f"ORDER BY o.block_height DESC, o.position DESC LIMIT ?",
                tuple(owners) + (max(1, min(limit, 500)),)).fetchall()
        return [dict(row) for row in rows]

    def offer(self, txid: str) -> dict | None:
        """One offer, by the transaction that made it.

        The chain is what an answer is checked against: a wallet that has
        lost its own note of an offer, or was reinstalled since, still knows
        exactly what it asked for, because it is written down where everyone
        can see it (D-049).
        """
        with self.open() as db:
            row = db.conn.execute(
                "SELECT o.*, i.number, i.owner, c.collection, c.edition "
                "FROM nft_offer o JOIN inscription i ON i.txid = o.inscription "
                "LEFT JOIN collection_item c ON c.txid = o.inscription "
                "WHERE o.txid = ?", (str(txid),)).fetchone()
        if row:
            return dict(row)
        # Not in a block yet. The terms are readable in the mempool and are
        # the same terms, so a holder can answer an offer in the minutes
        # before it confirms rather than after (D-058).
        return next((o for o in self.pending_offers() if o["txid"] == str(txid)), None)

    def pending_names(self, limit: int = 200) -> list[dict]:
        """Token names claimed by issuances sitting in the mempool.

        The gap the first version of the name rule left open, found by
        the operator's own two machines: the wallet knew what IT had broadcast and
        nothing about what anybody else had, so a name claimed on another
        node two seconds ago was invisible here until a block landed. The
        chain refuses the second issuance either way -- that part was never
        in question -- but it refuses it AFTER the fee is spent, and a
        refusal that costs money is not the same service as a refusal that
        does not (D-124).

        Read fresh and never written down, exactly as offers and asks are:
        the ledger is built from blocks only, two nodes must agree on what
        the chain says rather than on what their mempools happened to hold,
        and a name claimed by a transaction that never confirms must leave
        nothing behind.
        """
        from .indexer import PrevOutCache
        from .tx import extract

        try:
            with self._rpc() as rpc:
                ids = list(rpc.call("getrawmempool") or [])[:max(0, limit)]
                if not ids:
                    self._pool_names = {}
                    return []
                known = getattr(self, "_pool_names", {})
                cache = PrevOutCache(rpc, self.params)
                found: dict[str, dict | None] = {}
                for txid in ids:
                    if txid in known:
                        found[txid] = known[txid]
                        continue
                    try:
                        tx = rpc.call("getrawtransaction", txid, True)
                        rtx = extract(tx, 0, 0, self.params, cache.lookup)
                    except Exception:                 # gone, or not ours
                        found[txid] = None
                        continue
                    found[txid] = self._name_row(rtx)
                self._pool_names = found
        except Exception as exc:                      # no node, no mempool
            log.debug("mempool names unavailable: %s", exc)
            return []
        return [row for row in self._pool_names.values() if row]

    def _name_row(self, rtx) -> dict | None:
        """One mempool transaction as a claimed name, or None if it is not one."""
        if rtx is None or not rtx.payload:
            return None
        try:
            parsed = P.decode(rtx.payload)
        except (P.PayloadError, P.UnknownMessageType, P.OutOfScopeMessageType):
            return None
        if not isinstance(parsed, (P.IssuanceFixed, P.IssuanceManaged)):
            return None
        name = getattr(parsed, "name", "")
        if not name:
            return None
        return {"txid": rtx.txid, "sender": rtx.sender, "name": name,
                "ecosystem": getattr(parsed, "ecosystem", None)}

    def pending_offers(self, limit: int = 200) -> list[dict]:
        """Offers sitting in the mempool, in the shape `offers_on` returns.

        Read fresh and never written down. The ledger is built from blocks
        only and stays that way -- an offer that never confirms must leave
        nothing behind, and two nodes must agree on what the chain says, not
        on what their own mempool happened to hold. This is the same
        distinction the messenger already makes: the mempool is read for
        carriage, and the ledger for what is settled.

        Why at all: an offer is a message to whoever holds a piece, and a
        block is a minute or ten. Danny made an offer for Goofball #100 and
        neither wallet showed it until the block landed, so both ends
        believed it had never been posted -- it was in the mempool the whole
        time (D-058).

        An offer read here is checked exactly as the indexer would check it:
        the inscription must exist, it must not already be the offerer's, and
        an offer of nothing is not an offer. What is not checked is whether
        the buyer can pay, which is answered when the holder accepts and the
        swap is built -- the same rule the block path uses, for the same
        reason.
        """
        from .indexer import PrevOutCache
        from .tx import extract

        try:
            with self._rpc() as rpc:
                ids = list(rpc.call("getrawmempool") or [])[:max(0, limit)]
                if not ids:
                    self._pool_offers = {}
                    return []
                # Keep what was read before: the mempool is asked on every
                # page load, and re-fetching a transaction that has not
                # changed is the whole cost of this.
                known = getattr(self, "_pool_offers", {})
                cache = PrevOutCache(rpc, self.params)
                found: dict[str, dict | None] = {}
                for txid in ids:
                    if txid in known:
                        found[txid] = known[txid]
                        continue
                    try:
                        tx = rpc.call("getrawtransaction", txid, True)
                        rtx = extract(tx, 0, 0, self.params, cache.lookup)
                    except Exception:                 # gone, or not ours
                        found[txid] = None
                        continue
                    found[txid] = self._offer_row(rtx)
                self._pool_offers = found
        except Exception as exc:                      # no node, no mempool
            log.debug("mempool offers unavailable: %s", exc)
            return []
        return [row for row in self._pool_offers.values() if row]

    def _offer_row(self, rtx) -> dict | None:
        """One mempool transaction as an offer row, or None if it is not one."""
        from . import inscriptions as I

        if rtx is None or not rtx.payload:
            return None
        try:
            parsed = P.decode(rtx.payload)
        except (P.PayloadError, P.UnknownMessageType, P.OutOfScopeMessageType):
            return None
        # An offer travels as AnyData carrying an inscription payload, which
        # is how every arcade inscription travels: the meta-layer sees data,
        # and what the data means is this module's business.
        data = getattr(parsed, "data", None)
        if not data or not I.is_inscription(data):
            return None
        try:
            item = I.parse(data)
        except Exception:
            return None
        if not isinstance(item, I.Offer):
            return None
        row = self.inscription(item.txid.hex())
        if row is None or row["owner"] == rtx.sender:
            return None
        if not item.take.amount and item.take.kind != I.LEG_INSCRIPTION:
            return None
        return {
            "txid": rtx.txid,
            "block_height": 0,
            "position": 0,
            "inscription": item.txid.hex(),
            "buyer": rtx.sender,
            "take_kind": item.take.kind,
            "take_property": item.take.property_id,
            "take_amount": item.take.amount,
            "number": row["number"],
            "owner": row["owner"],
            "content_type": row.get("content_type"),
            "collection": row.get("collection"),
            "edition": row.get("edition"),
            "when_": None,
            "pending": True,
        }

    def pending_asks(self, limit: int = 200) -> list[dict]:
        """Prices sitting in the mempool, in the shape `asks` returns.

        The same reason offers are read from the pool (D-058), met from the
        other side: somebody listed a piece and the marketplace showed
        nothing until the block landed, so it looked as though the listing
        had failed -- it was in the mempool the whole time (D-117).

        Read fresh and never written down: the ledger is built from blocks
        and stays that way. Checked exactly as the engine checks one -- the
        inscription must exist, the seller must hold it now, and a price is
        in coins or in a token -- so a pending ask is refused here for the
        same reasons it would be refused in a block.
        """
        from .indexer import PrevOutCache
        from .tx import extract

        try:
            with self._rpc() as rpc:
                ids = list(rpc.call("getrawmempool") or [])[:max(0, limit)]
                if not ids:
                    self._pool_asks = {}
                    return []
                known = getattr(self, "_pool_asks", {})
                cache = PrevOutCache(rpc, self.params)
                found: dict[str, dict | None] = {}
                for txid in ids:
                    if txid in known:
                        found[txid] = known[txid]
                        continue
                    try:
                        tx = rpc.call("getrawtransaction", txid, True)
                        rtx = extract(tx, 0, 0, self.params, cache.lookup)
                    except Exception:                 # gone, or not ours
                        found[txid] = None
                        continue
                    found[txid] = self._ask_row(rtx)
                self._pool_asks = found
        except Exception as exc:                      # no node, no mempool
            log.debug("mempool asks unavailable: %s", exc)
            return []
        return [row for row in self._pool_asks.values() if row]

    def _ask_row(self, rtx) -> dict | None:
        """One mempool transaction as an ask row, or None if it is not one."""
        from . import inscriptions as I

        if rtx is None or not rtx.payload:
            return None
        try:
            parsed = P.decode(rtx.payload)
        except (P.PayloadError, P.UnknownMessageType, P.OutOfScopeMessageType):
            return None
        data = getattr(parsed, "data", None)
        if not data or not I.is_inscription(data):
            return None
        try:
            item = I.parse(data)
        except Exception:
            return None
        if not isinstance(item, I.Ask) or item.cancelled:
            return None
        row = self.inscription(item.txid.hex())
        # Only the holder may price a piece, and a price is a number: the
        # engine's own two rules, applied here so the pool cannot show what a
        # block would refuse.
        if row is None or row["owner"] != rtx.sender:
            return None
        if item.take.kind == I.LEG_INSCRIPTION or not item.take.amount:
            return None
        return {
            "txid": rtx.txid,
            "block_height": 0,
            "position": 0,
            "inscription": item.txid.hex(),
            "seller": rtx.sender,
            "take_kind": item.take.kind,
            "take_property": item.take.property_id,
            "take_amount": item.take.amount,
            "number": row["number"],
            "owner": row["owner"],
            "creator": row.get("creator"),
            "content_type": row.get("content_type"),
            "held": row.get("held"),
            "collection": row.get("collection"),
            "edition": row.get("edition"),
            "when_": None,
            "pending": True,
        }

    def offers_by(self, buyers: list[str], limit: int = 100) -> list[dict]:
        """Offers these addresses have made, whatever became of them."""
        if not buyers:
            return []
        marks = ",".join("?" * len(buyers))
        with self.open() as db:
            rows = db.conn.execute(
                f"SELECT o.*, i.number, i.owner, c.collection, c.edition, "
                f"       b.time AS when_ "
                f"FROM nft_offer o "
                f"JOIN inscription i ON i.txid = o.inscription "
                f"LEFT JOIN collection_item c ON c.txid = o.inscription "
                f"LEFT JOIN block b ON b.height = o.block_height "
                f"WHERE o.buyer IN ({marks}) "
                f"ORDER BY o.block_height DESC, o.position DESC LIMIT ?",
                tuple(buyers) + (max(1, min(limit, 500)),)).fetchall()
        return [dict(row) for row in rows]

    #: A live ask: the newest one for its inscription, not a withdrawal, and
    #: still made by the address that holds the piece. The last clause is what
    #: makes a price honest without reserving anything -- a piece that has been
    #: sold or sent away stops being for sale with no transaction at all.
    _LIVE_ASK = (
        "SELECT a.*, i.owner, i.number, i.creator, i.content_type, "
        "       (i.content IS NOT NULL OR EXISTS (SELECT 1 FROM inscription_piece p WHERE p.inscription = i.txid)) AS held, c.collection, c.edition, "
        "       b.time AS when_ "
        "FROM nft_ask a "
        "JOIN inscription i ON i.txid = a.inscription "
        "LEFT JOIN collection_item c ON c.txid = a.inscription "
        "LEFT JOIN block b ON b.height = a.block_height "
        "WHERE a.take_kind != 0 AND a.seller = i.owner "
        "  AND NOT EXISTS (SELECT 1 FROM nft_ask n WHERE n.inscription = a.inscription "
        "                  AND (n.block_height > a.block_height "
        "                       OR (n.block_height = a.block_height "
        "                           AND n.position > a.position)))")

    def asks(self, limit: int = 200) -> list[dict]:
        """Every price standing on this chain, newest first."""
        with self.open() as db:
            return [dict(row) for row in db.conn.execute(
                f"{self._LIVE_ASK} ORDER BY a.block_height DESC, a.position DESC "
                f"LIMIT ?", (max(1, min(limit, 500)),))]

    def ask_on(self, inscription: str) -> dict | None:
        """The price standing on one piece, if there is one."""
        with self.open() as db:
            row = db.conn.execute(
                f"{self._LIVE_ASK} AND a.inscription = ?", (str(inscription),)).fetchone()
            return dict(row) if row else None

    def asks_in(self, creator: str, name: str) -> list[dict]:
        """The prices standing on one collection's pieces."""
        with self.open() as db:
            return [dict(row) for row in db.conn.execute(
                f"{self._LIVE_ASK} AND c.creator = ? AND c.collection = ?",
                (creator, name))]

    def collection_asks(self) -> dict[tuple[str, str], int]:
        """How many pieces of each collection have a price on them."""
        with self.open() as db:
            return {(row["creator"], row["collection"]): int(row["pieces"])
                    for row in db.conn.execute(
                        f"SELECT c.creator, c.collection, COUNT(*) AS pieces "
                        f"FROM ({self._LIVE_ASK}) x "
                        f"JOIN collection_item c ON c.txid = x.inscription "
                        f"GROUP BY c.creator, c.collection")}

    def collection_thumb(self, creator: str | None, name: str) -> dict | None:
        """One piece of a collection, picked at random, for a thumbnail.

        Random rather than the cover, so a collection looks like what it is
        -- a set of different things -- instead of like one picture that
        happens to be edition #1 (D-041). Only a piece whose bytes this node
        holds and that a browser will draw: a thumbnail that 404s is worse
        than no thumbnail.
        """
        sql = ("SELECT i.txid, i.number, i.content_type FROM collection_item c "
               "JOIN inscription i ON i.txid = c.txid "
               "WHERE c.collection = ? AND (i.content IS NOT NULL OR EXISTS (SELECT 1 FROM inscription_piece p WHERE p.inscription = i.txid)) "
               "  AND i.content_type LIKE 'image/%' ")
        args: list[Any] = [name]
        if creator:
            sql += "AND c.creator = ? "
            args.append(creator)
        with self.open() as db:
            row = db.conn.execute(sql + "ORDER BY RANDOM() LIMIT 1", args).fetchone()
            return dict(row) if row else None

    def collection_traits(self, creator: str, name: str) -> dict[str, dict[str, int]]:
        """How often each trait value occurs across the collection.

        Read from every item's JSON, HashLips style: `attributes` as a list of
        `{"trait_type": ..., "value": ...}`. That is what rarity is: how many
        of the set share a value, and nothing more.
        """
        import json as jsonlib
        counts: dict[str, dict[str, int]] = {}
        with self.open() as db:
            rows = db.conn.execute(
                "SELECT i.json FROM collection_item c JOIN inscription i "
                "ON i.txid = c.txid WHERE c.creator = ? AND c.collection = ?",
                (creator, name)).fetchall()
        for row in rows:
            try:
                data = jsonlib.loads(row["json"])
            except ValueError:
                continue
            attributes = data.get("attributes") if isinstance(data, dict) else None
            if not isinstance(attributes, list):
                continue
            for trait in attributes:
                if not isinstance(trait, dict):
                    continue
                kind = str(trait.get("trait_type", ""))[:100]
                value = str(trait.get("value", ""))[:100]
                if not kind:
                    continue
                bucket = counts.setdefault(kind, {})
                bucket[value] = bucket.get(value, 0) + 1
        return counts

    def unfinished_inscriptions(self, sender: str | None = None) -> list[dict]:
        """Sets that have pieces on chain and are not complete.

        Worth showing: an abandoned set is paid for and will never become
        anything, and the owner is the only one who can finish it.
        """
        sql = ("SELECT sender, inscription_id, COUNT(*) AS have, "
               "MAX(countdown) AS highest, MIN(block_height) AS first_block "
               "FROM inscription_chunk")
        args = []
        if sender:
            sql += " WHERE sender = ?"
            args.append(sender)
        sql += " GROUP BY sender, inscription_id"
        with self.open() as db:
            out = []
            for row in db.conn.execute(sql, args):
                entry = dict(row)
                entry["expected"] = entry["highest"] + 1
                out.append(entry)
            return out

    def history(self, property_id: int | None = None, address: str | None = None,
                limit: int = 200) -> list[dict[str, Any]]:
        """Recorded transactions, newest first, decoded for display.

        Filtered by token when `property_id` is given: the payload is what names
        the token, so every candidate is decoded and the ones about other tokens
        dropped -- there are few enough of these for that to be the right trade
        against a denormalised column the journal would have to carry.
        """
        where, args = [], []
        if address is not None:
            where.append("(sender = ? OR reference = ?)")
            args += [address, address]
        if property_id is not None:
            where.append("(message_type != 200)")     # never about a token
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        with self.open() as db:
            rows = db.conn.execute(
                f"SELECT t.*, b.time AS block_time, b.hash AS block_hash FROM arcade_tx t "
                f"JOIN block b ON b.height = t.block_height {clause} "
                f"ORDER BY t.block_height DESC, t.position DESC",
                tuple(args),
            ).fetchall()
            # A creation row's payload names no property id -- the engine
            # assigned it -- so the token's own creation is matched by txid.
            creation = None
            if property_id is not None:
                found = db.conn.execute(
                    "SELECT creation_txid FROM property WHERE property_id = ?", (property_id,)
                ).fetchone()
                creation = found["creation_txid"] if found else None
            props = {r["property_id"]: r for r in db.conn.execute("SELECT * FROM property")}
        out = []
        for row in rows:
            entry = self._history_entry(row, props)
            if property_id is not None and entry["property_id"] != property_id \
                    and row["txid"] != creation:
                continue
            if row["txid"] == creation:
                entry["property_id"] = property_id
            out.append(entry)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _history_entry(row: Any, props: dict[int, Any]) -> dict[str, Any]:
        entry = dict(row)
        entry["type_name"] = TYPE_NAMES.get(row["message_type"], f"type {row['message_type']}")
        entry["property_id"] = None
        entry["amount"] = None
        entry["amount_display"] = ""
        entry["name"] = ""
        try:
            msg = P.decode(bytes.fromhex(row["payload_hex"]))
        except P.PayloadError:
            return entry
        pid = getattr(msg, "property_id", None)
        if pid is not None:
            entry["property_id"] = pid
            prop = props.get(pid)
            if prop is not None:
                entry["name"] = prop["name"]
                divisible = prop["property_type"] == PROPERTY_DIVISIBLE
            else:
                divisible = False
            amount = getattr(msg, "amount", None)
            if amount is not None:
                entry["amount"] = amount
                entry["amount_display"] = format_amount(amount, divisible)
        elif isinstance(msg, (P.IssuanceFixed, P.IssuanceManaged)):
            entry["name"] = msg.name
            if isinstance(msg, P.IssuanceFixed):
                entry["amount"] = msg.amount
                entry["amount_display"] = format_amount(
                    msg.amount, msg.property_type == PROPERTY_DIVISIBLE)
        return entry

    def transaction(self, txid: str) -> dict[str, Any] | None:
        with self.open() as db:
            row = db.conn.execute(
                "SELECT t.*, b.time AS block_time, b.hash AS block_hash FROM arcade_tx t "
                "JOIN block b ON b.height = t.block_height WHERE txid = ?", (txid,)
            ).fetchone()
            if row is None:
                return None
            props = {r["property_id"]: r for r in db.conn.execute("SELECT * FROM property")}
        return self._history_entry(row, props)


# --- amounts ------------------------------------------------------------------
#
# Omni stores every amount as an integer. A divisible token counts in
# hundred-millionths, exactly like the coin itself (omnicore.h: COIN); an
# indivisible one counts in whole units. Both directions live here so the
# create form, the send form and every display agree.


def format_amount(units: int, divisible: bool) -> str:
    if not divisible:
        return f"{units:,}"
    whole, frac = divmod(units, COIN)
    text = f"{whole:,}.{frac:08d}".rstrip("0")
    return text[:-1] if text.endswith(".") else text


class AmountError(ValueError):
    """The text is not an amount this token can hold."""


def parse_amount(text: str, divisible: bool) -> int:
    """Turn what the user typed into token units, refusing anything lossy.

    Decimal rather than float: `0.1 * COIN` is not an integer, and a send that
    silently rounds is a send of the wrong amount.
    """
    from decimal import Decimal, InvalidOperation

    raw = (text or "").strip().replace(",", "").replace("_", "")
    if not raw:
        raise AmountError("enter an amount.")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise AmountError(f"{text!r} is not a number.") from None
    if value <= 0:
        raise AmountError("the amount must be more than zero.")
    if divisible:
        scaled = value * COIN
        if scaled != scaled.to_integral_value():
            raise AmountError("this token has eight decimal places at most.")
        units = int(scaled)
    else:
        if value != value.to_integral_value():
            raise AmountError("this token is indivisible: whole units only.")
        units = int(value)
    from .state import MAX_AMOUNT
    if units > MAX_AMOUNT:
        raise AmountError("the amount is larger than any token supply can be.")
    return units
