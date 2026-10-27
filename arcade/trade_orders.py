"""Orders on the chain: what a trader will swap, visible to every node while it is online.

2026-10-13: a facilitating node's trades "should be visible to other node operators
as long as that node is online. They can be inscribed like any other order the only
difference is that the node operator is the one who is doing the listing." So an order is
an inscription, batched like a seller's listings (listing_announce.py): content type
`CONTENT_TYPE`, filed by every node that reads it.

    {"arcade": "orders", "v": 1,
     "orders":  [{id, give: {network, kind, asset, amount}, get: {...},
                  trade_key, take_key?, msg_key, node, expires}],
     "cancels": [order id, ...]}

* `trade_key` is the lister's key in the locks (it gives `give` and takes `get`);
  `msg_key` is where trade steps are sealed to (the Messenger); `node` the mesh id of the
  node that answers for it -- the lister's own home node for a tab, itself for a
  facilitating node. The order shows on other nodes only while that node is online.
* Who listed it is the address that inscribed it, and only that address can cancel it.
  Nothing else here needs a signature: a lock pays only the key that is really in it, so
  an order naming somebody else's keys can waste a taker's time (they refund) and never
  their coins (arcade/trade.py checks every lock on the chain).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from . import ord_chains
from .config import NETWORKS, require_p2sh_payable, require_untimed
from .doginals import INSCRIPTION_ID

CONTENT_TYPE = "application/vnd.arcade.orders+json"
VERSION = 1
MAX_ORDERS = 50                 # per batch: an inscription, not a database
MAX_EXPIRY = 30 * 24 * 3600
COIN = 100_000_000
_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_KEY33 = re.compile(r"^0[23][0-9a-f]{64}$")
_HEX32 = re.compile(r"^[0-9a-f]{64}$")
#: A DRC-20 / PRC-20 tick as a swap leg names it (lower case, as the index files it): no space, no path character.
TICK20 = re.compile(r"^[^\sA-Z/?#%&]{1,16}$")
#: The most whole tokens one DRC-20 / PRC-20 leg moves (a whole number: the JS port counts it in a Number).
TOKEN20_MAX = 10 ** 15


class OrderError(ValueError):
    """Not an order this version reads."""


def _side(said: Any) -> dict:
    if not isinstance(said, dict):
        raise OrderError("a side of an order is an object")
    network, kind = said.get("network"), said.get("kind")
    if network not in NETWORKS:
        raise OrderError(f"no such network {network!r}")
    if kind not in ("coin", "token", "nft", "doginal", "token20"):
        raise OrderError("an order moves a coin, a token, an nft, a doginal or a DRC-20 / PRC-20 token")
    amount = said.get("amount")
    if not isinstance(amount, int) or amount <= 0:
        raise OrderError("an amount is a positive whole number")
    asset = str(said.get("asset", ""))
    if (kind == "coin" and asset) or (kind == "token" and not asset.isdigit()) or \
            (kind == "nft" and (not _HEX32.match(asset) or amount != 1)):
        raise OrderError("that asset does not fit that kind")
    # [Claude 2026-10-15] A Doginal (the operator: "use the atomic swap feature we have to trade doginals"): `asset` names its
    # inscription id, as ord-dogecoin writes it; one, on Dogecoin, locked by koinu order (htlc.doginal_out).
    # Pepinals too (2026-10-17: "Do the same for pepinals"): the same ord inscriptions on Pepecoin mainnet.
    # Any chain whose ords are moved here and on which a lock can be made (ord_chains.swappable).
    if kind == "doginal" and (not ord_chains.swappable(network) or not INSCRIPTION_ID.match(asset) or amount != 1):
        raise OrderError(f"a doginal (or pepinal) is on {ord_chains.chains_said(ord_chains.swap_nets())}, named by "
                         "its inscription id, and moves exactly one")
    # A DRC-20 / PRC-20 token (2026-10-22: "Add prc 20 and drc 20 tokens to the token/coin exchange"): `asset`
    # its tick, `amount` WHOLE tokens; locked as a transfer note on the lock itself (arcade/token20_swap.py).
    if kind == "token20" and (network not in ("doge", "main") or not TICK20.match(asset) or amount > TOKEN20_MAX):
        raise OrderError("a DRC-20 / PRC-20 token is on Dogecoin or Pepecoin main, named by its tick (lower "
                         "case), and moves a whole number of tokens")
    out = {"network": network, "kind": kind, "asset": asset, "amount": amount}
    # The lister's lock-fee terms for the side it locks (2026-10-21: "the sender of each lock script pay the
    # fee", "an operator fee output to each lock script"): its own arcade's rate and payee, which the taker copies into
    # that leg (trade.operator_fee). Optional both ways: an order without them is from an arcade before that day, and
    # an older reader drops them (that side then trades at rate 0).
    if said.get("fee_bps") is not None:
        from .trade_messages import TradeMessageError, fee_terms
        try:
            bps, to = fee_terms(said)
        except TradeMessageError as exc:
            raise OrderError(str(exc)) from None
        out["fee_bps"] = bps
        if to:
            out["fee_to"] = to
    return out


def check_order(said: Any, now: float | None = None) -> dict:
    """One order, every field checked; returns the cleaned order."""
    if not isinstance(said, dict):
        raise OrderError("an order is an object")
    if not isinstance(said.get("id"), str) or not _ID.match(said["id"]):
        raise OrderError("an order id is 8-64 letters, digits, - or _")
    give, get = _side(said.get("give")), _side(said.get("get"))
    if give["network"] == get["network"] and not one_chain_offer(give, get):
        # 2026-10-13: a swap on ONE chain is instant -- one atomic transaction on that
        # chain's own markets (pre-signed legs) -- so lock scripts are for two chains only.
        raise OrderError("a swap on one chain settles in one transaction on its own market")
    if give["network"] != get["network"]:
        # Two chains settle through lock scripts that refund by CHECKLOCKTIMEVERIFY: never on a chain that does not
        # enforce it (Wojakcoin, 2026-10-25), where the side that locked there could refund at once and keep both.
        for side in (give, get):
            params = NETWORKS.get(side["network"])
            if params is not None and not getattr(params, "cltv_enforced", True):
                raise OrderError(f"{params.label} does not enforce lock times, so it can't be swapped across chains")
            # Nor where the lock itself can't be made or spent (2026-10-27): an order that can never settle stays out of
            # the book, rather than failing once somebody takes it.
            try:
                if params is not None:
                    require_p2sh_payable(params)
                    require_untimed(params)
            except ValueError as exc:
                raise OrderError(str(exc)) from None
    for field, pattern in (("trade_key", _KEY33), ("msg_key", _HEX32), ("node", _HEX32)):
        if not isinstance(said.get(field), str) or not pattern.match(said[field]):
            raise OrderError(f"an order's {field} is hex of the right length")
    # The key the lister TAKES with, on the `get` chain, when it is not trade_key: a browser
    # wallet has a key per chain (coin type 1 on testnet, 3 on mainnet and Dogecoin).
    take_key = said.get("take_key", said["trade_key"])
    if not isinstance(take_key, str) or not _KEY33.match(take_key):
        raise OrderError("an order's take_key is a compressed public key, in hex")
    expires = said.get("expires")
    now = now or time.time()
    if not isinstance(expires, int) or not now < expires <= now + MAX_EXPIRY:
        raise OrderError("an order expires in the future, within 30 days")
    out = {"id": said["id"], "give": give, "get": get, "trade_key": said["trade_key"],
           "take_key": take_key,
           "msg_key": said["msg_key"], "node": said["node"], "expires": expires}
    if said.get("tab") is True:
        # Listed from a browser, answered by that browser: shown only while its page is open
        # (order_presence, 2026-10-17). Older readers ignore the field.
        out["tab"] = True
    if said.get("delegated") is True:
        # Finished by its lister's node with the order's own key (node_trader.delegate): no page needed.
        out["delegated"] = True
    return out


def batch_json(orders: list[dict], cancels: list[str] = ()) -> bytes:
    """What goes on the chain: some orders (checked), and ids to cancel."""
    if len(orders) > MAX_ORDERS:
        raise OrderError(f"at most {MAX_ORDERS} orders to an inscription")
    clean = [check_order(o) for o in orders]
    for c in cancels:
        if not isinstance(c, str) or not _ID.match(c):
            raise OrderError("a cancel names an order id")
    return json.dumps({"arcade": "orders", "v": VERSION, "orders": clean,
                       "cancels": list(cancels)}, separators=(",", ":"), sort_keys=True).encode()


SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_order (
    id          TEXT NOT NULL,
    lister      TEXT NOT NULL,      -- the address that inscribed it
    network     TEXT NOT NULL,      -- the chain the inscription is on
    inscription TEXT NOT NULL,
    body        TEXT NOT NULL,      -- the checked order, JSON
    node        TEXT NOT NULL,
    expires     INTEGER NOT NULL,
    cancelled   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (lister, id)
);
CREATE TABLE IF NOT EXISTS order_cursor (network TEXT PRIMARY KEY, number INTEGER NOT NULL);
"""


class OrderBook:
    """Every node's copy of the orders inscribed on the chains it reads."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._open() as conn:
            conn.executescript(SCHEMA)
        #: Told the ids of every order that changed here (book_relay.py says this arcade's own to the others).
        self.on_change: Callable[[list[str]], None] | None = None

    def _changed(self, ids: list[str]) -> None:
        if ids and self.on_change is not None:
            try:
                self.on_change(list(ids))
            except Exception:                                  # noqa: BLE001 -- the book is right either way
                pass

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def file_batch(self, lister: str, network: str, inscription: str, body: bytes,
                   now: float | None = None) -> int:
        """File one batch as `lister` inscribed it. Orders that do not check out are
        skipped one by one; cancels touch only this lister's own orders."""
        try:
            said = json.loads(body)
        except ValueError:
            return 0
        if not isinstance(said, dict) or said.get("arcade") != "orders" or said.get("v") != VERSION:
            return 0
        filed, touched = 0, []
        with self._open() as conn:
            for raw in (said.get("orders") or [])[:MAX_ORDERS]:
                try:
                    order = check_order(raw, now)
                except OrderError:
                    continue
                # Posted again by its own lister (a page's repost, 2026-10-17): its life is extended, not ignored.
                conn.execute("INSERT INTO trade_order(id, lister, network, inscription, "
                             "body, node, expires) VALUES(?,?,?,?,?,?,?) "
                             "ON CONFLICT(lister, id) DO UPDATE SET expires=excluded.expires, body=excluded.body "
                             "WHERE trade_order.cancelled=0",
                             (order["id"], lister, network, inscription, json.dumps(order),
                              order["node"], order["expires"]))
                filed += 1
                touched.append(order["id"])
            for oid in said.get("cancels") or []:
                if isinstance(oid, str):
                    conn.execute("UPDATE trade_order SET cancelled=1 WHERE lister=? AND id=?",
                                 (lister, oid))
                    touched.append(oid)
        self._changed(touched)
        return filed

    def set_taking(self, order_id: str, taking: bool) -> None:
        """Off the book while somebody's swap on it is under way (cancelled=2), back on if it falls through
        (2026-10-17: "when somebody clicks buy it needs to be instantly updated ... so that nobody else clicks
        buy on the same order")."""
        with self._open() as conn:
            if taking:
                conn.execute("UPDATE trade_order SET cancelled=2 WHERE id=? AND cancelled=0", (order_id,))
            else:
                conn.execute("UPDATE trade_order SET cancelled=0 WHERE id=? AND cancelled=2", (order_id,))
        self._changed([order_id])

    def cancel_id(self, order_id: str) -> None:
        """Off this node's book: an order its node finished for the seller (node_trader delegated orders)."""
        with self._open() as conn:
            conn.execute("UPDATE trade_order SET cancelled=1 WHERE id=?", (order_id,))
        self._changed([order_id])

    def cancelled_of(self, lister: str, ids: list[str]) -> list[str]:
        """Which of `ids`, listed by `lister`, this book already holds as cancelled (a tab that still lists them should
        forget them: 2026-10-23, @apple's tab re-posted two long-cancelled frgy orders with every new listing)."""
        if not lister or not ids:
            return []
        with self._open() as conn:
            return [r[0] for r in conn.execute(
                f"SELECT id FROM trade_order WHERE cancelled=1 AND lister=? AND id IN ({','.join('?' * len(ids))})",
                (lister, *ids))]

    def cancel_local(self, listers: list[str], ids: list[str]) -> int:
        """Mark `ids` cancelled, where one of `listers` listed them (its own withdraw, said to this node)."""
        if not listers or not ids:
            return 0
        with self._open() as conn:
            got = conn.execute(
                f"UPDATE trade_order SET cancelled=1 WHERE cancelled=0 AND lister IN ({','.join('?' * len(listers))})"
                f" AND id IN ({','.join('?' * len(ids))})", (*listers, *ids))
            n = got.rowcount
        self._changed(ids)
        return n

    # --- one book across arcades (book_relay.py, 2026-10-19) ---------------------------------------------

    def homed(self, node: str, ids: list[str] | None = None, now: float | None = None) -> list[dict]:
        """The orders whose home is `node` (`ids`: only those), as rows: lister, network, body, state -- "live",
        "taking", or "gone" (cancelled or expired)."""
        now = int(now or time.time())
        sql, args = "SELECT * FROM trade_order WHERE node=?", [node]
        if ids is not None:
            if not ids:
                return []
            sql += f" AND id IN ({','.join('?' * len(ids))})"
            args += list(ids)
        with self._open() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [{"id": r["id"], "lister": r["lister"], "network": r["network"], "body": json.loads(r["body"]),
                 "state": "gone" if r["cancelled"] == 1 or r["expires"] <= now
                 else "taking" if r["cancelled"] == 2 else "live"} for r in rows]

    def apply_from(self, origin: str, rows: list[dict], full: bool = False, now: float | None = None) -> int:
        """What arcade `origin` says of the orders whose home it is, filed here. Only those: an arcade speaks for
        its own orders and nobody else's. `full`: that is all of them, so any other of its orders here is gone."""
        changed = 0
        said: set[str] = set()
        with self._open() as conn:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                state, lister, network = row.get("state"), str(row.get("lister") or ""), str(row.get("network") or "")
                oid = str(row.get("id") or "")
                if not oid:
                    continue
                said.add(oid)
                if state in ("live", "taking"):
                    try:
                        order = check_order(row.get("body"), now)
                    except OrderError:
                        continue
                    if order["node"] != origin or order["id"] != oid or not lister or network not in NETWORKS:
                        continue
                    conn.execute("INSERT INTO trade_order(id, lister, network, inscription, body, node, expires, "
                                 "cancelled) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(lister, id) DO UPDATE SET "
                                 "expires=excluded.expires, body=excluded.body, cancelled=excluded.cancelled "
                                 "WHERE trade_order.node=excluded.node AND trade_order.cancelled != 1",
                                 (oid, lister, network, f"mesh-{origin[:16]}", json.dumps(order), origin,
                                  order["expires"], 2 if state == "taking" else 0))
                    changed += 1
                elif state == "gone":
                    changed += conn.execute("UPDATE trade_order SET cancelled=1 WHERE id=? AND node=? AND cancelled!=1",
                                            (oid, origin)).rowcount
            if full:
                for r in conn.execute("SELECT id FROM trade_order WHERE node=? AND cancelled!=1", (origin,)).fetchall():
                    if r["id"] not in said:
                        conn.execute("UPDATE trade_order SET cancelled=1 WHERE id=? AND node=?", (r["id"], origin))
                        changed += 1
        return changed

    def visible(self, online: Callable[[str], bool], now: float | None = None,
                tab_live: Callable[[str], bool] | None = None,
                browser: Callable[[str], bool] | None = None) -> list[dict]:
        """Orders to show: not expired, not cancelled, and their node online right now -- and one
        a browser listed (`tab`), only while that lister's page is open (order_presence)."""
        now = now or time.time()
        with self._open() as conn:
            rows = conn.execute("SELECT * FROM trade_order WHERE cancelled=0 AND expires>? "
                                "ORDER BY rowid", (int(now),)).fetchall()
        out = []
        for row in rows:
            body = json.loads(row["body"])
            # `browser`: a lister known to list from a browser though its order predates the flag
            # (this node's own accounts).
            # A delegated order is answered by its node, not a page: it stands while that node is online.
            tab = not body.get("delegated") and (body.get("tab") or (browser is not None and browser(row["lister"])))
            if online(row["node"]) and not (tab and tab_live is not None
                                            and not tab_live(row["lister"])):
                out.append({**json.loads(row["body"]), "lister": row["lister"],
                            "inscription": row["inscription"], "network": row["network"]})
        return out

    def standing_of(self, listers: list[str], now: float | None = None) -> list[dict]:
        """`listers`' orders standing on the book right now (not cancelled, not being taken, not expired), as their
        bodies: what each holds of its lister's (2026-10-22: a listing "should be locked as listed until it is
        unlisted or purchased"). One being taken (cancelled=2) holds nothing here: its own lock is under way."""
        if not listers:
            return []
        now = int(now or time.time())
        with self._open() as conn:
            rows = conn.execute(f"SELECT body FROM trade_order WHERE cancelled=0 AND expires>? AND lister IN "
                                f"({','.join('?' * len(listers))})", (now, *listers)).fetchall()
        return [json.loads(r["body"]) for r in rows]

    def lister_of(self, order_id: str) -> str:
        """Who listed an order (expired or cancelled included): "" if it was never filed here."""
        with self._open() as conn:
            row = conn.execute("SELECT lister FROM trade_order WHERE id = ? ORDER BY rowid DESC LIMIT 1",
                               (str(order_id),)).fetchone()
        return row["lister"] if row else ""

    def tab_listers(self, now: float | None = None) -> set[str]:
        """Who has a standing order (the pool watch redraws the markets when any of them come or go)."""
        now = now or time.time()
        with self._open() as conn:
            rows = conn.execute("SELECT lister, body FROM trade_order WHERE cancelled=0 AND expires>?",
                                (int(now),)).fetchall()
        # Every lister, flagged or not: whether each is live is the caller's question, and asking
        # it of a node-held order costs nothing and never changes.
        return {r["lister"] for r in rows}

    def cursor(self, network: str) -> int:
        with self._open() as conn:
            row = conn.execute("SELECT number FROM order_cursor WHERE network=?", (network,)).fetchone()
        # -1 before the first look: inscription numbers start at 0.
        return int(row["number"]) if row else -1

    def set_cursor(self, network: str, number: int) -> None:
        with self._open() as conn:
            conn.execute("INSERT OR REPLACE INTO order_cursor(network, number) VALUES(?,?)",
                         (network, int(number)))


def import_announced(book: OrderBook, index: Any, network: str, limit: int = 200) -> int:
    """File every order batch inscribed on `network` since the last look (by inscription
    number, as listing_announce does). Returns how many orders were filed."""
    last = book.cursor(network)
    with index.open() as db:
        rows = db.conn.execute(
            "SELECT txid, number, creator FROM inscription WHERE content_type=? AND number > ? "
            "ORDER BY number LIMIT ?", (CONTENT_TYPE, last, int(limit))).fetchall()
    filed = 0
    for row in rows:
        try:
            found = index.inscription_content(row["txid"])
            body = found[1] if found else b""
        except Exception:
            body = b""
        filed += book.file_batch(row["creator"], network, row["txid"], body)
        last = max(last, int(row["number"]))
    if rows:
        book.set_cursor(network, last)
    return filed


def coin_book(orders: list[dict], base: str, quote: str) -> dict:
    """A coin pair's market out of the visible orders (2026-10-13: "Will a PEP/DOGE
    pair be available?"): coin-for-coin orders between `base` and `quote`, both ways.

    asks: they give the base coin for the quote coin, cheapest first;
    bids: they give the quote coin for the base coin, dearest first.
    `price` is quote per base -- DOGE per PEP on the PEP/DOGE market. Each order is taken
    whole: a lock moves exactly its amount, so there are no partial fills."""
    def coin(side: dict, network: str) -> bool:
        return side["kind"] == "coin" and side["network"] == network
    asks, bids = [], []
    for o in orders:
        if coin(o["give"], base) and coin(o["get"], quote):
            asks.append({**o, "price": o["get"]["amount"] / o["give"]["amount"],
                         "base_amount": o["give"]["amount"], "quote_amount": o["get"]["amount"]})
        elif coin(o["give"], quote) and coin(o["get"], base):
            bids.append({**o, "price": o["give"]["amount"] / o["get"]["amount"],
                         "base_amount": o["get"]["amount"], "quote_amount": o["give"]["amount"]})
    asks.sort(key=lambda o: o["price"])
    bids.sort(key=lambda o: -o["price"])
    return {"asks": asks, "bids": bids,
            "spread": (asks[0]["price"] - bids[0]["price"]) if asks and bids else None}



#: The order coins go in a coin pair's name: the lower rank is the base (PEP TEST/PEP,
#: PEP TEST/DOGE, PEP/DOGE), so each pair of chains has one market, not two.
RANK = {"regtest": 0, "test": 0, "main": 1, "doge": 2, "ltc": 3}   # ltc: PEP/LTC, DOGE/LTC


def one_chain_offer(a: dict, b: dict) -> bool:
    """A swap on ONE chain that lock scripts still settle: a Doginal against a coin on Dogecoin (2026-10-15: offers in
    DOGE for a Doginal -- Dogecoin's own one-transaction market is listings only, the seller naming the price, so an offer
    goes through the same two locks a cross-chain swap does, both on Dogecoin)."""
    kinds = {a.get("kind"), b.get("kind")}
    net = a.get("network")
    return net == b.get("network") and ((kinds == {"doginal", "coin"} and ord_chains.swappable(net)) or
                                         (kinds == {"token20", "coin"} and net in ("doge", "main")))


def leg_key(side: dict) -> str:
    """One side of a market as a path piece: "main-coin", "test-token-26", "test-nft-<txid>"."""
    if side["kind"] == "coin":
        return f"{side['network']}-coin"
    return f"{side['network']}-{side['kind']}-{side.get('asset') or ''}"


def parse_leg_key(key: str) -> dict | None:
    """`leg_key` read back: {network, kind, asset}, or None if it is not one."""
    parts = str(key or "").split("-", 2)
    if len(parts) == 2 and parts[1] == "coin" and parts[0] in NETWORKS:
        return {"network": parts[0], "kind": "coin", "asset": ""}
    if (len(parts) == 3 and parts[0] in NETWORKS and parts[1] in ("token", "nft", "doginal") and parts[2]
            and re.fullmatch(r"[0-9]+" if parts[1] == "token" else r"[0-9a-fA-F]{64}i[0-9]+" if parts[1] == "doginal"
                             else r"[0-9a-fA-F]{64}(i[0-9]+)?", parts[2])):
        return {"network": parts[0], "kind": parts[1], "asset": parts[2].lower()}
    if len(parts) == 3 and parts[0] in ("doge", "main") and parts[1] == "token20" and TICK20.match(parts[2].lower()):
        return {"network": parts[0], "kind": "token20", "asset": parts[2].lower()}
    return None


def base_first(a: dict, b: dict) -> tuple[dict, dict]:
    """(base, quote) for a market between two sides: a token or piece is priced in what it
    is traded for (a coin, or a token on another chain by rank); two coins by RANK."""
    def order(s: dict) -> tuple:
        return ({"nft": 0, "doginal": 0, "token": 1, "token20": 1, "coin": 2}.get(s["kind"], 3), RANK.get(s["network"], 9),
                int(s["asset"]) if s["kind"] == "token" else 0)
    return (a, b) if order(a) <= order(b) else (b, a)


def pair_book(orders: list[dict], base: dict, quote: dict) -> dict:
    """Any market's book out of the visible orders (2026-10-15: "all pairs established
    the same way"): swaps that give `base` for `quote` (asks) or `quote` for `base` (bids).
    `base` and `quote` are {network, kind, asset, divisible}.

    `price` is quote per WHOLE unit of base -- a coin's and a divisible token's raw units are
    hundred-millionths, an NFT is one. asks cheapest first, bids dearest first. Each order is
    taken whole: a lock moves exactly its amount, so there are no partial fills."""
    def scale(s: dict) -> int:
        return COIN if s["kind"] == "coin" or (s["kind"] == "token" and s.get("divisible", True)) else 1

    def same(side: dict, want: dict) -> bool:
        return (side["kind"] == want["kind"] and side["network"] == want["network"]
                and (want["kind"] == "coin" or str(side.get("asset") or "").lower() == str(want["asset"]).lower())
                and int(side["amount"]) > 0)

    bs, qs = scale(base), scale(quote)
    asks, bids = [], []
    for o in orders:
        if same(o["give"], base) and same(o["get"], quote):
            units, paid = int(o["give"]["amount"]), int(o["get"]["amount"])
            asks.append({**o, "price": (paid / qs) / (units / bs), "base_amount": units, "quote_amount": paid})
        elif same(o["give"], quote) and same(o["get"], base):
            units, paid = int(o["get"]["amount"]), int(o["give"]["amount"])
            bids.append({**o, "price": (paid / qs) / (units / bs), "base_amount": units, "quote_amount": paid})
    asks.sort(key=lambda o: o["price"])
    bids.sort(key=lambda o: -o["price"])
    return {"asks": asks, "bids": bids,
            "spread": (asks[0]["price"] - bids[0]["price"]) if asks and bids else None}


def swap_markets(orders: list[dict]) -> dict[tuple[str, str], int]:
    """Every cross-chain market with a swap standing, base first: {(base key, quote key): orders}."""
    seen: dict[tuple[str, str], int] = {}
    for o in orders:
        if o["give"]["network"] == o["get"]["network"]:
            continue
        base, quote = base_first(o["give"], o["get"])
        key = (leg_key(base), leg_key(quote))
        seen[key] = seen.get(key, 0) + 1
    return seen

def asset_book(orders: list[dict], network: str, kind: str, asset: str, quote: str,
               divisible: bool = True) -> dict:
    """A testnet asset's market against a coin on another chain (2026-10-15): `pair_book`
    with the asset (`kind` token or nft) as base and the `quote` coin as quote."""
    return pair_book(orders, {"network": network, "kind": kind, "asset": str(asset), "divisible": divisible},
                     {"network": quote, "kind": "coin", "asset": ""})

def asset_markets(orders: list[dict], network: str, quote: str) -> dict[tuple[str, str], int]:
    """Which testnet assets have a swap standing against the `quote` coin: {(kind, asset): orders}."""
    seen: dict[tuple[str, str], int] = {}
    for o in orders:
        for a, b in ((o["give"], o["get"]), (o["get"], o["give"])):
            if (a["kind"] in ("token", "nft") and a["network"] == network
                    and b["kind"] == "coin" and b["network"] == quote):
                key = (a["kind"], str(a.get("asset") or ""))
                seen[key] = seen.get(key, 0) + 1
    return seen
