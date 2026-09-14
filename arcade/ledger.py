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

    # --- inscriptions ---------------------------------------------------------

    def inscriptions(self, owner: str | None = None, creator: str | None = None,
                     limit: int = 100, after: int = -1) -> list[dict]:
        """Newest first, or from a number onwards. Never the content: a listing
        of a hundred files would be a hundred files."""
        sql = ("SELECT txid, number, creator, owner, block_height, position, "
               "content_type, content_len, sha256, json, chunks, "
               "content IS NOT NULL AS held FROM inscription")
        where, args = [], []
        if owner:
            where.append("owner = ?"); args.append(owner)
        if creator:
            where.append("creator = ?"); args.append(creator)
        if after >= 0:
            where.append("number > ?"); args.append(after)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY number DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        with self.open() as db:
            return [dict(row) for row in db.conn.execute(sql, args)]

    def inscription(self, key: str | int) -> dict | None:
        """By txid or by number -- a number is what people say out loud."""
        with self.open() as db:
            column = "number" if isinstance(key, int) else "txid"
            row = db.conn.execute(
                f"SELECT txid, number, creator, owner, block_height, position, "
                f"content_type, content_len, sha256, json, chunks, "
                f"content IS NOT NULL AS held FROM inscription WHERE {column}=?",
                (key,)).fetchone()
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

    def inscription_count(self) -> int:
        with self.open() as db:
            return int(db.conn.execute("SELECT COUNT(*) FROM inscription").fetchone()[0])

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
