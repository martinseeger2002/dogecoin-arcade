"""SQLite storage for messages, key announcements, and scan progress.

Kept separate from the protocol-state database: messaging is testnet-only
(D-010) and its data is not consensus state. Mixing them would let a testnet
reset damage the ledger database.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
-- X25519 public keys announced on-chain, keyed by the announcing address.
-- An address may announce more than once (rotation); we keep every one, because
-- knowing that a key CHANGED and when is exactly what a user needs to notice.
CREATE TABLE IF NOT EXISTS key_announcement (
    txid        TEXT PRIMARY KEY,
    address     TEXT NOT NULL,
    pubkey      BLOB NOT NULL,
    fingerprint TEXT NOT NULL,
    height      INTEGER NOT NULL,
    block_time  INTEGER NOT NULL,
    seen_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS key_announcement_addr ON key_announcement(address, height);

-- Every candidate payload we have seen, decrypted or not. Undecryptable ones are
-- kept deliberately: they cost little, and re-scanning the chain after importing
-- a second identity would otherwise mean a full re-sync.
CREATE TABLE IF NOT EXISTS candidate (
    txid        TEXT PRIMARY KEY,
    height      INTEGER NOT NULL,
    position    INTEGER NOT NULL,
    block_time  INTEGER NOT NULL,
    sender_addr TEXT NOT NULL,
    payload     BLOB NOT NULL,
    msg_type    INTEGER NOT NULL,
    msg_id      BLOB,
    countdown   INTEGER,
    opened      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS candidate_height ON candidate(height, position);
CREATE INDEX IF NOT EXISTS candidate_msgid ON candidate(msg_id);
CREATE INDEX IF NOT EXISTS candidate_unopened ON candidate(opened);

-- Messages we successfully decrypted.
CREATE TABLE IF NOT EXISTS message (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id        BLOB,
    first_txid    TEXT NOT NULL,
    last_txid     TEXT NOT NULL,
    height        INTEGER NOT NULL,
    block_time    INTEGER NOT NULL,
    sender_addr   TEXT NOT NULL,
    sender_pubkey BLOB NOT NULL,
    recipient_fp  TEXT NOT NULL,
    body          BLOB NOT NULL,
    complete      INTEGER NOT NULL DEFAULT 1,
    read_at       INTEGER,
    UNIQUE (first_txid, recipient_fp)
);
CREATE INDEX IF NOT EXISTS message_time ON message(block_time DESC);

-- Resumable scanning: one cursor per network.
CREATE TABLE IF NOT EXISTS scan_state (
    network     TEXT PRIMARY KEY,
    last_height INTEGER NOT NULL,
    last_hash   TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@dataclass
class StoredMessage:
    id: int
    sender_addr: str
    sender_pubkey: bytes
    body: bytes
    height: int
    block_time: int
    complete: bool
    read_at: int | None
    first_txid: str


class MessageStore:
    """Message and key-announcement storage."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "MessageStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- meta -----------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # --- scan cursor ----------------------------------------------------------

    def scan_cursor(self, network: str) -> tuple[int, str] | None:
        row = self.conn.execute(
            "SELECT last_height, last_hash FROM scan_state WHERE network=?", (network,)
        ).fetchone()
        return (row["last_height"], row["last_hash"]) if row else None

    def set_scan_cursor(self, network: str, height: int, block_hash: str) -> None:
        self.conn.execute(
            "INSERT INTO scan_state(network,last_height,last_hash,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(network) DO UPDATE SET "
            "last_height=excluded.last_height, last_hash=excluded.last_hash, "
            "updated_at=excluded.updated_at",
            (network, height, block_hash, int(time.time())),
        )

    def rewind(self, network: str, height: int) -> int:
        """Drop everything at or above `height`, for reorg handling.

        Testnet reorgs are common and can be deep. A scanner that does not unwind
        would keep messages from orphaned blocks forever, which is worse than
        missing them: they look real.
        """
        cur = self.conn.execute("DELETE FROM candidate WHERE height >= ?", (height,))
        removed = cur.rowcount or 0
        self.conn.execute("DELETE FROM message WHERE height >= ?", (height,))
        self.conn.execute("DELETE FROM key_announcement WHERE height >= ?", (height,))
        self.conn.execute(
            "UPDATE scan_state SET last_height=? WHERE network=? AND last_height >= ?",
            (height - 1, network, height),
        )
        return removed

    # --- key announcements ----------------------------------------------------

    def add_key_announcement(
        self, txid: str, address: str, pubkey: bytes, fingerprint: str,
        height: int, block_time: int,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO key_announcement"
            "(txid,address,pubkey,fingerprint,height,block_time,seen_at) VALUES(?,?,?,?,?,?,?)",
            (txid, address, pubkey, fingerprint, height, block_time, int(time.time())),
        )

    def key_for(self, address: str) -> sqlite3.Row | None:
        """The most recent announced key for an address."""
        return self.conn.execute(
            "SELECT * FROM key_announcement WHERE address=? ORDER BY height DESC LIMIT 1",
            (address,),
        ).fetchone()

    def key_history(self, address: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM key_announcement WHERE address=? ORDER BY height", (address,)
        ))

    def all_keys(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT address, pubkey, fingerprint, MAX(height) AS height "
            "FROM key_announcement GROUP BY address ORDER BY height DESC"
        ))

    # --- candidates -----------------------------------------------------------

    def add_candidate(
        self, txid: str, height: int, position: int, block_time: int,
        sender_addr: str, payload: bytes, msg_type: int,
        msg_id: bytes | None, countdown: int | None,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO candidate"
            "(txid,height,position,block_time,sender_addr,payload,msg_type,msg_id,countdown) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (txid, height, position, block_time, sender_addr, payload, msg_type, msg_id, countdown),
        )

    def unopened_candidates(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM candidate WHERE opened=0 ORDER BY height, position"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(self.conn.execute(sql))

    def mark_opened(self, txid: str) -> None:
        self.conn.execute("UPDATE candidate SET opened=1 WHERE txid=?", (txid,))

    def chunks_for(self, msg_id: bytes) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM candidate WHERE msg_id=? ORDER BY countdown DESC", (msg_id,)
        ))

    # --- messages -------------------------------------------------------------

    def add_message(
        self, msg_id: bytes | None, first_txid: str, last_txid: str, height: int,
        block_time: int, sender_addr: str, sender_pubkey: bytes, recipient_fp: str,
        body: bytes, complete: bool = True,
    ) -> int:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO message"
            "(msg_id,first_txid,last_txid,height,block_time,sender_addr,sender_pubkey,"
            "recipient_fp,body,complete) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (msg_id, first_txid, last_txid, height, block_time, sender_addr,
             sender_pubkey, recipient_fp, body, 1 if complete else 0),
        )
        return cur.lastrowid or 0

    def inbox(self, recipient_fp: str | None = None, limit: int = 50,
              unread_only: bool = False) -> list[StoredMessage]:
        sql = "SELECT * FROM message WHERE 1=1"
        args: list = []
        if recipient_fp:
            sql += " AND recipient_fp=?"; args.append(recipient_fp)
        if unread_only:
            sql += " AND read_at IS NULL"
        sql += " ORDER BY block_time DESC, id DESC LIMIT ?"; args.append(limit)
        return [
            StoredMessage(
                id=r["id"], sender_addr=r["sender_addr"], sender_pubkey=r["sender_pubkey"],
                body=r["body"], height=r["height"], block_time=r["block_time"],
                complete=bool(r["complete"]), read_at=r["read_at"], first_txid=r["first_txid"],
            )
            for r in self.conn.execute(sql, args)
        ]

    def get_message(self, message_id: int) -> StoredMessage | None:
        r = self.conn.execute("SELECT * FROM message WHERE id=?", (message_id,)).fetchone()
        if r is None:
            return None
        return StoredMessage(
            id=r["id"], sender_addr=r["sender_addr"], sender_pubkey=r["sender_pubkey"],
            body=r["body"], height=r["height"], block_time=r["block_time"],
            complete=bool(r["complete"]), read_at=r["read_at"], first_txid=r["first_txid"],
        )

    def mark_read(self, message_id: int) -> None:
        self.conn.execute(
            "UPDATE message SET read_at=? WHERE id=? AND read_at IS NULL",
            (int(time.time()), message_id),
        )

    def stats(self) -> dict[str, int]:
        q = lambda s: self.conn.execute(s).fetchone()[0]
        return {
            "candidates": q("SELECT COUNT(*) FROM candidate"),
            "unopened": q("SELECT COUNT(*) FROM candidate WHERE opened=0"),
            "messages": q("SELECT COUNT(*) FROM message"),
            "unread": q("SELECT COUNT(*) FROM message WHERE read_at IS NULL"),
            "keys": q("SELECT COUNT(DISTINCT address) FROM key_announcement"),
        }
