"""Doginals for DOGE on the Exchange (2026-10-14: "I want doginals to be incorporated into the exchange on its own
separate tab. So people can list doginals for doge.").

A Doginal is not a payload the arcade can name in an OP_RETURN, the way `listings.py` names an arcade inscription: it is a
koinu, and it moves by koinu order (inputs' koinu fill the outputs first to last). So a listing here is the Ordinals
marketplace layout, adapted to Dogecoin's legacy sighash [Claude 2026-10-15]:

  the SELLER signs ONE input -- the inscribed coin -- at index 2, SIGHASH_SINGLE|ANYONECANPAY, over output 2, which pays
  the price in DOGE to the seller's own address. ANYONECANPAY puts only that input in the preimage, and SINGLE at index 2
  serialises outputs 0 and 1 as value -1 / empty script, so the digest is computed from a 3-output template before any
  buyer exists (`listing_digest`).

  the BUYER completes one transaction:

      inputs   0 dummy A (buyer)         outputs  0 A+B          -> buyer
               1 dummy B (buyer)                  1 V (the coin)  -> buyer   <- the Doginal, at offset 0
               2 inscribed coin V (seller, signed)  2 price         -> seller
               3.. buyer funding                  3 change        -> buyer (when it is not dust)

  Inputs 0 and 1 fill output 0 exactly, so the inscribed coin's first koinu is output 1's first koinu. The dummies and
  the funding coins pass the Doginal guard (funding.GUARD): a coin carrying a Doginal is never a dummy, never pays.

A buyer without two small clean coins first gets a "prepare" transaction that pays two DUMMY_VALUE coins to itself.
Every output is at least MIN_OUT (Dogecoin 1.14's hard dust limit); an output under fees.DUST_LIMIT costs the soft dust
fee, which `funding.price` already adds. Each number is the chain's own (2026-10-25, Bellinals: Bells 3 relays as
Bitcoin Core does, with no soft-dust fee and 1/100 of Dogecoin's fee rate): `min_out`, `dummy_value`, `dummy_max`,
`least_price`, and fees.rate_for / fees.dust_limit, from the chain's Params; with none, Dogecoin's.

The seller's signature lives in this node's book (`Book`) until the coin moves -- which is the only way to cancel one, as
with every other pre-signed listing (arcade/listings.py).
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import fees, funding, ord_chains, utxos
from .config import NETWORKS
from .doginals import POSTAGE_MAX
from .listings import _serialise
from .script import b58check_decode, hash160
from .txbuild import build_raw_tx, p2pkh_script, push

COIN = 100_000_000

#: The seller's input, and the output its signature stands over.
SELLER_AT = 2
#: Dogecoin 1.14's hard dust limit (0.001 DOGE): no output here is smaller.
MIN_OUT = 100_000
#: What the prepare transaction splits off for each dummy: over doginals.POSTAGE_MAX, so the guard takes it as clean even
#: while the chain reading has not seen the prepare yet, and at or over the soft dust limit, so it costs no dust fee.
DUMMY_VALUE = 2 * fees.DUST_LIMIT
#: The largest coin used as a dummy: a dummy only travels back to its owner in output 0, but a big one makes a big output 0.
DUMMY_MAX = 10 * fees.DUST_LIMIT


def min_out(params=None) -> int:
    """The smallest output a listing or a purchase makes: Dogecoin 1.14's hard dust limit (MIN_OUT) on the Dogecoin
    family (soft dust); elsewhere the chain's own least change (Params.dust_limit: 0.0001 BEL, over Bells' 546)."""
    return MIN_OUT if params is None or getattr(params, "soft_dust", True) else fees.dust_limit(params)


def dummy_value(params=None) -> int:
    """A dummy the prepare transaction splits off: at or over the chain's soft dust line (no dust fee) and over
    doginals.POSTAGE_MAX, a bound in coins on every chain, so the guard takes it as clean before its block."""
    return max(2 * fees.dust_limit(params), 2 * POSTAGE_MAX)


def dummy_max(params=None) -> int:
    """The largest coin used as a dummy."""
    return max(10 * fees.dust_limit(params), 5 * dummy_value(params))


def least_price(params=None) -> int:
    """The smallest price: the chain's own dust line (fees.dust_limit)."""
    return fees.dust_limit(params)


#: How long a Doginal listing is shown when the seller does not say. Not a term of the signature (there is no lock time):
#: the coin moving is what ends it.
LISTED_FOR = 30 * 24 * 60 * 60


class MarketError(Exception):
    """Something that cannot be listed or bought. Shown as written."""


class NeedDummies(MarketError):
    """The buyer holds fewer than two small clean coins: a prepare transaction comes first."""


def _guard(address: str = ""):
    """The inscription guard for `address`'s chain (funding.guard_for): Doginals on Dogecoin, Pepinals on Pepecoin
    mainnet (2026-10-17: "Add pepinals support")."""
    guard = (funding.guard_for(address) if address else None) or funding.GUARD
    if guard is None:
        raise MarketError("inscriptions are not checked on this chain here, so none is listed or bought")
    return guard


def names(address: str, params=None) -> tuple[str, str]:
    """("Doginal", "DOGE") on Dogecoin, ("Pepinal", "PEP") on Pepecoin ...: what the words say for the chain `params`
    names, else for `address`'s (by its version byte, among the ord chains: ord_chains.WORDS); Dogecoin's otherwise."""
    net = getattr(params, "name", None)
    if net not in ord_chains.WORDS:
        try:
            version = b58check_decode(str(address))[0]
        except Exception:                                  # noqa: BLE001 -- words only
            version = None
        net = next((n for n in ord_chains.WORDS if n in NETWORKS and NETWORKS[n].pubkeyhash_version == version),
                   "doge")
    return ord_chains.word(net, "Doginal"), ord_chains.coin(net) or "DOGE"


def holds_exactly(coin: dict, inscription: str, address: str = "") -> None:
    """Refuse unless the chain says this coin holds exactly `inscription`, at offset 0."""
    word = names(address)[0] if address else "Doginal"
    on = _guard(address).held(coin["txid"]).get(int(coin["vout"])) or []
    if [i.get("id") for i in on] != [inscription]:
        raise MarketError("that coin does not hold exactly that %s (the chain says: %s)"
                          % (word, ", ".join(str(i.get("id")) for i in on) or "nothing"))
    if int(on[0].get("offset") or 0) != 0:
        raise MarketError(f"that {word} does not sit at the start of its coin, so it cannot be sold by koinu order")


def listing_digest(seller: str, coin: dict, price: int) -> bytes:
    """What the seller signs: its coin at input 2, SINGLE|ANYONECANPAY over output 2 = the price to its own address.

    The preimage holds input 2 alone and outputs 0..1 as nulls, so the other inputs and outputs given here are never
    read -- which is the point: they belong to a buyer nobody knows yet."""
    pay = (int(price), p2pkh_script(seller))
    return funding.sighash([coin] * 3, [funding.NULL_OUTPUT, funding.NULL_OUTPUT, pay], SELLER_AT,
                           p2pkh_script(seller), sighash_type=funding.SINGLE_ANYONECANPAY)


@dataclass
class Ask:
    """The seller's half, before it is signed: one coin, one price, one digest."""

    seller: str
    coin: dict
    inscription: str
    price: int
    digest: str
    what: str = ""

    def as_json(self) -> dict:
        return {"seller": self.seller, "coin": dict(self.coin), "inscription": self.inscription,
                "price": self.price, "digest": self.digest, "at": SELLER_AT,
                "sighash_type": funding.SINGLE_ANYONECANPAY, "signed_by": "account", "what": self.what}


def build_ask(seller: str, coin: dict, inscription: str, price: int, params=None) -> Ask:
    """The digest a seller signs to list `inscription` (on `coin`) for `price` koinu on the chain `params` describes
    (Dogecoin's numbers without it). Checks the Doginal first."""
    coin = {"txid": str(coin["txid"]), "vout": int(coin["vout"]), "value": int(coin["value"])}
    if len(coin["txid"]) != 64:
        raise MarketError("a coin is a 32-byte txid and an index")
    if coin["value"] < min_out(params):
        raise MarketError(f"that coin is {coin['value']} koinu, under the {min_out(params)} the network carries as an "
                          "output")
    word, ticker = names(seller, params)
    if int(price) < least_price(params):
        raise MarketError(f"the smallest price is {least_price(params) / COIN:g} {ticker} -- less is dust")
    holds_exactly(coin, inscription, seller)
    digest = listing_digest(seller, coin, int(price))
    return Ask(seller=seller, coin=coin, inscription=inscription, price=int(price), digest=digest.hex(),
               what=f"list {word} {inscription} for {int(price) / COIN:g} {ticker}")


def verify_signature(digest: bytes, signature: bytes, pubkey: bytes) -> bool:
    """Whether a DER signature (without its sighash byte) by `pubkey` is over `digest`, and low-S as the network wants."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), bytes(pubkey))
        r, s = utils.decode_dss_signature(bytes(signature))
        if s > 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141 // 2:
            return False
        key.verify(bytes(signature), bytes(digest), ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        return True
    except (InvalidSignature, ValueError):
        return False


# --- the buyer's side -------------------------------------------------------------------------------------------------

def _clean(db, address: str, exclude=frozenset(), extra: list | None = None) -> tuple[list[dict], set]:
    """(the coins at `address` the guard calls clean, the outpoints it does not): a Doginal is never a dummy or a fee."""
    guard = _guard(address)
    listed = [u for u in utxos.unspent(db, address) if (u["txid"], u["vout"]) not in exclude]
    seen = {(u["txid"], u["vout"]) for u in listed}
    listed += [dict(c) for c in (extra or []) if (c["txid"], c["vout"]) not in exclude
               and (c["txid"], c["vout"]) not in seen]
    clean = guard.spendable(listed)
    ok = {(c["txid"], c["vout"]) for c in clean}
    return clean, {(u["txid"], u["vout"]) for u in listed} - ok


def dummies(db, address: str, exclude=frozenset(), extra: list | None = None, params=None) -> list[dict]:
    """The buyer's two smallest clean coins between min_out and dummy_max, or NeedDummies."""
    clean, _ = _clean(db, address, exclude, extra)
    least, most = min_out(params), dummy_max(params)
    small = sorted((c for c in clean if least <= int(c["value"]) <= most), key=lambda c: int(c["value"]))
    if len(small) < 2:
        word, ticker = names(address, params)
        raise NeedDummies(f"buying a {word} takes two small coins of your own (up to {most / COIN:g} {ticker} each) "
                          f"in front of the seller's, and this address has {len(small)}: split two off first")
    return small[:2]


def build_prepare(db, params, buyer: str, rate: int, exclude=frozenset(), extra: list | None = None) -> funding.Unsigned:
    """Two dummy_value coins from the buyer to itself, paid from clean coins only."""
    clean, unclean = _clean(db, buyer, exclude, extra)
    script = p2pkh_script(buyer)
    each = dummy_value(params)
    return funding.build(db, params, buyer, [(each, script), (each, script)], rate,
                         what=f"split two small coins off, to buy a {names(buyer, params)[0]} with",
                         exclude=set(exclude) | unclean, extra=_fresh(db, buyer, clean))


def _fresh(db, address: str, clean: list[dict]) -> list[dict]:
    """The clean coins the index does not list yet (change on its way): `funding.choose` takes those as `extra`."""
    known = {(u["txid"], u["vout"]) for u in utxos.unspent(db, address)}
    return [c for c in clean if (c["txid"], c["vout"]) not in known]


@dataclass
class Purchase:
    """The buyer's transaction, waiting for the buyer's signatures on every input but the seller's."""

    raw: str
    inputs: list[dict[str, Any]]
    outputs: list[tuple[int, bytes]]
    #: The inputs the buyer signs (all but SELLER_AT), and one SIGHASH_ALL digest for each, in that order.
    buyer_inputs: list[int]
    sighashes: list[str]
    fee: int
    change: int
    what: str = ""
    extra: dict = field(default_factory=dict)

    def as_json(self) -> dict:
        return {"raw": self.raw,
                "inputs": [{"txid": i["txid"], "vout": i["vout"], "value": i["value"], "address": i["address"]}
                           for i in self.inputs],
                "outputs": [{"value": v, "script": s.hex()} for v, s in self.outputs],
                "buyer_inputs": self.buyer_inputs, "sighashes": self.sighashes, "seller_at": SELLER_AT,
                "signed_by": "account", "fee": self.fee, "change": self.change, "what": self.what, **self.extra}


def build_purchase(db, params, buyer: str, listing: dict, rate: int, exclude=frozenset(),
                   extra: list | None = None) -> Purchase:
    """The one transaction that buys `listing` (a Book row): the layout at the top of this file."""
    seller, coin, price = listing["seller"], dict(listing["coin"]), int(listing["price"])
    if buyer == seller:
        raise MarketError("that listing is your own")
    keep_out = set(exclude) | {(coin["txid"], int(coin["vout"]))}
    two = dummies(db, buyer, keep_out, extra, params)
    keep_out |= {(c["txid"], c["vout"]) for c in two}
    clean, unclean = _clean(db, buyer, keep_out, extra)
    mine = p2pkh_script(buyer)
    outs = [(int(two[0]["value"]) + int(two[1]["value"]), mine),          # 0: the dummies, back
            (int(coin["value"]), mine),                                     # 1: the Doginal, at offset 0
            (price, p2pkh_script(seller))]                                  # 2: the seller's price
    rate = fees.rate_for(params, rate)              # this chain's own minimum
    fee = funding.price(4, outs, rate, change=True, params=params)
    fresh = _fresh(db, buyer, clean)
    paying = funding.choose(db, buyer, price + fee, exclude=keep_out | unclean, extra=fresh)
    fee = funding.price(3 + len(paying), outs, rate, change=True, params=params)
    if sum(int(c["value"]) for c in paying) < price + fee:
        paying = funding.choose(db, buyer, price + fee, exclude=keep_out | unclean, extra=fresh)
        fee = funding.price(3 + len(paying), outs, rate, change=True, params=params)
    change = sum(int(c["value"]) for c in paying) - price - fee
    outputs = list(outs)
    if change >= fees.dust_limit(params):
        outputs.append((change, mine))
    else:
        fee += max(0, change)
        change = 0
    inputs = ([{**two[0], "address": buyer}, {**two[1], "address": buyer},
               {"txid": coin["txid"], "vout": int(coin["vout"]), "value": int(coin["value"]), "address": seller}]
              + [{**c, "address": buyer} for c in paying])
    inputs = [{"txid": i["txid"], "vout": int(i["vout"]), "value": int(i["value"]), "address": i["address"]}
              for i in inputs]
    raw = build_raw_tx([(i["txid"], i["vout"]) for i in inputs], outputs)
    signs = [n for n in range(len(inputs)) if n != SELLER_AT]
    hashes = [funding.sighash(inputs, outputs, n, mine).hex() for n in signs]
    return Purchase(raw=raw, inputs=inputs, outputs=outputs, buyer_inputs=signs, sighashes=hashes, fee=fee,
                    change=change, what="buy %s %s for %g %s" % (names(buyer, params)[0], listing['inscription'],
                                                                  price / COIN, names(buyer, params)[1]))


def assemble_purchase(purchase: Purchase, listing: dict, signatures: list[str], pubkey: bytes) -> str:
    """The finished transaction: the seller's stored scriptSig at input 2, the buyer's signatures everywhere else."""
    if len(signatures) != len(purchase.buyer_inputs):
        raise MarketError(f"{len(purchase.buyer_inputs)} signatures were needed and {len(signatures)} came back")
    coin = purchase.inputs[SELLER_AT]
    if (coin["txid"], coin["vout"]) != (listing["coin"]["txid"], int(listing["coin"]["vout"])):
        raise MarketError("input 2 is not the listed coin")
    if purchase.outputs[SELLER_AT] != (int(listing["price"]), p2pkh_script(listing["seller"])):
        raise MarketError("output 2 does not pay the seller what the seller signed for")
    seller_sig = push(bytes.fromhex(listing["signature"])) + push(bytes.fromhex(listing["pubkey"]))
    by_input = dict(zip(purchase.buyer_inputs, signatures))
    ins = [(i["txid"], i["vout"], seller_sig if n == SELLER_AT
            else push(bytes.fromhex(by_input[n])) + push(pubkey)) for n, i in enumerate(purchase.inputs)]
    return _serialise(ins, purchase.outputs)


# --- the book ---------------------------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS doginal_listing (
    id          TEXT PRIMARY KEY,
    network     TEXT NOT NULL,
    seller      TEXT NOT NULL,
    inscription TEXT NOT NULL,
    txid        TEXT NOT NULL,
    vout        INTEGER NOT NULL,
    value       INTEGER NOT NULL,
    price       INTEGER NOT NULL,
    signature   TEXT NOT NULL,
    pubkey      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    created     REAL NOT NULL,
    expires     REAL NOT NULL,
    spent_by    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS doginal_open ON doginal_listing(status, network);
CREATE INDEX IF NOT EXISTS doginal_coin ON doginal_listing(txid, vout);
"""


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["coin"] = {"txid": d["txid"], "vout": int(d["vout"]), "value": int(d["value"])}
    return d


class Book:
    """Every Doginal listing this node holds: the seller's one signature over its coin and price, checked on the way in."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._open() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def register(self, rpc: Any, *, network: str, seller: str, coin: dict, inscription: str, price: int,
                 signature: str, pubkey: bytes, seconds: float = LISTED_FOR, params=None) -> dict:
        """File a signed listing, or say why not. Nothing from the request is believed that the chain, the Doginal source
        and the signature do not say: the coin's value and owner come from `gettxout`, the inscription from the chain,
        and the signature is checked against the digest worked out here."""
        sig = bytes.fromhex(str(signature or ""))
        if not sig or sig[-1] != funding.SINGLE_ANYONECANPAY:
            raise MarketError("a Doginal listing is signed SINGLE|ANYONECANPAY, and this signature says otherwise")
        if hash160(bytes(pubkey)) != b58check_decode(seller)[1]:
            raise MarketError("that public key is not the key behind the seller's address")
        txid, vout = str(coin.get("txid") or ""), int(coin.get("vout", -1))
        held = rpc.call("gettxout", txid, vout, True)
        if not held:
            raise MarketError(f"{txid[:16]}…:{vout} is spent or unseen here, so there is nothing to list")
        if str((held.get("scriptPubKey") or {}).get("hex") or "") != p2pkh_script(seller).hex():
            raise MarketError("that coin is not the seller's")
        value = int(round(float(held.get("value", 0)) * COIN))
        ask = build_ask(seller, {"txid": txid, "vout": vout, "value": value}, inscription, int(price), params)
        if not verify_signature(bytes.fromhex(ask.digest), sig[:-1], bytes(pubkey)):
            raise MarketError("that signature is not over this coin at this price")
        now = time.time()
        row = {"id": secrets.token_urlsafe(18), "network": network, "seller": seller, "inscription": inscription,
               "txid": txid, "vout": vout, "value": value, "price": int(price), "signature": sig.hex(),
               "pubkey": bytes(pubkey).hex(), "status": "open", "created": now, "expires": now + float(seconds),
               "spent_by": ""}
        with self._open() as conn:
            # A newer listing of the same coin is the one shown. The older signature still stands until the coin moves.
            conn.execute("UPDATE doginal_listing SET status='replaced' WHERE txid=? AND vout=? AND status='open'",
                         (txid, vout))
            conn.execute("INSERT INTO doginal_listing(id, network, seller, inscription, txid, vout, value, price, "
                         "signature, pubkey, status, created, expires, spent_by) "
                         "VALUES(:id,:network,:seller,:inscription,:txid,:vout,:value,:price,:signature,:pubkey,"
                         ":status,:created,:expires,:spent_by)", row)
        return self.get(row["id"])

    def get(self, listing_id: str) -> dict | None:
        with self._open() as conn:
            r = conn.execute("SELECT * FROM doginal_listing WHERE id=?", (str(listing_id),)).fetchone()
        return _row(r) if r else None

    def open_listings(self, network: str, limit: int = 200) -> list[dict]:
        with self._open() as conn:
            conn.execute("UPDATE doginal_listing SET status='expired' WHERE status='open' AND expires<=?",
                         (time.time(),))
            rows = conn.execute("SELECT * FROM doginal_listing WHERE network=? AND status='open' "
                                "ORDER BY created DESC LIMIT ?", (network, int(limit))).fetchall()
        return [_row(r) for r in rows]

    def close(self, listing_id: str, status: str, spent_by: str = "") -> None:
        with self._open() as conn:
            conn.execute("UPDATE doginal_listing SET status=?, spent_by=? WHERE id=?",
                         (status, spent_by, str(listing_id)))

    def live(self, rpc: Any, network: str, limit: int = 200) -> list[dict]:
        """The open listings whose coin is still unspent; a spent one is closed ('gone') as it is found."""
        out = []
        for row in self.open_listings(network, limit):
            if rpc.call("gettxout", row["txid"], int(row["vout"]), True):
                out.append(row)
            else:
                self.close(row["id"], "gone")
        return out


def public(row: dict) -> dict:
    """What a page shows of a listing: everything but the seller's signature, which only the buyer's completion needs."""
    return {k: row[k] for k in ("id", "network", "seller", "inscription", "coin", "price", "created", "expires")}

