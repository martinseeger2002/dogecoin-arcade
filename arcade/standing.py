"""Buy orders that fill without the buyer standing over them (2026-09-28:
"all orders should fill while the user is away ... whatever type of swaps we
have should be atomic").

A sell order can fill while its maker is away because the tokens it sells are
held back by the ledger. A buy order pays in coins, and coins are not in the
ledger: nothing can hold a coin back for whoever turns up with tokens. So a buy
order here is held by this node rather than written on the chain, in one of two
modes the buyer picks (a checkbox, the operator's choice):

* ``away`` -- the buyer pre-signs LOTS: coins of their own, each signed
  SINGLE|ANYONECANPAY over one output, their own change. This node keeps the
  signatures (never publishes them) and, when a sell order at or under the
  price appears, spends one lot as the buyer: a take (type 29) of that sell
  order, paying its maker from the lot, the tokens coming out of the seller's
  reserve to the buyer. The change is fixed by the buyer's signature, so a lot
  can never spend more than its own price and fee; what the buyer trusts is
  this node, with that much, not to pay somebody else.
* ``back`` -- nothing is signed ahead. When a sell order crosses the price the
  buyer's phone is told (a push), and the next time they open the app it signs
  the take itself. Trusts nobody; fills when the buyer is back.

Either way the tokens only ever come out of a sell order's reserve, in the same
transaction as the payment, which is the atomic part.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS standing (
    id          TEXT PRIMARY KEY,
    network     TEXT NOT NULL,
    account     TEXT NOT NULL,
    buyer       TEXT NOT NULL,
    buyer_key   TEXT NOT NULL DEFAULT '',
    property_id INTEGER NOT NULL,
    units       INTEGER NOT NULL,
    coins       INTEGER NOT NULL,
    left_units  INTEGER NOT NULL,
    away        INTEGER NOT NULL,
    status      TEXT NOT NULL,
    created     REAL NOT NULL,
    told        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS standing_open ON standing(network, status);
CREATE TABLE IF NOT EXISTS lot (
    standing   TEXT NOT NULL,
    txid       TEXT NOT NULL,
    vout       INTEGER NOT NULL,
    value      INTEGER NOT NULL,
    units      INTEGER NOT NULL,
    change     INTEGER NOT NULL,
    signature  TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL,
    done_txid  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (txid, vout)
);
CREATE INDEX IF NOT EXISTS lot_standing ON lot(standing, status);
"""

OPEN, UNSIGNED, FILLED, CANCELLED = "open", "unsigned", "filled", "cancelled"
LOT_OPEN, LOT_UNSIGNED, LOT_USED, LOT_GONE = "open", "unsigned", "used", "gone"


class StandingError(Exception):
    pass


class Standing:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._open() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add(self, *, network: str, account: str, buyer: str, buyer_key: str,
            property_id: int, units: int, coins: int, away: bool) -> dict:
        row = {"id": secrets.token_hex(12), "network": network, "account": account,
               "buyer": buyer, "buyer_key": buyer_key, "property_id": int(property_id),
               "units": int(units), "coins": int(coins), "left_units": int(units),
               "away": 1 if away else 0, "status": UNSIGNED if away else OPEN,
               "created": time.time()}
        with self._open() as conn:
            conn.execute(
                "INSERT INTO standing (id, network, account, buyer, buyer_key, property_id, "
                "units, coins, left_units, away, status, created) VALUES "
                "(:id,:network,:account,:buyer,:buyer_key,:property_id,:units,:coins,"
                ":left_units,:away,:status,:created)", row)
        return row

    def get(self, standing_id: str) -> dict | None:
        with self._open() as conn:
            row = conn.execute("SELECT * FROM standing WHERE id=?", (standing_id,)).fetchone()
        return dict(row) if row else None

    def open_on(self, network: str, property_id: int | None = None) -> list[dict]:
        with self._open() as conn:
            rows = conn.execute(
                "SELECT * FROM standing WHERE network=? AND status=? AND left_units>0"
                + (" AND property_id=?" if property_id is not None else "")
                + " ORDER BY created",
                (network, OPEN) + ((int(property_id),) if property_id is not None else ())
            ).fetchall()
        return [dict(r) for r in rows]

    def mine(self, account: str, network: str) -> list[dict]:
        with self._open() as conn:
            rows = conn.execute(
                "SELECT * FROM standing WHERE account=? AND network=? AND status IN (?,?) "
                "ORDER BY created DESC", (account, network, OPEN, UNSIGNED)).fetchall()
        return [dict(r) for r in rows]

    def set(self, standing_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._open() as conn:
            conn.execute(f"UPDATE standing SET {cols} WHERE id=?",
                         (*fields.values(), standing_id))

    def took(self, standing_id: str, units: int) -> None:
        """`units` of it filled: what is left, and filled when nothing is."""
        with self._open() as conn:
            conn.execute("UPDATE standing SET left_units = MAX(0, left_units - ?) WHERE id=?",
                         (int(units), standing_id))
            conn.execute("UPDATE standing SET status=? WHERE id=? AND left_units=0",
                         (FILLED, standing_id))

    def add_lots(self, standing_id: str, lots: list[dict]) -> None:
        with self._open() as conn:
            for lot in lots:
                conn.execute(
                    "INSERT OR REPLACE INTO lot (standing, txid, vout, value, units, change, "
                    "status) VALUES (?,?,?,?,?,?,?)",
                    (standing_id, lot["txid"], int(lot["vout"]), int(lot["value"]),
                     int(lot["units"]), int(lot["change"]), LOT_UNSIGNED))

    def lots(self, standing_id: str, status: str | None = None) -> list[dict]:
        with self._open() as conn:
            rows = conn.execute(
                "SELECT * FROM lot WHERE standing=?" + (" AND status=?" if status else "")
                + " ORDER BY vout", (standing_id,) + ((status,) if status else ())).fetchall()
        return [dict(r) for r in rows]

    def sign_lots(self, standing_id: str, signatures: list[str]) -> None:
        lots = self.lots(standing_id, LOT_UNSIGNED)
        if len(signatures) != len(lots):
            raise StandingError(f"{len(lots)} lots to sign and {len(signatures)} signatures")
        with self._open() as conn:
            for lot, sig in zip(lots, signatures):
                conn.execute("UPDATE lot SET signature=?, status=? WHERE txid=? AND vout=?",
                             (str(sig), LOT_OPEN, lot["txid"], lot["vout"]))
            conn.execute("UPDATE standing SET status=? WHERE id=? AND status=?",
                         (OPEN, standing_id, UNSIGNED))

    def lot_done(self, txid: str, vout: int, status: str, done_txid: str = "") -> None:
        with self._open() as conn:
            conn.execute("UPDATE lot SET status=?, done_txid=?, "
                         "signature=CASE WHEN ?='used' THEN signature ELSE '' END "
                         "WHERE txid=? AND vout=?", (status, done_txid, status, txid, vout))

    def cancel(self, standing_id: str) -> None:
        """Stop it, and forget every signature it holds: a lot's coins are then
        just the buyer's coins again."""
        with self._open() as conn:
            conn.execute("UPDATE standing SET status=? WHERE id=?", (CANCELLED, standing_id))
            conn.execute("UPDATE lot SET status=?, signature='' WHERE standing=? AND status IN (?,?)",
                         (LOT_GONE, standing_id, LOT_OPEN, LOT_UNSIGNED))

    def reserved(self, account: str, network: str = "") -> frozenset:
        """The coins this account's open buy orders are signed over (or about to be)."""
        with self._open() as conn:
            rows = conn.execute(
                "SELECT l.txid, l.vout FROM lot l JOIN standing s ON s.id = l.standing "
                "WHERE s.account=? AND (?='' OR s.network=?) AND l.status IN (?,?)",
                (account, network, network, LOT_OPEN, LOT_UNSIGNED)).fetchall()
        return frozenset((r[0], int(r[1])) for r in rows)
