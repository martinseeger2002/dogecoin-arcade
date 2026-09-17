"""Protocol state: properties, balances, and the transaction logic over them.

Two kinds of failure are carefully distinguished here, because conflating them is
how meta-layers silently diverge:

  * **Unsupported message type** -> raise, and stop the indexer. We do not know
    what the rest of the network did with it, so any state we produce afterwards
    is untrustworthy. (Hard rule #2.)

  * **Invalid transaction** -> record it as invalid and change NO state. This is
    normal and expected: an underfunded send, a bad property id, a missing
    recipient. Every implementation must agree these do nothing, so they are part
    of consensus, not errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import inscriptions as I
from . import tags as T
from . import payload as P
from .config import (
    FIRST_PROPERTY_ID_MAIN,
    FIRST_PROPERTY_ID_TEST,
    RESERVED_PROPERTY_IDS,
    Params,
)
from .db import (Database, StateDB, add_missing_columns,
                 register_journalled_table)
from .tx import ArcadeTransaction, EncodingClass

# Omni's MAX_INT_8_BYTES -- amounts are signed 64-bit on the wire despite being
# carried in a uint64 field.
MAX_AMOUNT = 9_223_372_036_854_775_807

ECOSYSTEM_MAIN = 1
ECOSYSTEM_TEST = 2

# Property types (omnicore.h:89-95).
PROPERTY_INDIVISIBLE = 1
PROPERTY_DIVISIBLE = 2
PROPERTY_NONFUNGIBLE = 5
FUNGIBLE_PROPERTY_TYPES = frozenset({1, 2, 65, 66, 129, 130})
ALL_PROPERTY_TYPES = FUNGIBLE_PROPERTY_TYPES | {PROPERTY_NONFUNGIBLE}


SCHEMA = """
-- One row per created property.
CREATE TABLE IF NOT EXISTS property (
    property_id     INTEGER PRIMARY KEY,
    ecosystem       INTEGER NOT NULL,
    property_type   INTEGER NOT NULL,
    issuer          TEXT    NOT NULL,
    category        TEXT    NOT NULL DEFAULT '',
    subcategory     TEXT    NOT NULL DEFAULT '',
    name            TEXT    NOT NULL DEFAULT '',
    url             TEXT    NOT NULL DEFAULT '',
    data            TEXT    NOT NULL DEFAULT '',
    managed         INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    creation_txid   TEXT    NOT NULL,
    creation_block  INTEGER NOT NULL
);

-- Balance buckets, mirroring Omni's TallyType (tally.h:8-15).
-- PENDING is deliberately absent: it is a wallet-side concept and is excluded
-- from the consensus hash (consensushash.cpp:49-58).
CREATE TABLE IF NOT EXISTS balance (
    address            TEXT    NOT NULL,
    property_id        INTEGER NOT NULL,
    balance            INTEGER NOT NULL DEFAULT 0,
    selloffer_reserve  INTEGER NOT NULL DEFAULT 0,
    accept_reserve     INTEGER NOT NULL DEFAULT 0,
    metadex_reserve    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (address, property_id)
);

-- Feature activations received on-chain (types 65533/65534).
CREATE TABLE IF NOT EXISTS activation (
    feature_id         INTEGER PRIMARY KEY,
    activation_block   INTEGER NOT NULL,
    min_client_version INTEGER NOT NULL,
    active             INTEGER NOT NULL DEFAULT 1
);

-- Every protocol-carrying transaction seen, valid or not. Invalid ones are kept
-- deliberately: "this did nothing, and here is why" is consensus-relevant and
-- the first thing anyone asks when two implementations disagree.
CREATE TABLE IF NOT EXISTS arcade_tx (
    txid           TEXT    PRIMARY KEY,
    block_height   INTEGER NOT NULL,
    position       INTEGER NOT NULL,
    encoding_class TEXT    NOT NULL,
    message_type   INTEGER,
    message_version INTEGER,
    sender         TEXT    NOT NULL,
    reference      TEXT,
    payload_hex    TEXT    NOT NULL,
    valid          INTEGER NOT NULL,
    invalid_reason TEXT
);

-- Inscriptions. A file written onto the chain in full, owned by an address and
-- moved only by its owner saying so.
--
-- `number` is assigned in chain order when the LAST piece arrives, which is the
-- moment the inscription exists. Two nodes replaying the same chain therefore
-- agree without having to talk to each other: the order is the chain's, not
-- anybody's opinion.
--
-- `content` may be NULL. The hash and the length are always kept, so a body
-- that was not stored can be fetched back off the chain and proved to be the
-- right one -- which is what lets a node keep its own inscriptions in full
-- without also keeping every megabyte a stranger ever wrote.
CREATE TABLE IF NOT EXISTS inscription (
    txid          TEXT    PRIMARY KEY,
    number        INTEGER NOT NULL,
    creator       TEXT    NOT NULL,
    owner         TEXT    NOT NULL,
    block_height  INTEGER NOT NULL,
    position      INTEGER NOT NULL,
    content_type  TEXT    NOT NULL,
    content_len   INTEGER NOT NULL,
    sha256        TEXT    NOT NULL,
    json          TEXT    NOT NULL DEFAULT '',
    chunks        INTEGER NOT NULL,
    content       BLOB
);

CREATE UNIQUE INDEX IF NOT EXISTS inscription_number_idx ON inscription(number);
CREATE INDEX IF NOT EXISTS inscription_owner_idx ON inscription(owner);
CREATE INDEX IF NOT EXISTS inscription_creator_idx ON inscription(creator);

-- Pieces seen so far, keyed by who sent them as well as by the id in the
-- payload. The id is in the clear on the chain, so anyone can publish a chunk
-- claiming somebody else's -- grouping by sender is what stops one injected
-- piece making a real inscription permanently incomplete.
CREATE TABLE IF NOT EXISTS inscription_chunk (
    sender        TEXT    NOT NULL,
    inscription_id TEXT   NOT NULL,
    countdown     INTEGER NOT NULL,
    txid          TEXT    NOT NULL,
    block_height  INTEGER NOT NULL,
    position      INTEGER NOT NULL,
    body          BLOB    NOT NULL,
    PRIMARY KEY (sender, inscription_id, countdown)
);

-- Which transactions an assembled inscription was made of, in the order its
-- countdown gives them. Kept so the FILE does not have to be: the bytes are
-- on the chain in full, this says where, and `ledger.inscription_content`
-- reads them back and checks them against the manifest's sha256 when
-- somebody actually asks for them (D-113).
CREATE TABLE IF NOT EXISTS inscription_piece (
    inscription  TEXT    NOT NULL,
    countdown    INTEGER NOT NULL,
    txid         TEXT    NOT NULL,
    PRIMARY KEY (inscription, countdown)
);

-- Collections. Which set an inscription belongs to, read off its JSON by one
-- rule (`inscriptions.collection_of`) at the moment it completes, so every
-- node files it the same way. A set from the HashLips Art Engine lands here
-- with no extra work: its metadata already says `name: "Prefix #12"` and
-- `edition: 12`, and that is the rule.
CREATE TABLE IF NOT EXISTS collection_item (
    txid          TEXT    PRIMARY KEY,
    creator       TEXT    NOT NULL,
    collection    TEXT    NOT NULL,
    edition       INTEGER,
    name          TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS collection_item_idx
    ON collection_item(creator, collection, edition);

-- @tags. One name to an address and one address to a name: a name that points
-- at two people is not a name, and an address with two gives a reader two
-- answers to the same question. Both are enforced by the keys here rather than
-- by the code that writes them.
CREATE TABLE IF NOT EXISTS tag (
    tag           TEXT    PRIMARY KEY,
    address       TEXT    NOT NULL UNIQUE,
    claimed_txid  TEXT    NOT NULL,
    block_height  INTEGER NOT NULL,
    position      INTEGER NOT NULL
);

-- The order book. An order is a standing, public instruction: this address
-- will sell (or buy) this much of a token at this price, until it is
-- cancelled or it can no longer be honoured. Price is a pair of integers and
-- is never a float: amount_desired / amount_for_sale, exact (D-048).
--
-- Property 0 is the chain's own coin. Omni has no such id, because Omni's
-- MetaDEx pairs two tokens and leaves the native coin to its older DEx; here
-- the coin is one side of every pair, so it needs a name.
CREATE TABLE IF NOT EXISTS book_order (
    txid          TEXT    PRIMARY KEY,
    block_height  INTEGER NOT NULL,
    position      INTEGER NOT NULL,
    address       TEXT    NOT NULL,
    sale_property INTEGER NOT NULL,
    sale_amount   INTEGER NOT NULL,     -- what is left of it
    want_property INTEGER NOT NULL,
    want_amount   INTEGER NOT NULL,     -- for what is left, at the same price
    reserved      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS book_order_pair_idx
    ON book_order(sale_property, want_property);

-- Where an inscription has been. One row per hand it changed in, written as
-- the move is applied, because the inscription row itself only ever holds the
-- CURRENT owner -- it is overwritten on every transfer, so indexed state kept
-- no history at all and "when did this first leave the creator" had no answer
-- on any node (D-071).
--
-- Not the same thing as the transaction list: a page asking for provenance
-- wants who held it and when, not which transactions carried the bytes.
CREATE TABLE IF NOT EXISTS inscription_move (
    -- Keyed on the pair, not the transaction. One swap can hand over TWO
    -- pieces -- a Goofball for a Goofball is the trade this was built to
    -- make possible -- and `_move_leg` is then called twice for one txid.
    -- Keyed on txid alone the second insert collided, and it raised
    -- StateError rather than InvalidTransaction, so it threw out of the
    -- handler instead of marking the transaction invalid: one such swap on
    -- the chain would have stopped indexing at that height on every node
    -- that met it (D-080).
    txid          TEXT    NOT NULL,
    inscription   TEXT    NOT NULL,
    from_address  TEXT    NOT NULL,
    to_address    TEXT    NOT NULL,
    block_height  INTEGER NOT NULL,
    position      INTEGER NOT NULL,
    how           TEXT    NOT NULL DEFAULT 'transfer',  -- or 'swap'
    PRIMARY KEY (txid, inscription)
);
CREATE INDEX IF NOT EXISTS inscription_move_item
    ON inscription_move(inscription, block_height, position);

-- An offer for somebody's inscription, made in public (D-042). Said on the
-- chain because a holder who never published a key cannot be messaged, and
-- never asked to be. Nothing is locked by one: it is an offer.
CREATE TABLE IF NOT EXISTS nft_offer (
    txid          TEXT    PRIMARY KEY,
    block_height  INTEGER NOT NULL,
    position      INTEGER NOT NULL,
    inscription   TEXT    NOT NULL,
    buyer         TEXT    NOT NULL,
    take_kind     INTEGER NOT NULL,
    take_property INTEGER,
    take_amount   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS nft_offer_item_idx ON nft_offer(inscription);

-- The other half: a price put on an inscription by whoever holds it (D-099).
-- An Omni-style ask, in the sense that matters -- it is a standing public
-- instruction, indexed from the chain, that anybody can read and act on --
-- but it reserves nothing. Nothing on this chain can hold an inscription
-- back, and an ask is live only while the address that made it still holds
-- what it names, so sending a piece away withdraws its price with no
-- transaction at all. One row per ask transaction; the newest for an
-- inscription is the one that counts, and a take_kind of 0 is a withdrawal.
CREATE TABLE IF NOT EXISTS nft_ask (
    txid          TEXT    PRIMARY KEY,
    block_height  INTEGER NOT NULL,
    position      INTEGER NOT NULL,
    inscription   TEXT    NOT NULL,
    seller        TEXT    NOT NULL,
    take_kind     INTEGER NOT NULL,
    take_property INTEGER,
    take_amount   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS nft_ask_item_idx ON nft_ask(inscription, block_height, position);

CREATE INDEX IF NOT EXISTS ribbit_tx_block_idx ON arcade_tx(block_height, position);
CREATE INDEX IF NOT EXISTS balance_property_idx ON balance(property_id);
"""


def install_schema(db: Database) -> None:
    """Create the protocol tables and register them for journalling."""
    db.conn.executescript(SCHEMA)
    add_missing_columns(db.conn, SCHEMA)
    register_journalled_table("property", ("property_id",))
    register_journalled_table("balance", ("address", "property_id"))
    register_journalled_table("activation", ("feature_id",))
    register_journalled_table("arcade_tx", ("txid",))
    register_journalled_table("tag", ("tag",))
    register_journalled_table("inscription", ("txid",))
    register_journalled_table("nft_offer", ("txid",))
    register_journalled_table("nft_ask", ("txid",))
    register_journalled_table("inscription_move", ("txid", "inscription"))
    register_journalled_table("book_order", ("txid",))
    register_journalled_table("inscription_chunk",
                              ("sender", "inscription_id", "countdown"))
    register_journalled_table("collection_item", ("txid",))
    register_journalled_table("inscription_piece", ("inscription", "countdown"))
    _rekey_moves(db)
    _file_collections(db)


def _rekey_moves(db: Database) -> None:
    """Replace an inscription_move keyed on txid alone.

    Shipped that way and it cannot record a swap of two pieces. Dropped
    rather than migrated because every row is derived from transactions this
    node has already read: the backfill rebuilds the lot, and a rebuild from
    the source beats a rewrite of a table that was wrong (D-080).
    """
    row = db.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='inscription_move'"
    ).fetchone()
    if row and "PRIMARY KEY (txid, inscription)" not in (row["sql"] or ""):
        db.conn.execute("DROP TABLE inscription_move")
        db.conn.executescript(SCHEMA)


def _file_collections(db: Database) -> None:
    """File inscriptions indexed before collections were, once.

    The rule is a pure function of the JSON already in the row, so an index
    built by an older version is brought level here rather than by a rescan.
    Nothing is journalled: these rows are as old as the inscriptions they
    describe, and a reorg deep enough to remove those removes the whole
    index's recent history anyway.
    """
    rows = db.conn.execute(
        "SELECT i.txid, i.creator, i.json FROM inscription i "
        "LEFT JOIN collection_item c ON c.txid = i.txid "
        "WHERE c.txid IS NULL AND i.json != ''").fetchall()
    for row in rows:
        member = I.collection_of(row["json"])
        if member is None:
            continue
        collection, edition, name = member
        db.conn.execute(
            "INSERT OR IGNORE INTO collection_item "
            "(txid, creator, collection, edition, name) VALUES (?, ?, ?, ?, ?)",
            (row["txid"], row["creator"], collection, edition, name))


class InvalidTransaction(Exception):
    """The transaction is well-formed but breaks a protocol rule.

    This is a normal outcome, not a bug. It is caught by the engine, recorded,
    and changes no state.
    """


@dataclass
class Result:
    """What processing one transaction did."""

    valid: bool
    reason: str | None = None
    message_type: int | None = None
    message_version: int | None = None


class Engine:
    """Applies Arcade messages to protocol state."""

    def __init__(self, state: StateDB, params: Params,
                 keep_content: Callable[[str], bool] | None = None):
        self.state = state
        self.params = params
        # Whether an inscription's bytes are worth keeping, asked of its
        # creator's address. A node keeps its own in full and describes
        # everybody else's -- the hash and the length are always stored, so a
        # body that was not kept can be fetched back off the chain later and
        # proved to be the right one.
        # Nothing kept by default: the bytes are on the chain, the pieces
        # table says where, and they are read back when somebody asks
        # (D-113). A node that would rather hold them -- a gallery answering
        # strangers, an archive -- passes its own policy here.
        self.keep_content = keep_content or (lambda address: False)

    # --- balance helpers ------------------------------------------------------

    def get_balance(self, address: str, property_id: int) -> dict[str, int]:
        row = self.state.db.conn.execute(
            "SELECT * FROM balance WHERE address = ? AND property_id = ?",
            (address, property_id),
        ).fetchone()
        if row is None:
            return {
                "balance": 0,
                "selloffer_reserve": 0,
                "accept_reserve": 0,
                "metadex_reserve": 0,
            }
        return {
            "balance": row["balance"],
            "selloffer_reserve": row["selloffer_reserve"],
            "accept_reserve": row["accept_reserve"],
            "metadex_reserve": row["metadex_reserve"],
        }

    def credit(self, address: str, property_id: int, amount: int, bucket: str = "balance") -> None:
        if amount == 0:
            return
        current = self.get_balance(address, property_id)
        exists = self.state.db.conn.execute(
            "SELECT 1 FROM balance WHERE address = ? AND property_id = ?", (address, property_id)
        ).fetchone()
        new_value = current[bucket] + amount
        if new_value < 0:
            raise InvalidTransaction(
                f"{bucket} for {address} property {property_id} would go negative"
            )
        if exists:
            self.state.update(
                "balance", {"address": address, "property_id": property_id}, {bucket: new_value}
            )
        else:
            row = {"address": address, "property_id": property_id, **current}
            row[bucket] = new_value
            self.state.insert("balance", row)

    def debit(self, address: str, property_id: int, amount: int, bucket: str = "balance") -> None:
        self.credit(address, property_id, -amount, bucket)

    # --- property helpers -----------------------------------------------------

    def get_property(self, property_id: int) -> Any:
        return self.state.db.conn.execute(
            "SELECT * FROM property WHERE property_id = ?", (property_id,)
        ).fetchone()

    def next_property_id(self, ecosystem: int) -> int:
        """The next free property id in `ecosystem`.

        Arcade reserves ids 1 and 2 permanently (config.RESERVED_PROPERTY_IDS):
        Omni uses them for OMNI and TOMNI, and Arcade has no base token (D-007),
        so main-ecosystem properties start at 3.
        """
        floor = FIRST_PROPERTY_ID_MAIN if ecosystem == ECOSYSTEM_MAIN else FIRST_PROPERTY_ID_TEST
        row = self.state.db.conn.execute(
            "SELECT MAX(property_id) AS top FROM property WHERE ecosystem = ?", (ecosystem,)
        ).fetchone()
        if row is None or row["top"] is None:
            return floor
        candidate = row["top"] + 1
        while candidate in RESERVED_PROPERTY_IDS:
            candidate += 1
        return max(candidate, floor)

    # --- entry point ----------------------------------------------------------

    def process(self, rtx: ArcadeTransaction) -> Result:
        """Decode and apply one transaction.

        An unsupported message type propagates out of here and stops the indexer.
        An invalid transaction is recorded and changes nothing.
        """
        message = P.decode(rtx.payload)   # raises for unsupported types -- by design

        result = Result(
            valid=True, message_type=message.TYPE, message_version=message.version
        )
        try:
            self._apply(rtx, message)
        except InvalidTransaction as exc:
            result.valid = False
            result.reason = str(exc)

        self.state.insert(
            "arcade_tx",
            {
                "txid": rtx.txid,
                "block_height": rtx.block_height,
                "position": rtx.position,
                "encoding_class": rtx.encoding_class.value,
                "message_type": message.TYPE,
                "message_version": message.version,
                "sender": rtx.sender,
                "reference": rtx.reference,
                "payload_hex": rtx.payload.hex(),
                "valid": 1 if result.valid else 0,
                "invalid_reason": result.reason,
            },
        )
        return result

    def _apply(self, rtx: ArcadeTransaction, message: P.Message) -> None:
        handler = {
            P.SimpleSend: self._simple_send,
            P.SendAll: self._send_all,
            P.IssuanceFixed: self._issuance_fixed,
            P.IssuanceManaged: self._issuance_managed,
            P.Grant: self._grant,
            P.Revoke: self._revoke,
            P.ChangeIssuer: self._change_issuer,
            P.ActivateFeature: self._activate_feature,
            P.DeactivateFeature: self._deactivate_feature,
            P.Alert: self._alert,
            P.AnyData: self._any_data,
            P.MetaDExTrade: self._book_order,
            P.MetaDExCancelPrice: self._book_cancel_price,
            P.MetaDExCancelPair: self._book_cancel_pair,
        }.get(type(message))

        if handler is None:
            raise InvalidTransaction(
                f"message type {message.TYPE} is recognised but not yet implemented "
                f"(arrives in a later milestone)"
            )
        handler(rtx, message)

    # --- type 0 ---------------------------------------------------------------

    def _simple_send(self, rtx: ArcadeTransaction, msg: P.SimpleSend) -> None:
        """Type 0. tx.cpp:logicMath_SimpleSend"""
        if rtx.reference is None:
            raise InvalidTransaction("simple send has no reference (recipient) address")
        if not 0 < msg.amount <= MAX_AMOUNT:
            raise InvalidTransaction(f"amount {msg.amount} out of range")

        prop = self.get_property(msg.property_id)
        if prop is None:
            raise InvalidTransaction(f"property {msg.property_id} does not exist")
        if prop["property_type"] == PROPERTY_NONFUNGIBLE:
            raise InvalidTransaction(
                f"property {msg.property_id} is non-fungible; use send type 5"
            )

        available = self.get_balance(rtx.sender, msg.property_id)["balance"]
        if available < msg.amount:
            raise InvalidTransaction(
                f"insufficient balance: {rtx.sender} holds {available} of property "
                f"{msg.property_id}, needs {msg.amount}"
            )

        self.debit(rtx.sender, msg.property_id, msg.amount)
        self.credit(rtx.reference, msg.property_id, msg.amount)

    # --- type 4 ---------------------------------------------------------------

    def _send_all(self, rtx: ArcadeTransaction, msg: P.SendAll) -> None:
        """Type 4. Moves every non-zero balance in one ecosystem."""
        if rtx.reference is None:
            raise InvalidTransaction("send all has no reference (recipient) address")
        if msg.ecosystem not in (ECOSYSTEM_MAIN, ECOSYSTEM_TEST):
            raise InvalidTransaction(f"invalid ecosystem {msg.ecosystem}")

        rows = self.state.db.conn.execute(
            "SELECT b.property_id, b.balance FROM balance b "
            "JOIN property p ON p.property_id = b.property_id "
            "WHERE b.address = ? AND p.ecosystem = ? AND b.balance > 0 "
            "ORDER BY b.property_id",
            (rtx.sender, msg.ecosystem),
        ).fetchall()

        if not rows:
            raise InvalidTransaction(
                f"{rtx.sender} holds nothing in ecosystem {msg.ecosystem}"
            )

        for row in rows:
            self.debit(rtx.sender, row["property_id"], row["balance"])
            self.credit(rtx.reference, row["property_id"], row["balance"])

    # --- types 50 and 54 ------------------------------------------------------

    def _validate_issuance(self, msg: Any) -> None:
        if msg.ecosystem not in (ECOSYSTEM_MAIN, ECOSYSTEM_TEST):
            raise InvalidTransaction(f"invalid ecosystem {msg.ecosystem}")
        if msg.property_type not in ALL_PROPERTY_TYPES:
            raise InvalidTransaction(f"invalid property type {msg.property_type}")
        if not msg.name:
            raise InvalidTransaction("property name must not be empty")

    def _create_property(
        self, rtx: ArcadeTransaction, msg: Any, managed: bool, total: int
    ) -> int:
        property_id = self.next_property_id(msg.ecosystem)
        self.state.insert(
            "property",
            {
                "property_id": property_id,
                "ecosystem": msg.ecosystem,
                "property_type": msg.property_type,
                "issuer": rtx.sender,
                "category": msg.category,
                "subcategory": msg.subcategory,
                "name": msg.name,
                "url": msg.url,
                "data": msg.data,
                "managed": 1 if managed else 0,
                "total_tokens": total,
                "creation_txid": rtx.txid,
                "creation_block": rtx.block_height,
            },
        )
        return property_id

    def _issuance_fixed(self, rtx: ArcadeTransaction, msg: P.IssuanceFixed) -> None:
        """Type 50. Entire supply is credited to the issuer at creation."""
        self._validate_issuance(msg)
        if not 0 < msg.amount <= MAX_AMOUNT:
            raise InvalidTransaction(f"amount {msg.amount} out of range")

        property_id = self._create_property(rtx, msg, managed=False, total=msg.amount)
        self.credit(rtx.sender, property_id, msg.amount)

    def _issuance_managed(self, rtx: ArcadeTransaction, msg: P.IssuanceManaged) -> None:
        """Type 54. No supply at creation; tokens arrive via grants (type 55)."""
        self._validate_issuance(msg)
        self._create_property(rtx, msg, managed=True, total=0)

    # --- types 55, 56 and 70: managing a property ----------------------------

    def _managed_property(self, property_id: int) -> Any:
        """The property row, if it exists and is managed. Shared by 55 and 56."""
        prop = self.get_property(property_id)
        if prop is None:
            raise InvalidTransaction(f"property {property_id} does not exist")
        if not prop["managed"]:
            raise InvalidTransaction(
                f"property {property_id} is not managed; its supply was fixed at creation"
            )
        return prop

    def _grant(self, rtx: ArcadeTransaction, msg: P.Grant) -> None:
        """Type 55. tx.cpp:logicMath_GrantTokens (2158-2257).

        Only the issuer may grant -- Omni also allows a delegate, which arrives
        with types 73/74 in M6, so until then the issuer is the only authority.
        The tokens go to the reference address when there is one and otherwise
        to the sender (tx.cpp:672-675, "assume grant to self").
        """
        if not 0 < msg.amount <= MAX_AMOUNT:
            raise InvalidTransaction(f"amount {msg.amount} out of range")
        prop = self._managed_property(msg.property_id)
        if rtx.sender != prop["issuer"]:
            raise InvalidTransaction(
                f"{rtx.sender} is not the issuer of property {msg.property_id} "
                f"(issuer is {prop['issuer']})"
            )
        if msg.amount > MAX_AMOUNT - prop["total_tokens"]:
            raise InvalidTransaction(
                f"granting {msg.amount} would take property {msg.property_id} past "
                f"the {MAX_AMOUNT} tokens that can ever exist"
            )

        receiver = rtx.reference or rtx.sender
        self.credit(receiver, msg.property_id, msg.amount)
        self.state.update(
            "property", {"property_id": msg.property_id},
            {"total_tokens": prop["total_tokens"] + msg.amount},
        )

    def _revoke(self, rtx: ArcadeTransaction, msg: P.Revoke) -> None:
        """Type 56. tx.cpp:logicMath_RevokeTokens (2260-2326).

        Anyone holding a managed property may destroy their own tokens; there is
        no issuer check, exactly as in Omni. Non-fungible properties are refused.
        """
        if not 0 < msg.amount <= MAX_AMOUNT:
            raise InvalidTransaction(f"amount {msg.amount} out of range")
        prop = self._managed_property(msg.property_id)
        if prop["property_type"] == PROPERTY_NONFUNGIBLE:
            raise InvalidTransaction(f"property {msg.property_id} is non-fungible")

        held = self.get_balance(rtx.sender, msg.property_id)["balance"]
        if held < msg.amount:
            raise InvalidTransaction(
                f"insufficient balance: {rtx.sender} holds {held} of property "
                f"{msg.property_id}, cannot revoke {msg.amount}"
            )
        self.debit(rtx.sender, msg.property_id, msg.amount)
        self.state.update(
            "property", {"property_id": msg.property_id},
            {"total_tokens": prop["total_tokens"] - msg.amount},
        )

    def _change_issuer(self, rtx: ArcadeTransaction, msg: P.ChangeIssuer) -> None:
        """Type 70. tx.cpp:logicMath_ChangeIssuer (2329-2390).

        The reference address becomes the issuer. Works for fixed and managed
        properties alike; Omni's crowdsale checks do not apply (D-008).
        """
        prop = self.get_property(msg.property_id)
        if prop is None:
            raise InvalidTransaction(f"property {msg.property_id} does not exist")
        if prop["property_type"] == PROPERTY_NONFUNGIBLE:
            raise InvalidTransaction(f"property {msg.property_id} is non-fungible")
        if rtx.sender != prop["issuer"]:
            raise InvalidTransaction(
                f"{rtx.sender} is not the issuer of property {msg.property_id} "
                f"(issuer is {prop['issuer']})"
            )
        if rtx.reference is None:
            raise InvalidTransaction("change issuer has no reference (new issuer) address")
        self.state.update(
            "property", {"property_id": msg.property_id}, {"issuer": rtx.reference}
        )

    # --- type 200: any data ---------------------------------------------------

    def _any_data(self, rtx: ArcadeTransaction, msg: P.AnyData) -> None:
        """Type 200. tx.cpp:logicMath_AnyData (2735-2747): valid, changes nothing.

        Every Messenger transaction is one of these, so when testnet stands in
        for the ledger (D-016) the indexer walks through thousands of them. They
        are recorded like any other transaction and touch no balance.

        Inscriptions ride in here too, behind their own magic. That is the whole
        reason they do: a client that predates them reads this method, finds
        nothing it knows, and carries on -- where an unrecognised payload TYPE
        would have stopped it dead.
        """
        if not I.is_inscription(msg.data):
            return None
        if len(msg.data) > 5 and msg.data[5] in (T.KIND_CLAIM, T.KIND_TRANSFER):
            try:
                kind, tag = T.parse(msg.data)
            except I.InscriptionError as exc:
                raise InvalidTransaction(f"malformed tag: {exc}") from None
            try:
                T.validate(tag)
            except T.TagError as exc:
                raise InvalidTransaction(f"that tag cannot be claimed: {exc}") from None
            return self._tag(rtx, kind, tag)

        try:
            parsed = I.parse(msg.data)
        except I.InscriptionError as exc:
            # Malformed, and that is all it is: the transaction is recorded as
            # invalid and the index carries on. A stranger's broken payload must
            # never be able to halt anybody's node.
            raise InvalidTransaction(f"malformed inscription: {exc}") from None

        if isinstance(parsed, I.Transfer):
            self._inscription_transfer(rtx, parsed)
        elif isinstance(parsed, I.Swap):
            self._swap(rtx, parsed)
        elif isinstance(parsed, I.Offer):
            self._offer(rtx, parsed)
        elif isinstance(parsed, I.Ask):
            self._ask(rtx, parsed)
        else:
            self._inscription_chunk(rtx, parsed)

    # --- types 25-27: the order book ------------------------------------------

    #: Property 0 is the chain's own coin, which has no property row and no
    #: balance to reserve. Omni has no such id; here the coin is one side of
    #: every pair, so it needs a name (D-048).
    COIN_PROPERTY = 0

    def _book_order(self, rtx: ArcadeTransaction, msg: P.MetaDExTrade) -> None:
        """Type 25. A standing order: this much, at this price, until cancelled.

        What is sold is reserved when it is a token, so the book never shows
        what the seller has since spent. Coins cannot be reserved -- there is
        no covenant that would hold them and still let the wallet live -- so
        an order to BUY is an intent, and whether the coins are there is
        settled when somebody fills it and the transaction either stands up
        or does not.

        Nothing is matched here. A fill is a swap: one transaction both sides
        sign, which is the only way a coin leg and a token leg move together
        (D-048).
        """
        sale, want = msg.property_id_for_sale, msg.property_id_desired
        if sale == want:
            raise InvalidTransaction("an order has two different sides")
        if self.COIN_PROPERTY not in (sale, want):
            raise InvalidTransaction(
                "one side of an order is this chain's coin (property 0)")
        if not 0 < msg.amount_for_sale <= MAX_AMOUNT:
            raise InvalidTransaction(f"amount {msg.amount_for_sale} out of range")
        if not 0 < msg.amount_desired <= MAX_AMOUNT:
            raise InvalidTransaction(f"amount {msg.amount_desired} out of range")

        token = want if sale == self.COIN_PROPERTY else sale
        if self.get_property(token) is None:
            raise InvalidTransaction(f"property {token} does not exist")

        reserved = 0
        if sale != self.COIN_PROPERTY:
            available = self.get_balance(rtx.sender, sale)["balance"]
            if available < msg.amount_for_sale:
                raise InvalidTransaction(
                    f"{rtx.sender} holds {available} of property {sale}, "
                    f"not {msg.amount_for_sale}")
            self._move_to_reserve(rtx.sender, sale, msg.amount_for_sale,
                                  "metadex_reserve")
            reserved = msg.amount_for_sale

        self.state.insert("book_order", {
            "txid": rtx.txid, "block_height": rtx.block_height,
            "position": rtx.position, "address": rtx.sender,
            "sale_property": sale, "sale_amount": msg.amount_for_sale,
            "want_property": want, "want_amount": msg.amount_desired,
            "reserved": reserved,
        })

    def _book_cancel_price(self, rtx: ArcadeTransaction,
                           msg: P.MetaDExCancelPrice) -> None:
        """Type 26. Cancel this sender's orders on one pair at one exact price.

        Exact, as a pair of integers: 3 for 2 and 6 for 4 are the same price
        and both are cancelled; 3 for 2 and 3 for 2.000001 are not.
        """
        self._cancel_orders(
            rtx, msg.property_id_for_sale, msg.property_id_desired,
            price=(msg.amount_desired, msg.amount_for_sale))

    def _book_cancel_pair(self, rtx: ArcadeTransaction,
                          msg: P.MetaDExCancelPair) -> None:
        """Type 27. Cancel every order this sender has on one pair."""
        self._cancel_orders(rtx, msg.property_id_for_sale,
                            msg.property_id_desired, price=None)

    def _cancel_orders(self, rtx: ArcadeTransaction, sale: int, want: int,
                       price: tuple[int, int] | None) -> None:
        rows = self.state.db.conn.execute(
            "SELECT * FROM book_order WHERE address=? AND sale_property=? "
            "AND want_property=?", (rtx.sender, sale, want)).fetchall()
        hit = []
        for row in rows:
            if price is not None:
                # a/b == c/d without dividing anything
                if price[0] * row["sale_amount"] != price[1] * row["want_amount"]:
                    continue
            hit.append(row)
        if not hit:
            raise InvalidTransaction("no order of yours matches that")
        for row in hit:
            self._close_order(row)

    def _close_order(self, row: Any) -> None:
        """Take an order off the book and give back whatever it was holding."""
        if row["reserved"]:
            self._move_from_reserve(row["address"], row["sale_property"],
                                    row["reserved"], "metadex_reserve")
        self.state.delete("book_order", {"txid": row["txid"]})

    def _move_to_reserve(self, address: str, property_id: int, amount: int,
                         column: str) -> None:
        balance = self.get_balance(address, property_id)
        self.state.update("balance", {"address": address, "property_id": property_id},
                          {"balance": balance["balance"] - amount,
                           column: balance[column] + amount})

    def _move_from_reserve(self, address: str, property_id: int, amount: int,
                           column: str) -> None:
        balance = self.get_balance(address, property_id)
        self.state.update("balance", {"address": address, "property_id": property_id},
                          {"balance": balance["balance"] + amount,
                           column: max(0, balance[column] - amount)})

    def _offer(self, rtx: ArcadeTransaction, offer: I.Offer) -> None:
        """Write down an offer for an inscription. Nothing moves.

        Refused when the inscription is not one this chain has, when the
        offer is for nothing, and when it is the holder offering for their
        own thing -- none of which anybody could act on. Everything else is
        recorded: whether the buyer can pay is answered when the holder
        accepts and the swap is built, not here, because a balance at this
        block says nothing about a balance three blocks later.
        """
        found = self.state.db.conn.execute(
            "SELECT owner FROM inscription WHERE txid=?",
            (offer.txid.hex(),)).fetchone()
        if found is None:
            raise InvalidTransaction("there is no such inscription on this chain")
        if found["owner"] == rtx.sender:
            raise InvalidTransaction("that one is already yours")
        if not offer.take.amount and offer.take.kind != I.LEG_INSCRIPTION:
            raise InvalidTransaction("an offer of nothing is not an offer")
        self.state.insert("nft_offer", {
            "txid": rtx.txid,
            "block_height": rtx.block_height,
            "position": rtx.position,
            "inscription": offer.txid.hex(),
            "buyer": rtx.sender,
            "take_kind": offer.take.kind,
            "take_property": offer.take.property_id or None,
            "take_amount": int(offer.take.amount or 0),
        })

    def _ask(self, rtx: ArcadeTransaction, ask: I.Ask) -> None:
        """Write down what the holder wants for a piece. Nothing moves.

        Only the current owner may price a piece, and the price is in coins
        or in a token -- an ask that wanted another inscription would be a
        barter nobody could match against a number, and the marketplace is
        built on numbers.

        Nothing is reserved. There is no way on this chain to hold an
        inscription back from its owner, and pretending otherwise would be a
        promise the ledger cannot keep -- the same reason a bid holds no
        coins (D-048). What makes an ask honest instead is that it is checked
        against the chain when it is READ: `ledger.asks` drops any whose
        seller no longer holds the piece, so a sold or sent-away piece
        withdraws its own price.
        """
        since = self.params.asks_from
        if since is None:
            raise InvalidTransaction("asks are not read on this chain")
        if rtx.block_height < since:
            raise InvalidTransaction(f"asks are read from block {since}")
        found = self.state.db.conn.execute(
            "SELECT owner FROM inscription WHERE txid=?",
            (ask.txid.hex(),)).fetchone()
        if found is None:
            raise InvalidTransaction("there is no such inscription on this chain")
        if found["owner"] != rtx.sender:
            raise InvalidTransaction("only the holder of an inscription can price it")
        if ask.take.kind == I.LEG_INSCRIPTION:
            raise InvalidTransaction("an ask is priced in coins or in a token")
        if not ask.cancelled and not ask.take.amount:
            raise InvalidTransaction("an ask for nothing is not an ask")
        self.state.insert("nft_ask", {
            "txid": rtx.txid,
            "block_height": rtx.block_height,
            "position": rtx.position,
            "inscription": ask.txid.hex(),
            "seller": rtx.sender,
            "take_kind": ask.take.kind,
            "take_property": ask.take.property_id or None,
            "take_amount": int(ask.take.amount or 0),
        })

    def _tag(self, rtx: ArcadeTransaction, kind: int, tag: str) -> None:
        """Claim a tag, or hand one to the reference address."""
        held = self.state.db.conn.execute(
            "SELECT tag, address FROM tag WHERE tag=?", (tag,)).fetchone()
        mine = self.state.db.conn.execute(
            "SELECT tag FROM tag WHERE address=?", (rtx.sender,)).fetchone()

        if kind == T.KIND_TRANSFER:
            if held is None or held["address"] != rtx.sender:
                raise InvalidTransaction("that tag is not yours to send")
            if not rtx.reference:
                raise InvalidTransaction("a transfer needs a reference address")
            if rtx.reference == rtx.sender:
                raise InvalidTransaction("that tag is already there")
            taken = self.state.db.conn.execute(
                "SELECT tag FROM tag WHERE address=?", (rtx.reference,)).fetchone()
            if taken is not None:
                # Refused rather than replaced: quietly dropping somebody's
                # existing name because a stranger sent them another is a way to
                # lose a name without being asked.
                raise InvalidTransaction(
                    f"that address already holds @{taken['tag']}")
            self.state.update("tag", {"tag": tag}, {"address": rtx.reference,
                                                    "claimed_txid": rtx.txid,
                                                    "block_height": rtx.block_height,
                                                    "position": rtx.position})
            return

        if held is not None:
            raise InvalidTransaction(
                "that tag is already yours" if held["address"] == rtx.sender
                else "that tag is taken")
        if mine is not None:
            # Changing your tag frees the old one: holding names you no longer
            # use is how a namespace fills up with nothing.
            self.state.delete("tag", {"tag": mine["tag"]})
        self.state.insert("tag", {
            "tag": tag, "address": rtx.sender, "claimed_txid": rtx.txid,
            "block_height": rtx.block_height, "position": rtx.position})

    # --- inscriptions ---------------------------------------------------------

    def _inscription_chunk(self, rtx: ArcadeTransaction, chunk: I.Chunk) -> None:
        """One piece of an inscription. The last one to arrive completes it."""
        key = {"sender": rtx.sender,
               "inscription_id": chunk.inscription_id.hex(),
               "countdown": chunk.countdown}
        seen = self.state.db.conn.execute(
            "SELECT 1 FROM inscription_chunk WHERE sender=? AND inscription_id=? "
            "AND countdown=?", (key["sender"], key["inscription_id"],
                                key["countdown"])).fetchone()
        if seen is not None:
            raise InvalidTransaction("that piece of the inscription is already on chain")

        self.state.insert("inscription_chunk", {
            **key, "txid": rtx.txid, "block_height": rtx.block_height,
            "position": rtx.position, "body": chunk.body})

        rows = self.state.db.conn.execute(
            "SELECT * FROM inscription_chunk WHERE sender=? AND inscription_id=?",
            (rtx.sender, chunk.inscription_id.hex())).fetchall()
        assembly = I.Assembly(inscription_id=chunk.inscription_id)
        first = None
        for row in rows:
            assembly.pieces[row["countdown"]] = bytes(row["body"])
            if first is None or row["countdown"] > first["countdown"]:
                first = row
        if not assembly.complete():
            return None

        try:
            manifest, content = assembly.join()
        except I.InscriptionError as exc:
            # Every piece is here and they do not make the file the manifest
            # describes. Leave the pieces: a later, correct set from the same
            # sender under a different id still works, and throwing away chain
            # data on a hunch is worse than keeping it.
            raise InvalidTransaction(f"inscription does not assemble: {exc}") from None

        self.state.insert("inscription", {
            # Named by the piece that carried the manifest, which is fixed the
            # moment it is broadcast and does not depend on what order the rest
            # confirmed in.
            "txid": first["txid"],
            "number": self._next_inscription_number(),
            "creator": rtx.sender,
            "owner": rtx.sender,
            # Where it BECAME an inscription, which is what the numbering and
            # every replay agree on.
            "block_height": rtx.block_height,
            "position": rtx.position,
            "content_type": manifest.content_type,
            "content_len": manifest.total,
            "sha256": manifest.sha256.hex(),
            "json": manifest.json,
            "chunks": len(assembly.pieces),
            # Not kept. The file is on the chain in full and this index says
            # which transactions hold it, so keeping a second copy here was
            # paying twice to store what cannot be lost (D-113). A node that
            # wants the bytes to hand -- a gallery serving strangers -- says
            # so with `keep_content`.
            "content": content if self.keep_content(rtx.sender) else None,
        })
        for row in rows:
            self.state.insert("inscription_piece", {
                "inscription": first["txid"],
                "countdown": row["countdown"],
                "txid": row["txid"],
            })
        member = I.collection_of(manifest.json)
        if member is not None:
            collection, edition, name = member
            self.state.insert("collection_item", {
                "txid": first["txid"], "creator": rtx.sender,
                "collection": collection, "edition": edition, "name": name})
        for row in rows:
            self.state.delete("inscription_chunk", {
                "sender": row["sender"], "inscription_id": row["inscription_id"],
                "countdown": row["countdown"]})

    def _next_inscription_number(self) -> int:
        row = self.state.db.conn.execute(
            "SELECT MAX(number) AS n FROM inscription").fetchone()
        highest = row["n"] if row and row["n"] is not None else -1
        return highest + 1

    def _inscription_transfer(self, rtx: ArcadeTransaction, move: I.Transfer) -> None:
        """Hand an inscription to the reference address. Only the owner may."""
        txid = move.txid.hex()
        existing = self.state.db.conn.execute(
            "SELECT owner FROM inscription WHERE txid=?", (txid,)).fetchone()
        if existing is None:
            raise InvalidTransaction("no such inscription")
        if existing["owner"] != rtx.sender:
            raise InvalidTransaction(
                "only the owner of an inscription can send it")
        if not rtx.reference:
            raise InvalidTransaction("a transfer needs a reference address")
        if rtx.reference == existing["owner"]:
            raise InvalidTransaction("that inscription is already there")
        self.state.update("inscription", {"txid": txid}, {"owner": rtx.reference})
        self._note_move(rtx, txid, existing["owner"], rtx.reference, "transfer")

    def _note_move(self, rtx: ArcadeTransaction, inscription: str, giver: str,
                   taker: str, how: str) -> None:
        """Write down that a piece changed hands. Keyed by the transaction, so
        replaying a block cannot double it, and journalled so a reorg unwinds
        it with everything else."""
        self.state.insert("inscription_move", {
            "txid": rtx.txid, "inscription": inscription,
            "from_address": giver, "to_address": taker,
            "block_height": rtx.block_height, "position": rtx.position,
            "how": how,
        })

    # --- swaps ----------------------------------------------------------------

    def _swap(self, rtx: ArcadeTransaction, swap: I.Swap) -> None:
        """Two parties trade in one transaction, or nothing moves.

        The seller is the sender -- Class C, so the first input, which a
        buyer building the transaction cannot shift by adding bigger inputs
        of their own. The buyer is the first input that is not the seller's:
        an input is a signature, since nobody else can spend it, and the
        payload has no room to name the buyer (inscriptions.Swap). Both legs
        are checked before either moves, so a swap is never half done. Coins
        are counted on the transaction itself: what the receiving side ends
        up with, net of what it put in, must cover the leg.
        """
        since = self.params.swaps_from
        if since is None:
            raise InvalidTransaction("swaps are not read on this chain")
        if rtx.block_height < since:
            raise InvalidTransaction(f"swaps are read from block {since}")
        if rtx.encoding_class is not EncodingClass.C:
            raise InvalidTransaction("a swap must be Class C so its seller is the first input")
        seller = rtx.sender
        buyer = next((where for where, _ in rtx.inputs
                      if where is not None and where != seller), None)
        if buyer is None:
            raise InvalidTransaction("a swap needs two parties: every input is the seller's")
        if swap.give.kind == I.LEG_NONE or swap.take.kind == I.LEG_NONE:
            raise InvalidTransaction("a swap has two sides")

        # What the seller has on the book, this swap may take out of. Worked
        # out before anything is checked and applied after everything is, so a
        # swap is still never half done.
        fills = self._fills_for(rtx, seller, swap.give, swap.take,
                                named=getattr(swap, "order", b""))
        self._check_leg(rtx, swap.give, seller, buyer,
                        extra=sum(taken for _, taken, _ in fills))
        self._check_leg(rtx, swap.take, buyer, seller)
        for row, taken, want in fills:
            self._fill_order(row, taken, want)
        self._move_leg(swap.give, seller, buyer, rtx)
        self._move_leg(swap.take, buyer, seller, rtx)

    def _fills_for(self, rtx: ArcadeTransaction, seller: str, give: I.Leg,
                   take: I.Leg, named: bytes = b"") -> list[tuple[dict, int, int]]:
        """The seller's orders this swap fills: (order, tokens, coins) each.

        A resting ask holds its tokens in `metadex_reserve`, where a swap
        cannot reach them, so before this the only way to fill one was to
        cancel it first -- which takes a block, and tells everybody the price
        is gone before the trade that was meant to take it.

        Which orders are consumed is a pure function of indexed state, so
        every node picks the same ones in the same order: cheapest first,
        then oldest, then by txid. The swap does not name them and does not
        need to -- what it names is what moves, and the price guard below is
        what protects the maker.

        The guard: this swap must pay at least the order's own price for what
        it takes. An order is a public promise to sell at a price, and the
        reserve behind it may be spent at that price or better, never worse.
        The maker signs the transaction as well -- an input is a signature --
        so consent is explicit; this is the rule that holds when a wallet
        signs something it did not read carefully (D-062).
        """
        from fractions import Fraction

        since = self.params.fills_from
        if since is None or rtx.block_height < since:
            return []
        if give.kind != I.LEG_TOKEN or take.kind != I.LEG_COINS:
            return []                     # only a token sold for coins is a fill
        rows = [dict(r) for r in self.state.db.conn.execute(
            "SELECT * FROM book_order WHERE address=? AND sale_property=? "
            "AND want_property=? AND reserved>0",
            (seller, give.property_id, self.COIN_PROPERTY))]
        if not rows:
            return []
        rows.sort(key=lambda r: (Fraction(r["want_amount"], r["sale_amount"]),
                                 r["block_height"], r["position"], r["txid"]))

        # A swap that NAMES an order fills that order, whatever the seller has
        # loose. Deriving it was the mistake: "sold 333 at 0.012 while
        # advertising 500 at 0.012" is produced byte-identically by a fill of
        # the advertisement and by a private sale beside it, and which one it
        # was is a fact about what the taker asked for, not about state. The
        # first live fill proved it -- the trade went through, the tokens came
        # out of a large free balance, and the book went on advertising what
        # had just been sold (D-082).
        if named and self.params.named_fills_from is not None \
                and rtx.block_height >= self.params.named_fills_from:
            want = named.hex()
            for row in rows:
                if row["txid"] != want:
                    continue
                # The same price guard. A named order is a preference, so a
                # price this swap does not meet means the order is left alone,
                # never that the swap is refused.
                if take.amount * row["sale_amount"] < row["want_amount"] * give.amount:
                    break
                taken = min(give.amount, row["reserved"], row["sale_amount"])
                if taken <= 0:
                    break
                return [(row, taken, row["want_amount"] * taken // row["sale_amount"])]
            # Named and unusable -- gone, re-priced, or emptied since the
            # taker asked. The swap still stands: its coin leg settles on the
            # chain below whether this layer likes it or not, and refusing it
            # here would mean somebody paid and received nothing.
            return []

        held = self.get_balance(seller, give.property_id)["balance"]
        need = give.amount - held         # what the free balance cannot cover
        out: list[tuple[dict, int, int]] = []
        for row in rows:
            if need <= 0:
                break
            # take.amount/give.amount >= want/sale, without dividing anything
            if take.amount * row["sale_amount"] < row["want_amount"] * give.amount:
                continue
            taken = min(need, row["reserved"], row["sale_amount"])
            if taken <= 0:
                continue
            # Rounded down, so what is left of the order keeps at least the
            # price it had. Rounding the other way would let a fill leave a
            # remainder cheaper than the maker ever offered.
            want = row["want_amount"] * taken // row["sale_amount"]
            out.append((row, taken, want))
            need -= taken
        return out

    def _fill_order(self, row: dict, taken: int, want: int) -> None:
        """Take `taken` out of an order's reserve and reduce the order by it."""
        self._move_from_reserve(row["address"], row["sale_property"], taken,
                                "metadex_reserve")
        left = row["sale_amount"] - taken
        if left <= 0:
            self.state.delete("book_order", {"txid": row["txid"]})
            return
        self.state.update("book_order", {"txid": row["txid"]}, {
            "sale_amount": left,
            # max(1): the arithmetic cannot reach zero while `left` is
            # positive, and an order wanting nothing would be a price of zero.
            "want_amount": max(1, row["want_amount"] - want),
            "reserved": row["reserved"] - taken,
        })

    def _check_leg(self, rtx: ArcadeTransaction, leg: I.Leg, giver: str,
                   taker: str, extra: int = 0) -> None:
        """`extra` is what a standing order of the giver's would release."""
        if leg.kind == I.LEG_INSCRIPTION:
            row = self.state.db.conn.execute(
                "SELECT owner FROM inscription WHERE txid=?", (leg.txid.hex(),)).fetchone()
            if row is None:
                raise InvalidTransaction("no such inscription")
            if row["owner"] != giver:
                raise InvalidTransaction(f"inscription {leg.txid.hex()[:12]} is not {giver}'s to give")
            return
        if leg.kind == I.LEG_TOKEN:
            if not 0 < leg.amount <= MAX_AMOUNT:
                raise InvalidTransaction(f"amount {leg.amount} out of range")
            prop = self.get_property(leg.property_id)
            if prop is None:
                raise InvalidTransaction(f"property {leg.property_id} does not exist")
            if prop["property_type"] == PROPERTY_NONFUNGIBLE:
                raise InvalidTransaction(f"property {leg.property_id} is non-fungible")
            held = self.get_balance(giver, leg.property_id)["balance"] + extra
            if held < leg.amount:
                raise InvalidTransaction(
                    f"insufficient balance: {giver} holds {held} of property "
                    f"{leg.property_id}, needs {leg.amount}")
            return
        if leg.kind == I.LEG_COINS:
            if leg.amount <= 0:
                raise InvalidTransaction("a coin leg needs an amount")
            paid = rtx.paid_to(taker)
            if paid < leg.amount:
                raise InvalidTransaction(
                    f"{taker} is paid {paid} satoshis by this transaction, "
                    f"the swap says {leg.amount}")
            return
        raise InvalidTransaction(f"unknown swap leg {leg.kind}")

    def _move_leg(self, leg: I.Leg, giver: str, taker: str,
                  rtx: ArcadeTransaction | None = None) -> None:
        if leg.kind == I.LEG_INSCRIPTION:
            self.state.update("inscription", {"txid": leg.txid.hex()}, {"owner": taker})
            if rtx is not None:
                # A swap is a hand it changed in as much as a transfer is.
                # Provenance that counted only transfers would miss every
                # piece that was ever bought.
                self._note_move(rtx, leg.txid.hex(), giver, taker, "swap")
        elif leg.kind == I.LEG_TOKEN:
            self.debit(giver, leg.property_id, leg.amount)
            self.credit(taker, leg.property_id, leg.amount)
        # Coins moved when the transaction did; the ledger only checked.

    # --- system messages ------------------------------------------------------

    def _activate_feature(self, rtx: ArcadeTransaction, msg: P.ActivateFeature) -> None:
        """Type 65534.

        Arcade accepts activations only from a configured authority address. Omni
        does the same (rules.cpp checks against a hard-coded list); allowing any
        address to activate features would let anyone change consensus.
        """
        authority = getattr(self.params, "activation_authority", None)
        if authority is not None and rtx.sender != authority:
            raise InvalidTransaction(
                f"{rtx.sender} is not the activation authority"
            )
        if msg.activation_block < rtx.block_height:
            raise InvalidTransaction(
                f"activation block {msg.activation_block} is in the past "
                f"(current height {rtx.block_height})"
            )

        existing = self.state.db.conn.execute(
            "SELECT 1 FROM activation WHERE feature_id = ?", (msg.feature_id,)
        ).fetchone()
        row = {
            "feature_id": msg.feature_id,
            "activation_block": msg.activation_block,
            "min_client_version": msg.min_client_version,
            "active": 1,
        }
        if existing:
            self.state.update("activation", {"feature_id": msg.feature_id}, row)
        else:
            self.state.insert("activation", row)

    def _deactivate_feature(self, rtx: ArcadeTransaction, msg: P.DeactivateFeature) -> None:
        """Type 65533."""
        authority = getattr(self.params, "activation_authority", None)
        if authority is not None and rtx.sender != authority:
            raise InvalidTransaction(f"{rtx.sender} is not the activation authority")

        existing = self.state.db.conn.execute(
            "SELECT 1 FROM activation WHERE feature_id = ?", (msg.feature_id,)
        ).fetchone()
        if not existing:
            raise InvalidTransaction(f"feature {msg.feature_id} was never activated")
        self.state.update("activation", {"feature_id": msg.feature_id}, {"active": 0})

    def _alert(self, rtx: ArcadeTransaction, msg: P.Alert) -> None:
        """Type 65535. Alerts carry no state; they are recorded in arcade_tx only."""
        return None
