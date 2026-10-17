"""Half a swap, signed somewhere else, that this node is holding.

`arcade/swap.py` keeps the book of offers this wallet made with its own keys. An
`offer` row means the node can sign, which means it holds a private key. A
listing is the opposite state, and the row has to look different because of it:
an account built a leg in its own browser (`funding.build_leg`), signed it there
with a key this node was never shown, handed the signatures back, and closed the
tab. This node never had the key and never will, so it cannot make that piece
unsellable again, cannot move it, and cannot promise anybody that the seller
still means it. That is the whole difference, and it is why this is its own
table in its own file
rather than `offer` with a column, and why finishing one is `paste_leg` and not
`countersign` -- "the node signed it" and "the node pasted what somebody else
signed" must not be the same word.

Two things are true of every row here and both belong on any page that shows one:

* the book holds nothing. Deleting a row does not unlock a piece, because a
  signature that already left this node does not know it was withdrawn. A
  listing is cancelled by SPENDING its input, and until a block has done that
  the honest word for what happened here is "withdrawn", not "cancelled".
* `expires` is a promise from this node and not a term of the signature. There
  is no lock time in a leg: what the chain enforces is the output each of the
  leg's signatures stands over, and the expiry only says when this node stops
  putting the leg in front of a buyer. A buyer who turns up after it with a
  signature it was given earlier can still complete the swap, and that is a fact
  about pre-signed transactions, not a bug in this table.

What is stored is the signed leg itself -- its inputs, its outputs and the
scriptSigs that came back with it -- so the piece cannot be described
differently to two people, and every number a buyer is shown is read off the
bytes that were signed rather than from a column that could have drifted.

A leg that names the thing it sells has two inputs and two outputs, and the
reason is arithmetic rather than taste: a `SINGLE` signature reaches the output
standing at its own input's index and no other, so the payload at output 0 is
signed by the piece at input 0 and the payment at output 1 is signed by a coin of
the seller's at input 1. A leg that commits to nothing but a payment has one of
each, which is honest for a coin and for nothing else. That is why `payload` is
a column with a check behind it: a row that claims to sell a thing and carries no
bytes saying what that thing is, is a row a page would be lying about.
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
from .funding import SINGLE_ANYONECANPAY, Leg, build_partial, swap_fee
from .script import OP_RETURN, b58check_decode, hash160, iter_pushes
from .txbuild import is_scripthash, op_return_script, p2pkh_script, push, varint

COIN = 100_000_000

#: How long a listing is put in front of a buyer when the seller does not say.
#: This is not a term of the signature -- there is no lock time in a leg -- it is
#: how long this node keeps advertising it. A seller who wants a deadline sells
#: by spending the piece.
LISTED_FOR = 24 * 60 * 60

#: How long a leg that was ANSWERED rather than advertised stays completable.
#: `LISTED_FOR` would be the wrong number to hand it, because it measures a thing
#: that never happens to an answered leg: this node putting the piece in front of
#: strangers and then stopping. Nobody was shown an answered leg, and
#: `expire_due` never sees one, because it was never written down. The leg's real
#: deadline is the piece being spent -- which `paste_leg` asks the chain about,
#: every time, and which is the only way either side can cancel -- so this is
#: only the backstop for a completion that arrives a year after the answer, by
#: which time the fee floor and the dust rules have moved and the arithmetic
#: below is being done against numbers from another release. A completion that
#: reaches this says "expired" about a listing that never existed, which is a
#: sentence nobody should have to read; if it ever is read, the answer is that
#: the trade is old, not that anybody unsold the piece.
ANSWERED_FOR = 365 * 24 * 60 * 60

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
    -- The signed leg: the seller's inputs, its outputs, and the scriptSigs that
    -- came with them. Everything else in this row is read off it, and
    -- `check_leg` says so by checking.
    leg         TEXT NOT NULL,
    in_txid     TEXT NOT NULL,
    in_vout     INTEGER NOT NULL,
    in_value    INTEGER NOT NULL,
    -- The bytes output 0 carries: what the thing being sold actually IS, as the
    -- signature that signed it wrote it. Empty for a leg that commits only to
    -- its payment, which is a coin and not an asset.
    payload     TEXT NOT NULL DEFAULT '',
    -- The seller's OWN coin, the one behind the signature over the payment. It
    -- is not the piece and it is not the buyer's; it comes back to the seller
    -- inside out_value. Empty for a leg with one input.
    coin_txid   TEXT NOT NULL DEFAULT '',
    coin_vout   INTEGER NOT NULL DEFAULT -1,
    coin_value  INTEGER NOT NULL DEFAULT 0,
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
    spent_by    TEXT NOT NULL DEFAULT '',
    claim_hash  TEXT NOT NULL DEFAULT '',
    bound       TEXT NOT NULL DEFAULT ''
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


def sign_leg(leg: Leg, signatures: list[str], pubkey: bytes) -> str:
    """The leg as it goes into the book: its own bytes with its own scriptSigs.

    `signatures` is one per input, in signing order -- DER plus the sighash
    byte, which is what a browser hands back and what `funding.sighash` hashed.
    The type is part of the signature and the network reads it off the end, so
    it is stored, not remembered.

    A leg that names a thing takes two of them, and both are needed before
    there is a listing: one signature over the payment is a promise about coins,
    not about the piece, and the difference is the payload's digest.
    """
    if leg.sighash_type != SINGLE_ANYONECANPAY:
        raise ListingError("a leg is signed with SINGLE|ANYONECANPAY and this "
                           "one says it was signed otherwise")
    if len(signatures) != len(leg.inputs):
        raise ListingError(
            f"this leg has {len(leg.inputs)} input"
            f"{'s' if len(leg.inputs) != 1 else ''} to sign and "
            f"{len(signatures)} signature{'s' if len(signatures) != 1 else ''}"
            f" came back. An unsigned input is an unsigned output, and the "
            f"output it leaves free is the one the seller was told it was "
            f"signing")
    return _serialise([(coin["txid"], coin["vout"],
                        push(bytes.fromhex(signature)) + push(pubkey))
                       for coin, signature in zip(leg.inputs, signatures)],
                      leg.outputs)


def listing_row(network: str, owner: str, raw: str, piece: dict,
                paid: tuple[int, bytes], fee: int, price: int, what: str,
                seconds: float, coin: dict | None = None,
                payload: bytes = b"", pool_pays: int = 0) -> dict:
    """A listing in the shape `check_leg` reads, before anybody is told it exists.

    One builder, because there are two ways a leg gets here -- handed a `Leg` by
    code that built it, or handed raw bytes by a browser two requests later --
    and a row that differed between the two would mean a listing page said one
    thing about a piece listed by a route and another about the same piece
    listed by the other.
    """
    return {
        "id": secrets.token_urlsafe(18), "network": network, "owner": owner,
        "leg": raw,
        "input": {"txid": str(piece["txid"]), "vout": int(piece["vout"]),
                  "value": int(piece["value"])},
        "coin": None if coin is None else {
            "txid": str(coin["txid"]), "vout": int(coin["vout"]),
            "value": int(coin["value"])},
        "payload": bytes(payload).hex(),
        "output": {"value": int(paid[0]), "script": bytes(paid[1]).hex()},
        "fee": int(fee), "price": int(price), "what": what,
        # What a lot that pays its own claim keeps back (funding.build_leg).
        **({"pool_pays": int(pool_pays)} if pool_pays else {}),
        "status": "open", "created": time.time(),
        "expires": time.time() + seconds,
    }


#: A CLAIM (2026-09-28) is a listing with `claim_hash` set: sha256 of a
#: phrase, hex. It is never on a public page, never announced on the chain, and
#: `/account/buy` completes it only for whoever says the phrase -- which is what
#: lets any interactive inscription give a piece to whoever solves it, finds
#: it, or is told it. The leg itself is never served to anybody who has not
#: said the phrase, because a signed leg is all a stranger needs to finish it.
#: (No SQL comment beside the column: add_missing_columns reads the lines.)
#:
#: A claim may be BOUND to one inscription, its txid in `bound` (2026-09-29:
#: "only NFTs that are in your wallet should be able to send out your tokens
#: or NFTs"): it pays out only while its seller holds that inscription, so a
#: game is retired by sending it away. `/account/buy` refuses otherwise.


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
                "in_value, payload, coin_txid, coin_vout, coin_value, "
                "out_value, out_script, fee, price, what, status, "
                "created, expires, claim_hash, bound) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (listing["id"], listing["network"], listing["owner"],
                 listing["leg"], listing["input"]["txid"],
                 int(listing["input"]["vout"]), int(listing["input"]["value"]),
                 str(listing.get("payload") or ""),
                 str((listing.get("coin") or {}).get("txid") or ""),
                 int((listing.get("coin") or {}).get("vout", -1)),
                 int((listing.get("coin") or {}).get("value", 0)),
                 int(listing["output"]["value"]), listing["output"]["script"],
                 int(listing["fee"]), int(listing["price"]),
                 str(listing.get("what") or ""),
                 str(listing.get("status") or "open"),
                 float(listing["created"]), float(listing["expires"]),
                 str(listing.get("claim_hash") or ""),
                 str(listing.get("bound") or "")))
        return listing

    def from_leg(self, rpc: Any, leg: Leg, signatures: list[str],
                 pubkey: bytes, network: str, owner: str, price: int,
                 seconds: float) -> dict:
        """Write down a leg a browser just signed, after checking it.

        The check happens on the way IN rather than only on the way out because
        a row is a promise to a stranger: everything on a listing page is read
        from this table, and a row written from an unverified POST would put
        numbers on a public page that no signature backs.
        """
        raw = sign_leg(leg, signatures, pubkey)
        listing = listing_row(network, owner, raw, leg.inputs[0], leg.outputs[-1],
                              leg.fee, price, leg.what, seconds,
                              coin=leg.inputs[1] if len(leg.inputs) > 1 else None,
                              payload=leg.payload)
        check_leg(rpc, listing)
        return self.add(listing)

    def register(self, rpc: Any, *, raw: str, signatures: list[str],
                 pubkey: bytes, network: str, owner: str, price: int,
                 seconds: float = LISTED_FOR, what: str = "",
                 record: bool = True, claim_hash: str = "", bound: str = "", pool_pays: int = 0) -> dict:
        """File a leg a browser signed, with nothing remembered from before.

        A listing is two requests: this node builds a leg and shows it, the tab
        signs it and posts it back. Nothing is held between them. That costs a
        binding, and the binding is bought back with arithmetic rather than with
        state -- the two coins' values come from the chain, the payload, the
        payment and the outpoints come from the leg's own bytes, and a leg whose
        numbers do not close against this piece at this price is refused instead
        of filed. So a node that restarted between the two requests files
        exactly the row it would have filed anyway, which is more than an
        in-memory book of pending legs would have survived.

        The arithmetic is not a courtesy to the seller. `price` is the one number
        a listing row states for itself -- it is not in the signature and cannot
        be -- so it is derived here from the piece, the payment and the fee, and
        `check_leg` then refuses the row again if the two ever disagree.

        `record=False` runs every check above and writes nothing down. That is
        what a BUYER needs when a leg arrives addressed to it instead of being
        advertised: the row is the shape `paste_leg` reads its terms out of, and
        the checks are the reason it can be believed, but a leg sent to one
        buyer in a sealed message is not a listing -- filed, it would put a
        price on a public page that every stranger could then take. The row that
        comes back is complete and lives only as long as the trade it is
        finishing.
        """
        try:
            leg = rpc.call("decoderawtransaction", str(raw))
        except Exception as exc:
            raise ListingError(f"that is not a transaction: {exc}") from None
        vin, vout = leg.get("vin") or [], leg.get("vout") or []
        if len(vin) not in (1, 2) or len(vout) != len(vin):
            raise ListingError(
                "a leg is its seller's coins in and its seller's outputs out, "
                "one output for every signature it takes, and this is "
                f"{len(vin)} in and {len(vout)} out")
        for n, spent in enumerate(vin):
            if str((spent.get("scriptSig") or {}).get("hex") or ""):
                raise ListingError(
                    f"input {n} of that leg already has a scriptSig. A leg comes "
                    f"here unsigned and the signatures come beside it, because "
                    f"the two are checked against each other and a pre-pasted "
                    f"one cannot be")
        if len(signatures) != len(vin):
            raise ListingError(
                f"that leg has {len(vin)} input{'s' if len(vin) != 1 else ''} "
                f"and {len(signatures)} signature{'s' if len(signatures) != 1 else ''}"
                f" came with it. The signature that is missing is the one over "
                f"the output standing at that index, so the leg would promise "
                f"whatever this node puts there")
        for n, signature in enumerate(signatures):
            sig = bytes.fromhex(str(signature))
            if not sig or sig[-1] != SINGLE_ANYONECANPAY:
                raise ListingError(
                    f"a leg is signed with SINGLE|ANYONECANPAY, and the "
                    f"signature for input {n} says "
                    f"{sig[-1] if sig else 'nothing'} at the end, so it commits "
                    f"to a different transaction from the leg it came with")

        piece = {"txid": str(vin[0].get("txid") or ""),
                 "vout": int(vin[0].get("vout", -1))}
        held = piece_held(rpc, {"input": piece})
        if held is None:
            raise ListingError(
                f"{piece['txid'][:16]}…:{piece['vout']} is spent or unseen here, "
                f"so it is not a piece to list -- it sold, or its owner spent it")

        coin = None
        if len(vin) > 1:
            mine = {"txid": str(vin[1].get("txid") or ""),
                    "vout": int(vin[1].get("vout", -1))}
            if (mine["txid"], mine["vout"]) == (piece["txid"], piece["vout"]):
                raise ListingError(
                    f"{piece['txid'][:16]}…:{piece['vout']} is both the piece "
                    f"and the coin behind the second signature. Nothing spends "
                    f"one outpoint twice, so this leg has one input wearing two "
                    f"names and no signature over its payment")
            prev = rpc.call("gettxout", mine["txid"], mine["vout"], True)
            if not prev:
                raise ListingError(
                    f"{mine['txid'][:16]}…:{mine['vout']} is spent or unseen "
                    f"here, and that is the coin the seller put its second "
                    f"signature on. Without it the payment is unsigned, so this "
                    f"is not a leg to file -- the seller needs to build a new "
                    f"one from coins it still holds")
            coin = {**mine, "value": int(round(float(prev.get("value", 0))
                                               * COIN))}

        paid = vout[-1]
        out_value = int(round(float(paid.get("value", 0)) * COIN))
        script = bytes.fromhex(str((paid.get("scriptPubKey") or {})
                                   .get("hex") or ""))
        payload = b""
        if len(vout) > 1:
            payload = _payload_of(vout[0])
        fee = swap_fee(FEE_FLOOR_PER_KB,
                       op_return_script(payload) if payload else b"")
        behind = coin["value"] if coin else 0
        if pool_pays:
            # A lot that pays its own claim keeps back `pool_pays` instead of a
            # reservation to be repaid (funding.build_leg).
            fee = int(pool_pays)
        if out_value != held + behind + int(price) - fee:
            raise ListingError(
                f"a piece worth {held}"
                + (f" with a second coin worth {behind}" if coin else "")
                + f" listed at {int(price)} pays its seller "
                f"{held + behind + int(price) - fee} once the {fee} it reserves "
                f"for a block is taken out, and this leg pays {out_value}, so it "
                f"was not built from this piece at this price")
        signed = _serialise(
            [(spent["txid"], int(spent["vout"]),
              push(bytes.fromhex(str(signature))) + push(pubkey))
             for spent, signature in zip(vin, signatures)],
            ([(0, op_return_script(payload))] if payload else [])
            + [(out_value, script)])
        listing = listing_row(network, owner, signed, {**piece, "value": held},
                              (out_value, script), 0 if pool_pays else fee, int(price), what,
                              seconds, coin=coin, payload=payload, pool_pays=int(pool_pays))
        check_leg(rpc, listing)
        listing["claim_hash"] = str(claim_hash or "")
        listing["bound"] = str(bound or "")
        if not record:
            return listing
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

    def has_leg(self, network: str, leg: str) -> bool:
        """Whether this node already holds this exact signed leg, in any state:
        an announced listing is read by every node and filed once."""
        with self._open() as conn:
            return conn.execute("SELECT 1 FROM listing WHERE network=? AND leg=? LIMIT 1",
                                (network, str(leg))).fetchone() is not None

    def open_listings(self, network: str, limit: int = 200,
                      claims: bool = False) -> list[dict]:
        """Open listings, newest first. Claims only when asked for: every
        public reader leaves `claims` False, so a claim is on no page."""
        with self._open() as conn:
            rows = conn.execute(
                "SELECT * FROM listing WHERE network=? AND status='open' "
                + ("" if claims else "AND claim_hash='' ")
                + "ORDER BY created DESC LIMIT ?",
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


def _payload_of(out: dict) -> bytes:
    """The bytes a payload output carries, or a refusal of a thing that is not.

    Shape rather than belief: nothing here decodes the bytes or says what they
    mean. What is checked is that output 0 pays nothing and says something, in
    one push, because this is the field a page renders in words and a completion
    is refused for changing. A second output that pays coins is a leg whose
    outputs are not the two its signatures were made over, and a payload spread
    over two pushes is not the bytes any signature committed to.
    """
    if int(round(float(out.get("value", 0)) * COIN)) != 0:
        raise ListingError(
            "output 0 of that leg pays coins, and a payload output pays "
            "nothing. A leg has two outputs and they are the bytes it sells and "
            "the payment it accepts; there is no third thing a leg pays")
    script = bytes.fromhex(str((out.get("scriptPubKey") or {}).get("hex") or ""))
    pushes = iter_pushes(script)
    if len(script) < 2 or script[0] != OP_RETURN or len(pushes) != 1:
        raise ListingError(
            "output 0 of that leg is not one OP_RETURN push, so there are no "
            "bytes here to say what is being sold -- and a leg that cannot say "
            "is a leg that should not be on a page")
    return pushes[0]


def row_listing(row: dict) -> dict:
    """A database row, in the shape a leg travels in."""
    return {
        "id": row["id"], "network": row["network"], "owner": row["owner"],
        "leg": row["leg"],
        "input": {"txid": row["in_txid"], "vout": int(row["in_vout"]),
                  "value": int(row["in_value"])},
        "coin": None if not row["coin_txid"] else {
            "txid": row["coin_txid"], "vout": int(row["coin_vout"]),
            "value": int(row["coin_value"])},
        "payload": row["payload"],
        "output": {"value": int(row["out_value"]), "script": row["out_script"]},
        "fee": int(row["fee"]), "price": int(row["price"]),
        "what": row["what"], "status": row["status"],
        "created": float(row["created"]), "expires": float(row["expires"]),
        "spent_by": row["spent_by"] or None,
        "claim_hash": row.get("claim_hash") or "",
        "bound": row.get("bound") or "",
    }


def public(listing: dict) -> dict:
    """What a buyer is shown: the terms, and none of this node's bookkeeping.

    `coin` is in here because the buyer has to put the seller's second input in
    front of its own coins, and `payload` because it is what the buyer is buying.
    Neither is a secret; the scriptSigs in `leg` are the only thing here that
    belongs to somebody else, and a buyer needs those too.
    """
    return {k: listing[k] for k in ("id", "network", "owner", "leg", "input",
                                    "coin", "payload", "output", "fee",
                                    "price", "what", "created", "expires")}


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
    if len(vin) not in (1, 2) or len(vout) != len(vin):
        raise ListingError(
            "a leg is its seller's coins in and one output for every signature "
            f"it takes, and this is {len(vin)} in and {len(vout)} out. Anything "
            "else either commits to coins its seller has never seen or leaves "
            "an output nobody signed")
    first, paid = vin[0], vout[-1]
    piece, coin = listing.get("input") or {}, listing.get("coin")
    if coin is not None and len(vin) < 2:
        raise ListingError(
            f"this row names {coin['txid'][:16]}…:{coin['vout']} as the coin "
            f"behind its second signature and that leg has one input, so the "
            f"row is describing a different transaction from the one it stores")
    if coin is None and len(vin) > 1:
        raise ListingError(
            "that leg has a second input and this row names no coin beside it, "
            "so the payment at output 1 stands over an outpoint nothing here "
            "ever looked at")
    if str(first.get("txid") or "") != str(piece.get("txid") or "") \
            or int(first.get("vout", -1)) != int(piece.get("vout", -1)):
        raise ListingError("that leg does not promise the piece this listing "
                           "says it promises")
    if coin is not None and (str(vin[1].get("txid") or "") != coin["txid"]
                             or int(vin[1].get("vout", -1)) != int(coin["vout"])):
        raise ListingError(
            f"that leg's second input is not {coin['txid'][:16]}…:{coin['vout']}"
            ", the coin this row says its second signature stands on")
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
    # The payload is the one part of a listing that is not arithmetic, so it is
    # compared byte for byte: the row holds the bytes, output 0 holds the bytes,
    # and a page renders what the row holds. If the two ever differed, the
    # signature would be over a thing the page is not describing.
    payload = str(listing.get("payload") or "")
    if payload:
        if len(vout) < 2 or bytes.fromhex(payload) != _payload_of(vout[0]):
            raise ListingError(
                "the bytes at output 0 of that leg are not the bytes this "
                "listing says it sells, so the page and the signature are "
                "describing two different things")
    elif len(vout) > 1:
        raise ListingError(
            "that leg carries a payload and this row says it carries none, so "
            "what is on the page is not what the seller signed")
    # The price is not in the signature and cannot be: the leg commits to the
    # seller's output, and what the seller paid for its own coins is arithmetic
    # from there. So the one number a row states for itself has to be the one
    # number the other four force, or the page is quoting a bargain nobody
    # signed.
    derived = (value - int(piece.get("value", 0))
               - int((coin or {}).get("value", 0))
               + int(listing.get("fee", 0)) + int(listing.get("pool_pays", 0)))
    if int(listing.get("price", -1)) != derived:
        raise ListingError(
            f"this listing says {int(listing.get('price', -1)) / COIN:.8f} and "
            f"its own numbers say {derived / COIN:.8f}, so the price on it is "
            f"not the price its signature was made against")

    for n, spent in enumerate(vin):
        script_sig = str((spent.get("scriptSig") or {}).get("hex") or "")
        if not script_sig:
            raise ListingError(
                f"input {n} of that leg is unsigned, and its signature is the "
                f"only thing that reaches output {n}. A transaction is not a "
                f"promise until somebody's key is on it")
        pushes = iter_pushes(bytes.fromhex(script_sig))
        # At a script-hash address (a refereed prize pool) the second push is the
        # redeem script, which hashes to the address the same way a key does.
        if len(pushes) != 2 or (len(pushes[1]) not in (33, 65)
                                and not is_scripthash(owner)):
            raise ListingError("a leg's scriptSig is a signature and a public key")
        if hash160(pushes[1]) != b58check_decode(owner)[1]:
            raise ListingError(
                f"the key on input {n} of this leg is not the key behind the "
                f"address it is listed from, so this row would credit somebody's "
                f"sale to somebody else")
    return leg


def piece_held(rpc: Any, listing: dict) -> int | None:
    """Satoshis still sitting at the listed outpoint, or None if it is gone.

    One definition, because two readers need the same answer. `paste_leg`
    refuses to finish a swap over a piece the chain no longer holds, and a
    listing page has to say the same thing in the same words: "cancelled" is
    not a state this book can create. A row goes stale when a BLOCK spends the
    input, and until that happens a signature already handed out still works.
    Reading it here rather than in two places is what keeps the page and the
    combiner from ever disagreeing about whether a piece is for sale.
    """
    piece = listing["input"]
    held = rpc.call("gettxout", piece["txid"], int(piece["vout"]), True)
    if not held:
        return None
    return int(round(float(held.get("value", 0)) * COIN))


def paste_leg(rpc: Any, listing: dict, unsigned: Any, signatures: list[str],
              pubkey: bytes, referee_sigs: list[bytes] | None = None,
              pool_pays: bool = False) -> str:
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
    held = piece_held(rpc, listing)
    if held is None:
        raise ListingError(
            f"{piece['txid'][:16]}…:{piece['vout']} is spent or unseen here, so "
            f"this piece is not this listing's to sell any more -- it sold, or "
            f"its owner spent it, which is the only way to cancel one")
    if held != int(piece["value"]):
        raise ListingError(
            "that piece is not the size this listing says it is, so the price "
            "on it would be arithmetic done on a number that changed")

    coin = listing.get("coin")
    if coin is not None:
        # The seller's second signature stands on this outpoint, so if it is
        # gone the payment at output 1 is signed by nothing and this listing is
        # finished in a way nobody agreed to. Refused here, loudly, and with the
        # one thing the seller can actually do about it.
        still = rpc.call("gettxout", coin["txid"], int(coin["vout"]), True)
        if not still:
            raise ListingError(
                f"{coin['txid'][:16]}…:{coin['vout']} is spent or unseen here. "
                f"That is the coin the seller's second signature stands on, so "
                f"the payment on this listing is signed by nothing now and this "
                f"cannot be completed -- the seller has to build a new leg out "
                f"of coins it still holds")

    signed_at = len(leg["vin"])            # the inputs the seller already signed
    if len(unsigned.inputs) < signed_at + (0 if pool_pays else 1):
        raise ListingError(
            "a swap has the listed piece"
            + (", the coin behind its second signature" if signed_at > 1 else "")
            + ", and the buyer's coins")
    mine = unsigned.inputs[0]
    if mine["txid"] != piece["txid"] or int(mine["vout"]) != int(piece["vout"]):
        raise ListingError("the listed piece is not the first input, so the "
                           "seller would not be the one paying out of this "
                           "transaction")
    if signed_at > 1 and (unsigned.inputs[1]["txid"] != coin["txid"]
                          or int(unsigned.inputs[1]["vout"]) != int(coin["vout"])):
        raise ListingError(
            f"the second input is not {coin['txid'][:16]}…:{coin['vout']}, the "
            f"coin this listing's second signature was made over, so that "
            f"signature would be pasted onto somebody else's coin")

    payload = bytes.fromhex(str(listing.get("payload") or ""))
    at = 1 if payload else 0
    if payload and (len(unsigned.outputs) < 2
                    or unsigned.outputs[0] != (0, op_return_script(payload))):
        raise ListingError(
            "the transaction does not carry the bytes this listing sells. Output "
            "0 is the one output the seller's FIRST signature reaches, so a "
            "completion that changes it is a trade the seller never signed")
    want = (int(listing["output"]["value"]),
            bytes.fromhex(listing["output"]["script"]))
    if len(unsigned.outputs) <= at or unsigned.outputs[at] != want:
        raise ListingError(
            f"the transaction pays the seller something at output {at} other "
            f"than the {want[0]} its own signature commits to")

    for n, buyer_coin in enumerate(unsigned.inputs[signed_at:], signed_at):
        prev = rpc.call("gettxout", buyer_coin["txid"], int(buyer_coin["vout"]),
                        True)
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

    raw = _combine(leg, unsigned, signatures, pubkey, referee_sigs)
    decoded = rpc.call("decoderawtransaction", raw)
    total_in = (int(listing["input"]["value"])
                + (int(coin["value"]) if coin else 0)
                + sum(int(c["value"]) for c in unsigned.inputs[signed_at:]))
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


def leg_terms(listing: dict) -> tuple[list, list]:
    """What a leg's own signatures demand of the transaction that finishes it.

    Its outputs, first and in their own order, and the outpoints that carry them.
    `funding.build_partial` puts the buyer's coins behind those and its change
    after, and `paste_leg` refuses the result if either of the two moved by a
    satoshi -- so this is the one place that has to be right about what a
    completion looks like, and it has to be the SAME place for the two buyers
    there are: one signing in a browser tab over coins this node holds no key
    for, one paying out of this node's own wallet. Two readings of it, drifting by
    one output, is a trade one of them agreed to and the other did not.

    The fee the leg reserved comes back as a payment to the seller, which is the
    one term easy to get wrong and impossible to fudge. A leg fixes its seller's
    output the moment it signs and takes a fee out of that output in case nobody
    ever pays one; whoever completes the trade knows what a block actually costs
    and pays all of it. The engine measures a coin leg by what the receiving side
    NETS (`tx.paid_to` is outputs minus inputs at that address), so a completion
    that kept the reservation would leave the seller a little under its own price
    and the index would call the whole swap invalid.
    """
    naming = bytes.fromhex(str(listing.get("payload") or ""))
    outputs = ([(0, op_return_script(naming))] if naming else []) + [
        (int(listing["output"]["value"]),
         bytes.fromhex(listing["output"]["script"]))]
    reserved = int(listing.get("fee") or 0)
    if reserved:
        outputs.append((reserved, p2pkh_script(listing["owner"])))
    foreign = [listing["input"]]
    if listing.get("coin"):
        foreign.append(listing["coin"])
    return foreign, outputs


def _is_token_send(payload: bytes) -> bool:
    """Whether a listing's bytes are a token Simple Send (a claim lot)."""
    from . import payload as P
    from .encoding import decode_class_c
    try:
        body = decode_class_c(bytes(payload)) if payload else None
        return isinstance(P.decode(body), P.SimpleSend) if body else False
    except Exception:                                           # noqa: BLE001
        return False


def named_swap(payload: bytes) -> Any:
    """The trade a listing's own bytes promise, or None when they promise none.

    Read from the payload and never from the row's `input`, because that input is
    a COIN -- the seller's own, whose signature is what stands over the bytes --
    while the piece that changes hands is the inscription those bytes name. For a
    piece that arrived by transfer those are two different transactions, and a
    reader that asked the coin would describe the seller's change instead of the
    sale.
    """
    from . import inscriptions as inscriptionlib, payload as P
    from .encoding import decode_class_c

    body = decode_class_c(bytes(payload))
    if body is None:
        return None
    try:
        message = P.decode(body)
        if not isinstance(message, P.AnyData):          # a token lot sends, it swaps nothing
            return None
        found = inscriptionlib.parse(message.data)
    except (P.PayloadError, inscriptionlib.InscriptionError):
        return None
    return found if isinstance(found, inscriptionlib.Swap) else None


def wallet_completes(rpc: Any, db: Any, params: Any, listing: dict,
                     address: str, rate: int) -> str:
    """Finish a leg with a wallet this node DOES hold the keys to, and send it.

    The same trade `/account/buy` offers an account, with the signing step done
    here instead of in a tab: the buyer's half is built by
    `funding.build_partial` -- which is what that function was written for, a
    transaction whose first inputs belong to somebody else -- signed by this
    node's wallet, and finished by `paste_leg`, which is what says the difference
    between a node that signs a transaction and a node that completes one. The
    wallet is asked to sign bytes whose seller's scriptSigs are still blank, so
    what it signs is the same preimage a tab would have been shown, and its
    signatures are read back out of the result rather than trusted from the
    `complete` flag: `complete` is false here whatever happens, because the
    seller's half was never this wallet's to sign.

    The coins come from `listunspent` and not from the index. This node watches
    the accounts on itself and the @names it can see, and not its own wallet, so
    the index has nothing to say about `address` -- and the wallet's own answer is
    the better one anyway, because it leaves out what the pool has already spent,
    which is the same reason `swap.build` asks the wallet and not the index.
    """
    if _is_token_send(bytes.fromhex(str(listing.get("payload") or ""))):
        # A token lot's tokens go to the last output that is not the seller's,
        # and this completion ends on change it may not keep: claimed from an
        # account (`/account/buy`), which always ends on the buyer.
        raise ListingError("a token lot is claimed from an account, not by a node's wallet")
    foreign, outputs = leg_terms(listing)
    coins = [{"txid": str(u["txid"]), "vout": int(u["vout"]),
              "value": int(round(float(u["amount"]) * COIN)),
              "address": str(u.get("address") or address)}
             for u in (rpc.call("listunspent", 1, 9_999_999, [address]) or [])
             if u.get("spendable", True)]
    unsigned = build_partial(db, params, address, foreign, outputs, rate=rate,
                             extra=coins)
    signed = rpc.call("signrawtransaction", unsigned.raw)
    decoded = rpc.call("decoderawtransaction", signed["hex"])
    signatures: list[str] = []
    pubkey = b""
    for n, spent in enumerate((decoded.get("vin") or [])[unsigned.signed_from:]):
        pushes = iter_pushes(bytes.fromhex(str((spent.get("scriptSig") or {})
                                               .get("hex") or "")))
        if len(pushes) != 2:
            raise ListingError(
                f"this wallet put nothing on input {unsigned.signed_from + n}, so "
                f"it holds no key for {address} and cannot pay for this piece")
        if not signatures:
            pubkey = pushes[1]
        elif pushes[1] != pubkey:
            raise ListingError(
                f"this wallet signed its own inputs with two different keys, and "
                f"a transaction that cannot say which key paid is not one to send")
        signatures.append(pushes[0].hex())
    if hash160(pubkey) != b58check_decode(address)[1]:
        raise ListingError(
            f"the key this wallet signed with is not the key behind {address}")
    raw = paste_leg(rpc, listing, unsigned, signatures, pubkey)
    return str(rpc.call("sendrawtransaction", raw))


def _combine(leg: dict, unsigned: Any, signatures: list[str],
             pubkey: bytes, referee_sigs: list[bytes] | None = None) -> str:
    """The finished swap: the leg's scriptSigs, then the buyer's, one input each.

    The leg's scriptSigs are taken from the decoded leg rather than sliced out
    of its bytes, and put at the front in the leg's own order. They have already
    been checked by then, and there is no reason to have a second, hand-counted
    way of reading the same four fields -- nor a reason for the outpoints the
    seller signed to be the ones the buyer happened to send.
    """
    signed_at = len(leg["vin"])
    if len(signatures) != len(unsigned.inputs) - signed_at:
        raise ListingError(
            f"{len(unsigned.inputs) - signed_at} signatures were needed and "
            f"{len(signatures)} came back")
    inputs = [(spent["txid"], int(spent["vout"]),
               bytes.fromhex(str((spent.get("scriptSig") or {}).get("hex")
                                 or "")))
              for spent in leg["vin"]]
    if referee_sigs is not None:
        # A refereed lot (2026-09-29): the leg holds the pool's signature and
        # the redeem script; the claim spends the IF branch, which wants the
        # referee's signature beside them.
        from .referee import claim_script_sig
        if len(referee_sigs) != len(inputs):
            raise ListingError("the referee signed a different number of inputs "
                               "than this lot has")
        inputs = [(txid, vout, claim_script_sig(pushed[0], ref, pushed[1]))
                  for (txid, vout, sig), ref in zip(inputs, referee_sigs)
                  for pushed in [iter_pushes(sig)]]
    inputs += [(coin["txid"], int(coin["vout"]),
                push(bytes.fromhex(sig)) + push(pubkey))
               for coin, sig in zip(unsigned.inputs[signed_at:], signatures)]
    return _serialise(inputs, unsigned.outputs)
