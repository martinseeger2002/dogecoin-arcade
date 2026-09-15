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
from typing import Any, Callable, ContextManager

from . import payload as P
from .chain import ChainFollower, ReorgTooDeep, SyncResult
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
_INSCRIPTION_SELECT = (
    "SELECT i.txid, i.number, i.creator, i.owner, i.block_height, i.position, "
    "i.content_type, i.content_len, i.sha256, i.json, i.chunks, "
    "i.content IS NOT NULL AS held, c.collection, c.edition "
    "FROM inscription i LEFT JOIN collection_item c ON c.txid = i.txid")


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

    def _stop(self, exc: Exception) -> None:
        """Record why the index halted, at the height it was working on."""
        height = self.indexed_height()
        working_on = (height + 1) if height is not None else (self.params.activation_height or 0)
        if isinstance(exc, (P.UnknownMessageType, P.OutOfScopeMessageType)):
            reason = (f"a transaction uses Omni message type {exc.message_type}, which "
                      f"this version does not implement. Balances after this block "
                      f"cannot be trusted until a version that does is installed.")
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

    def inscription_content(self, key: str | int) -> tuple[str, bytes] | None:
        """(content type, bytes) if this node kept them, else None.

        None means "not held here", never "does not exist": the row says how
        long it is and what it hashes to, so it can be fetched back off the
        chain and proved.
        """
        with self.open() as db:
            column = "number" if isinstance(key, int) else "txid"
            row = db.conn.execute(
                f"SELECT content_type, content FROM inscription WHERE {column}=?",
                (key,)).fetchone()
            if row is None or row["content"] is None:
                return None
            return row["content_type"], bytes(row["content"])

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
               "WHERE c.collection = ? AND i.content IS NOT NULL "
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
