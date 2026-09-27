"""A shop that is an inscription, and the swap that buys from it.

Why it exists
-------------
A marketplace needs two things a send does not have: a way to say what is for
sale, and a way for two parties to exchange without either trusting the other
to go second. This gives both, and the engine gives the second its teeth: a
swap is ONE transaction that both parties have signed (inscriptions.Swap,
state.Engine._swap). Both legs move or neither does, so there is no block in
which the buyer has paid and the seller has not delivered.

The shop is an inscription
--------------------------
What a store sells is written in the inscription's own JSON, the field that
travels with the content and is covered by its hash (inscribe.py). So the
terms are on the chain, immutable, and identical on every node: a buyer's
wallet reads them from its own ledger before anything is asked of the seller,
and the seller's wallet reads the same bytes before it offers anything.
Nobody's page is trusted about the price.

    {"shop": {"node": "arcade:test:...",
              "listings": [
                {"give": {"token": 3, "amount": "100"}, "take": {"coins": "2"}},
                {"give": {"collection": "Goofball", "pick": "random"},
                 "take": {"token": 3, "amount": "10"}},
                {"give": {"inscription": 57}, "take": {"inscription": 58}}]}}

`node` is where the seller's wallet listens (its contact code or public key).
A listing's `give` is what the seller hands over, `take` what the seller gets.
`collection` names a set of the shop's creator's and hands over a random
item of it the shop still holds -- a minting event is exactly this listing.

Who may sell from a shop
------------------------
The wallet that CREATED the inscription and still HOLDS it. Creating it means
the terms are the owner's own words; holding it means the shop can be
closed by sending the inscription away, and that a shop cannot be
copied -- a second inscription with the same JSON belongs to whoever made it
and sells from THEIR wallet, not this one. The shopkeeper (shopkeeper.py)
answers for every such inscription without anybody pressing anything: an
order is a question about an item and a price the owner already wrote down.

The exchange, step by step
--------------------------
1. The buyer's wallet sends the shop's node a node-to-node message (nodetalk)
   naming the shop, the listing and the address that will pay and receive.
2. The shop's node checks it still holds what the listing gives and that the
   buyer holds what it takes, picks the item if the listing is random, LOCKS
   one of its own outputs so nothing else in that wallet spends it, and
   answers with an offer: the two legs made concrete, and the outpoint.
3. The buyer's wallet builds the transaction -- the seller's outpoint first,
   its own inputs after, the swap in OP_RETURN, the seller made whole -- and
   shows it to the buyer in the approvals pop-up. Approve signs the buyer's
   half; the seller's input cannot be signed here, and the node says so.
4. The half-signed transaction goes back to the shop's node in a second
   message. The node checks it against the offer it made -- its own outpoint
   in first place, exactly the legs it offered, paid what it asked -- and only
   then signs and broadcasts. One transaction; one block; both sides.

An offer that is not taken up expires and unlocks the output. A buyer who
sends something other than what was offered is refused and nothing is
signed. The engine checks everything again when the block arrives, on every
node, so a shop's node that lied would only be recorded as having sent an
invalid transaction.

Testnet only, because the messages are (D-010).
"""

from __future__ import annotations

import json
import math
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .db import add_missing_columns
from . import inscriptions as I
from . import payload as P
from .encoding import decode_class_c, encode_class_c
from .ledger import COIN, format_amount, parse_amount as parse_token_amount
from .messaging.sender import OUTPUT_VALUE
from .script import b58check_decode
from .txbuild import build_raw_tx, op_return_script, p2pkh_script
from .wallet import parse_amount as parse_coin_amount

#: How long an offer stands. Long enough to read the pop-up on a phone, short
#: enough that an output is not locked for an afternoon by a buyer who left.
OFFER_TTL = 900

#: The fee per kilobyte, RECOMMENDED_MIN_TX_FEE (policy/policy.h:23), and the
#: least the buyer's change may be without being dust the node refuses.
FEE_PER_KB = COIN // 100
MIN_CHANGE = OUTPUT_VALUE

#: How many listings a shop may have. A page can show more than this; it
#: cannot ask a node to price them all.
MAX_LISTINGS = 200

#: What a node may ask of a trade it made the offer for, in basis points --
#: §1d. An offer carries the rate, not an amount, so both sides work out the
#: same number from the same price and neither has to trust the other's
#: arithmetic.
CUT_CEILING = 10_000


def cut_sats(cut: Any, price: int) -> int:
    """What an offer's cut comes to on a price, in satoshis; 0 for none.

    Floored, so a node is never paid more than the rate it announced. And 0
    when the result could not be an output at all: §1d's "nothing below the
    dust floor". A trade too small to carry a cut goes free rather than being
    refused by a minimum that would eat the whole of it -- which is avoidable
    by trading in pieces, and accepted.
    """
    bps = int((cut or {}).get("bps") or 0) if isinstance(cut, dict) else int(cut or 0)
    if not 0 < bps <= CUT_CEILING or int(price) <= 0:
        return 0
    sats = int(price) * bps // 10_000
    return sats if sats >= MIN_CHANGE else 0


def cut_bps(said: Any) -> int:
    """A percentage as an operator typed it, as whole basis points.

    Integers on the wire rather than a float in an offer: a rate both nodes
    multiply by the price has to land on the same satoshi on both, and a
    percent that has been through JSON, a settings file and a form does not
    reliably do that.
    """
    try:
        percent = float(str(said).strip().rstrip("%").strip())
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(percent) or percent <= 0:
        return 0
    return min(int(round(percent * 100)), CUT_CEILING)


def _cut(cut: Any) -> dict:
    """A node's announced cut, checked well enough to be worth offering.

    Checked here rather than at each caller, because the alternative is that
    a buyer's node finds out: an offer naming no address, or one no
    transaction could pay, costs the buyer a message fee and leaves the
    seller's output locked for nothing.
    """
    cut = cut or {}
    if not isinstance(cut, dict):
        raise SwapError("a cut is a rate and an address to pay it to")
    try:
        bps = int(cut.get("bps") or 0)
    except (TypeError, ValueError):
        raise SwapError(f"{cut.get('bps')!r} is not a rate") from None
    if bps <= 0:
        return {}
    if bps > CUT_CEILING:
        raise SwapError(f"a cut of {bps} basis points is past the ceiling of "
                        f"{CUT_CEILING} -- that is the whole trade and more")
    to = str(cut.get("to") or "")
    try:
        p2pkh_script(to)
    except Exception:
        raise SwapError(f"{to or 'nothing'} is not an address to pay a cut to") from None
    return {"bps": bps, "to": to}


STATUSES = ("open", "sent", "expired", "refused", "failed")

#: What the messages between the two wallets speak. Every one carries it as
#: `swapv`, and a wallet answers only its own: a buyer on an older release
#: is told so in words, rather than offered something it will build wrongly.
PROTOCOL = 1


class SwapError(ValueError):
    """What was asked cannot be offered, built or signed."""


# --- the shop: what an inscription says it sells ------------------------------

def shop_of(row: dict) -> dict:
    """The shop an inscription describes, or a SwapError saying why not."""
    try:
        data = json.loads(row.get("json") or "")
    except (TypeError, ValueError):
        raise SwapError("this inscription has no JSON, so it is not a shop") from None
    shop = data.get("shop") if isinstance(data, dict) else None
    if not isinstance(shop, dict):
        raise SwapError("this inscription's JSON has no \"shop\" in it")
    listings = shop.get("listings")
    if not isinstance(listings, list) or not listings:
        raise SwapError("a shop needs a list of listings")
    if len(listings) > MAX_LISTINGS:
        raise SwapError(f"a shop may list {MAX_LISTINGS} things at most")
    out = []
    for n, entry in enumerate(listings):
        if not isinstance(entry, dict) or not isinstance(entry.get("give"), dict) \
                or not isinstance(entry.get("take"), dict):
            raise SwapError(f"listing {n} needs a \"give\" and a \"take\"")
        _check_spec(entry["give"], n, giving=True)
        _check_spec(entry["take"], n, giving=False)
        out.append({"give": entry["give"], "take": entry["take"]})
    node = shop.get("node")
    return {"node": str(node).strip() if node else None, "listings": out}


def _check_spec(spec: dict, n: int, *, giving: bool) -> None:
    kinds = [k for k in ("inscription", "collection", "token", "coins") if k in spec]
    if len(kinds) != 1:
        raise SwapError(f"listing {n}: name one of inscription, collection, token or coins")
    kind = kinds[0]
    if kind == "collection" and not giving:
        raise SwapError(f"listing {n}: a shop can give a random item, not take one")
    if kind == "token":
        try:
            int(spec["token"])
        except (TypeError, ValueError):
            raise SwapError(f"listing {n}: token must be a property id") from None
    if kind == "token" and not str(spec.get("amount", "")).strip():
        raise SwapError(f"listing {n}: a token needs an amount")
    if kind == "coins" and not str(spec.get("coins", "")).strip():
        raise SwapError(f"listing {n}: coins is the amount, like {{\"coins\": \"2\"}}")


def leg_of(spec: dict, index: Any) -> I.Leg:
    """A listing's spec as a concrete leg. Not for `collection`: that is
    picked by the seller at offer time (pick_item)."""
    if "inscription" in spec:
        # A number is what people say out loud, so a listing written by hand
        # may name one -- but a txid is 64 characters and may be all digits,
        # and reading one as a number sells whatever happens to have that
        # number. Length settles it before the digits do.
        key = spec["inscription"]
        numbered = isinstance(key, int) or (str(key).isdigit() and len(str(key)) != 64)
        row = index.inscription(int(key) if numbered else str(key))
        if row is None:
            raise SwapError(f"there is no inscription {key}")
        return I.Leg(I.LEG_INSCRIPTION, txid=bytes.fromhex(row["txid"]))
    if "token" in spec:
        pid = int(spec["token"])
        prop = index.property(pid)
        if prop is None:
            raise SwapError(f"there is no token {pid}")
        try:
            units = parse_token_amount(str(spec["amount"]), bool(prop["divisible"]))
        except Exception as exc:
            raise SwapError(f"token amount: {exc}") from None
        if units <= 0:
            raise SwapError("a token amount must be more than zero")
        return I.Leg(I.LEG_TOKEN, property_id=pid, amount=units)
    if "coins" in spec:
        try:
            sats = parse_coin_amount(str(spec["coins"]))
        except Exception as exc:
            raise SwapError(f"coin amount: {exc}") from None
        if sats <= 0:
            raise SwapError("a coin amount must be more than zero")
        return I.Leg(I.LEG_COINS, amount=sats)
    raise SwapError("a collection is picked at offer time")


def pick_item(index: Any, creator: str, name: str, owner: str,
              exclude: set[str] = frozenset()) -> dict:
    """A random item of the collection that `owner` still holds.

    Random rather than next-in-line, because a mint whose order is known is
    a mint whose best pieces are known. `exclude` is what is already in an
    open offer."""
    held = [row for row in index.collection_items(creator, name, limit=500)
            if row["owner"] == owner and row["txid"] not in exclude]
    if not held:
        raise SwapError(f"nothing of {name} is left to give")
    return held[secrets.randbelow(len(held))]


def leg_json(leg: I.Leg, index: Any) -> dict:
    """A leg as pages and people see it."""
    if leg.kind == I.LEG_INSCRIPTION:
        row = index.inscription(leg.txid.hex())
        return {"kind": "inscription", "txid": leg.txid.hex(),
                "number": row["number"] if row else None,
                "collection": row.get("collection") if row else None,
                "edition": row.get("edition") if row else None}
    if leg.kind == I.LEG_TOKEN:
        prop = index.property(leg.property_id)
        divisible = bool(prop["divisible"]) if prop else True
        return {"kind": "token", "propertyid": leg.property_id,
                "name": prop["name"] if prop else None,
                "amount": format_amount(leg.amount, divisible), "units": leg.amount}
    if leg.kind == I.LEG_COINS:
        return {"kind": "coins", "amount": f"{leg.amount / COIN:.8f}", "sats": leg.amount}
    raise SwapError("a leg has to be an inscription, a token or coins")


def leg_from_json(data: Any) -> I.Leg:
    if not isinstance(data, dict):
        raise SwapError("a leg is an object")
    kind = data.get("kind")
    try:
        if kind == "inscription":
            return I.Leg(I.LEG_INSCRIPTION, txid=bytes.fromhex(str(data["txid"])))
        if kind == "token":
            return I.Leg(I.LEG_TOKEN, property_id=int(data["propertyid"]),
                         amount=int(data["units"]))
        if kind == "coins":
            return I.Leg(I.LEG_COINS, amount=int(data["sats"]))
    except (KeyError, TypeError, ValueError):
        raise SwapError(f"a malformed {kind} leg") from None
    raise SwapError("a leg has to be an inscription, a token or coins")


def _tidy(amount: Any) -> str:
    """8dp without the zeros nobody reads: 1.00000000 -> 1, 0.50000000 -> 0.5.

    Every place a price is shown says the same thing, so the page shims and
    the wallet's own screens cannot disagree about what a listing costs.
    """
    text = str(amount if amount is not None else "")
    if "." not in text:
        return text
    text = text.rstrip("0").rstrip(".")
    return text or "0"


def describe_leg(data: dict) -> str:
    """One phrase, for the person deciding."""
    if data.get("kind") == "random":
        return f"a random {data.get('collection')} ({data.get('left', 0)} left)"
    if data.get("kind") == "inscription":
        what = f"inscription #{data['number']}" if data.get("number") is not None \
            else f"inscription {str(data.get('txid', ''))[:16]}…"
        if data.get("collection"):
            what += f" ({data['collection']}"
            what += f" #{data['edition']})" if data.get("edition") is not None else ")"
        return what
    if data.get("kind") == "token":
        return f"{data.get('amount')} {data.get('name') or 'token ' + str(data.get('propertyid'))}"
    return f"{_tidy(data.get('amount'))} coins"


def listings_json(shop_row: dict, index: Any) -> list[dict]:
    """A shop's listings as a page shows them: each leg made concrete where
    it can be, and whether the shop can give it right now. A random listing
    stays random -- what it will be is picked when it is bought -- and says
    how many are left. Nothing here is the seller's word: it is this node's
    own ledger, read the same way the seller's reads it."""
    shop = shop_of(shop_row)
    seller = shop_row["owner"]
    out = []
    for n, listing in enumerate(shop["listings"]):
        entry: dict[str, Any] = {"n": n}
        give = listing["give"]
        if "collection" in give:
            left = [r for r in index.collection_items(shop_row["creator"], give["collection"],
                                                      limit=500) if r["owner"] == seller]
            entry["give"] = {"kind": "random", "collection": give["collection"],
                             "left": len(left)}
            entry["available"] = None if left else f"nothing of {give['collection']} is left"
        else:
            try:
                leg = leg_of(give, index)
                entry["give"] = leg_json(leg, index)
                entry["available"] = holds(index, None, seller, leg)
            except SwapError as exc:
                entry["give"] = {"kind": "unknown", "spec": give}
                entry["available"] = str(exc)
        try:
            entry["take"] = leg_json(leg_of(listing["take"], index), index)
        except SwapError as exc:
            entry["take"] = {"kind": "unknown", "spec": listing["take"]}
            entry["available"] = entry["available"] or str(exc)
        entry["text"] = f"{describe_leg(entry['give'])} for {describe_leg(entry['take'])}"
        out.append(entry)
    return out


def holds(index: Any, rpc: Any, address: str, leg: I.Leg) -> str | None:
    """Why `address` cannot give `leg`, or None if it can.

    Coins are not checked against the ledger: they are paid inside the swap
    itself, and the wallet building it either has them or fails to build."""
    if leg.kind == I.LEG_INSCRIPTION:
        row = index.inscription(leg.txid.hex())
        if row is None:
            return "no such inscription"
        if row["owner"] != address:
            return f"inscription #{row['number']} is held by {row['owner']}, not {address}"
        return None
    if leg.kind == I.LEG_TOKEN:
        prop = index.property(leg.property_id)
        if prop is None:
            return f"there is no token {leg.property_id}"
        held = index.balance(address, leg.property_id)
        if held < leg.amount:
            return (f"{address} holds {format_amount(held, bool(prop['divisible']))} "
                    f"of {prop['name']}, not {format_amount(leg.amount, bool(prop['divisible']))}")
        return None
    return None


# --- offers: what a shop's node has promised, and to whom ---------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS offer (
    id            TEXT PRIMARY KEY,
    network       TEXT NOT NULL,
    shop          TEXT NOT NULL,
    -- The standing order this offer fills, for an offer that fills one. What
    -- it is for: two buyers asking for the same order at the same time must
    -- not both be promised the whole of it, and the subtraction that stops
    -- that can only be done if the offer says which order it came from.
    "order"       TEXT NOT NULL DEFAULT '',
    listing       INTEGER NOT NULL,
    seller        TEXT NOT NULL,
    buyer         TEXT NOT NULL,
    buyer_pubkey  TEXT NOT NULL DEFAULT '',
    give          TEXT NOT NULL,
    take          TEXT NOT NULL,
    outpoint_txid TEXT NOT NULL,
    outpoint_vout INTEGER NOT NULL,
    outpoint_value INTEGER NOT NULL,
    -- §1d: what this node takes of a trade it made the offer for, and where
    -- it says to pay it. The RATE travels, never an amount, because both
    -- sides must be able to work the amount out of the price for themselves
    -- -- and an offer that announced a number could announce a different one
    -- to each of them.
    cut_bps       INTEGER NOT NULL DEFAULT 0,
    cut_to        TEXT NOT NULL DEFAULT '',
    created       REAL NOT NULL,
    expires       REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',
    txid          TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS offer_open ON offer(status, network);
-- An offer somebody made on an NFT, in either direction. Not a listing: it
-- is made on an inscription whatever its owner has or has not put up for
-- sale, and it is the owner's to accept or refuse (D-038).
-- A fill this wallet asked for: "take 300 off order 3f9a at its own price".
-- Written down BEFORE the question goes out, because the answer to it makes
-- this wallet sign a transaction that pays coins. Without a note of having
-- asked, any node could send an unsolicited answer and be paid for it.
CREATE TABLE IF NOT EXISTS fill (
    id            TEXT PRIMARY KEY,      -- the txid of the question
    network       TEXT NOT NULL,
    "order"       TEXT NOT NULL,
    maker         TEXT NOT NULL,
    buyer         TEXT NOT NULL,
    tokens        INTEGER NOT NULL,
    coins         INTEGER NOT NULL,      -- the most this wallet will pay
    status        TEXT NOT NULL DEFAULT 'asked',
    offer_id      TEXT NOT NULL DEFAULT '',
    txid          TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    created       REAL NOT NULL,
    expires       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bid (
    id            TEXT PRIMARY KEY,
    network       TEXT NOT NULL,
    direction     TEXT NOT NULL,          -- 'in' offered to me, 'out' I offered
    inscription   TEXT NOT NULL,
    number        INTEGER,
    owner         TEXT NOT NULL,
    buyer         TEXT NOT NULL,
    peer_pubkey   TEXT NOT NULL DEFAULT '',
    take          TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    created       REAL NOT NULL,
    expires       REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',
    offer_id      TEXT NOT NULL DEFAULT '',
    txid          TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    coins         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS bid_open ON bid(network, direction, status);
CREATE TABLE IF NOT EXISTS cursor (
    network TEXT PRIMARY KEY,
    last    INTEGER NOT NULL
);
"""


class Offers:
    """The seller's book: every offer made, open or closed, in one file."""

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
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add(self, offer: dict) -> None:
        with self._open() as conn:
            conn.execute(
                'INSERT INTO offer(id, network, shop, "order", listing, seller, buyer, '
                "buyer_pubkey, give, take, outpoint_txid, outpoint_vout, outpoint_value, "
                "cut_bps, cut_to, created, expires) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (offer["id"], offer["network"], offer["shop"],
                 str(offer.get("order") or ""), int(offer["listing"]),
                 offer["seller"], offer["buyer"], offer.get("buyer_pubkey", ""),
                 json.dumps(offer["give"]), json.dumps(offer["take"]),
                 offer["outpoint"]["txid"], int(offer["outpoint"]["vout"]),
                 int(offer["outpoint"]["value"]),
                 int((offer.get("cut") or {}).get("bps") or 0),
                 str((offer.get("cut") or {}).get("to") or ""),
                 offer["created"], offer["expires"]))

    def get(self, offer_id: str) -> dict | None:
        with self._open() as conn:
            row = conn.execute("SELECT * FROM offer WHERE id = ?",
                               (str(offer_id),)).fetchone()
            return _offer_row(row) if row else None

    # --- offers made on an NFT, in either direction ------------------------

    def add_fill(self, fill: dict) -> None:
        with self._open() as conn:
            conn.execute(
                'INSERT INTO fill(id, network, "order", maker, buyer, tokens, coins, '
                "created, expires) VALUES(?,?,?,?,?,?,?,?,?)",
                (fill["id"], fill["network"], fill["order"], fill["maker"],
                 fill["buyer"], int(fill["tokens"]), int(fill["coins"]),
                 fill["created"], fill["expires"]))

    def get_fill(self, fill_id: str) -> dict | None:
        with self._open() as conn:
            row = conn.execute("SELECT * FROM fill WHERE id=?",
                               (str(fill_id),)).fetchone()
            return dict(row) if row else None

    def fills(self, network: str, limit: int = 50) -> list[dict]:
        with self._open() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM fill WHERE network=? ORDER BY created DESC LIMIT ?",
                (network, int(limit)))]

    def stale_fills(self, network: str, now: float) -> list[dict]:
        """Fills still waiting for an answer that will never come.

        A query for CANDIDATES, not a page of recent history. `fills()` is
        newest-first with a limit, for showing somebody what they asked for;
        expiring through it meant an old note fell off the end of the page and
        was never examined again -- permanently stuck at "waiting for their
        node", which is the state this exists to abolish. It survives only in
        a wallet with more than fifty notes, which is the long-lived
        heavily-traded one, and no test that makes a handful can see it
        (D-094).
        """
        with self._open() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM fill WHERE network=? AND status='asked' "
                "AND expires<=? ORDER BY created", (network, float(now)))]

    def close_fill(self, fill_id: str, status: str, offer_id: str = "",
                   txid: str = "", error: str = "") -> None:
        with self._open() as conn:
            conn.execute(
                "UPDATE fill SET status=?, offer_id=COALESCE(NULLIF(?,''), offer_id), "
                "txid=COALESCE(NULLIF(?,''), txid), error=? WHERE id=?",
                (status, offer_id, txid, error, str(fill_id)))

    def add_bid(self, bid: dict) -> None:
        with self._open() as conn:
            conn.execute(
                "INSERT INTO bid(id, network, direction, inscription, number, owner, "
                "buyer, peer_pubkey, take, note, created, expires, coins) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (bid["id"], bid["network"], bid["direction"], bid["inscription"],
                 bid.get("number"), bid["owner"], bid["buyer"],
                 bid.get("peer_pubkey", ""), json.dumps(bid["take"]),
                 str(bid.get("note", ""))[:200], bid["created"], bid["expires"],
                 json.dumps(bid["coins"]) if bid.get("coins") else ""))

    def bids(self, network: str, direction: str | None = None,
             status: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM bid WHERE network = ?"
        args: list[Any] = [network]
        if direction:
            sql += " AND direction = ?"; args.append(direction)
        if status:
            sql += " AND status = ?"; args.append(status)
        with self._open() as conn:
            return [_bid_row(r) for r in conn.execute(
                sql + " ORDER BY created DESC LIMIT ?", args + [int(limit)])]

    def get_bid(self, bid_id: str) -> dict | None:
        with self._open() as conn:
            row = conn.execute("SELECT * FROM bid WHERE id = ?",
                               (str(bid_id),)).fetchone()
            return _bid_row(row) if row else None

    def close_bid(self, bid_id: str, status: str, offer_id: str = "",
                  txid: str = "", error: str = "") -> bool:
        """Close a bid. False if it was not open -- answered twice, or gone."""
        if status == "open":
            raise ValueError("that is not a decision")
        with self._open() as conn:
            done = conn.execute(
                "UPDATE bid SET status = ?, offer_id = ?, txid = ?, error = ? "
                "WHERE id = ? AND status = 'open'",
                (status, offer_id, txid, str(error)[:500], str(bid_id)))
            return done.rowcount == 1

    def sold_offers(self, network: str) -> list[dict]:
        """Offers that were signed and broadcast, newest first.

        Not the same as finished: a sale is only settled once its block is
        indexed, and until then the item it sold still reads as the seller's
        (D-033).
        """
        with self._open() as conn:
            return [_offer_row(r) for r in conn.execute(
                "SELECT * FROM offer WHERE status = 'sent' AND network = ? "
                "ORDER BY created DESC LIMIT 200", (network,))]

    def open_offers(self, network: str) -> list[dict]:
        with self._open() as conn:
            return [_offer_row(r) for r in conn.execute(
                "SELECT * FROM offer WHERE status = 'open' AND network = ? ORDER BY created",
                (network,))]

    def close(self, offer_id: str, status: str, txid: str = "", error: str = "") -> bool:
        if status not in STATUSES or status == "open":
            raise ValueError(f"not a closing status: {status!r}")
        with self._open() as conn:
            done = conn.execute(
                "UPDATE offer SET status = ?, txid = ?, error = ? "
                "WHERE id = ? AND status = 'open'",
                (status, txid, error[:500], str(offer_id)))
            return done.rowcount == 1

    def recent(self, network: str, limit: int = 50) -> list[dict]:
        with self._open() as conn:
            return [_offer_row(r) for r in conn.execute(
                "SELECT * FROM offer WHERE network = ? ORDER BY created DESC LIMIT ?",
                (network, int(limit)))]

    # The shopkeeper's place in the inbox: the id of the last message it read.
    # Its own cursor rather than the read flags, which a bot on the RPC may
    # set on anything it fetches -- a message it marked read is still an
    # order. None before the shopkeeper has ever run on this chain.

    def cursor(self, network: str) -> int | None:
        with self._open() as conn:
            row = conn.execute("SELECT last FROM cursor WHERE network = ?",
                               (network,)).fetchone()
            return int(row["last"]) if row else None

    def set_cursor(self, network: str, last: int) -> None:
        with self._open() as conn:
            conn.execute("INSERT INTO cursor(network, last) VALUES(?,?) "
                         "ON CONFLICT(network) DO UPDATE SET last = excluded.last",
                         (network, int(last)))


def _offer_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    return {
        "id": data["id"], "network": data["network"], "shop": data["shop"],
        "order": data.get("order") or "", "listing": data["listing"], "seller": data["seller"], "buyer": data["buyer"],
        "buyer_pubkey": data["buyer_pubkey"],
        "give": json.loads(data["give"]), "take": json.loads(data["take"]),
        "outpoint": {"txid": data["outpoint_txid"], "vout": data["outpoint_vout"],
                     "value": data["outpoint_value"]},
        "cut": {"bps": int(data.get("cut_bps") or 0),
                "to": data.get("cut_to") or ""},
        "created": data["created"], "expires": data["expires"],
        "status": data["status"], "txid": data["txid"] or None,
        "error": data["error"] or None,
    }


def public(offer: dict) -> dict:
    """The offer as it is sent to the buyer: everything but the bookkeeping."""
    out = {k: offer[k] for k in ("id", "network", "shop", "listing", "seller", "buyer",
                                 "give", "take", "outpoint", "created", "expires")}
    # The order a fill is for travels with it. The buyer already knows which
    # order it asked about; carrying it here lets every check downstream be
    # made against the offer itself rather than against who called what.
    if offer.get("order"):
        out["order"] = str(offer["order"])
    # And so does the cut, when this node takes one: it is a term of the
    # trade, and a fee the buyer's wallet has not been shown is a fee it is
    # being asked to sign away blind (§1d).
    cut = offer.get("cut") or {}
    if int(cut.get("bps") or 0) > 0:
        out["cut"] = {"bps": int(cut["bps"]), "to": str(cut.get("to") or "")}
    return out


# --- the seller's side --------------------------------------------------------

def coins_in(leg: I.Leg) -> int:
    return leg.amount if leg.kind == I.LEG_COINS else 0


def _lock_output(rpc: Any, seller: str, give: Any, take: Any,
                 locked_outs: set) -> dict:
    """One of the seller's outputs, held until the swap goes or expires.

    It comes back to the seller plus what the buyer pays in coins, less what
    the seller pays, and has to stay above dust once that is done -- so the
    smallest output that can carry it is the one taken.
    """
    floor = coins_in(give) + MIN_CHANGE - coins_in(take)
    unspent = [u for u in (rpc.call("listunspent", 1, 9_999_999, [seller]) or [])
               if (u["txid"], int(u["vout"])) not in locked_outs
               and u.get("spendable", True)]
    fitting = sorted((u for u in unspent if int(round(float(u["amount"]) * COIN)) >= floor),
                     key=lambda u: float(u["amount"]))
    if not unspent:
        # Not "no output worth 0.00000000", which reads as nonsense and was
        # what a holder with tokens and no coins was told. Every side of a
        # swap puts one of its own outputs into the transaction; an address
        # with none cannot take part, whatever it holds (D-045).
        raise SwapError(
            f"{seller} holds no spendable coins, and every side of a swap puts "
            f"one of its own outputs into the transaction. Send it a few coins "
            f"-- a fraction of one is enough -- and try again once they confirm"
            + (", or wait for the offers already holding its outputs to expire"
               if locked_outs else ""))
    if not fitting:
        raise SwapError(
            f"{seller} has no single output worth {max(floor, 0) / COIN:.8f} to "
            f"swap from; its largest is "
            f"{max(float(u['amount']) for u in unspent):.8f}"
            + (" (others are held by open offers)" if locked_outs else ""))
    chosen = fitting[0]
    outpoint = {"txid": chosen["txid"], "vout": int(chosen["vout"]),
                "value": int(round(float(chosen["amount"]) * COIN))}
    if not rpc.call("lockunspent", False, [{"txid": outpoint["txid"], "vout": outpoint["vout"]}]):
        raise SwapError("the node would not lock the output")
    return outpoint


def _unsettled(index: Any, offer: dict) -> bool:
    """Whether a sold offer's transaction is still out of the index.

    An index that cannot answer is treated as not having it: holding an item
    back for one more block costs a buyer nothing, and offering it twice
    costs them a fee.
    """
    try:
        return index.transaction(offer["txid"]) is None
    except Exception:
        return True


def _bid_row(row: Any) -> dict:
    out = dict(row)
    out["take"] = json.loads(out["take"]) if out["take"] else {}
    out["coins"] = json.loads(out["coins"]) if out.get("coins") else []
    return out


def make_offer(rpc: Any, index: Any, offers: Offers, network: str, shop_row: dict,
               listing_no: int, buyer: str, buyer_pubkey: str, own: list[str],
               cut: Any = None) -> dict:
    """Price one listing for one buyer, lock an output, and write it down.

    Everything that can be refused here is: a shop this wallet cannot sell
    from, a listing that is not there, a give the shop no longer holds, a
    take the buyer does not hold. What is left is an offer the buyer can act
    on as it stands.

    `cut` is this node's announced percentage (§1d) -- the node that made the
    offer is the one that did the matching and the pricing, so it is the one
    the trade pays, and the buyer pays it on top of the price rather than out
    of what the seller asked for.
    """
    announced = _cut(cut)          # refused before an output is locked for it
    seller = shop_row["owner"]
    if shop_row["creator"] != seller or seller not in own:
        raise SwapError("this wallet did not create this shop, or no longer holds it")
    if buyer == seller:
        raise SwapError("a shop cannot sell to its own wallet")
    if buyer in own:
        raise SwapError("the buyer's address is in this wallet")
    shop = shop_of(shop_row)
    try:
        listing = shop["listings"][int(listing_no)]
    except (IndexError, TypeError, ValueError):
        raise SwapError(f"there is no listing {listing_no}") from None

    expire(rpc, offers, network)
    # What is spoken for: the offers still open, and the ones already sold
    # whose transaction has not been indexed yet. Between broadcast and its
    # block the ledger still names the seller as the owner of what was sold,
    # so without the second half the same item is offered to a second buyer.
    # The engine refuses that swap when it lands -- nothing moves -- but the
    # buyer has paid a message fee to be told no (D-033).
    standing = offers.open_offers(network) + [
        offer for offer in offers.sold_offers(network) if _unsettled(index, offer)]
    locked_items = {o["give"]["txid"] for o in standing if o["give"].get("kind") == "inscription"}
    locked_outs = {(o["outpoint"]["txid"], o["outpoint"]["vout"]) for o in standing}

    give_spec = listing["give"]
    if "collection" in give_spec:
        item = pick_item(index, shop_row["creator"], str(give_spec["collection"]), seller,
                         exclude=locked_items)
        give = I.Leg(I.LEG_INSCRIPTION, txid=bytes.fromhex(item["txid"]))
    else:
        give = leg_of(give_spec, index)
        if give.kind == I.LEG_INSCRIPTION and give.txid.hex() in locked_items:
            raise SwapError("that item is already offered to somebody else")
    take = leg_of(listing["take"], index)
    for who, leg, name in ((seller, give, "the shop"), (buyer, take, "the buyer")):
        problem = holds(index, rpc, who, leg)
        if problem:
            raise SwapError(f"{name} cannot give that: {problem}")
    if give.kind == I.LEG_TOKEN and take.kind == I.LEG_TOKEN \
            and give.property_id == take.property_id:
        raise SwapError("a swap of a token for itself is not a swap")

    outpoint = _lock_output(rpc, seller, give, take, locked_outs)
    now = time.time()
    offer = {"id": secrets.token_hex(8), "network": network, "shop": shop_row["txid"],
             "listing": int(listing_no), "seller": seller, "buyer": buyer,
             "buyer_pubkey": buyer_pubkey, "give": leg_json(give, index),
             "take": leg_json(take, index), "outpoint": outpoint, "cut": announced,
             "created": now, "expires": now + OFFER_TTL}
    offers.add(offer)
    return public(offer)


def offer_for_order(rpc: Any, index: Any, offers: Offers, network: str,
                    order_txid: str, tokens: int, buyer: str, buyer_pubkey: str,
                    own: list[str], cut: Any = None) -> dict:
    """The maker's half of a swap that fills one of its own standing orders.

    This is what a node answers when somebody asks to take a price off its
    book. The taker needs one thing it cannot work out alone -- which of the
    maker's outputs will carry the swap -- and gets it here, in an offer it
    can build on. Publishing that outpoint with the order instead would save
    nothing: the maker has to be online to sign either way, and an outpoint
    named an hour ago may be spent by the time anybody takes it (D-063).

    Everything the shop path checks is checked here, plus the two that are
    particular to a book: the price is the maker's own, taken from the order
    rather than from anything the taker said, and what is already promised
    out of this order to other buyers is subtracted before more is offered.
    The engine will check the finished transaction again (D-062), and an
    offer that cannot become one is a message fee spent on a refusal.

    `cut` is this node's percentage, announced the same way a shop's offer
    announces it (§1d). A fill paid in tokens carries nothing: there is no
    coin amount on this side to add a percentage to, and taking one out of
    what either side hands over is the thing §1d rules out.
    """
    announced = _cut(cut)
    expire(rpc, offers, network)
    order = index.order(str(order_txid))
    if order is None:
        raise SwapError("no such order on this node -- it may have been "
                        "cancelled, filled, or not yet mined")
    seller = order["address"]
    if seller not in own:
        raise SwapError("that order is not this wallet's to fill")
    if buyer == seller or buyer in own:
        raise SwapError("a wallet cannot fill its own order")
    a_bid = order["sale_property"] == 0 and order["want_property"] != 0
    if order["want_property"] != 0 and not a_bid:
        raise SwapError("an order is filled in coins or in tokens, not both")

    standing = offers.open_offers(network) + [
        offer for offer in offers.sold_offers(network) if _unsettled(index, offer)]
    tokens = int(tokens)
    if tokens <= 0:
        raise SwapError("a fill needs an amount")
    if a_bid:
        # The mirror: this wallet's BID is being filled, so it gives coins and
        # takes tokens. Both sides of a crossing book can be taken now, and
        # the one that acts is whichever side is not resting (D-118).
        promised = sum(int(o["take"].get("units") or 0) for o in standing
                       if o.get("order") == str(order_txid))
        left = order["want_amount"] - promised
        if tokens > left:
            raise SwapError(
                f"that bid has {left} left of {order['want_amount']}"
                + (" -- the rest is promised to other sellers until their "
                   "offers expire" if promised else ""))
        # Rounded DOWN, so the maker never pays more per token than it bid.
        # The engine's own guard is the same arithmetic from the other side.
        coins = order["sale_amount"] * tokens // order["want_amount"]
        if coins <= 0:
            raise SwapError("that much of this bid comes to less than a satoshi")
        give = I.Leg(I.LEG_COINS, amount=coins)
        take = I.Leg(I.LEG_TOKEN, property_id=order["want_property"], amount=tokens)
    else:
        promised = sum(int(o["give"].get("units") or 0) for o in standing
                       if o.get("order") == str(order_txid))
        left = order["sale_amount"] - promised
        if tokens > left:
            raise SwapError(
                f"that order has {left} left of {order['sale_amount']}"
                + (" -- the rest is promised to other buyers until their offers "
                   "expire" if promised else ""))

        # The maker's own price, from the order, rounded so the maker is never
        # paid less than it asked: the engine's guard is the same arithmetic
        # (D-062), and an offer that rounded the other way would be refused by
        # the chain after both wallets had signed it.
        coins = -(-order["want_amount"] * tokens // order["sale_amount"])
        give = I.Leg(I.LEG_TOKEN, property_id=order["sale_property"], amount=tokens)
        take = I.Leg(I.LEG_COINS, amount=coins)
    # Neither side's balance is checked here, and both for reasons rather
    # than by omission. The maker's tokens are in the reserve this order
    # holds, which is exactly where the fill takes them from -- the free
    # balance says nothing. The buyer's coins are paid inside the swap
    # itself, so the buyer either builds a transaction that covers them or
    # fails to build one.
    locked_outs = {(o["outpoint"]["txid"], o["outpoint"]["vout"]) for o in standing}
    outpoint = _lock_output(rpc, seller, give, take, locked_outs)
    now = time.time()
    offer = {"id": secrets.token_hex(8), "network": network, "shop": "",
             "order": str(order_txid), "listing": -1, "seller": seller,
             "buyer": buyer, "buyer_pubkey": buyer_pubkey,
             "give": leg_json(give, index), "take": leg_json(take, index),
             "outpoint": outpoint, "cut": announced,
             "created": now, "expires": now + OFFER_TTL}
    offers.add(offer)
    return public(offer)


def offer_for_bid(rpc: Any, index: Any, offers: Offers, network: str,
                  bid: dict, own: list[str], cut: Any = None) -> dict:
    """The seller's half of a swap, for an offer somebody made on an NFT.

    The same offer a shop would make, for an item nobody listed: the holder
    accepting is the listing. Everything a shop's offer is checked for is
    checked here too -- the item is still theirs, the buyer holds what they
    promised, an output is locked to carry it -- because the engine will
    check the transaction again and an offer that cannot become one is a
    fee spent on a refusal (D-038). `cut` is this node's percentage, as in
    the other two makers (§1d).
    """
    announced = _cut(cut)
    expire(rpc, offers, network)
    row = index.inscription(str(bid["inscription"]))
    if row is None:
        raise SwapError("no such inscription on this node")
    seller = row["owner"]
    if seller not in own:
        raise SwapError("that is not yours to sell")
    standing = offers.open_offers(network) + [
        offer for offer in offers.sold_offers(network) if _unsettled(index, offer)]
    already = [o for o in standing if o["give"].get("kind") == "inscription"
               and o["give"]["txid"] == row["txid"]]
    if already:
        # Usually this IS the same buyer pressing Accept twice, because the
        # page drew the button again. Say which it is: "somebody else" when
        # it is somebody else, and "you already accepted this" when it is
        # not (D-049).
        held = already[0]
        when = time.strftime("%H:%M", time.localtime(held["expires"]))
        if held["buyer"] == str(bid.get("buyer") or ""):
            raise SwapError(
                f"you have already accepted this offer -- it is reserved for "
                f"{held['buyer']} until {when}, and their wallet has to sign "
                f"before it can go")
        raise SwapError(
            f"that item is offered to somebody else until {when}")
    locked_outs = {(o["outpoint"]["txid"], o["outpoint"]["vout"]) for o in standing}

    give = I.Leg(I.LEG_INSCRIPTION, txid=bytes.fromhex(row["txid"]))
    take = leg_from_json(bid["take"])
    buyer = str(bid["buyer"])
    for who, leg, name in ((seller, give, "you"), (buyer, take, "the buyer")):
        problem = holds(index, rpc, who, leg)
        if problem:
            raise SwapError(f"{name} cannot give that: {problem}")
    outpoint = _lock_output(rpc, seller, give, take, locked_outs)
    now = time.time()
    offer = {"id": secrets.token_hex(8), "network": network,
             "shop": row["txid"],           # the item stands in for the shop
             "listing": 0, "seller": seller, "buyer": buyer,
             "buyer_pubkey": str(bid.get("peer_pubkey", "")),
             "give": leg_json(give, index), "take": leg_json(take, index),
             "outpoint": outpoint, "cut": announced,
             "created": now, "expires": now + OFFER_TTL}
    offers.add(offer)
    return public(offer)


def expire(rpc: Any, offers: Offers, network: str) -> int:
    """Close offers past their time, and fills nobody ever answered.

    An order rests on the book whether or not the node behind it can still
    answer. A taker who asks a node that is down gets silence: the note sits
    at "waiting for their node" for ever, and the book goes on advertising
    what cannot be traded. Silence is the one answer a person cannot act on,
    so it becomes a refusal with a reason after the offer's own lifetime
    (D-094).
    """
    closed = 0
    for offer in offers.open_offers(network):
        if offer["expires"] <= time.time():
            offers.close(offer["id"], "expired")
            _unlock(rpc, offer)
            closed += 1
    for fill in offers.stale_fills(network, time.time()):
        offers.close_fill(
            fill["id"], "unanswered",
            error="their node never answered. The order may still be on the "
                  "book -- an order stands whether or not the wallet behind "
                  "it is running -- so try again, or take another at the "
                  "same price")
        closed += 1
    return closed


def _unlock(rpc: Any, offer: dict) -> None:
    try:
        rpc.call("lockunspent", True, [{"txid": offer["outpoint"]["txid"],
                                        "vout": offer["outpoint"]["vout"]}])
    except Exception:
        pass        # spent already, or the node restarted and forgot the lock


def countersign(rpc: Any, index: Any, offers: Offers, offer: dict, hex_: str) -> str:
    """Check the buyer's half against the offer, sign the seller's, broadcast.

    The seller signs nothing it did not offer. Its own outpoint must be the
    first input (that makes it the sender, and so the seller, to the engine);
    no other input may be the seller's; the OP_RETURN must carry exactly the
    two legs offered; and the seller's outputs must return its input plus
    what the buyer owes in coins. What the buyer does with its own inputs and
    change is the buyer's business -- the engine holds the buyer to its leg.
    """
    if offer["status"] != "open":
        raise SwapError(f"that offer is {offer['status']}")
    if offer["expires"] <= time.time():
        offers.close(offer["id"], "expired")
        _unlock(rpc, offer)
        raise SwapError("that offer has expired")
    try:
        decoded = rpc.call("decoderawtransaction", str(hex_))
    except Exception as exc:
        raise SwapError(f"that is not a transaction: {exc}") from None
    vin = decoded.get("vin") or []
    if len(vin) < 2:
        raise SwapError("a swap has the seller's input and the buyer's")
    first = vin[0]
    if first.get("txid") != offer["outpoint"]["txid"] \
            or int(first.get("vout", -1)) != offer["outpoint"]["vout"]:
        raise SwapError("the seller's output is not the first input")
    seller, buyer = offer["seller"], offer["buyer"]
    for n, entry in enumerate(vin[1:], 1):
        prev = rpc.call("gettxout", entry.get("txid"), int(entry.get("vout", -1)), True)
        if not prev:
            raise SwapError(
                f"input {n} of the buyer's half is spent or unknown here: either "
                f"that wallet spent it on something else since the offer was "
                f"made, or this node has not seen the block it is in. Ask for "
                f"another offer and it will be built from what is there now")
        addresses = prev.get("scriptPubKey", {}).get("addresses") or []
        if seller in addresses:
            raise SwapError(f"input {n} is the seller's; only the offered output may be")
        if n == 1 and buyer not in addresses:
            raise SwapError("the first input after the seller's is not the buyer's")

    give, take = leg_from_json(offer["give"]), leg_from_json(offer["take"])
    named = offer.get("order") or ""
    swaps, outs = [], []
    for out in decoded.get("vout") or []:
        script = out.get("scriptPubKey", {})
        if script.get("type") == "nulldata":
            swaps.append(_swap_in(script.get("hex", "")))
            continue
        outs.append((int(round(float(out.get("value", 0)) * COIN)),
                     script.get("addresses") or [], script.get("hex", "")))
    paid = sum(value for value, addresses, _ in outs if seller in addresses)
    if len(swaps) != 1:
        raise SwapError("a swap has exactly one OP_RETURN")
    swap = swaps[0]
    if swap is None or swap.give != give or swap.take != take:
        raise SwapError("the transaction does not carry the legs that were offered")
    # Which order this fills is one of the terms, so the seller checks it like
    # the others. A buyer that named a different order, or none, would be
    # asking this wallet to sign a private sale beside its own advertisement
    # rather than a fill of it -- the same amount of tokens, a different thing
    # (D-082).
    if (getattr(swap, "order", b"") or b"").hex() != named:
        raise SwapError("the transaction does not fill the order that was offered")
    owed = offer["outpoint"]["value"] + coins_in(take) - coins_in(give)
    if paid < owed:
        raise SwapError(f"the seller is paid {paid / COIN:.8f}, not the "
                        f"{owed / COIN:.8f} the offer says")
    # And the cut the offer announced, checked as a term rather than put up
    # with. A seller that signed whatever came back would be signing away its
    # own percentage without noticing, which is the same blindness §1d asks
    # the buyer's side not to have -- and an output nobody counted is an
    # output nobody agreed to.
    want = cut_sats(offer.get("cut"), max(coins_in(take) - coins_in(give), 0))
    if want:
        to = str((offer.get("cut") or {}).get("to") or "")
        try:
            pays = p2pkh_script(to).hex()
        except Exception:
            raise SwapError(f"the cut this offer announced cannot be checked: "
                            f"{to or 'no address'} is not one") from None
        # By script, not by address: the same payment spelled in another chain's
        # alphabet is still this payment, and this is what was offered.
        if not any(value == want and script_hex == pays for value, _, script_hex in outs):
            raise SwapError(
                f"the transaction does not carry the cut this offer announced "
                f"({want / COIN:.8f} to {to}). Nothing is signed until it is "
                f"there -- if their wallet is an older release, that is why")
    for who, leg in ((seller, give), (buyer, take)):
        if who == seller and named and leg.kind == I.LEG_TOKEN:
            continue          # backed by the order's reserve, not the balance
        problem = holds(index, rpc, who, leg)
        if problem:
            raise SwapError(problem)

    signed = rpc.call("signrawtransaction", str(hex_))
    if not signed.get("complete"):
        raise SwapError(f"the transaction is still not complete after the seller "
                        f"signed: {signed.get('errors')}")
    txid = str(rpc.call("sendrawtransaction", signed["hex"]))
    offers.close(offer["id"], "sent", txid=txid)
    _unlock(rpc, offer)     # the lock has done its job; the outpoint is spent
    return txid


def _swap_in(script_hex: str) -> I.Swap | None:
    """The swap an OP_RETURN script carries, or None if it carries something else."""
    try:
        raw = bytes.fromhex(script_hex)
        if not raw or raw[0] != 0x6a:
            return None
        at, push = 1, raw[1]
        if push == 0x4c:
            at, push = 3, raw[2]
        else:
            at = 2
        data = decode_class_c(raw[at:at + push])
        if data is None:
            return None
        parsed = P.decode(data)
        if not isinstance(parsed, P.AnyData):
            return None
        found = I.parse(parsed.data)
        return found if isinstance(found, I.Swap) else None
    except Exception:
        return None


# --- the buyer's side ---------------------------------------------------------

@dataclass
class Built:
    """The buyer's half: signed by the buyer, waiting for the seller."""

    hex: str
    txid: str               # of the half-signed transaction; the seller's signature changes it
    fee_sats: int
    size: int
    buyer: str
    seller: str
    give: dict
    take: dict
    outputs: list[dict] = field(default_factory=list)
    #: What the offer's maker takes on this trade, as {bps, sats, to} -- empty
    #: when there is none. It is shown, never folded into anything else,
    #: because a fee somebody did not see is a fee they did not agree to (§1d).
    cut: dict = field(default_factory=dict)

    @property
    def what(self) -> str:
        return f"{describe_leg(self.take)} for {describe_leg(self.give)}"

    @property
    def cut_coins(self) -> float:
        return int(self.cut.get("sats") or 0) / COIN


def check_offer(offer: Any, *, shop: str, own: list[str], height: int | None,
                params: Any) -> dict:
    """An offer as a page handed it back, checked before it is shown to anyone."""
    if not isinstance(offer, dict):
        raise SwapError("an offer is an object")
    try:
        out = {"id": str(offer["id"]), "network": str(offer["network"]),
               "shop": str(offer["shop"]), "listing": int(offer["listing"]),
               "seller": str(offer["seller"]), "buyer": str(offer["buyer"]),
               "give": dict(offer["give"]), "take": dict(offer["take"]),
               "outpoint": {"txid": str(offer["outpoint"]["txid"]),
                            "vout": int(offer["outpoint"]["vout"]),
                            "value": int(offer["outpoint"]["value"])},
               "order": str(offer.get("order") or ""),
               "created": float(offer["created"]), "expires": float(offer["expires"])}
    except (KeyError, TypeError, ValueError):
        raise SwapError("that is not an offer") from None
    if out["shop"] != shop:
        raise SwapError("that offer is from a different shop")
    if out["buyer"] not in own:
        raise SwapError(f"the offer is to {out['buyer']}, which this wallet cannot sign for")
    if out["seller"] in own:
        raise SwapError("the seller's address is in this wallet")
    if out["expires"] <= time.time():
        raise SwapError("that offer has expired; ask for another")
    if params.swaps_from is None:
        raise SwapError("swaps are not read on this chain")
    if height is not None and height < params.swaps_from:
        raise SwapError(f"swaps are read from block {params.swaps_from:,}; "
                        f"the chain is at {height:,}")
    leg_from_json(out["give"]), leg_from_json(out["take"])
    # A cut is optional, and an offer from an older node has none. What one
    # that carries it says has to be a rate inside the ceiling and an address
    # this chain could actually pay: the buyer is about to be shown a fee and
    # asked to sign it, and neither of those is worth doing on a number that
    # cannot be spent -- or one that turns the transaction into a fee to an
    # address on another network.
    out["cut"] = {}
    said = offer.get("cut")
    if isinstance(said, dict) and said.get("bps"):
        try:
            bps = int(said["bps"])
        except (TypeError, ValueError):
            raise SwapError(f"the cut in that offer is not a rate: {said['bps']!r}") from None
        if not 0 < bps <= CUT_CEILING:
            raise SwapError(f"the cut in that offer is {bps} basis points, and "
                            f"{CUT_CEILING} is as much as a node may ask")
        to = str(said.get("to") or "")
        try:
            version, hash160 = b58check_decode(to)
        except Exception:
            raise SwapError("the cut in that offer names "
                            f"{to or 'no address'}, which is not an address") from None
        if version != params.pubkeyhash_version or len(hash160) != 20:
            raise SwapError("the cut in that offer names an address that is not "
                            "this chain's")
        out["cut"] = {"bps": bps, "to": to}
    return out


@dataclass
class Terms:
    """What an offer demands of the transaction that fills it, read once.

    An offer is a promise about a trade, and the transaction that becomes it
    has to be worked out from the offer's own numbers. Two readings of those
    numbers is a trade one side agreed to and the other did not, so there is
    one function that reads them: `build`, which finishes it out of a wallet
    whose keys are here, and an account's route, which finishes it out of a
    transaction a browser holds the key to, go through the same lines.

    `foreign` is the seller's offered output, which goes first and stays
    unsigned by whoever builds this -- and `outputs` is everything the buyer's
    side must place before its own change: the bytes, the seller made whole,
    and the cut the offer announced.
    """

    give: I.Leg
    take: I.Leg
    buyer: str
    seller: str
    owes: int             # coins buyer to seller, which may be negative
    cut: int              # sats for the offer's maker's node, on top
    cut_to: str
    cut_script: bytes
    seller_out: int
    named: str            # the order this fills, hex; "" for a shop swap
    payload: bytes
    lines: int
    foreign: list[dict]
    outputs: list[tuple[int, bytes]]


def offer_terms(rpc: Any, index: Any, offer: dict, own: list[str],
                from_order: dict | None = None) -> Terms:
    """Every check and every number in an offer, before anybody builds on it.

    `own` is the addresses the caller can sign for, and it is only used to say
    which side of the trade the caller is on: a wallet's own legs are checked
    against what that wallet holds, and the other side's against what the
    order reserves. That is the whole reason this is a function and not a few
    lines inside `build` -- the account that takes a price off the book holds a
    key and no wallet, so the caller is a browser tab, and a tab is owed the
    same refusal as a node before it spends a fee on a trade that cannot land.
    """
    buyer, seller = offer["buyer"], offer["seller"]
    give, take = leg_from_json(offer["give"]), leg_from_json(offer["take"])
    for who, leg, name in ((seller, give, "the shop"), (buyer, take, "this wallet")):
        if who == seller and from_order is not None and leg.kind == I.LEG_TOKEN:
            # Filling a standing order. The seller's tokens are in the reserve
            # that order holds, not in its free balance -- `holds` reads the
            # free balance and would refuse every fill ever made. What has to
            # be true is that THIS order holds THESE tokens, which is what the
            # engine checks for itself when the swap lands (D-062).
            #
            # All four conditions, not just the amount. A test machine found this
            # checking only `leg.amount > reserved`, which reads as "some
            # order holds enough units of something" -- the tokens it
            # authorised spending did not have to be the tokens that order
            # reserved, or even the same property. Nothing could reach it
            # through today's caller, because `_fill` fetches the order from
            # its own note; but a guard that is correct only because of who
            # calls it is a guard that breaks when somebody else calls it.
            if str(from_order.get("address") or "") != seller:
                raise SwapError("that order is not the seller's")
            if int(from_order.get("sale_property") or 0) != leg.property_id:
                raise SwapError(
                    f"that order sells property {from_order.get('sale_property')}, "
                    f"not {leg.property_id}")
            named = str(offer.get("order") or "")
            if named and named != str(from_order.get("txid") or ""):
                raise SwapError("that is not the order this offer is for")
            if leg.amount > int(from_order.get("reserved") or 0):
                raise SwapError(
                    f"that order holds {from_order.get('reserved')} of property "
                    f"{leg.property_id}, not {leg.amount}")
            continue
        problem = holds(index, rpc, who, leg)
        if problem:
            raise SwapError(f"{name} cannot give that: {problem}")
    owes = coins_in(take) - coins_in(give)          # buyer to seller, may be negative
    seller_out = offer["outpoint"]["value"] + owes
    if seller_out < MIN_CHANGE:
        raise SwapError("the seller's output would be dust; ask for another offer")
    # What the offer announced for its maker's node (§1d), worked out here
    # from the price rather than taken from anywhere: the same arithmetic the
    # seller will do when it countersigns, on the same numbers, so the two
    # nodes cannot disagree about a term they both read from the same offer.
    cut = cut_sats(offer.get("cut"), max(owes, 0))
    cut_to = str((offer.get("cut") or {}).get("to") or "") if cut else ""
    cut_script = b""
    if cut:
        try:
            cut_script = p2pkh_script(cut_to)
        except Exception:
            raise SwapError("the cut this offer announces cannot be paid: "
                            f"{cut_to or 'no address'} is not one") from None
    lines = 3 + (1 if cut else 0)

    # Name the order this fills, so the engine takes it from the book rather
    # than from whatever the seller happens to hold loose (D-082). Empty for
    # every other swap, which is then encoded exactly as it was before.
    named = offer.get("order") or ""
    payload = P.AnyData(data=I.Swap(
        give=give, take=take,
        order=bytes.fromhex(named) if named else b"").encode()).encode()
    outputs = [(0, op_return_script(encode_class_c(payload))),
               (seller_out, p2pkh_script(seller))]
    if cut:
        outputs.append((cut, cut_script))
    return Terms(give=give, take=take, buyer=buyer, seller=seller, owes=owes,
                 cut=cut, cut_to=cut_to, cut_script=cut_script,
                 seller_out=seller_out, named=named, payload=payload,
                 lines=lines, outputs=outputs,
                 foreign=[dict(offer["outpoint"], address=seller)])


def build(rpc: Any, index: Any, offer: dict, own: list[str],
          from_order: dict | None = None) -> Built:
    """The buyer's transaction, signed by the buyer only.

    The seller's outpoint first, then the buyer's own outputs, from the one
    address that pays and receives. Outputs: the swap, the seller made whole
    (its input back, plus the coins it is owed, less any it gives), the cut of
    whatever the offer announced for the node that made it, and the buyer's
    change. The buyer pays the fee: the buyer is the one asking, and it pays
    the announced cut on top of the price rather than out of it, so the price
    keeps meaning what the seller asked for (§1d).

    What the offer says and what this trade costs are `offer_terms`'s, so a
    tab that holds the buyer's key instead of a wallet can be shown the same
    transaction; what is left here is the coin selection and the signature,
    which only a node that holds this buyer's coins can do."""
    buyer, seller = offer["buyer"], offer["seller"]
    if buyer not in own:
        raise SwapError(f"{buyer} is not this wallet's")
    terms = offer_terms(rpc, index, offer, own, from_order)
    owes, cut, cut_to, cut_script = (terms.owes, terms.cut, terms.cut_to,
                                     terms.cut_script)
    lines, payload = terms.lines, terms.payload
    unspent = sorted((u for u in (rpc.call("listunspent", 1, 9_999_999, [buyer]) or [])
                      if u.get("spendable", True)),
                     key=lambda u: -float(u["amount"]))
    chosen: list[tuple[str, int]] = []
    total = 0
    for utxo in unspent:
        chosen.append((utxo["txid"], int(utxo["vout"])))
        total += int(round(float(utxo["amount"]) * COIN))
        fee = _fee(len(chosen) + 1, lines, len(payload))
        if total - max(owes, 0) - cut - fee >= 0:
            break
    else:
        need = max(owes, 0) + cut + _fee(len(chosen) + 1, lines, len(payload))
        raise SwapError(f"{buyer} holds {total / COIN:.8f} spendable, and this swap "
                        f"needs {need / COIN:.8f} (what is owed plus the fee)")
    # What is left for the MESSAGE that carries this half. The signed half
    # travels as a node-to-node message, which is a transaction paying its
    # own fee out of this same address; with every coin spent here there is
    # nothing left to send it with, and locking the inputs (below) only turns
    # a silent failure into a stuck one. Say so now, while it can be fixed.
    spare = [u for u in unspent if (u["txid"], int(u["vout"])) not in set(chosen)]
    if not spare:
        raise SwapError(
            f"{buyer} has its coins in {len(unspent)} output"
            f"{'' if len(unspent) == 1 else 's'} and this swap needs "
            f"{'it' if len(unspent) == 1 else 'all of them'}, leaving nothing to "
            f"pay for the message that carries it. Split the address into a few "
            f"outputs first -- Wallet, Fast sending -- and ask for another offer")

    fee = _fee(len(chosen) + 1, lines, len(payload))
    change = total - owes - cut - fee
    outputs = list(terms.outputs)
    if change >= MIN_CHANGE:
        outputs.append((change, p2pkh_script(buyer)))
    else:
        fee += max(change, 0)       # too small to be worth an output; the miner has it
    raw = build_raw_tx([(offer["outpoint"]["txid"], offer["outpoint"]["vout"])] + chosen,
                       outputs)
    # Hold what this half spends. The offer and the signed half travel as
    # messages, which take blocks and cost fees out of this same wallet, so
    # without this the wallet can spend its own swap input on the very
    # message that carries the swap -- and the seller, checking a block
    # later, finds the input gone (D-044).
    try:
        rpc.call("lockunspent", False,
                 [{"txid": txid, "vout": vout} for txid, vout in chosen])
    except Exception:
        pass            # a node that will not lock is not a reason to refuse
    signed = rpc.call("signrawtransaction", raw)
    for problem in signed.get("errors") or []:
        if int(problem.get("vout", -1)) != offer["outpoint"]["vout"] \
                or problem.get("txid") != offer["outpoint"]["txid"]:
            raise SwapError(f"the wallet could not sign its own input: {problem.get('error')}")
    if signed.get("complete"):
        raise SwapError("the wallet signed the seller's input too: the seller is this wallet")
    decoded = rpc.call("decoderawtransaction", signed["hex"])
    shown = []
    for out in decoded.get("vout") or []:
        script = out.get("scriptPubKey", {})
        addresses = script.get("addresses") or []
        where = addresses[0] if addresses else script.get("type", "unknown")
        # The cut is recognised by its script, not by its address: an address
        # spelled in another chain's alphabet is the same payment, and this is
        # the output this builder put there.
        shown.append({"value": float(out.get("value", 0)), "where": where,
                      "is_change": bool(addresses) and addresses[0] == buyer,
                      "is_recipient": bool(addresses) and addresses[0] == seller,
                      "is_cut": bool(cut) and script.get("hex", "") == cut_script.hex()})
    return Built(hex=signed["hex"], txid=decoded["txid"], fee_sats=fee,
                 size=len(signed["hex"]) // 2, buyer=buyer, seller=seller,
                 give=offer["give"], take=offer["take"], outputs=shown,
                 cut={"bps": int((offer.get("cut") or {}).get("bps") or 0),
                      "sats": cut, "to": cut_to} if cut else {})


def _fee(inputs: int, outputs: int, payload_len: int) -> int:
    """FEE_PER_KB for each started kilobyte of the signed transaction, sized
    in advance: 148 bytes an input, 34 an output, the OP_RETURN as long as
    its payload plus the marker and pushes."""
    size = 10 + 148 * inputs + 34 * outputs + payload_len + 8
    return math.ceil(size / 1000) * FEE_PER_KB
