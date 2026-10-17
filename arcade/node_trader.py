"""A facilitating node's trader: the same lock-script trades a browser tab makes, run by
the node for its operator (2026-10-13: node-facilitated trades "just like a regular
lock script that the client side does"; off by default, the operator's switch).

It is a trader like any other. It agrees terms over the Messenger (trade_courier.py), and
every move it makes is `trade.next_step` on what the chains show -- locks it funds from
the node's own wallet (Core pays to a lock's P2SH address like any other), claims and
refunds it signs with its own trade key (`<home>/trade.key`, 0600, beside the referee's),
paying back into the node's wallet. The counterparty trusts it with nothing: the lock
script holds both sides.

Coins on every leg for now; tokens and NFTs move through a lock exactly as htlc.move_out
shows, and come next (plan phase 5). A Doginal on Dogecoin moves through a lock by koinu
order, first and whole (htlc.doginal_out; [Claude 2026-10-15]).
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import htlc as H, trade as T, trade_courier as courier, trade_messages as M

COIN = 100_000_000
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


class TradeKey:
    """The node's own key for the locks it trades through: made once, kept 0600."""

    def __init__(self, home: Path):
        self.path = Path(home) / "trade.key"
        self._lock = threading.Lock()
        self._key = None

    def _private(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        if self._key is None:
            with self._lock:
                if not self.path.exists():
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    secret = secrets.randbelow(_N - 1) + 1
                    fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as f:
                        f.write(f"{secret:064x}\n")
                secret = int(self.path.read_text().strip(), 16)
            self._key = ec.derive_private_key(secret, ec.SECP256K1())
        return self._key

    @property
    def pubkey(self) -> bytes:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        return self._private().public_key().public_bytes(Encoding.X962, PublicFormat.CompressedPoint)

    def sign(self, digest: bytes, sighash_type: int = 1) -> bytes:
        """DER, low-S, and the sighash byte (as referee.Referee.sign)."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        der = self._private().sign(bytes(digest), ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        r, s = utils.decode_dss_signature(der)
        if s > _N // 2:
            s = _N - s
        return utils.encode_dss_signature(r, s) + bytes([sighash_type])


# --- what the chains show --------------------------------------------------------------------

def observe(rpc: Any, lock: dict | None, my_addresses: set[str],
            scanned: dict[str, int] | None = None) -> T.LockSeen | None:
    """A lock as its chain shows it: depth, value, script, and who spent it if anyone.
    `scanned` remembers per lock how far blocks were searched for its spender, so a
    long trade does not re-read the chain every tick."""
    if not lock:
        return None
    try:
        tx = rpc.call("getrawtransaction", lock["txid"], 1)
    except Exception:
        return None
    out = tx["vout"][int(lock["vout"])]
    seen = T.LockSeen(lock["txid"], int(lock["vout"]), round(float(out["value"]) * COIN),
                      out["scriptPubKey"]["hex"], int(tx.get("confirmations", 0)))
    if rpc.call("gettxout", lock["txid"], int(lock["vout"]), True) is not None:
        return seen
    spender = _spender(rpc, lock, scanned if scanned is not None else {})
    if spender is not None:
        vin = next(v for v in spender["vin"]
                   if v.get("txid") == lock["txid"] and v.get("vout") == int(lock["vout"]))
        seen.spent_by_script_sig = vin["scriptSig"]["hex"]
        seen.spent_by_me = any(a in my_addresses for o in spender["vout"]
                               for a in o["scriptPubKey"].get("addresses", []))
    return seen


def swap_big(state: Any) -> dict:
    """The operator's "big swap" lines per mainnet network, in sats (admin: swap_big:<net>);
    a swap with a coin leg at or above its line waits 3 confirmations instead of 1."""
    out = {}
    for network, default in T.BIG.items():
        try:
            out[network] = int(state.setting(f"swap_big:{network}", default) or default)
        except (TypeError, ValueError):
            out[network] = default
    return out


def asset_seen(seen: T.LockSeen | None, leg: T.Leg, lock_address: str, index: Any,
               rpc: Any) -> T.LockSeen | None:
    """`seen` with the ledger's word on a token or NFT leg: asset_ok, or -- while the
    ledger has not read the lock's block -- confirmations 0, so a lagging ledger makes a
    lock look not-yet-there and never wrong (trade.asset_at)."""
    if seen is None or leg.kind == "coin" or seen.spent_by_script_sig:
        return seen
    height = None
    if seen.confirmations > 0:
        height = int(rpc.call("getblockcount")) - seen.confirmations + 1
    said = T.asset_at(index, leg, lock_address, height)
    if said is None:
        seen.confirmations = 0
    else:
        seen.asset_ok = said
    return seen


def _spends(tx: dict, lock: dict) -> bool:
    return any(v.get("txid") == lock["txid"] and v.get("vout") == int(lock["vout"]) for v in tx["vin"])


def _spender(rpc: Any, lock: dict, scanned: dict[str, int]) -> dict | None:
    for txid in rpc.call("getrawmempool"):
        tx = rpc.call("getrawtransaction", txid, 1)
        if _spends(tx, lock):
            return tx
    key = f"{lock['txid']}:{lock['vout']}"
    tip = int(rpc.call("getblockcount"))
    start = scanned.get(key)
    if start is None:
        lock_tx = rpc.call("getrawtransaction", lock["txid"], 1)
        conf = int(lock_tx.get("confirmations", 0))
        start = tip - conf + 1 if conf else tip
    for height in range(max(0, start), tip + 1):
        block = rpc.call("getblock", rpc.call("getblockhash", height))
        for txid in block["tx"]:
            tx = rpc.call("getrawtransaction", txid, 1)
            if _spends(tx, lock):
                return tx
        scanned[key] = height + 1
    return None


# --- the trader --------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS link (            -- per trade: who, and where the locks are
    id        TEXT PRIMARY KEY,
    peer      TEXT NOT NULL,                  -- the counterparty's messaging key, hex
    my_lock   TEXT NOT NULL DEFAULT '',       -- "txid:vout"
    their_lock TEXT NOT NULL DEFAULT '',
    agreed    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS standing_order (  -- what this node will answer a proposal with
    id   TEXT PRIMARY KEY,
    give TEXT NOT NULL,                       -- leg JSON, giver = this node
    get  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


class NodeTrader:
    """One node's trades. `chains` maps a network name to its ChainContext; `messaging`
    is the chain trade steps travel on (PEP testnet, or regtest in tests); `enabled`
    is the operator's switch (node-facilitated trades)."""

    def __init__(self, state: Any, chains: dict[str, Any], messaging: Any,
                 enabled: Callable[[], bool] = lambda: True, doginals: Any = None):
        self.state, self.chains, self.messaging, self.enabled = state, chains, messaging, enabled
        # What sits on Dogecoin coins (arcade/doginals.Doginals): the app's own (funding.GUARD) unless one is given.
        self._doginals = doginals
        self.home = Path(state.home)
        self.key = TradeKey(self.home)
        self.book = T.Book(self.home / "trades.sqlite")
        self._db_path = self.home / "trades.sqlite"
        with self._db() as conn:
            conn.executescript(SCHEMA)
        self._scanned: dict[str, int] = {}
        self._addresses: dict[str, str] = {}

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # --- orders and proposals ----------------------------------------------------------

    def stand(self, order_id: str, give: dict, get: dict) -> None:
        """An order this node answers: it gives `give` for `get`, each
        {network, kind, asset, amount}. Keys come with a proposal, not the order."""
        import json
        from .trade_orders import one_chain_offer
        if give["network"] == get["network"] and not one_chain_offer(give, get):
            raise ValueError("a swap on one chain settles in one transaction on its own market; "
                             "lock-script swaps are between two chains (and Doginal offers in DOGE)")
        what = lambda d: {k: d.get(k, "") for k in ("network", "kind", "asset")} | {"amount": int(d["amount"])}
        with self._db() as conn:
            conn.execute("INSERT OR REPLACE INTO standing_order(id, give, get) VALUES(?,?,?)",
                         (order_id, json.dumps(what(give)), json.dumps(what(get))))

    def propose(self, peer_pub: bytes, order_id: str, give: T.Leg, get: T.Leg,
                now: int | None = None) -> T.Trade:
        """Start a trade as the initiator against `peer_pub`'s order: make the secret,
        plan the lock times, send the terms."""
        secret, h = H.make_secret()
        t1, t2 = T.plan_locktimes(give, get, T.INITIATOR, int(now or time.time()))
        trade = T.Trade(id=f"tr-{uuid.uuid4().hex[:16]}", role=T.INITIATOR, give=give, get=get,
                        secret_hash=h.hex(), secret=secret.hex(),
                        initiator_locktime=t1, responder_locktime=t2, need=self.need(give, get))
        self.book.save(trade)
        with self._db() as conn:
            conn.execute("INSERT INTO link(id, peer) VALUES(?,?)", (trade.id, peer_pub.hex()))
        courier.send(self.state, self.messaging, peer_pub, M.build(
            "propose", trade.id, order=order_id, give=M.leg_json(give), get=M.leg_json(get),
            secret_hash=h.hex(), t1=t1, t2=t2))
        return trade

    def need(self, give: T.Leg, get: T.Leg) -> int:
        """Confirmations this node waits for on a swap: the operator's lines (swap_big)."""
        return T.confirmations_for(give, get, swap_big(self.state))

    def leg(self, network: str, amount: int, taker_key: bytes, kind: str = "coin",
            asset: str = "") -> T.Leg:
        """A leg this node gives: its trade key as giver. A token or NFT goes from its home
        address (state.home_address), where the node's own tokens are kept."""
        return T.Leg(network, kind, str(asset), int(amount), self.key.pubkey.hex(), taker_key.hex())

    # --- telling the other nodes ----------------------------------------------------------

    #: How long a posted order stands before this node posts it again (it is shown only
    #: while this node is online in any case; the expiry bounds a node that vanished).
    POST_EVERY = 12 * 3600
    ORDER_LIFE = 24 * 3600

    def standing(self) -> list[dict]:
        import json
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM standing_order ORDER BY id").fetchall()
        return [{"id": r["id"], "give": json.loads(r["give"]), "get": json.loads(r["get"])} for r in rows]

    def withdraw(self, order_id: str) -> None:
        with self._db() as conn:
            conn.execute("DELETE FROM standing_order WHERE id=?", (order_id,))
            conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('posted', '0')")
            row = conn.execute("SELECT v FROM meta WHERE k='cancels'").fetchone()
            cancels = (row["v"].split(",") if row and row["v"] else []) + [order_id]
            conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('cancels', ?)", (",".join(cancels),))

    def batch(self, node_id: str, now: int | None = None) -> bytes:
        """This node's standing orders as an order inscription (trade_orders.py)."""
        from . import trade_orders as O
        now = int(now or time.time())
        with self._db() as conn:
            row = conn.execute("SELECT v FROM meta WHERE k='cancels'").fetchone()
        cancels = [c for c in (row["v"].split(",") if row and row["v"] else []) if c]
        orders = [{"id": o["id"], "give": {**o["give"]}, "get": {**o["get"]},
                   "trade_key": self.key.pubkey.hex(),
                   "msg_key": self.state.ensure_identity().public_bytes.hex(),
                   "node": node_id, "expires": now + self.ORDER_LIFE} for o in self.standing()]
        return O.batch_json(orders, cancels=cancels)

    def post_due(self, now: int | None = None) -> bool:
        with self._db() as conn:
            row = conn.execute("SELECT v FROM meta WHERE k='posted'").fetchone()
        return bool(self.standing()) and int(now or time.time()) - int(row["v"] if row else 0) >= self.POST_EVERY

    def post(self, node_id: str) -> str:
        """Inscribe this node's orders on the messaging chain, where every node reads them.
        Returns the inscription id."""
        from . import inscribe as inscribelib, trade_orders as O
        from .messaging.sender import MessageSender, funded_address
        body = self.batch(node_id)
        plan = inscribelib.plan(body, O.CONTENT_TYPE, "")
        with self.messaging.rpc() as rpc:
            sender = MessageSender(rpc, self.messaging.params, public_only=True)
            address = funded_address(rpc, prefer=self.state.derived_address)
            inscribelib.prepare_wallet(sender, address, plan)
            txids = sender.send_all(address, plan.payloads)
        with self._db() as conn:
            conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('posted', ?)", (str(int(time.time())),))
            conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('cancels', '')")
        return txids[0]

    # --- one pass --------------------------------------------------------------------------

    def tick(self, now: int | None = None) -> list[tuple[str, str, str]]:
        """Read new trade steps, then take each open trade one step. Returns what was
        done: [(trade id, step, why)]. Does nothing while the operator's switch is off."""
        if not self.enabled():
            return []
        done: list[tuple[str, str, str]] = []
        self._read_inbox()
        for trade in self.book.open_trades():
            step, why = self._advance(trade, int(now or time.time()))
            done.append((trade.id, step, why))
        return done

    def _read_inbox(self) -> None:
        import json
        with self._db() as conn:
            row = conn.execute("SELECT v FROM meta WHERE k='cursor'").fetchone()
        cursor = int(row["v"]) if row else 0
        for msg in courier.inbox(self.state, self.messaging, after_id=cursor):
            cursor = max(cursor, msg["id"])
            said, peer = msg["step"], msg["sender"]
            with self._db() as conn:
                link = conn.execute("SELECT * FROM link WHERE id=?", (said["id"],)).fetchone()
            if said["step"] == "propose" and link is None:
                self._consider(said, peer)
            elif link is not None and link["peer"] == peer:
                self._note(said)
        with self._db() as conn:
            conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('cursor', ?)", (str(cursor),))

    def _consider(self, said: dict, peer: str) -> None:
        """A proposal against one of this node's orders: accept exactly-matching terms."""
        import json
        with self._db() as conn:
            order = conn.execute("SELECT * FROM standing_order WHERE id=?", (said["order"],)).fetchone()
        if order is None:
            courier.send(self.state, self.messaging, bytes.fromhex(peer),
                         M.build("cancel", said["id"], reason="no such order here"))
            return
        want_give, want_get = json.loads(order["give"]), json.loads(order["get"])
        they_give, they_get = M.leg_from(said["give"]), M.leg_from(said["get"])
        mine = self.key.pubkey.hex()
        ok = (they_get.network == want_give["network"] and they_get.kind == want_give["kind"]
              and they_get.asset == want_give["asset"] and they_get.amount == want_give["amount"]
              and they_get.giver_key == mine
              and they_give.network == want_get["network"] and they_give.kind == want_get["kind"]
              and they_give.asset == want_get["asset"] and they_give.amount >= want_get["amount"]
              and they_give.taker_key == mine)
        if not ok:
            courier.send(self.state, self.messaging, bytes.fromhex(peer),
                         M.build("cancel", said["id"], reason="those are not this order's terms"))
            return
        trade = T.Trade(id=said["id"], role=T.RESPONDER, give=they_get, get=they_give,
                        secret_hash=said["secret_hash"],
                        initiator_locktime=said["t1"], responder_locktime=said["t2"],
                        need=self.need(they_get, they_give))
        self.book.save(trade)
        with self._db() as conn:
            conn.execute("INSERT INTO link(id, peer, agreed) VALUES(?,?,1)", (trade.id, peer))
        courier.send(self.state, self.messaging, bytes.fromhex(peer), M.build("accept", trade.id))

    def _note(self, said: dict) -> None:
        with self._db() as conn:
            if said["step"] == "accept":
                conn.execute("UPDATE link SET agreed=1 WHERE id=?", (said["id"],))
            elif said["step"] == "locked":
                conn.execute("UPDATE link SET their_lock=? WHERE id=?",
                             (f"{said['txid']}:{said['vout']}", said["id"]))
            elif said["step"] == "cancel":
                conn.execute("UPDATE link SET agreed=-1 WHERE id=?", (said["id"],))

    def _address(self, network: str) -> str:
        if network not in self._addresses:
            with self.chains[network].rpc() as rpc:
                self._addresses[network] = rpc.call("getnewaddress")
        return self._addresses[network]

    # --- tokens and NFTs in a lock -------------------------------------------------------

    def _asset(self, seen: T.LockSeen | None, leg: T.Leg, redeem: bytes, chain: Any,
               rpc: Any) -> T.LockSeen | None:
        """For a token or NFT leg, what the ledger says is at the lock. While the ledger
        has not read the lock's block yet the lock counts as unconfirmed (trade.asset_at)."""
        if leg.kind == "doginal":
            return self._doginal_seen(seen, leg, redeem, rpc)
        return asset_seen(seen, leg, H.address_of(redeem, chain.params),
                          self.state.token_index(chain), rpc)

    @property
    def doginals(self) -> Any:
        from . import funding
        got = self._doginals or funding.GUARD
        if got is None:
            raise ValueError("Doginals are not checked on this node: it trades none")
        return got

    def _doginal_seen(self, seen: T.LockSeen | None, leg: T.Leg, redeem: bytes,
                      rpc: Any) -> T.LockSeen | None:
        return doginal_seen(seen, leg, redeem, rpc, self.doginals)


    def _send_coins(self, rpc: Any, network: str, address: str, amount: int) -> str:
        """A coin leg into its lock with `sendtoaddress` -- which picks its own coins, and on
        Dogecoin could pick one carrying a Doginal (2026-10-15). So there, every coin an ordinary
        send may not use (doginals.spendable: inscribed, or small and not vouched for) is locked
        in the wallet for the one send and let go after; a coin already locked stays locked."""
        from . import funding
        guard = self._doginals or funding.GUARD
        if network != "doge" or guard is None:
            # Off Dogecoin there is nothing to set aside; a node that checks no Doginals sends as it always did
            # (the app sets the guard whenever it runs Dogecoin, so a live node always has one).
            return rpc.call("sendtoaddress", address, amount / COIN)
        coins = [{"txid": u["txid"], "vout": int(u["vout"]), "value": round(float(u["amount"]) * COIN)}
                 for u in rpc.call("listunspent", 0)]
        # From what is already known only: reading every coin in the wallet anew, one by one, held
        # held the swap (and the tests) up for good (2026-10-15).
        clean = {(c["txid"], c["vout"]) for c in guard.spendable_known(coins)}
        keep = [{"txid": c["txid"], "vout": c["vout"]} for c in coins if (c["txid"], c["vout"]) not in clean]
        if keep:
            rpc.call("lockunspent", False, keep)
        try:
            return rpc.call("sendtoaddress", address, amount / COIN)
        finally:
            if keep:
                rpc.call("lockunspent", True, keep)

    def _lock_doginal(self, leg: T.Leg, redeem: bytes, rpc: Any) -> str:
        """The Doginal into its lock from this node's wallet (htlc.doginal_lock_outputs):
        the coin holding exactly it at offset 0 as input 0, whole to the lock as output 0;
        the reserve to the same lock as output 1; clean wallet coins for both and the fee
        after it -- never a coin carrying another Doginal; change last."""
        from . import doginals as D, funding, txbuild
        found = None
        coins = [{"txid": u["txid"], "vout": int(u["vout"]), "value": round(float(u["amount"]) * COIN)}
                 for u in rpc.call("listunspent", 1)]
        for coin in coins:
            try:
                on = self.doginals.held(coin["txid"]).get(coin["vout"])
            except D.Unknown:
                continue
            if any(i.get("id") == leg.asset for i in on or []):
                bad = D.exactly(on, leg.asset)
                if bad:
                    raise ValueError(bad)
                found = coin
                break
        if found is None:
            raise ValueError(f"this node's wallet does not hold Doginal {leg.asset}")
        outputs = H.doginal_lock_outputs(found["value"], redeem)
        owed = sum(v for v, _ in outputs[1:])
        clean = sorted(self.doginals.spendable([c for c in coins if c is not found]), key=lambda c: c["value"])
        change_script = txbuild.p2pkh_script(rpc.call("getrawchangeaddress"))
        fees_in: list[dict] = []
        for coin in clean:
            fees_in.append(coin)
            fee = funding.price(1 + len(fees_in), outputs, funding.fees.MIN_FEE_PER_KB, change=True)
            if sum(c["value"] for c in fees_in) >= owed + fee:
                break
        else:
            raise ValueError("this node's wallet has too little clean Dogecoin to lock a Doginal")
        change = sum(c["value"] for c in fees_in) - owed - fee
        if change >= funding.fees.DUST_LIMIT:
            outputs = outputs + [(change, change_script)]
        chosen = [found] + fees_in
        raw = txbuild.build_raw_tx([(c["txid"], c["vout"]) for c in chosen], outputs)
        signed = rpc.call("signrawtransaction", raw)
        if not signed.get("complete"):
            raise ValueError("this node's wallet could not sign its Doginal lock")
        return rpc.call("sendrawtransaction", signed["hex"])

    def _lock_asset(self, leg: T.Leg, lock_address: str, chain: Any, rpc: Any) -> str:
        """The token or NFT into the lock, from this node's home address, with the reserve
        that pays its way out as the lock output (the reference: last, so the lock is the
        recipient)."""
        from . import inscriptions as I, payload as P, tokens as tokenlib
        if leg.kind == "token":
            payload = tokenlib.send_payload(int(leg.asset), leg.amount)
        else:
            payload = P.AnyData(data=I.Transfer(txid=bytes.fromhex(leg.asset)).encode()).encode()
        sender = tokenlib.TokenSender(rpc, chain.params)
        prepared = sender.prepare(self.state.home_address(chain), payload, lock_address,
                                  reference_value=T.LOCK_RESERVE)
        return sender.broadcast(prepared)

    def _way_out(self, coin: dict, leg: T.Leg, redeem: bytes, chain: Any):
        """The claim or refund's inputs and outputs, to this node: the coin itself, or the
        token/NFT moved out by the lock's own reserve."""
        to = self._address(leg.network)
        if leg.kind == "coin":
            return H.pay_out(coin, to)
        if leg.kind == "doginal":
            return H.doginal_out(coin, {"txid": coin["txid"], "vout": 1, "value": coin["reserve"]}, to)
        return H.move_out(coin, H.address_of(redeem, chain.params), T.move_data(leg), to)

    def _advance(self, trade: T.Trade, now: int) -> tuple[str, str]:
        with self._db() as conn:
            link = conn.execute("SELECT * FROM link WHERE id=?", (trade.id,)).fetchone()
        if link is None:
            return T.WAIT, "no link"
        if link["agreed"] == -1 and not link["my_lock"]:
            trade.state = "aborted"
            self.book.save(trade)
            return T.ABORT, "the other side walked away before anything was locked"
        if not link["agreed"]:
            return T.WAIT, "waiting for the other side to accept"
        mine = _pointer(link["my_lock"])
        theirs = _pointer(link["their_lock"])
        give_chain, get_chain = self.chains[trade.give.network], self.chains[trade.get.network]
        with give_chain.rpc() as grpc, get_chain.rpc() as trpc:
            facts = T.Facts(
                mine=self._asset(observe(grpc, mine, {self._address(trade.give.network)}, self._scanned),
                                 trade.give, trade.my_redeem(), give_chain, grpc),
                theirs=self._asset(observe(trpc, theirs, {self._address(trade.get.network)}, self._scanned),
                                   trade.get, trade.their_redeem(), get_chain, trpc))
            step, why = T.next_step(trade, facts, now)
            peer = bytes.fromhex(link["peer"])
            if step == T.LOCK:
                lock_address = H.address_of(trade.my_redeem(), give_chain.params)
                if trade.give.kind == "coin":
                    txid = self._send_coins(grpc, trade.give.network, lock_address, trade.give.amount)
                elif trade.give.kind == "doginal":
                    txid = self._lock_doginal(trade.give, trade.my_redeem(), grpc)
                else:
                    txid = self._lock_asset(trade.give, lock_address, give_chain, grpc)
                tx = grpc.call("getrawtransaction", txid, 1)
                vout = next(o["n"] for o in tx["vout"]
                            if lock_address in o["scriptPubKey"].get("addresses", []))
                with self._db() as conn:
                    conn.execute("UPDATE link SET my_lock=? WHERE id=?", (f"{txid}:{vout}", trade.id))
                courier.send(self.state, self.messaging, peer, M.build(
                    "locked", trade.id, network=trade.give.network, txid=txid, vout=vout))
            elif step == T.CLAIM:
                coin = {**theirs, "value": facts.theirs.value, "reserve": facts.theirs.reserve}
                inputs, outputs = self._way_out(coin, trade.get, trade.their_redeem(), get_chain)
                txid = trpc.call("sendrawtransaction", H.claimed(
                    inputs, outputs, trade.their_redeem(), bytes.fromhex(trade.secret), self.key.sign))
                courier.send(self.state, self.messaging, peer, M.build(
                    "claimed", trade.id, network=trade.get.network, txid=txid))
            elif step == T.REFUND:
                coin = {**mine, "value": facts.mine.value, "reserve": facts.mine.reserve}
                inputs, outputs = self._way_out(coin, trade.give, trade.my_redeem(), give_chain)
                txid = grpc.call("sendrawtransaction", H.refunded(
                    inputs, outputs, trade.my_redeem(), self.key.sign))
                courier.send(self.state, self.messaging, peer, M.build(
                    "refunded", trade.id, network=trade.give.network, txid=txid))
            elif step in (T.DONE, T.ABORT):
                trade.state = "done" if step == T.DONE else "aborted"
            trade.note = why
            self.book.save(trade)
        return step, why


def _pointer(text: str) -> dict | None:
    if not text:
        return None
    txid, vout = text.split(":")
    return {"txid": txid, "vout": int(vout)}


def doginal_seen(seen: T.LockSeen | None, leg: T.Leg, redeem: bytes, rpc: Any,
                 doginals: Any) -> T.LockSeen | None:
    """For a Doginal leg, whether the lock holds it at offset 0 of output 0 with its
    reserve as output 1 (trade.doginal_at). While the chain cannot be read the lock
    counts as unconfirmed, never as wrong. Module level: a tab's swaps read it too
    (/account/trade/facts, 2026-10-15)."""
    if seen is None or seen.spent_by_script_sig:
        return seen
    lock_tx = rpc.call("getrawtransaction", seen.txid, 1)
    parent = rpc.call("getrawtransaction", lock_tx["vin"][0]["txid"], 1)
    said = T.doginal_at(doginals, leg, redeem, lock_tx, parent) if seen.vout == 0 else False
    if said is None:
        seen.confirmations = 0
    else:
        seen.asset_ok = said
    if len(lock_tx["vout"]) > 1:
        seen.reserve = round(float(lock_tx["vout"][1]["value"]) * COIN)
    return seen
