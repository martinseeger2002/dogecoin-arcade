"""Sends that somebody other than the user asked for, and the user's say-so.

Why this exists
---------------
Two kinds of caller can want this wallet to spend without being the person at
the keyboard: an inscribed page running in its sandbox -- a marketplace, a
game, a mint button -- and a program on the bot RPC. Neither is allowed to.
Giving either the power to broadcast would make every inscription anyone ever
looks at, and every script with the cookie, a hand in the wallet.

So a caller does not send. It files a **request**: what kind of thing, to
whom, how much, and a note saying why. The request sits in a queue on disk
until the person who owns the wallet looks at it, sees the transaction that
would go out -- built, signed, decoded, fee and every output -- and says yes
or no. Nothing is spent on the caller's word. Nothing is spent on the
caller's silence either: a request nobody answers expires.

What can be asked for
---------------------
Every kind of value this wallet holds: base coins, tokens, and inscriptions.
One shape for all three, because the person approving should see the same
screen whatever is being moved and the caller should learn one API, not
three.

Why a queue on disk, not a prompt
---------------------------------
The person is not necessarily looking. A bot files at three in the morning; a
page files in a tab that has since been closed. A request is a row that
survives the process and is shown wherever the wallet is next opened -- on
this machine or on the phone over the remote tunnel -- and the caller polls
for the answer. The transaction itself is built only when the request is
being looked at, so what is approved is priced against the wallet as it is
then, not as it was when the request was filed.

What a caller cannot do
-----------------------
Choose the coins, see the wallet's private state, or make the request look
like it came from the user. The caller's own description of itself is shown
in quotes as exactly that: what it says about itself.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import fees
from . import inscriptions as inscriptionlib
from . import payload as P
from . import tokens as tokenlib
from . import wallet as walletlib
from .ledger import COIN, parse_amount as parse_token_amount
from .txbuild import build_raw_tx, p2pkh_script

#: What a request may be for. A swap is the buyer's half of an exchange with
#: a shop (arcade/swap.py): approving signs it and hands it to the shop's
#: node, which signs the other half and broadcasts.
KINDS = ("coins", "token", "inscription", "swap")

#: How long a request waits for an answer. An hour: long enough to notice on
#: a phone, short enough that a request filed by a page somebody looked at
#: last week is not still sitting there asking.
TTL = 3600

#: How many may wait at once. A page or a bot that files a hundred requests
#: is not asking, it is nagging, and the refusal is the answer it gets.
MAX_PENDING = 20

#: What the caller may say about itself and why, in characters. Shown to the
#: person deciding, so long enough to explain and short enough to read.
MAX_TEXT = 200

STATUSES = ("pending", "sent", "denied", "failed", "expired")

#: Who asked. "page" is an inscribed page, "rpc" a program on the bot RPC,
#: "own page" a page this wallet made and holds, which may send without being
#: asked -- and "shop" is a shop of your own selling what its inscription
#: lists, which the shopkeeper signs without asking either (D-024). The last
#: two are never pending: they are written down after the fact, so that one
#: page shows everything this wallet signed for something other than a
#: person at the keyboard.
ORIGINS = ("page", "rpc", "own page", "shop")


class RequestError(ValueError):
    """The request cannot be filed as asked -- a bad address, no such token."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS request (
    id           TEXT PRIMARY KEY,
    network      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    origin       TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    note         TEXT NOT NULL DEFAULT '',
    fromaddress  TEXT NOT NULL DEFAULT '',
    toaddress    TEXT NOT NULL,
    totag        TEXT NOT NULL DEFAULT '',
    units        INTEGER NOT NULL DEFAULT 0,
    amount       TEXT NOT NULL DEFAULT '',
    propertyid   INTEGER,
    propertyname TEXT NOT NULL DEFAULT '',
    inscription  TEXT NOT NULL DEFAULT '',
    number       INTEGER,
    created      REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    decided      REAL,
    txid         TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS request_status ON request(status, created);
"""

#: Columns added after the first release; a queue made before them gets them
#: on open. Each is what a swap carries: the offer the shop made (JSON), the
#: inscribed page that is buying, and the shop node's key to answer to.
LATER_COLUMNS = (("offer", "TEXT NOT NULL DEFAULT ''"),
                 ("page", "TEXT NOT NULL DEFAULT ''"),
                 ("peer", "TEXT NOT NULL DEFAULT ''"))


class Requests:
    """The queue, on disk."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._open() as conn:
            conn.executescript(SCHEMA)
            have = {r["name"] for r in conn.execute("PRAGMA table_info(request)")}
            for name, spec in LATER_COLUMNS:
                if name not in have:
                    conn.execute(f"ALTER TABLE request ADD COLUMN {name} {spec}")

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def file(self, network: str, kind: str, origin: str, toaddress: str, *,
             fromaddress: str = "", totag: str = "", units: int = 0, amount: str = "",
             propertyid: int | None = None, propertyname: str = "",
             inscription: str = "", number: int | None = None,
             label: str = "", note: str = "", offer: dict | None = None,
             page: str = "", peer: str = "", status: str = "pending",
             txid: str = "", error: str = "") -> str:
        """Put a request in the queue; returns its id.

        The caller's text is cut to length rather than refused: a note is for
        the person deciding, and a note that is too long still says something.

        A `status` other than pending files something already done (see
        `record`): it is not waiting for anybody, so the cap on how many may
        wait does not apply to it and it is decided the moment it is written.
        """
        if kind not in KINDS:
            raise RequestError(f"kind must be one of {', '.join(KINDS)}")
        if status not in STATUSES:
            raise RequestError(f"status must be one of {', '.join(STATUSES)}")
        waits = status == "pending"
        request_id = secrets.token_hex(8)
        now = time.time()
        with self._open() as conn:
            self._expire(conn)
            if waits:
                waiting = conn.execute(
                    "SELECT COUNT(*) FROM request WHERE status = 'pending'").fetchone()[0]
                if waiting >= MAX_PENDING:
                    raise RequestError(
                        f"{waiting} requests are already waiting for an answer; "
                        "try again once the wallet's owner has looked at them")
            conn.execute(
                "INSERT INTO request(id, network, kind, origin, label, note, fromaddress, "
                "toaddress, totag, units, amount, propertyid, propertyname, inscription, "
                "number, created, offer, page, peer, status, decided, txid, error) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, network, kind, origin, _text(label), _text(note),
                 fromaddress, toaddress, totag, int(units), amount, propertyid,
                 propertyname, inscription, number, now,
                 json.dumps(offer) if offer else "", page, peer,
                 status, None if waits else now, txid, _text(error, 500)))
        return request_id

    def record(self, network: str, kind: str, origin: str, toaddress: str, *,
               status: str = "sent", txid: str = "", error: str = "",
               **fields: Any) -> str:
        """Write down a send this wallet made without being asked.

        A shop sells what its inscription lists and the shopkeeper signs it;
        nobody is asked, and nothing here can be refused, because it has
        already happened. It is written to the same queue so that Approvals
        is one place to see everything a page or a shop moved, and it skips
        the MAX_PENDING check for the same reason -- it is not waiting.
        """
        if status == "pending":
            raise RequestError("a record is of something already done")
        return self.file(network, kind, origin, toaddress, status=status,
                         txid=txid, error=error, **fields)

    def get(self, request_id: str) -> dict | None:
        with self._open() as conn:
            self._expire(conn)
            row = conn.execute("SELECT * FROM request WHERE id = ?",
                               (str(request_id),)).fetchone()
            return dict(row) if row else None

    def pending(self, network: str | None = None) -> list[dict]:
        """What is waiting, oldest first: the order they should be looked at."""
        with self._open() as conn:
            self._expire(conn)
            sql, args = "SELECT * FROM request WHERE status = 'pending'", []
            if network:
                sql += " AND network = ?"
                args.append(network)
            return [dict(r) for r in conn.execute(sql + " ORDER BY created", args)]

    def recent(self, limit: int = 30, network: str | None = None) -> list[dict]:
        """What has been decided, newest first."""
        with self._open() as conn:
            self._expire(conn)
            sql, args = "SELECT * FROM request WHERE status != 'pending'", []
            if network:
                sql += " AND network = ?"
                args.append(network)
            sql += " ORDER BY decided DESC LIMIT ?"
            return [dict(r) for r in conn.execute(sql, args + [int(limit)])]

    def waiting(self) -> int:
        with self._open() as conn:
            self._expire(conn)
            return conn.execute(
                "SELECT COUNT(*) FROM request WHERE status = 'pending'").fetchone()[0]

    def decide(self, request_id: str, status: str, txid: str = "", error: str = "") -> bool:
        """Close a request. False if it was not pending -- decided twice, or
        expired while the page was open."""
        if status not in STATUSES or status == "pending":
            raise ValueError(f"not a decision: {status!r}")
        with self._open() as conn:
            done = conn.execute(
                "UPDATE request SET status = ?, decided = ?, txid = ?, error = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, time.time(), txid, _text(error, 500), str(request_id)))
            return done.rowcount == 1

    @staticmethod
    def _expire(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE request SET status = 'expired', decided = ? "
            "WHERE status = 'pending' AND created < ?",
            (time.time(), time.time() - TTL))


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    text = "" if value is None else str(value)
    text = "".join(ch for ch in text if ch.isprintable() or ch == " ").strip()
    return text[:limit]


def describe(row: dict) -> dict:
    """A request as a caller sees it -- and as the person deciding does."""
    return {
        "id": row["id"],
        "network": row["network"],
        "kind": row["kind"],
        "status": row["status"],
        "from": row["fromaddress"] or None,
        "to": row["toaddress"],
        "totag": row["totag"] or None,
        "amount": row["amount"] or None,
        "propertyid": row["propertyid"],
        "propertyname": row["propertyname"] or None,
        "inscription": row["inscription"] or None,
        "number": row["number"],
        "origin": row["origin"],
        "label": row["label"],
        "note": row["note"],
        "created": row["created"],
        "decided": row["decided"],
        "txid": row["txid"] or None,
        "error": row["error"] or None,
        "offer": json.loads(row["offer"]) if row["offer"] else None,
        "page": row["page"] or None,
    }


def summary(row: dict) -> str:
    """One line: what would move, and where."""
    to = f"@{row['totag']}" if row["totag"] else row["toaddress"]
    if row["kind"] == "coins":
        return f"{row['amount']} coins to {to}"
    if row["kind"] == "token":
        return f"{row['amount']} {row['propertyname'] or 'token ' + str(row['propertyid'])} to {to}"
    if row["kind"] == "swap":
        from .swap import describe_leg
        offer = json.loads(row["offer"])
        if row["origin"] == "shop":
            # The shop's own side: it hands over `give` and is paid `take`.
            return f"sold {describe_leg(offer['give'])} for {describe_leg(offer['take'])}"
        return f"swap {describe_leg(offer['take'])} for {describe_leg(offer['give'])}"
    return f"inscription #{row['number']} to {to}"


# --- filing: everything that can be refused before anybody is asked ----------

def validate(kind: str, index: Any, own: list[str], *, mainnet: bool,
             to: str, amount: str = "", propertyid: Any = None,
             inscription: str = "", fromaddress: str = "") -> dict:
    """Turn what a caller said into what a request row holds.

    Refuses at once what can be refused at once -- an address on the other
    chain, a token that does not exist, an inscription this wallet does not
    own -- so a mistake is an error to the caller, not a puzzle for the
    person approving. What can change before approval (a balance) is checked
    then, not now.
    """
    from .web.app import _check_address
    if kind == "swap":
        raise RequestError("a swap is asked for through the shop it buys from "
                           "(arcade.swap in an inscribed page), not filed as a send")
    fields: dict[str, Any] = {}
    to = str(to or "").strip()
    if to.startswith("@"):
        resolved = index.address_of(to)
        if not resolved:
            raise RequestError(f"nobody holds {to}")
        fields["totag"] = to[1:]
        to = resolved
    problem = _check_address(to, mainnet=mainnet)
    if problem:
        raise RequestError(f"to: {problem}")
    fields["toaddress"] = to

    fromaddress = str(fromaddress or "").strip()
    if fromaddress:
        if fromaddress not in own:
            raise RequestError(f"{fromaddress} is not an address this wallet can sign for")
        fields["fromaddress"] = fromaddress

    if kind == "coins":
        try:
            units = walletlib.parse_amount(str(amount))
        except Exception as exc:
            raise RequestError(f"amount: {exc}") from None
        if units <= 0:
            raise RequestError("amount must be more than zero")
        fields.update(units=units, amount=f"{units / COIN:.8f}".rstrip("0").rstrip("."))
    elif kind == "token":
        try:
            pid = int(propertyid)
        except (TypeError, ValueError):
            raise RequestError("propertyid must be a number") from None
        prop = index.property(pid)
        if prop is None:
            raise RequestError(f"there is no token {pid}")
        try:
            units = parse_token_amount(str(amount), bool(prop["divisible"]))
        except Exception as exc:
            raise RequestError(f"amount: {exc}") from None
        if units <= 0:
            raise RequestError("amount must be more than zero")
        from .ledger import format_amount
        fields.update(units=units, amount=format_amount(units, bool(prop["divisible"])),
                      propertyid=pid, propertyname=prop["name"])
    elif kind == "inscription":
        from .web.content import _key
        row = index.inscription(_key("" if inscription is None else str(inscription)))
        if row is None:
            raise RequestError("no such inscription")
        if own and row["owner"] not in own:
            raise RequestError(f"inscription #{row['number']} is not this wallet's to send")
        if fields.get("fromaddress") and fields["fromaddress"] != row["owner"]:
            raise RequestError(f"inscription #{row['number']} is held by {row['owner']}, "
                               f"not {fields['fromaddress']}")
        fields.update(inscription=row["txid"], number=row["number"],
                      fromaddress=row["owner"])
        if to == row["owner"]:
            raise RequestError("that would send it to the address that already holds it")
    else:
        raise RequestError(f"kind must be one of {', '.join(KINDS)}")
    return fields


# --- approving: the transaction, built when somebody is looking --------------

@dataclass
class Prepared:
    """One transaction of any kind, decoded for the person deciding."""

    hex: str
    txid: str
    what: str
    sender: str
    fee_sats: int
    dust_sats: int
    size: int
    outputs: list[dict] = field(default_factory=list)

    @property
    def fee_coins(self) -> float:
        return self.fee_sats / COIN

    @property
    def total_coins(self) -> float:
        return (self.fee_sats + self.dust_sats) / COIN


def prepare(row: dict, rpc: Any, params: Any, index: Any, own: list[str]) -> Prepared:
    """Build and sign what the request asks for. Does NOT broadcast."""
    to = row["toaddress"]
    if row["kind"] == "coins":
        return _prepare_coins(rpc, params, row["fromaddress"], to, int(row["units"]))
    if row["kind"] == "token":
        pid, units = int(row["propertyid"]), int(row["units"])
        sender = row["fromaddress"]
        if not sender:
            # The wallet decides which of its addresses pays: the one holding
            # enough, and the first of them if several do.
            sender = next((a for a in own if index.balance(a, pid) >= units), "")
            if not sender:
                raise RequestError(f"no address in this wallet holds {row['amount']} "
                                   f"of {row['propertyname']}")
        held = index.balance(sender, pid)
        if held < units:
            from .ledger import format_amount
            raise RequestError(f"{sender} holds {format_amount(held, True)} of "
                               f"{row['propertyname']}, not {row['amount']}")
        prepared = tokenlib.TokenSender(rpc, params).prepare(
            sender, tokenlib.send_payload(pid, units), to)
        return _from_token(prepared, f"{row['amount']} {row['propertyname']}")
    if row["kind"] == "swap":
        from . import swap as swaplib
        built = swaplib.build(rpc, index, json.loads(row["offer"]), own)
        return Prepared(hex=built.hex, txid=built.txid, what=built.what,
                        sender=built.buyer, fee_sats=built.fee_sats, dust_sats=0,
                        size=built.size, outputs=built.outputs)
    if row["kind"] == "inscription":
        found = index.inscription(row["inscription"])
        if found is None:
            raise RequestError("that inscription is no longer indexed")
        if found["owner"] not in own:
            raise RequestError(f"inscription #{found['number']} is not this wallet's any more")
        payload = P.AnyData(data=inscriptionlib.Transfer(
            txid=bytes.fromhex(found["txid"])).encode()).encode()
        prepared = tokenlib.TokenSender(rpc, params).prepare(found["owner"], payload, to)
        return _from_token(prepared, f"inscription #{found['number']}")
    raise RequestError(f"unknown kind {row['kind']!r}")


def broadcast(rpc: Any, prepared: Prepared) -> str:
    """Send exactly the bytes that were shown."""
    return str(rpc.call("sendrawtransaction", prepared.hex))


def _from_token(prepared: tokenlib.PreparedTokenTx, what: str) -> Prepared:
    return Prepared(hex=prepared.hex, txid=prepared.txid, what=what,
                    sender=prepared.sender, fee_sats=prepared.fee_sats,
                    dust_sats=prepared.dust_sats, size=prepared.size,
                    outputs=prepared.outputs)


def _prepare_coins(rpc: Any, params: Any, sender: str, to: str, sats: int) -> Prepared:
    """A plain payment.

    From one address when the request names one -- the coins come from it and
    the change goes back to it, the way every other send here keeps an
    address whole -- and from the wallet at large when it does not.
    """
    if not sender:
        plain = walletlib.prepare_send(rpc, to, sats)
        outputs = _plain_outputs(plain.decoded, to, None, sats)
        return Prepared(hex=plain.hex, txid=plain.txid, what=f"{sats / COIN:.8f} coins",
                        sender=next((o["where"] for o in outputs if o["is_change"]), ""),
                        fee_sats=plain.fee_sats, dust_sats=sats, size=plain.size,
                        outputs=outputs)
    from .messaging.sender import MessageSender, SendError
    try:
        inputs = MessageSender(rpc, params)._select_inputs(sender, sats + COIN)
    except SendError as exc:
        raise RequestError(str(exc)) from None
    raw = build_raw_tx(inputs, [(sats, p2pkh_script(to))])
    funded = fees.fund(rpc, raw, {"changeAddress": sender})
    if not funded or "hex" not in funded:
        raise RequestError("fundrawtransaction failed; is the wallet funded?")
    signed = rpc.call("signrawtransaction", funded["hex"])
    if not signed.get("complete"):
        raise RequestError(f"signing failed: {signed.get('errors')}")
    decoded = rpc.call("decoderawtransaction", signed["hex"])
    return Prepared(hex=signed["hex"], txid=decoded["txid"], what=f"{sats / COIN:.8f} coins",
                    sender=sender, fee_sats=int(round(float(funded.get("fee", 0)) * COIN)),
                    dust_sats=sats, size=len(signed["hex"]) // 2,
                    outputs=_plain_outputs(decoded, to, sender, sats))


def _plain_outputs(decoded: dict, to: str, sender: str | None,
                   sats: int | None = None) -> list[dict]:
    """Each output, and which one is the payment.

    Matched by amount as well as address, because the recipient can be one of
    this wallet's own addresses -- a page asking the owner to move a coin
    within the wallet, or the live demo of exactly that -- and then the change
    goes back to the same address. Matching on address alone called both
    outputs "the recipient" and left "From" blank.
    """
    rows = []
    paid = False
    for vout in decoded.get("vout", []):
        addresses = vout.get("scriptPubKey", {}).get("addresses") or []
        address = addresses[0] if addresses else None
        value = float(vout.get("value", 0))
        payment = (address == to and not paid
                   and (sats is None or int(round(value * COIN)) == sats))
        paid = paid or payment
        rows.append({"value": value,
                     "where": address or vout.get("scriptPubKey", {}).get("type", "unknown"),
                     "is_change": address is not None and not payment
                                  and (sender is None or address == sender),
                     "is_recipient": payment})
    return rows
