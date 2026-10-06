"""The cross-chain book: testnet assets for MAINNET Pepecoin, run by this node.

2026-10-06: "What can we do to add a swap where you can exchange test net
assets for main net Pepecoin?" -> option B, "that way nobody has to stay online.
The trades should happen automatically once you place an order just like test
net to test net trading", a fee of 0.5% a trade, any asset (tokens, testnet
coins and NFTs), six confirmations, and "make sure people know that it is not
completely trustless and to make sure that they trust the node operator".

Two chains cannot be made to move together in one transaction, so this node holds
both sides while an order stands -- that is the trust the pages say out loud:

* An order IS its deposit. A sell sends the testnet asset to this node's testnet
  exchange address; a buy sends mainnet PEPE to its mainnet one. The order is
  written down first (AWAITING), tied to that deposit's txid, and counts once the
  deposit has CONFIRMATIONS on its chain and was checked to deliver exactly what
  the order says, to the exchange address (an injected check: this module never
  reads a chain itself).
* The book is matched here, not by any chain: a newly counted order takes the
  resting ones on the other side at THEIR price, best first and then the oldest,
  partially if need be -- the rule token/token pairs follow on chain.
* What a match owes is written down as PAYOUTS in the same transaction as the
  match, so a crash can never leave a trade half-recorded. A payout is built and
  its raw transaction SAVED before it is broadcast; after a crash the same bytes
  go out again, and a payout is never built twice (`pay` is idempotent).
* The node's fee is FEE_PERMILLE of the PEPE of each trade, taken from what the
  seller receives. The buyer pays exactly the price.
* A cancel and a match go through the same lock, so a cancel can never refund
  what a match has already promised somebody else.

Amounts are integers throughout: an asset in its own raw units (a token's, a
testnet coin's satoshis, 1 for an NFT) and PEPE in mainnet satoshis.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterator

FEE_PERMILLE = 5                     # 0.5% of the PEPE of a trade
CONFIRMATIONS = {"testnet": 6, "main": 2}
KINDS = ("token", "coin", "nft")
SIDES = ("sell", "buy")

AWAITING, OPEN, FILLED, CANCELLED, FAILED = "awaiting", "open", "filled", "cancelled", "failed"
QUEUED, BUILT, SENT, STUCK = "queued", "built", "sent", "stuck"

SCHEMA = """
CREATE TABLE IF NOT EXISTS xorder (
    id            TEXT PRIMARY KEY,
    created       REAL NOT NULL,
    owner         TEXT NOT NULL,           -- account pubkey hex, or 'node'
    side          TEXT NOT NULL,           -- sell (deposits the asset) / buy (deposits PEPE)
    kind          TEXT NOT NULL,           -- token / coin / nft
    asset         TEXT NOT NULL,           -- property id, 'coin', or the inscription id
    amount        INTEGER NOT NULL,        -- of the asset, raw units
    pepe          INTEGER NOT NULL,        -- mainnet satoshis for `amount`: the price
    left_amount   INTEGER NOT NULL,
    left_pepe     INTEGER NOT NULL,        -- a buy: PEPE of its deposit not yet spent
    pay_test      TEXT NOT NULL,           -- testnet address: the asset bought, or a sell's refund
    pay_main      TEXT NOT NULL,           -- mainnet address: a sale's PEPE, or a buy's refund
    deposit_chain TEXT NOT NULL,           -- testnet / main
    deposit_txid  TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,
    counted       REAL NOT NULL DEFAULT 0, -- when its deposit counted: its place in the queue
    note          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS xorder_book ON xorder(kind, asset, side, status);
CREATE UNIQUE INDEX IF NOT EXISTS xorder_deposit ON xorder(deposit_txid) WHERE deposit_txid <> '';
CREATE TABLE IF NOT EXISTS xfill (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    created   REAL NOT NULL,
    kind      TEXT NOT NULL,
    asset     TEXT NOT NULL,
    maker     TEXT NOT NULL,
    taker     TEXT NOT NULL,
    seller    TEXT NOT NULL,
    buyer     TEXT NOT NULL,
    amount    INTEGER NOT NULL,
    pepe      INTEGER NOT NULL,
    fee       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS xpayout (
    id        TEXT PRIMARY KEY,            -- 'fill:<n>:asset', 'fill:<n>:pepe', 'refund:<order>', ...
    created   REAL NOT NULL,
    chain     TEXT NOT NULL,               -- testnet / main
    kind      TEXT NOT NULL,               -- token / coin / nft / pepe
    asset     TEXT NOT NULL,
    amount    INTEGER NOT NULL,
    to_addr   TEXT NOT NULL,
    raw       TEXT NOT NULL DEFAULT '',
    txid      TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL,
    tries     INTEGER NOT NULL DEFAULT 0,
    error     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS xpayout_status ON xpayout(status);
"""


class XchainError(Exception):
    pass


def fee_of(pepe: int) -> int:
    """The node's cut of a trade's PEPE, rounded down (in the seller's favour)."""
    return pepe * FEE_PERMILLE // 1000


class Book:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._open() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _open(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=60, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")      # one writer at a time: match, cancel, pay
            yield conn
            if write:
                conn.execute("COMMIT")
        except BaseException:
            if write:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # --- orders ---------------------------------------------------------------

    def place(self, *, owner: str, side: str, kind: str, asset: str, amount: int,
              pepe: int, pay_test: str, pay_main: str) -> dict:
        """Write an order down before its deposit is signed. It is AWAITING its
        deposit until `deposited` ties a txid to it."""
        if side not in SIDES:
            raise XchainError("an order buys or sells")
        if kind not in KINDS:
            raise XchainError(f"an asset is a {', '.join(KINDS)}")
        amount, pepe = int(amount), int(pepe)
        if amount <= 0 or pepe <= 0:
            raise XchainError("an amount and a price, both above zero")
        if kind == "nft" and amount != 1:
            raise XchainError("an NFT is one of a kind: the amount is 1")
        if not pay_test or not pay_main:
            raise XchainError("an order needs an address on each chain to be paid at")
        row = {"id": secrets.token_hex(12), "created": time.time(), "owner": owner,
               "side": side, "kind": kind, "asset": str(asset), "amount": amount,
               "pepe": pepe, "left_amount": amount,
               "left_pepe": pepe if side == "buy" else 0,
               "pay_test": pay_test, "pay_main": pay_main,
               "deposit_chain": "testnet" if side == "sell" else "main",
               "status": AWAITING}
        with self._open(write=True) as conn:
            conn.execute(
                "INSERT INTO xorder (id, created, owner, side, kind, asset, amount, pepe, "
                "left_amount, left_pepe, pay_test, pay_main, deposit_chain, status) VALUES "
                "(:id,:created,:owner,:side,:kind,:asset,:amount,:pepe,:left_amount,:left_pepe,"
                ":pay_test,:pay_main,:deposit_chain,:status)", row)
        return self.get(row["id"])

    def deposited(self, order_id: str, txid: str) -> dict:
        """The order's deposit was broadcast as `txid`. One deposit, one order."""
        with self._open(write=True) as conn:
            row = conn.execute("SELECT * FROM xorder WHERE id=?", (order_id,)).fetchone()
            if row is None:
                raise XchainError("there is no such order")
            if row["status"] != AWAITING or row["deposit_txid"]:
                raise XchainError("that order already has its deposit")
            try:
                conn.execute("UPDATE xorder SET deposit_txid=? WHERE id=?", (txid, order_id))
            except sqlite3.IntegrityError:
                raise XchainError("that deposit already belongs to another order") from None
        return self.get(order_id)

    def get(self, order_id: str) -> dict | None:
        with self._open() as conn:
            row = conn.execute("SELECT * FROM xorder WHERE id=?", (order_id,)).fetchone()
            return dict(row) if row else None

    def orders_of(self, owner: str, limit: int = 100) -> list[dict]:
        with self._open() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM xorder WHERE owner=? ORDER BY created DESC LIMIT ?", (owner, limit))]

    def awaiting(self) -> list[dict]:
        with self._open() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM xorder WHERE status=? AND deposit_txid<>'' ORDER BY created",
                (AWAITING,))]

    def book(self, kind: str, asset: str) -> dict[str, list[dict]]:
        """What stands on one market: sells cheapest first, buys dearest first,
        then the oldest. `price` is PEPE satoshis per raw unit, as a Fraction."""
        with self._open() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM xorder WHERE kind=? AND asset=? AND status=?",
                (kind, str(asset), OPEN))]
        for r in rows:
            r["price"] = Fraction(r["pepe"], r["amount"])
        sells = sorted((r for r in rows if r["side"] == "sell"), key=lambda r: (r["price"], r["counted"], r["id"]))
        buys = sorted((r for r in rows if r["side"] == "buy"), key=lambda r: (-r["price"], r["counted"], r["id"]))
        return {"sells": sells, "buys": buys}

    def markets(self) -> list[dict]:
        """Every market with an order standing or a trade done: kind, asset,
        trades, orders, last price."""
        with self._open() as conn:
            seen = {}
            for r in conn.execute("SELECT kind, asset, COUNT(*) AS n FROM xorder WHERE status=? "
                                  "GROUP BY kind, asset", (OPEN,)):
                seen[(r["kind"], r["asset"])] = {"kind": r["kind"], "asset": r["asset"],
                                                 "orders": r["n"], "trades": 0, "last": None}
            for r in conn.execute("SELECT kind, asset, COUNT(*) AS n FROM xfill GROUP BY kind, asset"):
                m = seen.setdefault((r["kind"], r["asset"]), {"kind": r["kind"], "asset": r["asset"],
                                                              "orders": 0, "trades": 0, "last": None})
                m["trades"] = r["n"]
                last = conn.execute("SELECT amount, pepe FROM xfill WHERE kind=? AND asset=? "
                                    "ORDER BY id DESC LIMIT 1", (r["kind"], r["asset"])).fetchone()
                m["last"] = Fraction(last["pepe"], last["amount"]) if last else None
        return list(seen.values())

    def fills(self, kind: str, asset: str, limit: int = 500) -> list[dict]:
        with self._open() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM xfill WHERE kind=? AND asset=? ORDER BY id DESC LIMIT ?",
                (kind, str(asset), limit))]

    # --- deposits count, and the book matches ----------------------------------

    def count_deposit(self, order_id: str, now: float | None = None) -> list[dict]:
        """The order's deposit has its confirmations and was checked: it opens and
        meets the book. Returns the fills it made."""
        with self._open(write=True) as conn:
            row = conn.execute("SELECT * FROM xorder WHERE id=?", (order_id,)).fetchone()
            if row is None or row["status"] != AWAITING or not row["deposit_txid"]:
                return []
            conn.execute("UPDATE xorder SET status=?, counted=? WHERE id=?",
                         (OPEN, now or time.time(), order_id))
            return self._match(conn, order_id)

    def fail_deposit(self, order_id: str, why: str) -> None:
        """The deposit never arrived, or did not deliver what the order said."""
        with self._open(write=True) as conn:
            conn.execute("UPDATE xorder SET status=?, note=? WHERE id=? AND status=?",
                         (FAILED, why[:300], order_id, AWAITING))

    def _match(self, conn: sqlite3.Connection, order_id: str) -> list[dict]:
        taker = dict(conn.execute("SELECT * FROM xorder WHERE id=?", (order_id,)).fetchone())
        other = "buy" if taker["side"] == "sell" else "sell"
        makers = [dict(r) for r in conn.execute(
            "SELECT * FROM xorder WHERE kind=? AND asset=? AND side=? AND status=? AND id<>?",
            (taker["kind"], taker["asset"], other, OPEN, order_id))]
        if other == "sell":
            makers.sort(key=lambda r: (Fraction(r["pepe"], r["amount"]), r["counted"], r["id"]))
        else:
            makers.sort(key=lambda r: (-Fraction(r["pepe"], r["amount"]), r["counted"], r["id"]))
        made = []
        for m in makers:
            if taker["left_amount"] <= 0:
                break
            # the maker's price, PEPE per unit = m.pepe / m.amount
            if taker["side"] == "buy":
                if m["pepe"] * taker["amount"] > taker["pepe"] * m["amount"]:
                    break                                     # dearer than the buyer's price
                units = min(m["left_amount"], taker["left_amount"],
                            taker["left_pepe"] * m["amount"] // m["pepe"])
                cost = -(-units * m["pepe"] // m["amount"])   # the seller is never paid below its price
                while units > 0 and cost > taker["left_pepe"]:
                    units -= 1
                    cost = -(-units * m["pepe"] // m["amount"])
                seller, buyer = m, taker
            else:
                if m["pepe"] * taker["amount"] < taker["pepe"] * m["amount"]:
                    break                                     # cheaper than the seller's price
                units = min(m["left_amount"], taker["left_amount"])
                cost = units * m["pepe"] // m["amount"]       # the buyer never pays above its price
                if cost > m["left_pepe"]:
                    units = m["left_pepe"] * m["amount"] // m["pepe"]
                    cost = units * m["pepe"] // m["amount"]
                seller, buyer = taker, m
            if units <= 0 or cost <= 0:
                continue
            made.append(self._fill(conn, maker=m, taker=taker, seller=seller, buyer=buyer,
                                   units=units, cost=cost))
            taker = dict(conn.execute("SELECT * FROM xorder WHERE id=?", (order_id,)).fetchone())
        return made

    def _fill(self, conn, *, maker, taker, seller, buyer, units: int, cost: int) -> dict:
        fee = fee_of(cost)
        now = time.time()
        cur = conn.execute(
            "INSERT INTO xfill (created, kind, asset, maker, taker, seller, buyer, amount, pepe, fee) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now, maker["kind"], maker["asset"], maker["id"], taker["id"], seller["id"],
             buyer["id"], units, cost, fee))
        n = cur.lastrowid
        for order, spent_pepe in ((seller, 0), (buyer, cost)):
            fresh = conn.execute("SELECT * FROM xorder WHERE id=?", (order["id"],)).fetchone()
            left = fresh["left_amount"] - units
            left_pepe = fresh["left_pepe"] - spent_pepe
            if left < 0 or left_pepe < 0:
                raise XchainError(f"fill {n} would take more than order {order['id']} holds")
            done = left == 0 or (fresh["side"] == "buy" and left_pepe == 0)
            conn.execute("UPDATE xorder SET left_amount=?, left_pepe=?, status=? WHERE id=?",
                         (left, left_pepe, FILLED if done else OPEN, order["id"]))
            if done and fresh["side"] == "buy" and left_pepe > 0:
                # a buy that got everything it wanted for less than it put down
                self._queue(conn, f"refund:{fresh['id']}", "main", "pepe", "pepe",
                            left_pepe, fresh["pay_main"])
        self._queue(conn, f"fill:{n}:asset", "testnet", maker["kind"], maker["asset"],
                    units, buyer["pay_test"])
        self._queue(conn, f"fill:{n}:pepe", "main", "pepe", "pepe", cost - fee, seller["pay_main"])
        return {"id": n, "amount": units, "pepe": cost, "fee": fee,
                "seller": seller["id"], "buyer": buyer["id"]}

    def _queue(self, conn, pid: str, chain: str, kind: str, asset: str, amount: int, to: str) -> None:
        if amount <= 0:
            return
        conn.execute("INSERT INTO xpayout (id, created, chain, kind, asset, amount, to_addr, status) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (pid, time.time(), chain, kind, str(asset), int(amount), to, QUEUED))

    # --- cancelling -----------------------------------------------------------

    def cancel(self, order_id: str, owner: str) -> dict:
        """Take what is left of an order off the book and queue its deposit back."""
        with self._open(write=True) as conn:
            row = conn.execute("SELECT * FROM xorder WHERE id=?", (order_id,)).fetchone()
            if row is None or row["owner"] != owner:
                raise XchainError("no order of yours by that id")
            if row["status"] == AWAITING:
                conn.execute("UPDATE xorder SET status=?, note=? WHERE id=?",
                             (CANCELLED, "cancelled before its deposit counted", order_id))
                # a deposit that arrives later is refunded by the watcher (refund_late)
                return dict(row, status=CANCELLED)
            if row["status"] != OPEN:
                raise XchainError(f"that order is {row['status']}")
            conn.execute("UPDATE xorder SET status=? WHERE id=?", (CANCELLED, order_id))
            if row["side"] == "sell":
                self._queue(conn, f"refund:{order_id}", "testnet", row["kind"], row["asset"],
                            row["left_amount"], row["pay_test"])
            else:
                self._queue(conn, f"refund:{order_id}", "main", "pepe", "pepe",
                            row["left_pepe"], row["pay_main"])
            return dict(row, status=CANCELLED)

    def refund_late(self, order_id: str) -> None:
        """A deposit that counted for an order cancelled while it waited: give it back."""
        with self._open(write=True) as conn:
            row = conn.execute("SELECT * FROM xorder WHERE id=?", (order_id,)).fetchone()
            if row is None or row["status"] != CANCELLED:
                return
            if conn.execute("SELECT 1 FROM xpayout WHERE id=?", (f"refund:{order_id}",)).fetchone():
                return
            if row["side"] == "sell":
                self._queue(conn, f"refund:{order_id}", "testnet", row["kind"], row["asset"],
                            row["amount"], row["pay_test"])
            else:
                self._queue(conn, f"refund:{order_id}", "main", "pepe", "pepe", row["pepe"], row["pay_main"])

    def cancelled_with_deposit(self) -> list[dict]:
        """Orders cancelled while their deposit waited, not yet refunded."""
        with self._open() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT o.* FROM xorder o WHERE o.status=? AND o.deposit_txid<>'' AND o.counted=0 "
                "AND NOT EXISTS (SELECT 1 FROM xpayout p WHERE p.id = 'refund:' || o.id)", (CANCELLED,))]

    def refund_received(self, order: dict, got: int) -> None:
        """A deposit that arrived but was not what its order said: back as it came."""
        if got <= 0:
            return
        with self._open(write=True) as conn:
            if conn.execute("SELECT 1 FROM xpayout WHERE id=?", (f"refund:{order['id']}",)).fetchone():
                return
            if order["side"] == "buy":
                self._queue(conn, f"refund:{order['id']}", "main", "pepe", "pepe", got, order["pay_main"])
            else:
                self._queue(conn, f"refund:{order['id']}", "testnet", order["kind"], order["asset"],
                            got, order["pay_test"])

    # --- paying out -----------------------------------------------------------

    def payouts(self, status: str | None = None) -> list[dict]:
        with self._open() as conn:
            if status:
                return [dict(r) for r in conn.execute(
                    "SELECT * FROM xpayout WHERE status=? ORDER BY created", (status,))]
            return [dict(r) for r in conn.execute("SELECT * FROM xpayout ORDER BY created")]

    def pay(self, build: Callable[[dict], tuple[str, str]],
            broadcast: Callable[[dict], str], limit: int = 20) -> list[dict]:
        """Send what is owed. Each payout is BUILT once -- `build(payout)` returns
        (raw_hex, txid), and both are saved before anything is broadcast -- and
        then `broadcast(payout)` sends those saved bytes, as often as it takes. A
        crash between the two leaves BUILT, and the next pass sends the same
        bytes again: there is never a second transaction for one payout."""
        import fcntl
        lock = open(self.path.with_suffix(".paylock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)   # one payer at a time, across processes
        except OSError:
            lock.close()
            return []
        try:
            return self._pay(build, broadcast, limit)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    def _pay(self, build, broadcast, limit: int) -> list[dict]:
        done = []
        for p in self.payouts(QUEUED)[:limit]:
            try:
                raw, txid = build(p)
            except Exception as exc:                       # nothing was built: try again later
                with self._open(write=True) as conn:
                    conn.execute("UPDATE xpayout SET tries=tries+1, error=?, status=? WHERE id=? AND status=?",
                                 (str(exc)[:300], STUCK if p["tries"] >= 20 else QUEUED, p["id"], QUEUED))
                continue
            with self._open(write=True) as conn:
                hit = conn.execute("UPDATE xpayout SET raw=?, txid=?, status=? WHERE id=? AND status=?",
                                   (raw, txid, BUILT, p["id"], QUEUED)).rowcount
            if not hit:
                continue                                   # somebody else built it first
        for p in self.payouts(BUILT)[:limit]:
            try:
                txid = broadcast(p)
            except Exception as exc:
                # the same bytes again next pass; after many refusals a person looks
                # (the bytes are kept: STUCK is never rebuilt by itself)
                with self._open(write=True) as conn:
                    conn.execute("UPDATE xpayout SET tries=tries+1, error=?, status=? WHERE id=?",
                                 (str(exc)[:300], STUCK if p["tries"] >= 50 else BUILT, p["id"]))
                continue
            with self._open(write=True) as conn:
                conn.execute("UPDATE xpayout SET status=?, error='' WHERE id=? AND status=?",
                             (SENT, p["id"], BUILT))
            done.append(dict(p, status=SENT, txid=txid or p["txid"]))
        return done

    # --- what the node must hold ----------------------------------------------

    def owed(self) -> dict[tuple[str, str, str], int]:
        """Everything this node holds for other people, by (chain, kind, asset):
        what open orders hold back plus what is queued or built and not sent.
        The exchange wallets must always hold at least this (`check`)."""
        out: dict[tuple[str, str, str], int] = {}
        with self._open() as conn:
            for r in conn.execute("SELECT * FROM xorder WHERE status=?", (OPEN,)):
                if r["side"] == "sell":
                    key = ("testnet", r["kind"], r["asset"])
                    out[key] = out.get(key, 0) + r["left_amount"]
                else:
                    key = ("main", "pepe", "pepe")
                    out[key] = out.get(key, 0) + r["left_pepe"]
            for r in conn.execute("SELECT * FROM xpayout WHERE status IN (?,?)", (QUEUED, BUILT)):
                key = (r["chain"], r["kind"], r["asset"])
                out[key] = out.get(key, 0) + r["amount"]
        return out
