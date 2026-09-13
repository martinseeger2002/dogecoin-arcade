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
from typing import Any

from . import payload as P
from .config import (
    FIRST_PROPERTY_ID_MAIN,
    FIRST_PROPERTY_ID_TEST,
    RESERVED_PROPERTY_IDS,
    Params,
)
from .db import Database, StateDB, register_journalled_table
from .tx import ArcadeTransaction

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

CREATE INDEX IF NOT EXISTS ribbit_tx_block_idx ON arcade_tx(block_height, position);
CREATE INDEX IF NOT EXISTS balance_property_idx ON balance(property_id);
"""


def install_schema(db: Database) -> None:
    """Create the protocol tables and register them for journalling."""
    db.conn.executescript(SCHEMA)
    register_journalled_table("property", ("property_id",))
    register_journalled_table("balance", ("address", "property_id"))
    register_journalled_table("activation", ("feature_id",))
    register_journalled_table("arcade_tx", ("txid",))


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

    def __init__(self, state: StateDB, params: Params):
        self.state = state
        self.params = params

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
        """
        return None

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
