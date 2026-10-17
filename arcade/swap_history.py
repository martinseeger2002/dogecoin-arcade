"""Lock-script swaps that finished, for their markets' Recent trades, chart and day line.

2026-10-17: "The swap worked but the recent trades didn't update and there is no chart
started." A swap is settled by the two pages between them (wallet.js tradeTick); the node never
saw it end. So each page, when its swap is done, says so (POST /account/trade/done) with the two
lock transactions, and this node files it once its chain shows both locks. Both pages report the
same swap id: the first says one side, the second fills in the other.

Kept the way the market reads it: the base and the quote as the order book orients them
(trade_orders.base_first), raw amounts, who sold the base and who bought it.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS swap_trade (
    id           TEXT PRIMARY KEY,
    base         TEXT NOT NULL,
    quote        TEXT NOT NULL,
    base_amount  INTEGER NOT NULL,
    quote_amount INTEGER NOT NULL,
    seller       TEXT NOT NULL DEFAULT '',
    buyer        TEXT NOT NULL DEFAULT '',
    at           INTEGER NOT NULL,
    lock_base    TEXT NOT NULL DEFAULT '',
    lock_quote   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS swap_trade_pair ON swap_trade(base, quote, at);
"""


class SwapHistory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        with self._open() as conn:
            conn.executescript(SCHEMA)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, trade_id: str, base: str, quote: str, base_amount: int, quote_amount: int,
               seller: str = "", buyer: str = "", lock_base: str = "", lock_quote: str = "",
               at: int | None = None) -> None:
        """File one finished swap, or fill in the side a first report did not know."""
        with self._lock, self._open() as conn:
            conn.execute(
                "INSERT INTO swap_trade (id, base, quote, base_amount, quote_amount, seller, buyer, at,"
                " lock_base, lock_quote) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "  seller = CASE WHEN swap_trade.seller = '' THEN excluded.seller ELSE swap_trade.seller END,"
                "  buyer = CASE WHEN swap_trade.buyer = '' THEN excluded.buyer ELSE swap_trade.buyer END,"
                "  lock_base = CASE WHEN swap_trade.lock_base = '' THEN excluded.lock_base ELSE swap_trade.lock_base END,"
                "  lock_quote = CASE WHEN swap_trade.lock_quote = '' THEN excluded.lock_quote ELSE swap_trade.lock_quote END",
                (trade_id, base, quote, int(base_amount), int(quote_amount), seller, buyer,
                 int(at or time.time()), lock_base, lock_quote))

    def for_pair(self, base: str, quote: str, limit: int = 500) -> list[dict]:
        """A market's finished swaps, newest first."""
        with self._open() as conn:
            rows = conn.execute("SELECT * FROM swap_trade WHERE base = ? AND quote = ? ORDER BY at DESC LIMIT ?",
                                (base, quote, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def get(self, trade_id: str) -> dict | None:
        with self._open() as conn:
            row = conn.execute("SELECT * FROM swap_trade WHERE id = ?", (trade_id,)).fetchone()
        return dict(row) if row else None

    def of(self, addresses: set[str], limit: int = 300) -> list[dict]:
        """Every finished swap these addresses were a side of (Trade history)."""
        if not addresses:
            return []
        marks = ",".join("?" * len(addresses))
        with self._open() as conn:
            rows = conn.execute(f"SELECT * FROM swap_trade WHERE seller IN ({marks}) OR buyer IN ({marks})"
                                " ORDER BY at DESC LIMIT ?", (*addresses, *addresses, int(limit))).fetchall()
        return [dict(r) for r in rows]
