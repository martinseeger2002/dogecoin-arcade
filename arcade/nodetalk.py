"""An inscribed page talking to another DogecoinArcade node, and hearing back.

Why it exists
-------------
A shop page needs to tell the shop's own node what was ordered; a game lobby
needs to hand a move to the node that runs the table. An inscribed page
cannot reach either: its sandbox lets it load nothing from outside this
machine (arcade/web/content.py). The one channel this wallet already has to
another machine is the node-to-node message (messaging/api.py): sealed to
that node's key, authenticated as from this one, carried on the chain. This
gives a page that channel, in both directions.

Why there is no approval
------------------------
Coins, tokens and inscriptions go through the approvals queue because they
move value. A message moves none. It costs one transaction on the messaging
chain, and the messaging chain is testnet, deliberately and only (D-010), so
what it costs is testnet coins -- free, in a wallet that keeps itself funded.
Asking the owner to press Approve on every ping a page sends would make the
channel unusable for what it is for; so a page sends at once, and what keeps
a page in a loop from spending the wallet's testnet coins is a cap on how
many it may send in an hour, not a person.

What a page can and cannot do
-----------------------------
It sends AS this node: the other node sees this node's key, not the page,
because the page has no key -- the wallet seals on its behalf and the page
never sees anything but the txid. It reads only what came back from a node
it wrote to, after it wrote: never the rest of this node's inbox, which
carries the traffic of every other page and every bot on the bot RPC. Two
pages that write to the same node both see that node's answers, since the
other node answers the node, not the page; a page that needs to tell its
answers from another's puts something of its own in the message.

The page's identity is the frame it runs in, exactly as for storage
(arcade/pagestore.py): the viewer knows which inscription it framed, and
files everything under that id. A page opened outside a viewer has no wallet
listening and can send nothing.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .messaging import api as apilib
from .db import add_missing_columns

#: How many messages one page may send in an hour. A page talking to a shop
#: sends a handful; one sending thirty a minute is not talking, it is
#: spending, and the refusal is the answer it gets.
MAX_PER_HOUR = 30
WINDOW = 3600

#: How many replies one read hands back at most.
MAX_REPLIES = 100


class TalkError(ValueError):
    """The page asked for something this cannot do."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS peer(
    page     TEXT NOT NULL,
    network  TEXT NOT NULL,
    pubkey   TEXT NOT NULL,
    -- The inbox row that was newest when this page first wrote to the peer:
    -- what the peer says from then on is this page's to read, and nothing
    -- from before is.
    since    INTEGER NOT NULL,
    first    REAL NOT NULL,
    PRIMARY KEY(page, network, pubkey)
);
CREATE TABLE IF NOT EXISTS letter(
    page     TEXT NOT NULL,
    network  TEXT NOT NULL,
    pubkey   TEXT NOT NULL,
    txid     TEXT NOT NULL,
    created  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS letter_page ON letter(page, network, created);
"""


class Talk:
    """What each page has said, and to whom, in one sqlite file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._open() as conn:
            conn.executescript(SCHEMA)
            add_missing_columns(conn, SCHEMA)

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def sent_lately(self, page: str, network: str) -> int:
        with self._open() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM letter WHERE page=? AND network=? AND created>?",
                (page, network, time.time() - WINDOW)).fetchone()[0]

    def record(self, page: str, network: str, pubkey: str, txid: str, since: int) -> None:
        """One message went out. The first to a peer opens that peer's replies."""
        now = time.time()
        with self._open() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO peer(page, network, pubkey, since, first) "
                "VALUES(?,?,?,?,?)", (page, network, pubkey, int(since), now))
            conn.execute(
                "INSERT INTO letter(page, network, pubkey, txid, created) VALUES(?,?,?,?,?)",
                (page, network, pubkey, txid, now))

    def peers(self, page: str, network: str) -> dict[str, int]:
        """Who this page has written to, and from which inbox row on."""
        with self._open() as conn:
            return {r["pubkey"]: r["since"] for r in conn.execute(
                "SELECT pubkey, since FROM peer WHERE page=? AND network=?",
                (page, network))}

    def letters(self, page: str, network: str, limit: int = 50) -> list[dict]:
        """What this page has sent, newest first."""
        with self._open() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT pubkey AS `to`, txid, created FROM letter WHERE page=? AND network=? "
                "ORDER BY created DESC LIMIT ?", (page, network, max(1, min(int(limit), 500))))]


# --- what the page hands over, and what it gets ------------------------------

def parse_pubkey(value: Any) -> bytes:
    """A 32-byte X25519 key, as 64 hex characters or as a contact code."""
    text = "" if value is None else str(value).strip()
    from .messaging import contact as contactlib
    if not text:
        raise TalkError("name who it is for: a public key or a contact code")
    try:
        return contactlib.decode(text)[1]
    except Exception:
        pass
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raise TalkError("a recipient is a contact code or 64 hex characters") from None
    if len(raw) != 32:
        raise TalkError("a public key is 32 bytes")
    return raw


def body_bytes(value: Any) -> bytes:
    """Text, or anything JSON-shaped, which goes as compact JSON."""
    import json
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":")).encode()
    if isinstance(value, str):
        return value.encode()
    raise TalkError("body must be a string or a JSON object")


def describe(row: Any) -> dict[str, Any]:
    """One received message, the way a page and a bot both see it."""
    message = apilib.ApiMessage(
        id=row["id"], txid=row["txid"], height=row["height"],
        block_time=row["block_time"], sender_pubkey=bytes(row["sender_pubkey"]),
        sender_address=row["sender_addr"], body=bytes(row["body"]))
    return {
        "id": message.id,
        "txid": message.txid,
        "block": message.height,
        "blocktime": message.block_time,
        "frompubkey": message.sender_pubkey.hex(),
        "fromaddress": message.sender_address,
        "body": message.text,
        "json": message.json(),
        "read": row["read_at"] is not None,
        # What the sender was speaking, and whether we agree. Reported rather
        # than enforced: only the program reading this knows whether the
        # command it cares about has changed between the two versions.
        "protocol": row["protocol"],
        "apihash": bytes(row["fingerprint"]).hex(),
        "compatible": apilib.compatible(row["protocol"], bytes(row["fingerprint"])),
    }


def replies(store: Any, talk: Talk, page: str, network: str, recipient_fp: str,
            after: int = 0, limit: int = MAX_REPLIES) -> list[dict[str, Any]]:
    """What the nodes this page wrote to have said since, oldest first.

    `after` is the page's own cursor -- the id of the last reply it handled --
    so it can pick up where it stopped, the way a bot does on da_inbox. The
    floor under it is the page's first message to each peer: nothing that
    node said before being written to is this page's business.
    """
    peers = talk.peers(page, network)
    if not peers:
        return []
    limit = max(1, min(int(limit), MAX_REPLIES))
    floor = max(int(after), min(peers.values()))
    out: list[dict[str, Any]] = []
    while len(out) < limit:
        rows = store.api_messages(recipient_fp, network, after_id=floor, limit=500)
        if not rows:
            break
        for row in rows:
            floor = row["id"]
            sender = bytes(row["sender_pubkey"]).hex()
            if sender in peers and row["id"] > peers[sender]:
                out.append(describe(row))
                if len(out) >= limit:
                    break
        if len(rows) < 500:
            break
    return out
