"""SQLite storage for messages, key announcements, and scan progress.

Kept separate from the protocol-state database: messaging is testnet-only
(D-010) and its data is not consensus state. Mixing them would let a testnet
reset damage the ledger database.
"""

from __future__ import annotations

import sqlite3
import time
from ..db import add_missing_columns
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import feed

SCHEMA_VERSION = 1

#: The address book, defined once: `SCHEMA` includes it and the migration that
#: rebuilds an older table reuses it.
CONTACT_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pubkey          BLOB UNIQUE,
    name            TEXT NOT NULL DEFAULT '',
    address         TEXT NOT NULL DEFAULT '',
    name_source     TEXT NOT NULL DEFAULT '',
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
    seen_at     INTEGER NOT NULL,
    pfp         TEXT NOT NULL DEFAULT '',
    bio         TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS key_announcement_addr ON key_announcement(address, height);
-- Arcade instances announcing who runs them (arcade/instance.py): a domain and
-- a revision, published by the FEE ADDRESS that paid for the transaction. Every
-- announcement is kept; the directory shows the latest per (address, domain).
CREATE TABLE IF NOT EXISTS instance_announcement (
    txid        TEXT PRIMARY KEY,
    network     TEXT NOT NULL,
    address     TEXT NOT NULL,
    domain      TEXT NOT NULL,
    revision    TEXT NOT NULL,
    height      INTEGER NOT NULL,
    block_time  INTEGER NOT NULL,
    seen_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS instance_announcement_domain
    ON instance_announcement(network, domain, height);
-- The inscription somebody uses as their picture, as announced. Honoured only
-- while the chain says they still hold it (D-138), so this is what they SAID,
-- never what is drawn.

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
    file_name      TEXT NOT NULL DEFAULT '',
    file_type      TEXT NOT NULL DEFAULT '',
    file_data      BLOB,
    height         INTEGER NOT NULL DEFAULT 0,
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
-- Node-to-node API messages. A separate table from `message` on purpose: these
-- are addressed to a program, not to a person, and putting them in the
-- conversation would fill somebody's chat with machine chatter they cannot read
-- and did not ask for. Same encryption, same chain, different destination.
CREATE TABLE IF NOT EXISTS api_message (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    network       TEXT    NOT NULL,
    txid          TEXT    NOT NULL,
    height        INTEGER NOT NULL,
    block_time    INTEGER NOT NULL,
    sender_addr   TEXT    NOT NULL DEFAULT '',
    sender_pubkey BLOB    NOT NULL,
    recipient_fp  TEXT    NOT NULL,
    body          BLOB    NOT NULL,
    -- What API the sender was speaking. Kept rather than checked once, so a
    -- program can decide for itself what to do about a mismatch: refuse, warn,
    -- or carry on because the command it cares about has not changed.
    protocol      INTEGER NOT NULL DEFAULT 0,
    fingerprint   BLOB    NOT NULL DEFAULT x'',
    mine          INTEGER NOT NULL DEFAULT 0,
    read_at       INTEGER,
    UNIQUE (txid, recipient_fp)
);
CREATE INDEX IF NOT EXISTS api_message_time ON api_message(id DESC);

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

-- What people did to each other's posts: one row per transaction, one table
-- for every kind (feed.py, D-138). Counts are queries against the index
-- below rather than columns kept in step, and a kind this version does not
-- know is simply a row nothing asks for -- which is what lets a kind be
-- added without a migration.
--
-- Deliberately not stored: the author's @tag. It is read from the chain when
-- a page is drawn, because a tag can move and a copy here would be a name
-- that used to be right (D-137). Deliberately not stored either: anything
-- the post row already holds.
CREATE TABLE IF NOT EXISTS feed_act (
    txid       TEXT NOT NULL,
    network    TEXT NOT NULL,
    kind       INTEGER NOT NULL,
    target     TEXT NOT NULL,
    author     TEXT NOT NULL,
    text       TEXT NOT NULL DEFAULT '',
    height     INTEGER NOT NULL,
    block_time INTEGER NOT NULL,
    mine       INTEGER NOT NULL DEFAULT 0,
    -- What a tip tipped, in sats, and which chain carried it -- both read off
    -- the transaction (feed.py, 2026-09-23), because the payload only
    -- says WHICH post and the caller's word is not evidence. Zero on every
    -- row that is not a tip. `paid_on` says nothing on a scanned row that the
    -- `network` column does not already say; it exists for the rows a node
    -- used to record about a tip paid on another chain under this feed's own
    -- name, whose text carried the truth in the old "chain:sats" encoding.
    amount     INTEGER NOT NULL DEFAULT 0,
    paid_on    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (network, txid)
);
CREATE INDEX IF NOT EXISTS feed_act_target ON feed_act(network, target, kind);
CREATE INDEX IF NOT EXISTS feed_act_author ON feed_act(network, author, height DESC);
CREATE INDEX IF NOT EXISTS feed_act_tips ON feed_act(target, kind, height);

-- People this machine will not draw. Local by design: it costs nothing, tells
-- them nothing, and every node decides for itself what it shows (D-138).
CREATE TABLE IF NOT EXISTS mute (
    address  TEXT PRIMARY KEY,
    muted_at INTEGER NOT NULL
);

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

-- How far the reader has got, per thing that can be behind. The board has no
-- per-post read mark and does not want one: a post is public, it is not
-- addressed to anybody, and "read" for a board means "I have looked since".
CREATE TABLE IF NOT EXISTS seen_mark (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
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
        # The feed's arithmetic, in SQL, because the feed's ordering is a
        # query rather than a Python sort (feed.py): a page cannot put posts
        # in an order it has not assembled them from, and the query and the
        # cards have to agree by being the same function, not by two
        # implementations agreeing to.
        self.conn.create_function("tip_value", 2, feed.tip_value,
                                  deterministic=True)
        self.conn.executescript(SCHEMA)
        add_missing_columns(self.conn, SCHEMA)
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
        ("key_announcement", "stated", "INTEGER NOT NULL DEFAULT 0"),
        ("key_announcement", "name", "TEXT NOT NULL DEFAULT ''"),
        ("key_announcement", "tag", "TEXT NOT NULL DEFAULT ''"),
        ("key_announcement", "other_address", "TEXT NOT NULL DEFAULT ''"),
        ("sent", "file_name", "TEXT NOT NULL DEFAULT ''"),
        ("sent", "file_type", "TEXT NOT NULL DEFAULT ''"),
        ("sent", "file_data", "BLOB"),
        # Every transaction of a send, not only the first. A picture is dozens
        # of them, they confirm in whatever order the miner chooses, and until
        # this existed the interface could only ask about the first -- so a
        # message with forty of its fifty transactions in blocks still read
        # "unconfirmed", and read it for as long as that one lagged.
        ("sent", "txids", "TEXT NOT NULL DEFAULT ''"),
        ("sent", "confirmed_count", "INTEGER NOT NULL DEFAULT 0"),
        ("group_post", "txids", "TEXT NOT NULL DEFAULT ''"),
        ("group_post", "confirmed_count", "INTEGER NOT NULL DEFAULT 0"),
        ("sent", "height", "INTEGER NOT NULL DEFAULT 0"),
        # '' = the user typed it, 'profile' = they told us in a message,
        # 'announce' = read off a public announcement, where names are cut to 12
        # bytes. Without this the same person could appear under two names
        # depending on which arrived first, which is arbitrary.
        ("contact", "name_source", "TEXT NOT NULL DEFAULT ''"),
        # A public post is also a chunked send that can be interrupted, but the
        # table was shaped for private messages only -- so group_post_send
        # recorded nothing, and an interrupted post spent the coins it had
        # already broadcast and left no trace that it happened. These columns
        # let one table hold both without the private resume path ever seeing a
        # post: 'private' is the default precisely because every row that
        # existed before this was one.
        ("pending_send", "kind", "TEXT NOT NULL DEFAULT 'private'"),
        ("pending_send", "channel", "TEXT NOT NULL DEFAULT ''"),
        ("pending_send", "network", "TEXT NOT NULL DEFAULT ''"),
        ("pending_send", "nickname", "TEXT NOT NULL DEFAULT ''"),
        # The feed's memory of what tips tipped (feed.py, 2026-09-23).
        ("feed_act", "amount", "INTEGER NOT NULL DEFAULT 0"),
        ("feed_act", "paid_on", "TEXT NOT NULL DEFAULT ''"),
    )

    #: Backfills run after the columns exist. A column added with a default is
    #: not neutral: the default becomes a claim about every existing row, and
    #: getting that claim wrong is how a scanned name ended up recorded as one
    #: the user had typed -- and therefore unrepairable for ever.
    BACKFILLS = (
        # Every name that existed before provenance was recorded came from
        # scanning an announcement, since nothing else wrote one. Saying so
        # restores the precedence the ranking was designed to give.
        ("contact", "name_source",
         "UPDATE contact SET name_source='announce' "
         "WHERE name != '' AND name_source = ''"),
        # The old tip rows said their amount and chain in the TEXT column, in
        # the "chain:sats" shape send_tip wrote. A rescan of the transaction
        # fixes every row this backfill cannot read, so the only rows that
        # stay at zero are ones whose transaction this node has never seen.
        ("feed_act", "amount",
         f"UPDATE feed_act SET "
         "  paid_on = substr(text, 1, instr(text, ':') - 1), "
         "  amount = CAST(substr(text, instr(text, ':') + 1) AS INTEGER) "
         f"WHERE kind = {feed.TIP} AND amount = 0 AND instr(text, ':') > 1 "
         "  AND substr(text, 1, instr(text, ':') - 1) NOT GLOB '*[^a-z0-9]*' "
         "  AND substr(text, instr(text, ':') + 1) GLOB '[0-9]*'"),
    )

    def _migrate(self) -> None:
        """Bring a store created by an earlier version up to the current shape.

        Additive only: no column is dropped and no data is discarded. An upgrade
        must never be able to lose a message.
        """
        self._rebuild_contact_if_keyless()
        added: set[tuple[str, str]] = set()
        for table, column, definition in self.MIGRATIONS:
            existing = {row["name"] for row in
                        self.conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue                 # table not created yet; SCHEMA handles it
            if column not in existing:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                added.add((table, column))

        for table, column, statement in self.BACKFILLS:
            # Run whenever the claim may be wrong, not only on the upgrade that
            # added the column: a store migrated by the broken version is
            # already carrying the wrong value and would never be revisited.
            try:
                self.conn.execute(statement)
            except Exception:
                continue
        self._repair_encoded_own_copies()

    def _repair_encoded_own_copies(self) -> None:
        """Decode sent rows whose body is the encoded message, not our copy.

        A resumed send recorded the encoded body (`\x01ARCB...`) as the
        sender's own copy, with no file columns, so a picture showed as its
        JSON header and raw bytes. Our own copy is never written encoded, so
        the marker identifies exactly the rows the defect wrote, and the body
        holds everything needed to write them properly.
        """
        from .content import BODY_MAGIC, own_copy

        rows = self.conn.execute(
            "SELECT id, body FROM sent WHERE substr(body, 1, ?) = ?",
            (len(BODY_MAGIC), BODY_MAGIC)).fetchall()
        for row in rows:
            text, attachment = own_copy(bytes(row["body"]))
            self.conn.execute(
                "UPDATE sent SET body=?, file_name=?, file_type=?, file_data=? "
                "WHERE id=?",
                (text, attachment.get("file_name", ""),
                 attachment.get("file_type", ""), attachment.get("file_data"),
                 row["id"]))

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

    def add_instance_announcement(self, txid: str, network: str, address: str,
                                  domain: str, revision: str, height: int,
                                  block_time: int) -> None:
        """An arcade announcing its domain and revision (arcade/instance.py).
        A pool row (height 0) is promoted in place when its block arrives."""
        import time as _time
        self.conn.execute(
            "INSERT INTO instance_announcement (txid, network, address, domain,"
            " revision, height, block_time, seen_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(txid) DO UPDATE SET"
            "  height = CASE WHEN excluded.height > 0 THEN excluded.height"
            "                ELSE instance_announcement.height END,"
            "  block_time = CASE WHEN excluded.height > 0 THEN excluded.block_time"
            "                ELSE instance_announcement.block_time END",
            (txid, network, address, domain, revision, int(height),
             int(block_time), int(_time.time())))
        self.conn.commit()

    def instances(self, network: str) -> list[sqlite3.Row]:
        """The latest announcement for each (fee address, domain), newest first.
        Confirmed ones only: a directory entry should be something a block says."""
        return self.conn.execute(
            "SELECT * FROM instance_announcement a WHERE a.network = ? AND a.height > 0"
            " AND NOT EXISTS (SELECT 1 FROM instance_announcement b"
            "   WHERE b.network = a.network AND b.address = a.address"
            "     AND b.domain = a.domain AND b.height > 0"
            "     AND (b.height, b.txid) > (a.height, a.txid))"
            " ORDER BY a.height DESC, a.txid DESC", (network,)).fetchall()

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

    def reset_history(self, network: str, from_height: int,
                      keep_contacts: bool = True,
                      keep_key: bytes | None = None) -> dict[str, int]:
        """Forget everything scanned so far and start watching from here.

        For clearing out test traffic. Nothing on the chain is affected -- those
        transactions are permanent and whoever they were addressed to can still
        read them. This is only what THIS installation remembers.

        The address book is kept by default: it is the one thing here that was
        typed rather than scanned, so losing it would be losing work rather than
        losing test data.
        """
        # Your own announcement survives; everything else goes, including
        # other people's. A reset is for clearing test traffic, and retaining
        # announcements from below the new floor simply brought that traffic
        # back under another name -- the point of the reset is a clean slate.
        #
        # Keeping your own is NOT what stops a second publication being paid
        # for. `publish_key` asks the chain before it spends, so an empty list
        # here cannot cost anything; this only spares you a page that has
        # forgotten something you can see on a block explorer.
        own = []
        if keep_key:
            own = list(self.conn.execute(
                "SELECT * FROM key_announcement WHERE pubkey=?", (keep_key,)))

        counts: dict[str, int] = {}
        tables = ["message", "sent", "candidate", "group_post", "group_chunk",
                  "pending_send", "key_announcement", "attachment"]
        if not keep_contacts:
            tables.append("contact")
        for table in tables:
            try:
                counts[table] = self.conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                self.conn.execute(f"DELETE FROM {table}")
            except Exception:
                counts[table] = 0

        for row in own:
            self.conn.execute(
                "INSERT OR IGNORE INTO key_announcement"
                "(txid,address,pubkey,fingerprint,height,block_time,seen_at,"
                "stated,name,tag,other_address) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (row["txid"], row["address"], row["pubkey"], row["fingerprint"],
                 row["height"], row["block_time"], row["seen_at"],
                 row["stated"], row["name"], row["tag"] if "tag" in row.keys() else "",
                 row["other_address"] if "other_address" in row.keys() else ""))
        if own:
            counts["key_announcement"] -= len(own)

        # Start from here rather than from the identity's creation, so nothing
        # just cleared is simply found again by the next scan. The caller passes
        # the protocol's shared start height where there is one, so clearing
        # brings a machine back into step with everyone else rather than pinning
        # it to whatever block it happened to be at.
        self.set_meta(f"identity_height:{network}", str(from_height))
        self.conn.execute("DELETE FROM scan_state WHERE network=?", (network,))
        return counts

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
        height: int, block_time: int, stated: bool = False, name: str = "",
        tag: str = "", other_address: str = "", pfp: str = "",
        bio: str = "", url: str = "",
    ) -> None:
        """Record an announcement.

        `stated` marks an address the announcement itself named, as against one
        inferred from the transaction's inputs. Without the distinction a reader
        cannot tell an authoritative row from a guess, and a stale inferred row
        goes on working as a target forever. a test machine asked for this after measuring
        two live rows for one key under two addresses.
        """
        self.conn.execute(
            "INSERT INTO key_announcement"
            "(txid,address,pubkey,fingerprint,height,block_time,seen_at,stated,name,"
            "tag,other_address,pfp,bio,url) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            # A longer name replaces a shorter one, so a rescan repairs a name an
            # older parser had cut. INSERT OR IGNORE meant the truncation was
            # permanent in the reader's store however often it was rescanned.
            "ON CONFLICT(txid) DO UPDATE SET "
            "  name=CASE WHEN LENGTH(excluded.name) > LENGTH(key_announcement.name) "
            "            THEN excluded.name ELSE key_announcement.name END, "
            "  stated=MAX(key_announcement.stated, excluded.stated), "
            # Height 0 means "seen in the mempool" (D-050), and the block is
            # supposed to promote the row in place -- which `add_candidate`
            # does and this statement did not. Nothing here looked at the
            # height, so an announcement read out of the pool and mined a
            # minute later went on reading as unmined for ever, and every page
            # that asks the difference between those two states inherited it.
            # The other fields are filled in by a newer reader whatever the
            # height, so this is a CASE rather than the WHERE guard the
            # candidate table gets away with.
            "  height=CASE WHEN key_announcement.height = 0 "
            "                THEN excluded.height "
            "                ELSE key_announcement.height END, "
            "  block_time=CASE WHEN key_announcement.height = 0 "
            "                   THEN excluded.block_time "
            "                   ELSE key_announcement.block_time END, "
            # A tag and the other chain's address are read by a parser that
            # understands them or not at all, so a rescan by a newer reader
            # fills them in where an older one saw nothing.
            "  tag=CASE WHEN excluded.tag <> '' THEN excluded.tag "
            "           ELSE key_announcement.tag END, "
            "  other_address=CASE WHEN excluded.other_address <> '' "
            "                     THEN excluded.other_address "
            "                     ELSE key_announcement.other_address END, "
            # A picture cleared is a picture cleared: unlike the fields above,
            # an empty one from a NEWER announcement is a person taking their
            # face down, and must not be read as an older parser seeing
            # nothing. The newest announcement for an address is what counts,
            # so this simply takes what arrived (D-138).
            # Cleared is cleared, for all three: an empty one from a NEWER
            # announcement is somebody taking it down, not an older parser
            # seeing nothing.
            "  pfp=excluded.pfp, bio=excluded.bio, url=excluded.url",
            (txid, address, pubkey, fingerprint, height, block_time,
             int(time.time()), 1 if stated else 0, name, tag, other_address,
             pfp, bio, url),
        )

    def superseded_addresses(self, pubkey: bytes) -> list[str]:
        """Inferred addresses for a key that a stated one has replaced.

        Once a key says where it lives, every address merely guessed from an
        earlier transaction's inputs is history rather than a live target.
        """
        stated = self.conn.execute(
            "SELECT address FROM key_announcement WHERE pubkey=? AND stated=1 "
            "ORDER BY height DESC LIMIT 1", (pubkey,)).fetchone()
        if stated is None:
            return []
        return [row["address"] for row in self.conn.execute(
            "SELECT DISTINCT address FROM key_announcement "
            "WHERE pubkey=? AND stated=0 AND address<>?", (pubkey, stated["address"]))]

    def set_contact_address(self, pubkey: bytes, address: str) -> None:
        """Set a contact's testnet address outright, not filling a blank.

        Used only for an address the key itself stated. That beats one inferred
        from inputs even when a value is already there, because the inferred one
        follows whichever coins paid for the transaction -- so the address book
        was handing people a funding address. It still does not beat an address
        the user typed; see `mine_wins` below.
        """
        row = self.contact_by_key(pubkey)
        now = int(time.time())
        if row is None:
            self.conn.execute(
                "INSERT INTO contact(pubkey,name,address,testnet_address,added,updated) "
                "VALUES(?,'',?,?,?,?)", (pubkey, address, address, now, now))
            return
        self.conn.execute(
            "UPDATE contact SET address=?, testnet_address=?, updated=? WHERE id=?",
            (address, address, now, row["id"]))

    def key_for(self, address: str) -> sqlite3.Row | None:
        """The announced key for an address, preferring what a key stated.

        An address the announcement named outranks one inferred from the
        transaction's inputs, whatever their heights: the inferred one follows
        the coins and can be older, newer, or simply wrong. Within each kind the
        most recent wins.
        """
        return self.conn.execute(
            "SELECT * FROM key_announcement WHERE address=? "
            "ORDER BY stated DESC, height DESC LIMIT 1", (address,),
        ).fetchone()

    def confirmed_key_for(self, address: str) -> sqlite3.Row | None:
        """The key for an address, but only once a block carries the announcement.

        `key_for` answers for anything this node has seen, an announcement in
        the mempool included. That is the right answer to "where do I seal this
        message" -- the transaction will land, and the key is already known --
        and the wrong answer to "is this key on the chain", which is a sentence
        said to a person about somebody else's ability to write to them. A
        stranger's node cannot read a transaction this node's pool has seen, so
        until the block arrives the honest answer is no.
        """
        return self.conn.execute(
            "SELECT * FROM key_announcement WHERE address=? AND height > 0 "
            "ORDER BY stated DESC, height DESC LIMIT 1", (address,),
        ).fetchone()

    def live_keys(self) -> list[sqlite3.Row]:
        """Announced keys that are still current targets.

        An address inferred from inputs stops being one once the same key has
        stated where it lives -- it is history, not a way to reach somebody.
        Leaving both live was the mess: two rows for one key, both resolving,
        and no way for a reader to tell which the owner meant.
        """
        rows = list(self.conn.execute(
            "SELECT address, pubkey, fingerprint, name, MAX(stated) AS stated, "
            "       MAX(height) AS height, MAX(block_time) AS block_time "
            "FROM key_announcement GROUP BY address ORDER BY height DESC"))
        has_stated = {bytes(r["pubkey"]) for r in rows if r["stated"]}
        return [r for r in rows
                if r["stated"] or bytes(r["pubkey"]) not in has_stated]

    def key_history(self, address: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM key_announcement WHERE address=? ORDER BY height", (address,)
        ))

    def all_keys(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT address, pubkey, fingerprint, MAX(height) AS height "
            "FROM key_announcement GROUP BY address ORDER BY height DESC"
        ))

    def unknown_published_keys(self, exclude: bytes | None = None) -> list[sqlite3.Row]:
        """Announced keys that are not in the address book yet.

        The address book is where you decide who somebody is, so this is the
        useful half of the announcement list: the people you have seen publish
        but have not yet written down. Anyone already recorded drops out, so the
        list shrinks as it is used rather than repeating what is known.
        """
        return list(self.conn.execute(
            "SELECT k.address, k.pubkey, k.fingerprint, k.name, k.tag, "
            "       k.other_address, "
            "       MAX(k.stated) AS stated, MAX(k.height) AS height, "
            "       MAX(k.block_time) AS block_time "
            "FROM key_announcement k "
            "LEFT JOIN contact c ON c.pubkey = k.pubkey "
            "WHERE c.id IS NULL AND (? IS NULL OR k.pubkey != ?) "
            "GROUP BY k.address ORDER BY height DESC",
            (exclude, exclude),
        ))

    # --- candidates -----------------------------------------------------------

    def add_candidate(
        self, txid: str, height: int, position: int, block_time: int,
        sender_addr: str, payload: bytes, msg_type: int,
        msg_id: bytes | None, countdown: int | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO candidate"
            "(txid,height,position,block_time,sender_addr,payload,msg_type,msg_id,countdown) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            # Height 0 means "seen in the mempool". When the block arrives the
            # same transaction is read again and the row is promoted rather
            # than ignored; anything already confirmed is left alone (D-050).
            "ON CONFLICT(txid) DO UPDATE SET "
            "  height=excluded.height, position=excluded.position, "
            "  block_time=excluded.block_time "
            "WHERE candidate.height = 0 AND excluded.height > 0",
            (txid, height, position, block_time, sender_addr, payload, msg_type, msg_id, countdown),
        )

    def unopened_candidates(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM candidate WHERE opened=0 ORDER BY height, position"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(self.conn.execute(sql))

    def candidates_for_others(self, after: int = 0,
                              limit: int = 200) -> list[sqlite3.Row]:
        """Candidate payloads for somebody whose key this node has not got.

        An account keeps its identity in a browser, so the node cannot tell
        which of these are addressed to it -- it hands them over and the
        browser finds out by trying. That is the cost of the node not being
        able to read anybody's messages, and it is the point.

        Ordered by `rowid`, which only ever grows, so a client keeps one
        number and asks for what came after it. Height would not do: a
        mempool row is height 0 until its block arrives and would be handed
        over twice, once at 0 and once at its real height.

        `opened` is deliberately ignored. It means "this node's own
        identity opened it", which says nothing about anybody else's.
        """
        return list(self.conn.execute(
            "SELECT rowid AS cursor, txid, height, block_time, sender_addr, "
            "       payload, msg_type, msg_id, countdown "
            "FROM candidate WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (int(after), int(limit))))

    def newest_candidate(self) -> int:
        row = self.conn.execute("SELECT MAX(rowid) FROM candidate").fetchone()
        return int(row[0] or 0)

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

    #: Which claimed name beats which. A name the user typed always wins; a name
    #: from a message beats one from an announcement, because an announcement is
    #: capped at 12 bytes and is therefore often a truncation of the real one.
    #: 'typed' is explicit on purpose. It used to be '', which is also SQLite's
    #: natural default for a NOT NULL TEXT column -- so a migration that added
    #: the column backfilled every scanned name as "typed by hand", pinning it at
    #: the one rank nothing can beat. a test machine proved it on a copy of its live store:
    #: "Big Chief En" was unrepairable for ever, by any rescan or republish.
    #:
    #: Anything unrecognised ranks LOWEST rather than highest, so a row whose
    #: provenance is unknown can still be corrected.
    NAME_RANK = {"typed": 3, "profile": 2, "announce": 1}
    UNKNOWN_NAME_RANK = 0

    def apply_profile(self, pubkey: bytes, name: str = "", testnet_address: str = "",
                      mainnet_address: str = "", source: str = "profile") -> None:
        """Fill in an address book entry from what a sender said about themselves.

        A name the user typed always wins, because the whole value of the local
        name is that nobody else chose it. Between two claimed names the fuller
        one wins: an announcement carries at most 12 bytes, so "Big Chief En" is
        a truncation of a name a message would deliver whole.
        """
        now = int(time.time())
        existing = self.contact_by_key(pubkey)
        if existing is None:
            self.conn.execute(
                "INSERT INTO contact(pubkey,name,name_source,testnet_address,"
                "mainnet_address,added,updated) VALUES(?,?,?,?,?,?,?)",
                (pubkey, name, source if name else "", testnet_address,
                 mainnet_address, now, now))
            return

        # Replace the name only if this claim outranks the one already there.
        if name:
            current = existing["name"]
            current_rank = self.NAME_RANK.get(
                existing["name_source"], self.UNKNOWN_NAME_RANK) if current else -1
            incoming_rank = self.NAME_RANK.get(source, self.UNKNOWN_NAME_RANK)
            # A higher rank always wins. At EQUAL rank a longer name wins too,
            # because that is the same claimant correcting itself -- an
            # announcement re-read with a fixed parser says "Big Chief Energy"
            # where it once said "Big Chief En", and refusing it on the grounds
            # of equal rank would leave the reader with the cut version for ever.
            if (not current
                    or incoming_rank > current_rank
                    or (incoming_rank == current_rank and len(name) > len(current))):
                self.conn.execute(
                    "UPDATE contact SET name=?, name_source=?, updated=? WHERE id=?",
                    (name, source, now, existing["id"]))
        self.conn.execute(
            "UPDATE contact SET "
            "testnet_address=CASE WHEN contact.testnet_address='' THEN ? "
            "  ELSE contact.testnet_address END, "
            "mainnet_address=CASE WHEN contact.mainnet_address='' THEN ? "
            "  ELSE contact.mainnet_address END, "
            "updated=? WHERE id=?",
            (testnet_address, mainnet_address, now, existing["id"]))

    # --- public group posts ---------------------------------------------------

    def add_group_post(self, network: str, channel: str, txid: str, height: int,
                       block_time: int, sender: str, nickname: str, text: str,
                       mine: bool = False, file_name: str = "",
                       file_type: str = "", file_data: bytes | None = None,
                       txids: list[str] | None = None) -> int:
        # A post this machine made is recorded optimistically at broadcast, with
        # height 0, so it appears straight away. The scan then sees the same txid
        # on chain. INSERT OR IGNORE kept the optimistic row and the real height
        # never landed, so a post read "pending" forever. Upsert the confirmation
        # instead, and never let a rescan overwrite `mine` -- the chain cannot
        # tell us that, only we know it.
        cur = self.conn.execute(
            "INSERT INTO group_post"
            "(network,channel,txid,height,block_time,sender,nickname,text,mine,"
            "file_name,file_type,file_data,txids) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
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
            "  file_data=COALESCE(excluded.file_data, group_post.file_data), "
            # A scan re-reading the post from the chain knows only the txid it
            # found; it must not wipe the list of every transaction we sent.
            "  txids=CASE WHEN excluded.txids != '' THEN excluded.txids "
            "              ELSE group_post.txids END",
            (network, channel, txid, height, block_time, sender, nickname, text,
             1 if mine else 0, file_name, file_type, file_data,
             ",".join(txids or [])))
        return cur.lastrowid or 0

    def address_for_tag(self, tag: str) -> str:
        """The address that published this @tag, as announced on the chain.

        The newest announcement wins: a tag moves when its holder says so,
        and a node that kept believing the first one would take instructions
        from an address its owner had left behind.
        """
        row = self.conn.execute(
            "SELECT address FROM key_announcement WHERE tag=? "
            "ORDER BY height DESC, seen_at DESC LIMIT 1",
            (str(tag).lstrip("@"),)).fetchone()
        return row["address"] if row else ""

    def tag_announced_at(self, address: str) -> str:
        """The @tag an announcement gives this address, on either chain.

        A tag is claimed on the messaging chain, so a mainnet address never
        holds one directly -- it is named by the announcement that binds both
        of a wallet's addresses to one key (D-032). Newest wins.
        """
        if not address:
            return ""
        row = self.conn.execute(
            "SELECT tag FROM key_announcement WHERE tag != '' "
            "AND (address = ? OR other_address = ?) "
            "ORDER BY height DESC, seen_at DESC LIMIT 1",
            (address, address)).fetchone()
        return row["tag"] if row else ""

    def board_unread(self, network: str) -> int:
        """Public posts, not this wallet's own, since the board was last read."""
        row = self.conn.execute("SELECT value FROM seen_mark WHERE name=?",
                                (f"board:{network}",)).fetchone()
        since = int(row["value"]) if row else 0
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM group_post WHERE network=? AND mine=0 AND id>?",
            (network, since)).fetchone()[0])

    def mark_board_read(self, network: str) -> None:
        """Everything on the board now counts as seen."""
        newest = self.conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM group_post WHERE network=?",
            (network,)).fetchone()[0]
        self.conn.execute(
            "INSERT INTO seen_mark(name, value) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (f"board:{network}", int(newest)))
        self.conn.commit()

    def unread_for(self, recipient_fp: bytes) -> int:
        """Private messages to this identity that have not been opened."""
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM message WHERE recipient_fp=? AND read_at IS NULL",
            (recipient_fp,)).fetchone()[0])

    # --- the feed ------------------------------------------------------------
    #
    # One table of actions, and everything else is a query against it. Counts
    # are not kept in columns: a column has to be corrected when a reorg takes
    # the row away, and a COUNT over an index does not (D-138).

    def add_feed_act(self, network: str, txid: str, kind: int, target: str,
                     author: str, text: str = "", height: int = 0,
                     block_time: int = 0, mine: bool = False,
                     amount: int = 0, paid_on: str = "") -> None:
        """Record one like, reply, share, edit, delete or tip.

        Upserts on confirmation the way a post does: an action of this
        machine's is written at broadcast with height 0 so the page moves at
        once, and the scan then sees the same txid on the chain. `mine` is
        never unset by a rescan -- the chain cannot tell us that. A confirmed
        row's `amount` and `paid_on` are the transaction's own, and win: the
        scan is the only reader that has actually seen the transaction.
        """
        self.conn.execute(
            "INSERT INTO feed_act"
            "(network,txid,kind,target,author,text,height,block_time,mine,"
            " amount,paid_on) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(network,txid) DO UPDATE SET "
            "  height=CASE WHEN excluded.height > 0 THEN excluded.height "
            "              ELSE feed_act.height END, "
            "  block_time=CASE WHEN excluded.height > 0 THEN excluded.block_time "
            "              ELSE feed_act.block_time END, "
            "  author=CASE WHEN excluded.author != '' THEN excluded.author "
            "              ELSE feed_act.author END, "
            "  amount=CASE WHEN excluded.height > 0 THEN excluded.amount "
            "              ELSE feed_act.amount END, "
            "  paid_on=CASE WHEN excluded.height > 0 THEN excluded.paid_on "
            "              ELSE feed_act.paid_on END, "
            "  mine=MAX(feed_act.mine, excluded.mine)",
            (network, txid, int(kind), target, author, text or "",
             int(height), int(block_time), 1 if mine else 0,
             int(amount), paid_on or ""))

    def feed_posts(self, network: str, author: str = "", before: int | None = None,
                   limit: int = 10) -> list[sqlite3.Row]:
        """A page of the feed, newest first.

        `before` is the id of the oldest post already shown, so scrolling
        asks for what comes after it rather than counting pages -- a post
        arriving between requests cannot then push a row onto two pages or
        off both (D-138).
        """
        where = "network = ?"
        params: list = [network]
        if author:
            where += " AND sender = ?"
            params.append(author)
        if before:
            where += " AND id < ?"
            params.append(int(before))
        return self.conn.execute(
            f"SELECT * FROM group_post WHERE {where} "
            f"ORDER BY id DESC LIMIT ?",
            (*params, max(1, min(limit, 100)))).fetchall()

    def complete_contacts(self) -> int:
        """Fill in what a contact's own announcement already says.

        A contact is saved by several paths and only one of them asked the
        announcement for the other chain's address and the key, so a book
        could hold somebody whose mainnet address was sitting one table away
        (D-139). Filling gaps ONLY: a value somebody typed is theirs and is
        never overwritten by the chain.

        Cheap and idempotent, so it runs when the page is drawn rather than
        needing anybody to re-add anybody.
        """
        filled = 0
        rows = self.conn.execute(
            "SELECT id, pubkey, testnet_address, mainnet_address FROM contact "
            "WHERE testnet_address != '' "
            "AND (mainnet_address = '' OR pubkey IS NULL)").fetchall()
        for row in rows:
            said = self.key_for(row["testnet_address"])
            if said is None:
                continue
            mainnet = row["mainnet_address"] or (said["other_address"] or "")
            key = row["pubkey"] or said["pubkey"]
            if mainnet == (row["mainnet_address"] or "") and key == row["pubkey"]:
                continue
            self.conn.execute(
                "UPDATE contact SET mainnet_address = ?, pubkey = ? WHERE id = ?",
                (mainnet, key, row["id"]))
            filled += 1
        return filled

    def feed_post_by_txid(self, network: str, txid: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM group_post WHERE network = ? AND txid = ?",
            (network, txid)).fetchone()

    def feed_acts_on(self, network: str, targets: list[str]) -> list[sqlite3.Row]:
        """Every action against any of these posts, oldest first.

        Asked once for a page of posts rather than once per post: a feed of
        fifty posts should be a handful of queries, not two hundred.
        """
        if not targets:
            return []
        marks = ",".join("?" * len(targets))
        return self.conn.execute(
            f"SELECT * FROM feed_act WHERE network = ? AND target IN ({marks}) "
            f"ORDER BY height, block_time", (network, *targets)).fetchall()

    def feed_tips_on(self, targets: list[str]) -> list[sqlite3.Row]:
        """Every tip against any of these posts, on whichever chain each was paid.

        Tips are the one feed action deliberately not partitioned with the
        feed: a tip is a payment, and payments may be made on either chain
        (feed.py says why), so a post's running total has to gather them from
        every chain this store has read -- and keep them apart by chain,
        because the units differ and adding them would be a lie (the operator,
        2026-09-23).
        """
        if not targets:
            return []
        marks = ",".join("?" * len(targets))
        return self.conn.execute(
            f"SELECT * FROM feed_act WHERE kind = {feed.TIP} "
            f"AND target IN ({marks}) ORDER BY height, block_time",
            tuple(targets)).fetchall()

    #: The feed's arithmetic, as SQL, for a post at table alias `alias`:
    #: likes that stand (a later unlike by the same person cancels a like,
    #: D-138's newest-wins rule in a WHERE clause), shares, and the tips --
    #: counted with the same `tip_value` the cards compute in Python
    #: (feed.py), confirmed only, and no self-tips: an author paying themself
    #: is not applause, and with amount-weighting it would otherwise be the
    #: cheapest road to the top of the feed (2026-09-23). This is the
    #: ORDER BY of `feed_posts_popular` and nothing else's: a page cannot
    #: sort a feed it assembled ten posts at a time, and an id cursor cannot
    #: page an order the database does not hold.
    _SCORE_SQL = """(
        (SELECT COUNT(*) * {like_value} FROM feed_act al
          WHERE al.target = {a}.txid AND al.network = {a}.network
            AND al.kind = {like}
            AND NOT EXISTS (SELECT 1 FROM feed_act au
              WHERE au.target = al.target AND au.network = al.network
                AND au.author = al.author AND au.kind = {unlike}
                AND (au.height, au.txid) > (al.height, al.txid)))
        + (SELECT COUNT(*) FROM feed_act ash
            WHERE ash.target = {a}.txid AND ash.network = {a}.network
              AND ash.kind = {share})
        + COALESCE((SELECT SUM(tip_value(at.amount,
                COALESCE(NULLIF(at.paid_on, ''), at.network)))
            FROM feed_act at
            WHERE at.target = {a}.txid AND at.kind = {tip}
              AND at.height > 0 AND at.author != {a}.sender), 0.0)
    )"""

    @classmethod
    def _score(cls, alias: str) -> str:
        return cls._SCORE_SQL.format(
            a=alias, like=feed.LIKE, unlike=feed.UNLIKE,
            share=feed.SHARE, tip=feed.TIP, like_value=feed.LIKE_VALUE)

    def feed_posts_popular(self, network: str, cursor: int | None = None,
                           limit: int = 10, author: str = "",
                           anchor: float | None = None) -> list[sqlite3.Row]:
        """A page of the feed, most-endorsed first, and the cursor for the next.

        The same page contract as `feed_posts` -- newest first is the feed's
        other order, and a profile page keeps it -- so scrolling needs no
        more than a different first page. The cursor is a post's id and
        `anchor` is the score that post had WHEN THE LINK WAS MADE, which is
        the part that makes a paged order honest at all: the keyset is
        `(score, id) < (anchor, cursor)`, and a keyset only means "everything
        after where I stopped" if the pair came from the moment the reader
        stopped. Recomputing it instead -- which is what this did first, and
        what the test below now refuses -- repeats a whole page the instant
        the anchor post itself is tipped, because everything that used to
        score lower now scores under it and comes round again.

        Carried, the failure the other way is bounded and one-sided: scores
        rise, so a carried score is never higher than the truth, so a page can
        miss a post that rose past where the reader stood -- and that post is
        at the top of the feed the next time anybody loads the first page.
        Missing something that moved is a feed. Showing the same post twice
        and offering a "more" link that loops is a broken page. A link with an
        id and no score in it -- bookmarked, typed, or made by something that
        has not seen this -- falls back to recomputing, which is exact on the
        first fetch and merely imprecise after one.

        Ties break by id, newest first, the same tie-break the counts use
        (D-122's lesson: never "whichever came back first").
        """
        where = "g.network = ?"
        params: list = [network]
        if author:
            where += " AND g.sender = ?"
            params.append(author)
        # Nothing here mentions the mute table, which is the decision rather
        # than an oversight. Muting hides an author's words, it is not a
        # ranking, and the row stays where its own score puts it so the page
        # can stand a gap and an Unmute button on it (D-138). Dropping muted
        # rows would let one reader's mute list change the ORDER BY, which is
        # the one number this feed promises is everybody's. The page marks the
        # row (`feedview.shown(muted=...)`), which is where a mute belongs.
        clause = ""
        if cursor:
            params_anchor = anchor
            if params_anchor is None:
                row = self.conn.execute(
                    f"SELECT {self._score('a')} AS s, id FROM group_post a"
                    f" WHERE a.id = ? AND a.network = ?",
                    (int(cursor), network)).fetchone()
                if row is None:
                    return []
                params_anchor = row["s"]
            clause = "WHERE (r.score, r.id) < (?, ?)"
            params.extend([float(params_anchor), int(cursor)])
        return self.conn.execute(
            f"SELECT * FROM (SELECT g.*, {self._score('g')} AS score"
            f"  FROM group_post g WHERE {where}) AS r "
            f"{clause} "
            f"ORDER BY r.score DESC, r.id DESC LIMIT ?",
            (*params, max(1, min(limit, 100)))).fetchall()

    def feed_acts_by(self, network: str, author: str, kind: int | None = None,
                     limit: int = 200) -> list[sqlite3.Row]:
        """What one person has done, newest first."""
        where = "network = ? AND author = ?"
        params: list = [network, author]
        if kind is not None:
            where += " AND kind = ?"
            params.append(int(kind))
        return self.conn.execute(
            f"SELECT * FROM feed_act WHERE {where} "
            f"ORDER BY height DESC, block_time DESC LIMIT ?",
            (*params, max(1, min(limit, 1000)))).fetchall()

    def feed_act_by_txid(self, network: str, txid: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM feed_act WHERE network = ? AND txid = ?",
            (network, txid)).fetchone()

    def muted(self) -> set[str]:
        """Addresses this machine will not draw."""
        return {row["address"] for row in self.conn.execute("SELECT address FROM mute")}

    def mute(self, address: str, on: bool = True) -> None:
        if on:
            self.conn.execute(
                "INSERT OR IGNORE INTO mute(address, muted_at) VALUES(?,?)",
                (address, int(time.time())))
        else:
            self.conn.execute("DELETE FROM mute WHERE address = ?", (address,))

    def group_posts(self, network: str, channel: str, limit: int = 50,
                    before_id: int | None = None) -> list[sqlite3.Row]:
        """One page of a channel, oldest first, so it reads like a room.

        It was newest-first for a while, as a feed. A channel presented beside a
        private conversation should behave like one: you read downward and the
        newest is at the bottom, where the composer is.

        A page rather than everything, because a busy channel is not a thing you
        can render: loading a year of posts to show the last twenty is slow on
        this machine and hopeless on a phone over a tunnel. `before_id` walks
        backwards through the older ones -- an id rather than a block time,
        because two posts can share a time and a page that overlaps or skips at
        the seam is worse than no paging at all.

        The file bytes are deliberately not selected: a channel listing must not
        pull every attachment in it into memory to render a page.
        """
        sql = ("SELECT id,network,channel,txid,height,block_time,sender,nickname,"
               "text,mine,file_name,file_type,LENGTH(file_data) AS file_size "
               "FROM group_post WHERE network=? AND channel=?")
        args: list[Any] = [network, channel]
        if before_id is not None:
            sql += " AND id < ?"
            args.append(before_id)
        sql += " ORDER BY block_time DESC, id DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        return list(reversed(list(self.conn.execute(sql, args))))

    def group_post_count(self, network: str, channel: str) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM group_post WHERE network=? AND channel=?",
            (network, channel)).fetchone()[0])

    def group_has_older(self, network: str, channel: str, before_id: int) -> bool:
        """Is there anything before this page? Asked rather than counted: the
        answer is one row, and the count is the whole table."""
        return self.conn.execute(
            "SELECT 1 FROM group_post WHERE network=? AND channel=? AND id<? "
            "LIMIT 1", (network, channel, before_id)).fetchone() is not None

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
        """Channels seen on this network, most recently active first.

        With how many posts in each are new to this wallet, so a list of
        channels can say WHICH one has something in it rather than only that
        the board does. A channel nobody has opened yet falls back to the
        board's own mark, so an update does not light every channel up red
        for posts that were read before there was a per-channel mark.
        """
        return list(self.conn.execute(
            "SELECT p.channel, COUNT(*) AS posts, MAX(p.block_time) AS last, "
            "  SUM(CASE WHEN p.mine = 0 AND p.id > COALESCE("
            "        (SELECT CAST(value AS INTEGER) FROM seen_mark "
            "         WHERE name = 'board:' || p.network || ':' || p.channel), "
            "        (SELECT CAST(value AS INTEGER) FROM seen_mark "
            "         WHERE name = 'board:' || p.network), 0) "
            "      THEN 1 ELSE 0 END) AS unread "
            "FROM group_post p WHERE p.network=? "
            "GROUP BY p.channel ORDER BY last DESC",
            (network,)))

    def mark_channel_read(self, network: str, channel: str) -> None:
        """This channel, as far as it has been read, counts as seen.

        Its own mark rather than the board's: opening #trading must not
        silence #releases (D-108).
        """
        newest = self.conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM group_post "
            "WHERE network=? AND channel=?", (network, channel)).fetchone()[0]
        self.conn.execute(
            "INSERT INTO seen_mark(name, value) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (f"board:{network}:{channel}", int(newest)))
        self.conn.commit()

    # --- chunked sends in progress --------------------------------------------

    def begin_pending_send(self, msg_id: bytes, recipient_key: bytes,
                           sender_address: str, body: bytes,
                           chunks: list[bytes]) -> None:
        """Record a chunked send before any of it is broadcast."""
        self.conn.execute(
            "INSERT OR REPLACE INTO pending_send"
            "(msg_id,recipient_key,sender_address,body,chunks,total,sent_count,"
            "txids,created,kind) VALUES(?,?,?,?,?,?,0,'',?,'private')",
            (msg_id, recipient_key, sender_address, body, _pack_chunks(chunks),
             len(chunks), int(time.time())),
        )

    def begin_pending_post(self, msg_id: bytes, network: str, channel: str,
                           nickname: str, sender_address: str, body: bytes,
                           chunks: list[bytes]) -> None:
        """Record a chunked PUBLIC post before any of it is broadcast.

        The same reasoning as the private path, which had this from the start: a
        post that stops half way has spent real outputs and cannot be taken
        back, so the one thing that must survive is a record of what went out.
        recipient_key is b'' -- a post has no recipient, and the column is NOT
        NULL from a schema that predates this.
        """
        self.conn.execute(
            "INSERT OR REPLACE INTO pending_send"
            "(msg_id,recipient_key,sender_address,body,chunks,total,sent_count,"
            "txids,created,kind,channel,network,nickname) "
            "VALUES(?,?,?,?,?,?,0,'',?,'public',?,?,?)",
            (msg_id, b"", sender_address, body, _pack_chunks(chunks),
             len(chunks), int(time.time()), channel, network, nickname),
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
        """Unfinished chunked private messages, oldest first.

        Filtered by kind. Without that, a public post would be offered to the
        private resume path, which would try to seal it to a recipient_key of
        b'' -- so the filter is load-bearing, not tidiness.
        """
        out = []
        for row in self.conn.execute(
                "SELECT * FROM pending_send WHERE kind='private' ORDER BY created"):
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

    def pending_posts(self, network: str | None = None) -> list[dict[str, Any]]:
        """Unfinished chunked public posts, oldest first."""
        sql = "SELECT * FROM pending_send WHERE kind='public'"
        args: list[Any] = []
        if network is not None:
            sql += " AND network=?"
            args.append(network)
        out = []
        for row in self.conn.execute(sql + " ORDER BY created", args):
            out.append({
                "msg_id": bytes(row["msg_id"]),
                "network": row["network"],
                "channel": row["channel"],
                "nickname": row["nickname"],
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
                 sender_fp: str, body: bytes, file_name: str = "",
                 file_type: str = "", file_data: bytes | None = None,
                 txids: list[str] | None = None) -> None:
        """Keep our own plaintext copy of a message we sent.

        Not an optimisation -- it is the only copy we will ever have. A message
        is sealed to the recipient, so the sender genuinely cannot read it back
        off the chain: that is `crypto_box_seal` doing its job, not a gap. A
        conversation on any machine is therefore what it received (from the
        chain) plus what it sent (from here), and a machine that fails to write
        this down shows a half conversation with its own replies missing.

        Which is what happened: this was called from the web interface and not
        from the CLI, so a machine that sent with `arcade-msg` saw only the other
        person's side. Both front ends go through `record_sent` now.
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO sent"
            "(txid,created,recipient_key,recipient_addr,sender_fp,body,"
            "file_name,file_type,file_data,txids) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (txid, int(time.time()), recipient_key, recipient_addr, sender_fp,
             body, file_name, file_type, file_data,
             ",".join(txids or [txid])),
        )

    def sent_file(self, sent_id: int) -> sqlite3.Row | None:
        """The file the sender kept. Their own copy: the chain's is sealed to
        the recipient, so this is the only one they will ever have."""
        return self.conn.execute(
            "SELECT file_name, file_type, file_data FROM sent WHERE id=?",
            (sent_id,)).fetchone()

    def mark_sent_confirmed(self, txid: str, block_time: int = 0,
                            height: int = 0) -> None:
        """Record that a message we sent is in a block.

        The block time replaces the local send time, because that is the moment
        the message actually exists for anyone else -- and until it arrives the
        bubble says "unconfirmed" rather than showing a time that only means
        "when this computer pressed send".
        """
        self.conn.execute(
            "UPDATE sent SET confirmed=1, "
            "created=CASE WHEN ? > 0 THEN ? ELSE created END, "
            "height=CASE WHEN ? > 0 THEN ? ELSE height END "
            "WHERE txid=?",
            (block_time, block_time, height, height, txid))

    def unconfirmed_sent(self) -> list[sqlite3.Row]:
        """Messages we have sent that are not in a block yet."""
        return list(self.conn.execute(
            "SELECT id, txid, txids, confirmed_count FROM sent WHERE confirmed=0"))

    def record_send_progress(self, table: str, row_id: int, confirmed: int) -> None:
        """How many of a send's transactions are in blocks so far."""
        if table not in ("sent", "group_post"):        # never interpolate freely
            raise ValueError(table)
        self.conn.execute(
            f"UPDATE {table} SET confirmed_count=? WHERE id=?", (confirmed, row_id))

    @staticmethod
    def txid_list(row: sqlite3.Row, fallback: str = "") -> list[str]:
        """The transactions of a send. Older rows only ever knew their first."""
        try:
            stored = row["txids"] or ""
        except (IndexError, KeyError):
            stored = ""
        found = [t for t in stored.split(",") if t]
        return found or ([fallback] if fallback else [])

    # --- node to node ---------------------------------------------------------

    def add_api_message(self, network: str, txid: str, height: int,
                        block_time: int, sender_addr: str, sender_pubkey: bytes,
                        recipient_fp: str, body: bytes, mine: bool = False,
                        protocol: int = 0, fingerprint: bytes = b"") -> int:
        cur = self.conn.execute(
            "INSERT INTO api_message"
            "(network,txid,height,block_time,sender_addr,sender_pubkey,"
            "recipient_fp,body,mine,protocol,fingerprint) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            # Promoted, not duplicated, when the block for a message this node
            # already read out of the mempool finally arrives. The row keeps
            # its id, so a program that has already acted on it does not see
            # it a second time (D-050).
            "ON CONFLICT(txid, recipient_fp) DO UPDATE SET "
            "  height=excluded.height, block_time=excluded.block_time "
            "WHERE api_message.height = 0 AND excluded.height > 0",
            (network, txid, height, block_time, sender_addr, sender_pubkey,
             recipient_fp, body, 1 if mine else 0, protocol, fingerprint))
        return cur.lastrowid or 0

    def api_messages(self, recipient_fp: str, network: str | None = None,
                     after_id: int = 0, limit: int = 50,
                     unread_only: bool = False) -> list[sqlite3.Row]:
        """Messages addressed to this node, oldest first.

        `after_id` rather than a timestamp: a program reading a queue needs a
        cursor it can store and resume from exactly, and two messages can share
        a block time.
        """
        sql = ("SELECT * FROM api_message WHERE recipient_fp=? AND id>? "
               "AND mine=0")
        args: list[Any] = [recipient_fp, after_id]
        if network is not None:
            sql += " AND network=?"
            args.append(network)
        if unread_only:
            sql += " AND read_at IS NULL"
        sql += " ORDER BY id LIMIT ?"
        args.append(max(1, min(limit, 500)))
        return list(self.conn.execute(sql, args))

    def mark_api_read(self, ids: list[int]) -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        cur = self.conn.execute(
            f"UPDATE api_message SET read_at=? WHERE id IN ({marks}) "
            f"AND read_at IS NULL", [int(time.time()), *ids])
        return cur.rowcount

    def unconfirmed_posts(self, network: str | None = None) -> list[sqlite3.Row]:
        """Posts of our own with no block yet -- height 0 means unconfirmed.

        Our own copy is written when it is sent, and its height only arrived
        when the scanner read the whole post back off the chain. For a picture
        that is eighteen transactions across ten blocks, so a post whose first
        transaction confirmed in a minute could sit marked pending for ten --
        with every byte of it already paid for and in a block.
        """
        sql = ("SELECT id, network, txid, txids, confirmed_count FROM group_post "
               "WHERE mine=1 AND height=0")
        args: list[Any] = []
        if network is not None:
            sql += " AND network=?"
            args.append(network)
        return list(self.conn.execute(sql, args))

    def mark_post_confirmed(self, post_id: int, height: int,
                            block_time: int = 0) -> None:
        """The block its first transaction reached. The rest follow it."""
        self.conn.execute(
            "UPDATE group_post SET height=?, "
            "block_time=CASE WHEN ? > 0 THEN ? ELSE block_time END WHERE id=?",
            (height, block_time, block_time, post_id))

    def waiting_chunks(self, network: str | None = None) -> int:
        """How many pieces of unfinished posts are held, waiting for the rest.

        A post is only assembled once every one of its chunks has been seen, so
        any number here means a scan still has work to find -- which is the
        difference between "there is nothing new" and "there is something new
        that has not been looked for yet".
        """
        sql = "SELECT COUNT(*) FROM group_chunk"
        args: list[Any] = []
        if network is not None:
            sql += " WHERE network=?"
            args.append(network)
        return int(self.conn.execute(sql, args).fetchone()[0])

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
                "UPDATE contact SET name=?, name_source=?, testnet_address=?, "
                "mainnet_address=?, notes=?, updated=? WHERE id=?",
                (name, "typed" if name else "", testnet_address, mainnet_address,
                 notes, now, contact_id),
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

    def thread(self, recipient_fp: str, peer_key: bytes,
               limit: int | None = None) -> list[dict]:
        """Every message with one correspondent, oldest first.

        A message is shown once even when both halves of it are held here. That
        happens whenever you are your own correspondent: the send is recorded
        locally *and* the same transaction is later decrypted off the chain, so
        the conversation showed every message twice. Both records are correct;
        they are the same message, and the sent copy is the one to keep because
        it is the plaintext as written.
        """
        items = []
        sent_txids = {
            row["txid"] for row in self.conn.execute(
                "SELECT txid FROM sent WHERE sender_fp=? AND recipient_key=?",
                (recipient_fp, peer_key))
        }
        for row in self.conn.execute(
            "SELECT id, body, block_time, read_at, first_txid, height FROM message "
            "WHERE recipient_fp=? AND sender_pubkey=? ", (recipient_fp, peer_key)
        ):
            if row["first_txid"] in sent_txids:
                continue          # our own message, already held as sent
            items.append({"id": row["id"], "body": row["body"], "when": row["block_time"],
                          "mine": False, "txid": row["first_txid"], "height": row["height"],
                          "unread": row["read_at"] is None})
        for row in self.conn.execute(
            "SELECT id, body, created, txid, confirmed, height, file_name, "
            "       file_type, LENGTH(file_data) AS file_size, txids, "
            "       confirmed_count FROM sent "
            "WHERE sender_fp=? AND recipient_key=?", (recipient_fp, peer_key)
        ):
            parts = len(self.txid_list(row, row["txid"]))
            items.append({"id": row["id"], "body": row["body"], "when": row["created"],
                          "mine": True, "txid": row["txid"],
                          "height": row["height"] or None,
                          "confirmed": bool(row["confirmed"]),
                          # A picture is dozens of transactions, so "unconfirmed"
                          # on its own says nothing about whether it is nearly
                          # done or has not started.
                          "parts": parts,
                          "parts_done": row["confirmed_count"] or 0,
                          "file_name": row["file_name"],
                          "file_type": row["file_type"],
                          "file_size": row["file_size"] or 0})
        items.sort(key=lambda i: i["when"])
        # The newest `limit` of them, still in reading order. A conversation
        # years long is not something a phone should be asked to draw to show
        # you the last thing somebody said.
        return items[-limit:] if limit else items

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
