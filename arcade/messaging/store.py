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

#: The address book, defined once: `SCHEMA` includes it and the migration that
#: rebuilds an older table reuses it.
CONTACT_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pubkey          BLOB UNIQUE,
    name            TEXT NOT NULL DEFAULT '',
    address         TEXT NOT NULL DEFAULT '',
    testnet_address TEXT NOT NULL DEFAULT '',
    mainnet_address TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    added           INTEGER NOT NULL,
    updated         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS contact_name ON contact(name);
"""

SCHEMA = CONTACT_SCHEMA + """
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

-- Messages we sent. The sealed box encrypts to the recipient only, so an
-- outgoing message cannot be read back off the chain even by its author. If the
-- plaintext is not kept here, a conversation shows only one side of itself.
CREATE TABLE IF NOT EXISTS sent (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    txid           TEXT NOT NULL,
    created        INTEGER NOT NULL,
    recipient_key  BLOB NOT NULL,
    recipient_addr TEXT NOT NULL DEFAULT '',
    sender_fp      TEXT NOT NULL,
    body           BLOB NOT NULL,
    confirmed      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (txid)
);
CREATE INDEX IF NOT EXISTS sent_time ON sent(created DESC);
CREATE INDEX IF NOT EXISTS sent_peer ON sent(recipient_key);

-- The address book. Entirely local: none of this is published, derivable from
-- the chain, or shared with anyone. It exists so conversations show names rather
-- than hex, and so the coin addresses you actually pay are somewhere other than
-- a scrap of paper.
--
-- A contact is keyed by messaging public key when there is one, because that is
-- the identity. An entry may exist with no key at all -- somebody you only ever
-- send coins to -- so the key is nullable and a synthetic id is the primary key.
-- A chunked send in progress.
--
-- A message too long for one transaction is sent as a chain, and a chain that
-- stops half way is permanently unreadable: what is on the chain cannot be taken
-- back, and the rest cannot be rebuilt later, because re-sealing the same text
-- produces a different message id. So the sealed chunks are written down BEFORE
-- the first broadcast and the progress after each one, which turns an
-- interrupted send into something that can be finished rather than an orphan.
-- A file carried inside a message.
--
-- Kept apart from `message.body` so the conversation view can list a thread
-- without reading megabytes of attachment data it is not going to show.
CREATE TABLE IF NOT EXISTS attachment (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   INTEGER NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT '',
    data         BLOB NOT NULL,
    UNIQUE (message_id)
);

-- Public group posts. NOT encrypted: every row here was readable by anyone with
-- a node the moment it was mined, and is stored in the clear because that is
-- what it already is. Kept per network, because the same channel name on
-- testnet and on mainnet is two different rooms with two different costs.
CREATE TABLE IF NOT EXISTS group_post (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    network    TEXT NOT NULL,
    channel    TEXT NOT NULL,
    txid       TEXT NOT NULL,
    height     INTEGER NOT NULL,
    block_time INTEGER NOT NULL,
    sender     TEXT NOT NULL DEFAULT '',
    nickname   TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL DEFAULT '',
    mine       INTEGER NOT NULL DEFAULT 0,
    file_name  TEXT NOT NULL DEFAULT '',
    file_type  TEXT NOT NULL DEFAULT '',
    file_data  BLOB,
    UNIQUE (network, txid)
);
CREATE INDEX IF NOT EXISTS group_post_channel
    ON group_post(network, channel, block_time DESC);

-- Links of a public post too large for one transaction, held until the chain is
-- complete. Not encrypted, so anyone can rejoin them -- no key, no identity.
CREATE TABLE IF NOT EXISTS group_chunk (
    network    TEXT NOT NULL,
    msg_id     BLOB NOT NULL,
    countdown  INTEGER NOT NULL,
    txid       TEXT NOT NULL,
    height     INTEGER NOT NULL,
    block_time INTEGER NOT NULL,
    sender     TEXT NOT NULL DEFAULT '',
    data       BLOB NOT NULL,
    PRIMARY KEY (network, msg_id, countdown)
);

CREATE TABLE IF NOT EXISTS pending_send (
    msg_id         BLOB PRIMARY KEY,
    recipient_key  BLOB NOT NULL,
    sender_address TEXT NOT NULL,
    body           BLOB NOT NULL,
    chunks         BLOB NOT NULL,
    total          INTEGER NOT NULL,
    sent_count     INTEGER NOT NULL DEFAULT 0,
    txids          TEXT NOT NULL DEFAULT '',
    created        INTEGER NOT NULL
);

-- (defined once in CONTACT_SCHEMA below, so the migration that rebuilds
-- this table cannot drift from the schema that creates it)


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


def _pack_chunks(chunks: list[bytes]) -> bytes:
    """Length-prefixed concatenation. Chunks are opaque sealed bytes."""
    out = bytearray()
    for chunk in chunks:
        out += len(chunk).to_bytes(4, "big") + chunk
    return bytes(out)


def _unpack_chunks(blob: bytes) -> list[bytes]:
    chunks, offset = [], 0
    while offset + 4 <= len(blob):
        size = int.from_bytes(blob[offset:offset + 4], "big")
        offset += 4
        chunks.append(blob[offset:offset + size])
        offset += size
    return chunks


class MessageStore:
    """Message and key-announcement storage."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        # Create the directory rather than failing on a fresh install: sqlite
        # reports a missing parent as "unable to open database file", which says
        # nothing useful to anyone.
        parent = Path(self.path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    #: Columns added to existing tables after the first release, as
    #: (table, column, definition). `CREATE TABLE IF NOT EXISTS` does nothing to
    #: a table that already exists, so a store made before a column was added
    #: keeps the old shape and fails at the first write -- which it did, on a
    #: live installation, the moment the address book gained `updated`.
    MIGRATIONS = (
        ("contact", "testnet_address", "TEXT NOT NULL DEFAULT ''"),
        ("contact", "mainnet_address", "TEXT NOT NULL DEFAULT ''"),
        ("contact", "notes", "TEXT NOT NULL DEFAULT ''"),
        ("contact", "updated", "INTEGER NOT NULL DEFAULT 0"),
        ("group_post", "file_name", "TEXT NOT NULL DEFAULT ''"),
        ("group_post", "file_type", "TEXT NOT NULL DEFAULT ''"),
        ("group_post", "file_data", "BLOB"),
    )

    def _migrate(self) -> None:
        """Bring a store created by an earlier version up to the current shape.

        Additive only: no column is dropped and no data is discarded. An upgrade
        must never be able to lose a message.
        """
        self._rebuild_contact_if_keyless()
        for table, column, definition in self.MIGRATIONS:
            existing = {row["name"] for row in
                        self.conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue                 # table not created yet; SCHEMA handles it
            if column not in existing:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _rebuild_contact_if_keyless(self) -> None:
        """Give the address book its `id` column, copying every row across.

        The original table was keyed by pubkey alone. The address book needs a
        synthetic id, because an entry may have no messaging key at all --
        somebody you only ever pay. SQLite cannot add a primary key with ALTER
        TABLE, so the table is rebuilt and the rows carried over.

        Found when a real message arrived: `contact_by_key(...)["id"]` raised
        IndexError on a store older than the address book, and the page 500'd.
        """
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(contact)")}
        if not columns or "id" in columns:
            return

        carried = [c for c in ("pubkey", "name", "address", "added") if c in columns]
        joined = ",".join(carried)
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("ALTER TABLE contact RENAME TO contact_old")
            # Statement by statement, not executescript: that commits implicitly
            # and would end the transaction this rebuild depends on.
            for statement in CONTACT_SCHEMA.split(";"):
                if statement.strip():
                    self.conn.execute(statement)
            self.conn.execute(
                f"INSERT INTO contact({joined}) SELECT {joined} FROM contact_old")
            self.conn.execute("DROP TABLE contact_old")
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

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
        # Everything needed to reply arrives with the message: the sender's key
        # comes from inside the sealed box, their address from the transaction.
        # Recording it here means a reply needs no contact code, no announcement
        # and no second channel -- receiving from somebody is itself the
        # introduction. Done in `add_message` rather than in the scanner so no
        # future caller can deliver a message and forget the reply information.
        if sender_pubkey:
            self.name_contact(sender_pubkey, "", sender_addr or "")
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

    # --- sent messages --------------------------------------------------------

    def add_attachment(self, message_id: int, name: str, content_type: str,
                       data: bytes) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO attachment(message_id,name,content_type,data) "
            "VALUES(?,?,?,?)", (message_id, name, content_type, data))

    def attachment_for(self, message_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM attachment WHERE message_id=?", (message_id,)).fetchone()

    def attachment_summary(self, message_id: int) -> sqlite3.Row | None:
        """Name, type and size without pulling the bytes into memory."""
        return self.conn.execute(
            "SELECT id, name, content_type, LENGTH(data) AS size FROM attachment "
            "WHERE message_id=?", (message_id,)).fetchone()

    def apply_profile(self, pubkey: bytes, name: str = "", testnet_address: str = "",
                      mainnet_address: str = "") -> None:
        """Fill in an address book entry from what a sender said about themselves.

        Only fills blanks. A name the user typed themselves always wins over one
        a stranger asserted, because the whole value of the local name is that
        nobody else chose it.
        """
        now = int(time.time())
        existing = self.contact_by_key(pubkey)
        if existing is None:
            self.conn.execute(
                "INSERT INTO contact(pubkey,name,testnet_address,mainnet_address,"
                "added,updated) VALUES(?,?,?,?,?,?)",
                (pubkey, name, testnet_address, mainnet_address, now, now))
            return
        self.conn.execute(
            "UPDATE contact SET "
            "name=CASE WHEN contact.name='' THEN ? ELSE contact.name END, "
            "testnet_address=CASE WHEN contact.testnet_address='' THEN ? "
            "  ELSE contact.testnet_address END, "
            "mainnet_address=CASE WHEN contact.mainnet_address='' THEN ? "
            "  ELSE contact.mainnet_address END, "
            "updated=? WHERE id=?",
            (name, testnet_address, mainnet_address, now, existing["id"]))

    # --- public group posts ---------------------------------------------------

    def add_group_post(self, network: str, channel: str, txid: str, height: int,
                       block_time: int, sender: str, nickname: str, text: str,
                       mine: bool = False, file_name: str = "",
                       file_type: str = "", file_data: bytes | None = None) -> int:
        # A post this machine made is recorded optimistically at broadcast, with
        # height 0, so it appears straight away. The scan then sees the same txid
        # on chain. INSERT OR IGNORE kept the optimistic row and the real height
        # never landed, so a post read "pending" forever. Upsert the confirmation
        # instead, and never let a rescan overwrite `mine` -- the chain cannot
        # tell us that, only we know it.
        cur = self.conn.execute(
            "INSERT INTO group_post"
            "(network,channel,txid,height,block_time,sender,nickname,text,mine,"
            "file_name,file_type,file_data) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(network,txid) DO UPDATE SET "
            "  height=CASE WHEN excluded.height > 0 THEN excluded.height "
            "              ELSE group_post.height END, "
            "  block_time=CASE WHEN excluded.height > 0 THEN excluded.block_time "
            "              ELSE group_post.block_time END, "
            "  sender=CASE WHEN excluded.sender != '' THEN excluded.sender "
            "              ELSE group_post.sender END, "
            "  mine=MAX(group_post.mine, excluded.mine), "
            "  file_name=CASE WHEN excluded.file_name != '' THEN excluded.file_name "
            "              ELSE group_post.file_name END, "
            "  file_type=CASE WHEN excluded.file_type != '' THEN excluded.file_type "
            "              ELSE group_post.file_type END, "
            "  file_data=COALESCE(excluded.file_data, group_post.file_data)",
            (network, channel, txid, height, block_time, sender, nickname, text,
             1 if mine else 0, file_name, file_type, file_data))
        return cur.lastrowid or 0

    def group_posts(self, network: str, channel: str,
                    limit: int = 200) -> list[sqlite3.Row]:
        """Posts in one channel, **newest first**.

        A feed, not a conversation. A private thread reads downward because it is
        a dialogue you follow from the start; a public channel is something you
        drop into, where the thing you have not seen is the newest.

        The file bytes are deliberately not selected: a channel listing must not
        pull every attachment in it into memory to render a page.
        """
        return list(self.conn.execute(
            "SELECT id,network,channel,txid,height,block_time,sender,nickname,text,"
            "mine,file_name,file_type,LENGTH(file_data) AS file_size "
            "FROM group_post WHERE network=? AND channel=? "
            "ORDER BY block_time DESC, id DESC LIMIT ?", (network, channel, limit)))

    def add_group_chunk(self, network: str, msg_id: bytes, countdown: int,
                        txid: str, height: int, block_time: int, sender: str,
                        data: bytes) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO group_chunk"
            "(network,msg_id,countdown,txid,height,block_time,sender,data) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (network, msg_id, countdown, txid, height, block_time, sender, data))

    def group_chunks(self, network: str, msg_id: bytes) -> list[sqlite3.Row]:
        """Every link seen so far, in send order (countdown counts down to 0)."""
        return list(self.conn.execute(
            "SELECT * FROM group_chunk WHERE network=? AND msg_id=? "
            "ORDER BY countdown DESC", (network, msg_id)))

    def drop_group_chunks(self, network: str, msg_id: bytes) -> None:
        self.conn.execute("DELETE FROM group_chunk WHERE network=? AND msg_id=?",
                          (network, msg_id))

    def group_post_file(self, post_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT file_name, file_type, file_data FROM group_post WHERE id=?",
            (post_id,)).fetchone()

    def group_channels(self, network: str) -> list[sqlite3.Row]:
        """Channels seen on this network, most recently active first."""
        return list(self.conn.execute(
            "SELECT channel, COUNT(*) AS posts, MAX(block_time) AS last "
            "FROM group_post WHERE network=? GROUP BY channel ORDER BY last DESC",
            (network,)))

    # --- chunked sends in progress --------------------------------------------

    def begin_pending_send(self, msg_id: bytes, recipient_key: bytes,
                           sender_address: str, body: bytes,
                           chunks: list[bytes]) -> None:
        """Record a chunked send before any of it is broadcast."""
        self.conn.execute(
            "INSERT OR REPLACE INTO pending_send"
            "(msg_id,recipient_key,sender_address,body,chunks,total,sent_count,"
            "txids,created) VALUES(?,?,?,?,?,?,0,'',?)",
            (msg_id, recipient_key, sender_address, body, _pack_chunks(chunks),
             len(chunks), int(time.time())),
        )

    def record_pending_progress(self, msg_id: bytes, txid: str) -> None:
        """Note one more chunk away. Called immediately after each broadcast."""
        row = self.conn.execute(
            "SELECT txids FROM pending_send WHERE msg_id=?", (msg_id,)).fetchone()
        if row is None:
            return
        txids = [t for t in row["txids"].split(",") if t] + [txid]
        self.conn.execute(
            "UPDATE pending_send SET sent_count=?, txids=? WHERE msg_id=?",
            (len(txids), ",".join(txids), msg_id),
        )

    def finish_pending_send(self, msg_id: bytes) -> None:
        self.conn.execute("DELETE FROM pending_send WHERE msg_id=?", (msg_id,))

    def pending_sends(self) -> list[dict[str, Any]]:
        """Unfinished chunked sends, oldest first."""
        out = []
        for row in self.conn.execute("SELECT * FROM pending_send ORDER BY created"):
            out.append({
                "msg_id": bytes(row["msg_id"]),
                "recipient_key": bytes(row["recipient_key"]),
                "sender_address": row["sender_address"],
                "body": bytes(row["body"]),
                "chunks": _unpack_chunks(bytes(row["chunks"])),
                "total": row["total"],
                "sent_count": row["sent_count"],
                "txids": [t for t in row["txids"].split(",") if t],
                "created": row["created"],
            })
        return out

    def add_sent(self, txid: str, recipient_key: bytes, recipient_addr: str,
                 sender_fp: str, body: bytes) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO sent"
            "(txid,created,recipient_key,recipient_addr,sender_fp,body) VALUES(?,?,?,?,?,?)",
            (txid, int(time.time()), recipient_key, recipient_addr, sender_fp, body),
        )

    def mark_sent_confirmed(self, txid: str) -> None:
        self.conn.execute("UPDATE sent SET confirmed=1 WHERE txid=?", (txid,))

    # --- contacts -------------------------------------------------------------

    def name_contact(self, pubkey: bytes, name: str, address: str = "") -> None:
        now = int(time.time())
        self.conn.execute(
            # An empty value means "not supplied", never "clear it". Starting a
            # conversation from an address must not wipe a name set earlier.
            # `testnet_address` is filled as well as `address`, because messaging
            # is testnet by construction (D-010) and the address book displays
            # the per-chain columns. Filling only the legacy one meant a contact
            # created by receiving a message showed no address at all.
            "INSERT INTO contact(pubkey,name,address,testnet_address,added,updated) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(pubkey) DO UPDATE SET updated=excluded.updated, "
            "name=CASE WHEN excluded.name != '' THEN excluded.name ELSE contact.name END, "
            "address=CASE WHEN excluded.address != '' THEN excluded.address ELSE contact.address END, "
            "testnet_address=CASE WHEN contact.testnet_address='' THEN excluded.testnet_address "
            "  ELSE contact.testnet_address END",
            (pubkey, name, address, address, now, now),
        )

    def save_contact(self, *, contact_id: int | None = None, pubkey: bytes | None = None,
                     name: str = "", testnet_address: str = "", mainnet_address: str = "",
                     notes: str = "") -> int:
        """Create or update an address book entry.

        Everything here stays on this machine. None of it is published, and none
        of it can be derived from the chain by anyone else.
        """
        now = int(time.time())
        if contact_id:
            self.conn.execute(
                "UPDATE contact SET name=?, testnet_address=?, mainnet_address=?, "
                "notes=?, updated=? WHERE id=?",
                (name, testnet_address, mainnet_address, notes, now, contact_id),
            )
            return contact_id

        if pubkey:
            existing = self.conn.execute(
                "SELECT id FROM contact WHERE pubkey=?", (pubkey,)).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE contact SET name=?, testnet_address=?, mainnet_address=?, "
                    "notes=?, updated=? WHERE id=?",
                    (name, testnet_address, mainnet_address, notes, now, existing["id"]),
                )
                return existing["id"]

        cur = self.conn.execute(
            "INSERT INTO contact(pubkey,name,testnet_address,mainnet_address,notes,added,updated) "
            "VALUES(?,?,?,?,?,?,?)",
            (pubkey, name, testnet_address, mainnet_address, notes, now, now),
        )
        return cur.lastrowid or 0

    def delete_contact(self, contact_id: int) -> None:
        self.conn.execute("DELETE FROM contact WHERE id=?", (contact_id,))

    def contact_name(self, pubkey: bytes) -> str | None:
        row = self.conn.execute("SELECT name FROM contact WHERE pubkey=?", (pubkey,)).fetchone()
        return row["name"] if row and row["name"] else None

    def contact_by_key(self, pubkey: bytes) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM contact WHERE pubkey=?", (pubkey,)).fetchone()

    def contact_by_id(self, contact_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM contact WHERE id=?", (contact_id,)).fetchone()

    def contacts(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM contact ORDER BY CASE WHEN name='' THEN 1 ELSE 0 END, name, added"))

    # --- conversations --------------------------------------------------------

    def conversations(self, recipient_fp: str) -> list[dict]:
        """One entry per correspondent, newest activity first.

        A conversation is keyed by the other party's public key rather than their
        address: the key is the identity, and the same person may send from
        several addresses.
        """
        peers: dict[bytes, dict] = {}

        for row in self.conn.execute(
            "SELECT sender_pubkey, sender_addr, body, block_time, read_at "
            "FROM message WHERE recipient_fp=? ORDER BY block_time", (recipient_fp,)
        ):
            key = bytes(row["sender_pubkey"])
            entry = peers.setdefault(key, {"pubkey": key, "address": row["sender_addr"],
                                           "last": 0, "preview": b"", "unread": 0,
                                           "count": 0, "outgoing": False})
            entry["count"] += 1
            entry["address"] = row["sender_addr"] or entry["address"]
            if row["block_time"] >= entry["last"]:
                entry.update(last=row["block_time"], preview=row["body"], outgoing=False)
            if row["read_at"] is None:
                entry["unread"] += 1

        for row in self.conn.execute(
            "SELECT recipient_key, recipient_addr, body, created FROM sent "
            "WHERE sender_fp=? ORDER BY created", (recipient_fp,)
        ):
            key = bytes(row["recipient_key"])
            entry = peers.setdefault(key, {"pubkey": key, "address": row["recipient_addr"],
                                           "last": 0, "preview": b"", "unread": 0,
                                           "count": 0, "outgoing": True})
            entry["count"] += 1
            entry["address"] = entry["address"] or row["recipient_addr"]
            if row["created"] >= entry["last"]:
                entry.update(last=row["created"], preview=row["body"], outgoing=True)

        for key, entry in peers.items():
            entry["name"] = self.contact_name(key)
        return sorted(peers.values(), key=lambda e: e["last"], reverse=True)

    def thread(self, recipient_fp: str, peer_key: bytes) -> list[dict]:
        """Every message with one correspondent, oldest first."""
        items = []
        for row in self.conn.execute(
            "SELECT id, body, block_time, read_at, first_txid, height FROM message "
            "WHERE recipient_fp=? AND sender_pubkey=? ", (recipient_fp, peer_key)
        ):
            items.append({"id": row["id"], "body": row["body"], "when": row["block_time"],
                          "mine": False, "txid": row["first_txid"], "height": row["height"],
                          "unread": row["read_at"] is None})
        for row in self.conn.execute(
            "SELECT id, body, created, txid, confirmed FROM sent "
            "WHERE sender_fp=? AND recipient_key=?", (recipient_fp, peer_key)
        ):
            items.append({"id": row["id"], "body": row["body"], "when": row["created"],
                          "mine": True, "txid": row["txid"], "height": None,
                          "confirmed": bool(row["confirmed"])})
        return sorted(items, key=lambda i: i["when"])

    def mark_thread_read(self, recipient_fp: str, peer_key: bytes) -> None:
        self.conn.execute(
            "UPDATE message SET read_at=? WHERE recipient_fp=? AND sender_pubkey=? "
            "AND read_at IS NULL", (int(time.time()), recipient_fp, peer_key))

    def stats(self) -> dict[str, int]:
        q = lambda s: self.conn.execute(s).fetchone()[0]
        return {
            "candidates": q("SELECT COUNT(*) FROM candidate"),
            "unopened": q("SELECT COUNT(*) FROM candidate WHERE opened=0"),
            "messages": q("SELECT COUNT(*) FROM message"),
            "unread": q("SELECT COUNT(*) FROM message WHERE read_at IS NULL"),
            "keys": q("SELECT COUNT(DISTINCT address) FROM key_announcement"),
            "sent": q("SELECT COUNT(*) FROM sent"),
        }
