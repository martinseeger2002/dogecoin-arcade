"""Half a swap, signed somewhere else, that this node is holding.

`arcade/swap.py` keeps the book of offers this wallet made with its own keys. An
`offer` row means the node can sign, which means it holds a private key. A
listing is the opposite state, and the row has to look different because of it:
an account signed a one-input, one-output transaction in its own browser
(`funding.build_leg`), handed the signature here, and closed the tab. This node
never had the key and never will, so it cannot make that piece unsellable again,
cannot move it, and cannot promise anybody that the seller still means it. That
is the whole difference, and it is why this is its own table in its own file
rather than `offer` with a column, and why finishing one is `paste_leg` and not
`countersign` -- "the node signed it" and "the node pasted what somebody else
signed" must not be the same word.

Two things are true of every row here and both belong on any page that shows one:

* the book holds nothing. Deleting a row does not unlock a piece, because a
  signature that already left this node does not know it was withdrawn. A
  listing is cancelled by SPENDING its input, and until a block has done that
  the honest word for what happened here is "withdrawn", not "cancelled".
* `expires` is a promise from this node and not a term of the signature. There
  is no lock time in a leg: what the chain enforces is the payment at output 0,
  and the expiry only says when this node stops putting the leg in front of a
  buyer. A buyer who turns up after it with a signature it was given earlier
  can still complete the swap, and that is a fact about pre-signed
  transactions, not a bug in this table.

What is stored is the signed leg itself -- one transaction, one input, one
output -- so the piece cannot be described differently to two people, and every
number a buyer is shown is read off the bytes that were signed rather than from
a column that could have drifted.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import fees
from .db import add_missing_columns
from .funding import SINGLE_ANYONECANPAY, Leg
from .script import b58check_decode, hash160, iter_pushes
from .txbuild import p2pkh_script, push, varint

COIN = 100_000_000

#: What a listing is worth completing at, per virtual kB. A leg reserves its
#: own fee when it is signed, so this is the floor the finished swap has to
#: clear, and it is `-blockmintxfee` rather than `-minrelaytxfee` on purpose:
#: a swap that relays and never gets into a block has promised two people a
#: trade and delivered neither.
FEE_FLOOR_PER_KB = fees.MIN_FEE_PER_KB


class ListingError(Exception):
    """Something that cannot be listed, completed, or believed. Shown as written."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS listing (
    id          TEXT PRIMARY KEY,
    network     TEXT NOT NULL,
    owner       TEXT NOT NULL,
    -- The signed leg: one input, one output, one scriptSig. Everything else in
    -- this row is read off it, and `check_leg` says so by checking.
    leg         TEXT NOT NULL,
    in_txid     TEXT NOT NULL,
    in_vout     INTEGER NOT NULL,
    in_value    INTEGER NOT NULL,
    out_value   INTEGER NOT NULL,
    out_script  TEXT NOT NULL,
    fee         INTEGER NOT NULL,
    price       INTEGER NOT NULL,
    what        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open',
    created     REAL NOT NULL,
    expires     REAL NOT NULL,
    -- The transaction that spent the listed piece: the swap that filled this,
    -- or the spend that cancelled it. Which one is `status`'s business.
    spent_by    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS listing_open ON listing(status, network);
CREATE INDEX IF NOT EXISTS listing_piece ON listing(in_txid, in_vout);
"""


def _serialise(inputs: list[tuple[str, int, bytes]],
               outputs: list[tuple[int, bytes]],
               version: int = 1, locktime: int = 0) -> str:
    """Transaction bytes, with the scriptSigs that go in them.

    `funding.assemble` puts one address's signatures into a transaction it
    built; a listing needs a scriptSig it was *given* placed at a named input,
    which is a different thing and wants its own serialiser rather than a
    flag on someone else's. Same bytes either way, and both hardcode the
    sequence number that `funding.sighash` hashes, because a combiner that
    disagreed with the hasher about four bytes would be signing one
    transaction and broadcasting another.
    """
    raw = version.to_bytes(4, "little") + varint(len(inputs))
    for txid, vout, script_sig in inputs:
        raw += bytes.fromhex(txid)[::-1] + int(vout).to_bytes(4, "little")
        raw += varint(len(script_sig)) + script_sig + b"\xff\xff\xff\xff"
    raw += varint(len(outputs))
    for value, script in outputs:
        raw += value.to_bytes(8, "little") + varint(len(script)) + script
    raw += locktime.to_bytes(4, "little")
    return raw.hex()


def sign_leg(leg: Leg, signature: str, pubkey: bytes) -> str:
    """The leg as it goes into the book: its own two bytes with one scriptSig.

    `signature` is DER plus the sighash byte, which is what a browser hands
    back and what `funding.sighash` hashed -- the type is part of the signature
    and the network reads it off the end, so it is stored, not remembered.
    """
    if leg.sighash_type != SINGLE_ANYONECANPAY:
        raise ListingError("a leg is signed with SINGLE|ANYONECANPAY and this "
                           "one says it was signed otherwise")
    return _serialise([(leg.input["txid"], leg.input["vout"],
                        push(bytes.fromhex(signature)) + push(pubkey))],
                      [leg.output])


class Listings:
    """Every leg this node is holding, open or closed, in one file.

    On disk, unlike the offers an account is signing right now
    (`web/account.Offers`), which are in memory because an offer that survived
    a restart would name coins chosen against an index that has moved. A
    listing is the other way round: a signature that outlives the request that
    made it is the entire point, so a restart must not forget it. What the
    restart genuinely invalidates is the piece's own status, and that is
    answered by asking the chain (`check_leg`), not by forgetting.
    """

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

    def add(self, listing: dict) -> dict:
        with self._open() as conn:
            conn.execute(
                "INSERT INTO listing(id, network, owner, leg, in_txid, in_vout, "
                "in_value, out_value, out_script, fee, price, what, status, "
                "created, expires) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (listing["id"], listing["network"], listing["owner"],
                 listing["leg"], listing["input"]["txid"],
                 int(listing["input"]["vout"]), int(listing["input"]["value"]),
                 int(listing["output"]["value"]), listing["output"]["script"],
                 int(listing["fee"]), int(listing["price"]),
                 str(listing.get("what") or ""),
                 str(listing.get("status") or "open"),
                 float(listing["created"]), float(listing["expires"])))
        return listing

    def from_leg(self, rpc: Any, leg: Leg, signature: str, pubkey: bytes,
                 network: str, owner: str, price: int, seconds: float) -> dict:
        """Write down a leg a browser just signed, after checking it.

        The check happens on the way IN rather than only on the way out because
        a row is a promise to a stranger: everything on a listing page is read
        from this table, and a row written from an unverified POST would put
        numbers on a public page that no signature backs.
        """
        raw = sign_leg(leg, signature, pubkey)
        listing = {
            "id": secrets.token_urlsafe(18), "network": network, "owner": owner,
            "leg": raw,
            "input": {"txid": leg.input["txid"], "vout": int(leg.input["vout"]),
                      "value": int(leg.input["value"])},
            "output": {"value": int(leg.output[0]),
                       "script": leg.output[1].hex()},
            "fee": int(leg.fee), "price": int(price), "what": leg.what,
            "status": "open", "created": time.time(),
            "expires": time.time() + seconds,
        }
        check_leg(rpc, listing)
        return self.add(listing)

    def get(self, listing_id: str) -> dict | None:
        with self._open() as conn:
            row = conn.execute("SELECT * FROM listing WHERE id = ?",
                               (str(listing_id),)).fetchone()
        return row_listing(dict(row)) if row else None

    def for_piece(self, txid: str, vout: int) -> list[dict]:
        """Every listing ever made of one piece, newest first.

        Asked by the piece rather than by the owner because the question the
        page answers is "is this already for sale, and did I already sign
        something for it" -- and a piece can have more than one row, which is
        exactly the thing § cancel-by-spending has to be honest about.
        """
        with self._open() as conn:
            rows = conn.execute(
                "SELECT * FROM listing WHERE in_txid=? AND in_vout=? "
                "ORDER BY created DESC", (str(txid), int(vout))).fetchall()
        return [row_listing(dict(r)) for r in rows]

    def open_listings(self, network: str, limit: int = 200) -> list[dict]:
        with self._open() as conn:
            rows = conn.execute(
                "SELECT * FROM listing WHERE network=? AND status='open' "
                "ORDER BY created DESC LIMIT ?",
                (network, int(limit))).fetchall()
        return [row_listing(dict(r)) for r in rows]

    def close(self, listing_id: str, status: str, spent_by: str = "") -> None:
        with self._open() as conn:
            conn.execute("UPDATE listing SET status=?, spent_by=? WHERE id=?",
                         (status, spent_by, str(listing_id)))

    def expire_due(self, network: str) -> int:
        """File off the listings whose expiry has passed. Nothing else happens.

        No unlock, because nothing was locked: the piece is still the seller's
        and still sellable by anything it already signed. `expired` is what
        this book has stopped offering, which is why the page has to say so.
        """
        with self._open() as conn:
            rows = conn.execute(
                "SELECT id FROM listing WHERE network=? AND status='open' "
                "AND expires<=?", (network, time.time())).fetchall()
            for row in rows:
                conn.execute("UPDATE listing SET status='expired' WHERE id=?",
                             (row["id"],))
        return len(rows)


def row_listing(row: dict) -> dict:
    """A database row, in the shape a leg travels in."""
    return {
        "id": row["id"], "network": row["network"], "owner": row["owner"],
        "leg": row["leg"],
        "input": {"txid": row["in_txid"], "vout": int(row["in_vout"]),
                  "value": int(row["in_value"])},
        "output": {"value": int(row["out_value"]), "script": row["out_script"]},
        "fee": int(row["fee"]), "price": int(row["price"]),
        "what": row["what"], "status": row["status"],
        "created": float(row["created"]), "expires": float(row["expires"]),
        "spent_by": row["spent_by"] or None,
    }


def public(listing: dict) -> dict:
    """What a buyer is shown: the terms, and none of this node's bookkeeping."""
    return {k: listing[k] for k in ("id", "network", "owner", "leg", "input",
                                    "output", "fee", "price", "what",
                                    "created", "expires")}


def check_leg(rpc: Any, listing: dict) -> dict:
    """Believe a listing, or say why not. Returns the decoded leg.

    Every check here exists because the row is what a page shows and what a
    buyer is asked to match. A leg whose bytes and columns disagree would let a
    node advertise a price no signature commits to -- which is the same lie as
    an unsigned listing, only harder to spot because the row looks checked.
    """
    owner = str(listing.get("owner") or "")
    try:
        wanted = p2pkh_script(owner).hex()
    except Exception:
        raise ListingError(f"{owner or 'nobody'} is not an address, so this "
                           f"listing has nobody who could have signed it") from None
    try:
        leg = rpc.call("decoderawtransaction", str(listing["leg"]))
    except Exception as exc:
        raise ListingError(f"that is not a transaction: {exc}") from None

    vin, vout = leg.get("vin") or [], leg.get("vout") or []
    if len(vin) != 1 or len(vout) != 1:
        raise ListingError(
            "a leg is one coin in and one payment out, and this is "
            f"{len(vin)} in and {len(vout)} out. A leg with anything else in it "
            "would commit to coins its seller has never seen")
    first, paid = vin[0], vout[0]
    piece = listing.get("input") or {}
    if str(first.get("txid") or "") != str(piece.get("txid") or "") \
            or int(first.get("vout", -1)) != int(piece.get("vout", -1)):
        raise ListingError("that leg does not promise the piece this listing "
                           "says it promises")
    out = listing.get("output") or {}
    value = int(round(float(paid.get("value", 0)) * COIN))
    script = str((paid.get("scriptPubKey") or {}).get("hex") or "")
    if value != int(out.get("value", -1)) or script != str(out.get("script") or ""):
        raise ListingError("the payment in the leg is not the payment this "
                           "listing advertises")
    if script != wanted:
        raise ListingError(f"a leg pays its seller at {owner} and this one pays "
                           f"somewhere else, so whoever signed it was not told "
                           f"where the coins would go")
    # The price is not in the signature and cannot be: the leg commits to the
    # seller's output, and what the seller paid for its own coin is arithmetic
    # from there. So the one number a row states for itself has to be the one
    # number the other three force, or the page is quoting a bargain nobody
    # signed.
    derived = value - int(piece.get("value", 0)) + int(listing.get("fee", 0))
    if int(listing.get("price", -1)) != derived:
        raise ListingError(
            f"this listing says {int(listing.get('price', -1)) / COIN:.8f} and "
            f"its own numbers say {derived / COIN:.8f}, so the price on it is "
            f"not the price its signature was made against")

    script_sig = str((first.get("scriptSig") or {}).get("hex") or "")
    if not script_sig:
        raise ListingError("that leg is unsigned, so nothing is promised by it")
    pushes = iter_pushes(bytes.fromhex(script_sig))
    if len(pushes) != 2 or len(pushes[1]) not in (33, 65):
        raise ListingError("a leg's scriptSig is a signature and a public key")
    if hash160(pushes[1]) != b58check_decode(owner)[1]:
        raise ListingError(
            "the key that signed this leg is not the key behind the address it "
            "is listed from, so this row would credit somebody's sale to "
            "somebody else")
    return leg


def paste_leg(rpc: Any, listing: dict, unsigned: Any, signatures: list[str],
              pubkey: bytes) -> str:
    """Finish a swap out of a stored leg. Checks first, bytes second.

    This is `swap.countersign` with the seller's half turned into something it
    was handed rather than something it signs, and the order of it is the
    point: nothing is combined until every term has been read off the leg
    itself. The buyer's signatures go in behind the leg's, at the indices
    `funding.build_partial` left empty for exactly this.

    Not called `assemble`, and not `assemble` with a flag. The difference
    between a node that signed a transaction and a node that pasted in a
    signature it was given is the security argument of the whole multi-user
    plan, and it should be findable in a name and in a stack trace.
    """
    leg = check_leg(rpc, listing)
    if float(listing["expires"]) <= time.time():
        when = time.strftime("%H:%M", time.localtime(float(listing["expires"])))
        raise ListingError(
            f"that listing expired at {when}. This node stopped offering it -- "
            f"if it handed a signature to a buyer before then, that signature "
            f"is still good until the piece is spent, and nothing here can "
            f"unsay it")
    if listing.get("status", "open") != "open":
        raise ListingError(f"that listing is {listing['status']}")

    piece = listing["input"]
    held = rpc.call("gettxout", piece["txid"], int(piece["vout"]), True)
    if not held:
        raise ListingError(
            f"{piece['txid'][:16]}…:{piece['vout']} is spent or unseen here, so "
            f"this piece is not this listing's to sell any more -- it sold, or "
            f"its owner spent it, which is the only way to cancel one")
    if int(round(float(held.get("value", 0)) * COIN)) != int(piece["value"]):
        raise ListingError(
            "that piece is not the size this listing says it is, so the price "
            "on it would be arithmetic done on a number that changed")

    if len(unsigned.inputs) < 2:
        raise ListingError("a swap has the listed piece and the buyer's coins")
    mine = unsigned.inputs[0]
    if mine["txid"] != piece["txid"] or int(mine["vout"]) != int(piece["vout"]):
        raise ListingError("the listed piece is not the first input, so the "
                           "seller would not be the one paying out of this "
                           "transaction")
    want = (int(listing["output"]["value"]),
            bytes.fromhex(listing["output"]["script"]))
    if unsigned.outputs[0] != want:
        raise ListingError(
            f"the transaction pays the seller {unsigned.outputs[0][0]}, not the "
            f"{want[0]} its own signature commits to")

    for n, coin in enumerate(unsigned.inputs[1:], 1):
        prev = rpc.call("gettxout", coin["txid"], int(coin["vout"]), True)
        if not prev:
            raise ListingError(
                f"input {n} is spent or unknown here: either that wallet spent "
                f"it on something else since this listing was built, or this "
                f"node has not seen the block it is in. Ask for another "
                f"transaction and it will be built from what is there now")
        if listing["owner"] in (prev.get("scriptPubKey", {})
                                    .get("addresses") or []):
            raise ListingError(
                f"input {n} belongs to the seller; a listing pays the seller "
                f"out of its own output, not out of its own wallet")

    raw = _combine(leg, unsigned, signatures, pubkey)
    decoded = rpc.call("decoderawtransaction", raw)
    total_in = int(listing["input"]["value"]) + sum(int(c["value"])
                                                    for c in unsigned.inputs[1:])
    total_out = sum(int(value) for value, _ in unsigned.outputs)
    floor = fees.fee_for(len(bytes.fromhex(raw)), fees.sigops_of(decoded),
                         FEE_FLOOR_PER_KB)
    if total_in - total_out < floor:
        raise ListingError(
            f"that leaves {total_in - total_out} sats for a block and it costs "
            f"{floor}. A leg reserves its fee when it is signed, so a buyer "
            f"that makes the finished transaction bigger than the listing "
            f"expected has to pay the difference out of its own change")
    return raw


def _combine(leg: dict, unsigned: Any, signatures: list[str],
             pubkey: bytes) -> str:
    """The finished swap: the leg's scriptSig, then the buyer's, one input each.

    The leg's scriptSig is taken from the decoded leg rather than sliced out of
    its bytes. It has already been checked by then, and there is no reason to
    have a second, hand-counted way of reading the same four fields.
    """
    script_sig = str((leg["vin"][0].get("scriptSig") or {}).get("hex") or "")
    if len(signatures) != len(unsigned.inputs) - 1:
        raise ListingError(
            f"{len(unsigned.inputs) - 1} signatures were needed and "
            f"{len(signatures)} came back")
    inputs = [(unsigned.inputs[0]["txid"], int(unsigned.inputs[0]["vout"]),
               bytes.fromhex(script_sig))]
    inputs += [(coin["txid"], int(coin["vout"]),
                push(bytes.fromhex(sig)) + push(pubkey))
               for coin, sig in zip(unsigned.inputs[1:], signatures)]
    return _serialise(inputs, unsigned.outputs)
