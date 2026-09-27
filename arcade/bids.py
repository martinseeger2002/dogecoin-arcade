"""Pre-signed offers: the buyer's half of a swap, signed when the offer is made.

2026-09-27: "Accepted offers need to complete the moment the offer is
accepted", whether or not the buyer is online. An offer used to be a message:
the seller's Accept answered it with a signed leg, and the trade waited for the
buyer's browser to sign the payment. Now the buyer signs that payment when they
make the offer -- every output fixed with ALL|ANYONECANPAY (`funding.build_bid`)
-- and this node keeps the signatures beside the offer. The seller's Accept adds
one coin of exactly `funding.EXACT_SELLER_COIN` in front, signs the whole, and
the node broadcasts it: one press, one transaction, no second party waiting.

What the node holds is signatures, never a key. A signature over these outputs
can only ever pay the seller this price for this piece and give the buyer their
change; the worst anybody holding it can do is complete the trade the buyer
asked for. The buyer's coins stay theirs: spending any of them ends the offer
(`Bids.stale` says so), and `/account/offer/withdraw` does exactly that.

A row is one offer. `id` is the offer transaction's txid once it is broadcast,
and the node's own offer id before (`status` 'unsigned' until the signatures
come back).
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS bid (
    id          TEXT PRIMARY KEY,         -- the offer's txid, once it has one
    draft       TEXT NOT NULL DEFAULT '', -- the node's offer id, before that
    network     TEXT NOT NULL,
    buyer       TEXT NOT NULL,            -- the buyer's address
    buyer_key   TEXT NOT NULL DEFAULT '', -- their coin public key, hex
    account     TEXT NOT NULL,            -- the buyer's account pubkey
    piece       TEXT NOT NULL,
    seller      TEXT NOT NULL,            -- who held the piece when offered
    take        TEXT NOT NULL,            -- the price, as swap.leg_json
    price_sats  INTEGER NOT NULL,         -- the coins output 1 carries, less the seller's coin
    inputs      TEXT NOT NULL,            -- [{txid, vout, value, address}]
    outputs     TEXT NOT NULL,            -- [[value, script hex]]
    signatures  TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL,            -- unsigned | open | filled | withdrawn | stale
    created     REAL NOT NULL,
    done_txid   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS bid_draft ON bid(draft);
CREATE INDEX IF NOT EXISTS bid_account ON bid(account, status);
"""

OPEN = "open"
UNSIGNED = "unsigned"


class BidError(Exception):
    pass


class Bids:
    def __init__(self, path: Path):
        self.path = Path(path)
        with self._open() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        for key in ("take", "inputs", "outputs", "signatures"):
            out[key] = json.loads(out[key] or "null")
        return out

    def draft(self, *, draft: str, network: str, buyer: str, account: str,
              piece: str, seller: str, take: dict, price_sats: int,
              inputs: list[dict], outputs: list[tuple[int, bytes]]) -> None:
        """The bid built with an offer, before either is signed."""
        with self._open() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bid(id, draft, network, buyer, account, piece, "
                "seller, take, price_sats, inputs, outputs, status, created) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"draft:{draft}", draft, network, buyer, account, piece, seller,
                 json.dumps(take), int(price_sats),
                 json.dumps([{k: c[k] for k in ("txid", "vout", "value", "address")}
                             for c in inputs]),
                 json.dumps([[int(v), s.hex()] for v, s in outputs]),
                 UNSIGNED, time.time()))

    def redraft(self, row_id: str, inputs: list[dict], outputs: list[tuple[int, bytes]]) -> None:
        """The half as built on the offer's own set-aside coin."""
        with self._open() as conn:
            conn.execute("UPDATE bid SET inputs=?, outputs=? WHERE id=?",
                         (json.dumps([{k: c[k] for k in ("txid", "vout", "value", "address")}
                                      for c in inputs]),
                          json.dumps([[int(v), s.hex()] for v, s in outputs]), row_id))

    def by_draft(self, draft: str) -> dict | None:
        with self._open() as conn:
            return self._row(conn.execute(
                "SELECT * FROM bid WHERE draft=? ORDER BY created DESC LIMIT 1",
                (draft,)).fetchone())

    def sign(self, draft: str, txid: str, signatures: list[str], buyer_key: str) -> dict:
        """The buyer's signatures, and the offer's txid as the row's name."""
        row = self.by_draft(draft)
        if row is None or row["status"] != UNSIGNED:
            raise BidError("there is no unsigned offer by that id")
        if len(signatures) != len(row["inputs"]):
            raise BidError(f"{len(row['inputs'])} signatures were needed and "
                           f"{len(signatures)} came")
        with self._open() as conn:
            conn.execute("UPDATE bid SET id=?, signatures=?, buyer_key=?, status=? "
                         "WHERE id=?", (txid, json.dumps(signatures), buyer_key,
                                        OPEN, row["id"]))
        return self.get(txid)

    def get(self, txid: str) -> dict | None:
        with self._open() as conn:
            return self._row(conn.execute("SELECT * FROM bid WHERE id=?",
                                          (str(txid),)).fetchone())

    def close(self, txid: str, status: str, done_txid: str = "") -> None:
        with self._open() as conn:
            conn.execute("UPDATE bid SET status=?, done_txid=? WHERE id=?",
                         (status, done_txid, str(txid)))

    def reserved(self, account: str, network: str = "") -> frozenset:
        """The coins this account's standing offers are signed over: not to be
        spent on anything else, or the offer quietly dies."""
        with self._open() as conn:
            rows = conn.execute(
                "SELECT inputs FROM bid WHERE account=? AND status IN (?, ?)"
                + (" AND network=?" if network else ""),
                (account, OPEN, UNSIGNED, *( [network] if network else []))).fetchall()
        out = set()
        for r in rows:
            for c in json.loads(r["inputs"] or "[]"):
                if c.get("txid"):
                    out.add((c["txid"], int(c["vout"])))
        return frozenset(out)

    def statuses(self, txids: list[str]) -> dict[str, str]:
        if not txids:
            return {}
        marks = ",".join("?" * len(txids))
        with self._open() as conn:
            return {r["id"]: r["status"] for r in conn.execute(
                f"SELECT id, status FROM bid WHERE id IN ({marks})", tuple(txids))}

    def open_of(self, account: str, network: str) -> list[dict]:
        with self._open() as conn:
            return [self._row(r) for r in conn.execute(
                "SELECT * FROM bid WHERE account=? AND network=? AND status=?",
                (account, network, OPEN))]


def outputs_of(row: dict) -> list[tuple[int, bytes]]:
    return [(int(v), bytes.fromhex(s)) for v, s in row["outputs"]]
