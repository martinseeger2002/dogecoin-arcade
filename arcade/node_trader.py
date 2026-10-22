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
from .config import NETWORKS

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


class HeldKey(TradeKey):
    """One delegated order's key (2026-10-17: orders that fill "without the counterparty being on Arcade"):
    made in the seller's browser from its seed, one per order, handed to this node to finish that order only."""

    def __init__(self, secret_hex: str):
        secret = int(secret_hex, 16)
        if not 0 < secret < _N:
            raise ValueError("that is not a key")
        self._secret, self._key, self._lock = secret, None, threading.Lock()
        self.path = None

    def _private(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        if self._key is None:
            self._key = ec.derive_private_key(self._secret, ec.SECP256K1())
        return self._key


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
    a swap with a coin leg at or above its line waits 1 confirmation instead of none (2026-10-17)."""
    out = {}
    for network, default in T.BIG.items():
        try:
            out[network] = int(state.setting(f"swap_big:{network}", default) or default)
        except (TypeError, ValueError):
            out[network] = default
    return out


#: How long a chain's payee address is kept before it is asked again (lock_fee): /account asks on every page.
PAYEE_FOR = 600
_payees: dict[str, tuple[float, str]] = {}


def lock_fee(state: Any, chain: Any) -> dict:
    """This arcade's lock-fee terms on `chain` (2026-10-21: "add an operator fee output to each lock script"):
    {"bps": whole basis points, "to": the node's own address there}, or {} when the fee is Off or the node has no
    address on that chain -- then its locks carry no operator fee.

    The rate is the one setting the operator already had, "Your cut of trades made here" (`trade_cut`, swap.cut_bps;
    the operator: "one setting for both"). The payee is the node's own address on that chain (AppState.home_address: the
    identity's on the messaging chain, the Core wallet's kept "arcade-identity" account address elsewhere, the operator
    account's on a borrowed chain or one whose Core keeps no wallet: payout_address), as `_node_cut` pays this node's cut to its derived address. It is only ever the
    LOCKER's: these terms go into the legs this arcade locks, never into somebody else's."""
    from . import swap as swaplib
    try:
        bps = swaplib.cut_bps(state.setting("trade_cut", 0))
    except Exception:                                      # noqa: BLE001 -- no settings: no fee
        return {}
    if bps <= 0:
        return {}
    network = getattr(chain, "network", "")
    kept = _payees.get(f"{getattr(state, 'home', '')}:{network}")
    if kept is not None and kept[0] > time.monotonic():
        to = kept[1]
    else:
        try:
            # A chain whose coins this arcade holds with its own key (a borrowed chain, a Core with its wallet off):
            # where it is paid there (payout_address), as its other fees are; else its one address on the chain.
            keyed = getattr(state, "keyed", None)
            if keyed is not None and network != state.messaging.network and keyed(chain):
                to = str(state.payout_address(chain) or "")
            else:
                to = str(state.home_address(chain) or "")
        except Exception:                                  # noqa: BLE001 -- a chain with no address here
            to = ""
        _payees[f"{getattr(state, 'home', '')}:{network}"] = (time.monotonic() + PAYEE_FOR, to)
    if not T.payee_ok(to):
        return {}
    return {"bps": bps, "to": to}


def fee_terms_fit(proposed: T.Leg, stood: dict) -> bool:
    """May a responder lock `proposed` -- its own side as the taker wrote it -- under the fee terms it listed (`stood`:
    the order's side, with or without fee_bps/fee_to)? Yes when the taker asks no operator fee of that lock (an arcade
    from before 2026-10-21, or a book that dropped the fields on the way): it trades at rate 0. Yes when the taker
    copied exactly the rate and payee listed. Never anything else: a taker who wrote ANOTHER payee, or a higher rate,
    into the lister's own lock would be paid out of it by its own claim."""
    if not proposed.fee_bps:
        return True
    return proposed.fee_bps == int(stood.get("fee_bps") or 0) and proposed.fee_to == str(stood.get("fee_to") or "")


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
    said = T.asset_at(index, leg, lock_address, height, lock_txid=seen.txid)
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
-- per trade: who, and where the locks are
CREATE TABLE IF NOT EXISTS link (
    id        TEXT PRIMARY KEY,
    peer      TEXT NOT NULL,                  -- the counterparty's messaging key, hex
    my_lock   TEXT NOT NULL DEFAULT '',       -- "txid:vout"
    their_lock TEXT NOT NULL DEFAULT '',
    agreed    INTEGER NOT NULL DEFAULT 0
);
-- what this node will answer a proposal with
CREATE TABLE IF NOT EXISTS standing_order (
    id   TEXT PRIMARY KEY,
    give TEXT NOT NULL,                       -- leg JSON, giver = this node
    get  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
-- a seller's order this node finishes with the order's own key
CREATE TABLE IF NOT EXISTS delegated (
    order_id  TEXT PRIMARY KEY,
    key       TEXT NOT NULL,                  -- the order key's secret, hex (this file is the node's, 0600 home)
    deposit   TEXT NOT NULL,                  -- {network: P2PKH of the order key}: where the seller's side waits
    payout    TEXT NOT NULL,                  -- {network: the seller's own address}: where the proceeds go
    owner     TEXT NOT NULL DEFAULT '',       -- the account that delegated it
    state     TEXT NOT NULL DEFAULT 'open',   -- open | filled | returned
    created   REAL NOT NULL DEFAULT 0
);
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
        try:                                           # delegated orders' keys live in here: the node's user only
            os.chmod(self._db_path, 0o600)
        except OSError:
            pass
        with self._db() as conn:
            try:                                       # which order a trade answers (delegated orders' keys)
                conn.execute("ALTER TABLE link ADD COLUMN order_id TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
        self._scanned: dict[str, int] = {}
        self._addresses: dict[str, str] = {}

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # --- orders and proposals ----------------------------------------------------------

    def fee_terms(self, network: str) -> dict:
        """This node's lock-fee terms on `network` (lock_fee), {} on a chain it does not run."""
        chain = self.chains.get(network)
        return lock_fee(self.state, chain) if chain is not None else {}

    def stand(self, order_id: str, give: dict, get: dict) -> None:
        """An order this node answers: it gives `give` for `get`, each
        {network, kind, asset, amount}. Keys come with a proposal, not the order.

        The side it gives carries this node's lock-fee terms as they stand now (fee_bps, fee_to; 2026-10-21), kept
        with the order: the batch posts them and a proposal is held to them (fee_terms_fit), whatever the rate is
        set to later."""
        import json
        from .trade_orders import one_chain_offer
        if give["network"] == get["network"] and not one_chain_offer(give, get):
            raise ValueError("a swap on one chain settles in one transaction on its own market; "
                             "lock-script swaps are between two chains (and Doginal offers in DOGE)")
        what = lambda d: {k: d.get(k, "") for k in ("network", "kind", "asset")} | {"amount": int(d["amount"])}
        terms = self.fee_terms(give["network"])
        mine = what(give) | {"fee_bps": int(terms.get("bps") or 0)} | ({"fee_to": terms["to"]} if terms else {})
        with self._db() as conn:
            conn.execute("INSERT OR REPLACE INTO standing_order(id, give, get) VALUES(?,?,?)",
                         (order_id, json.dumps(mine), json.dumps(what(get))))
        courier.mesh_inbox(self.state)                    # reachable on the mesh for whoever takes it

    def delegate(self, order_id: str, give: dict, get: dict, key_hex: str, deposit: dict, payout: dict,
                 owner: str = "") -> bytes:
        """Take on a seller's order to finish it with its own key (HeldKey): answer proposals, lock from the deposit,
        claim to the seller's payout address. Returns the order key's public key."""
        import json
        key = HeldKey(key_hex)
        if give["network"] not in deposit or get["network"] not in payout:
            raise ValueError("a deposit on the giving chain and a payout on the getting chain are needed")
        self.stand(order_id, give, get)
        self._watch_deposit(give["network"], deposit[give["network"]])
        with self._db() as conn:
            conn.execute("INSERT OR REPLACE INTO delegated(order_id, key, deposit, payout, owner, state, created) "
                         "VALUES(?,?,?,?,?,'open',?)", (order_id, key_hex, json.dumps(deposit), json.dumps(payout),
                                                         owner, time.time()))
        return key.pubkey

    #: How far back a new deposit address is read for coins sent to it before the order reached this node.
    DEPOSIT_LOOKBACK = 100

    def _watch_deposit(self, network: str, address: str) -> None:
        """The order key's address is watched from now (utxos.watch: the index follows only what it is asked to),
        and the last DEPOSIT_LOOKBACK blocks are read for the deposit the browser made just before."""
        import contextlib
        from . import utxos
        chain = self.chains[network]
        index = self.state.token_index(chain)
        with contextlib.closing(index.open()) as db:
            height = index.indexed_height() or 0
            utxos.watch(db, address, height, why="delegated order")
        with chain.rpc() as rpc:
            tip = rpc.get_block_count()
            utxos.backfill(index, rpc, chain.params, address, max(0, tip - self.DEPOSIT_LOOKBACK), tip)

    def delegated(self, order_id: str) -> dict | None:
        import json
        with self._db() as conn:
            row = conn.execute("SELECT * FROM delegated WHERE order_id=?", (order_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "deposit": json.loads(row["deposit"]), "payout": json.loads(row["payout"]),
                "held": HeldKey(row["key"])}

    def has_delegated(self) -> bool:
        with self._db() as conn:
            return conn.execute("SELECT 1 FROM delegated WHERE state IN ('open', 'releasing') LIMIT 1").fetchone() \
                is not None

    def release(self, order_id: str) -> str:
        """The seller withdrew a delegated order: everything at its deposit address goes back to the seller's own
        address on that chain, in one transaction signed with the order's key. While a taker's swap on it is under way
        it waits (state 'releasing', tried again every tick) -- that swap settles first. Returns the txid, "" while
        waiting, or raises."""
        import json
        held = self.delegated(order_id)
        if held is None or held["state"] == "returned":
            return ""
        with self._db() as conn:
            conn.execute("UPDATE delegated SET state='releasing' WHERE order_id=? AND state IN ('open', 'filled')",
                         (order_id,))
            conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES(?, ?)", (f"since:{order_id}", str(time.time())))
            order = conn.execute("SELECT give FROM standing_order WHERE id=?", (order_id,)).fetchone()
            busy = [r["id"] for r in conn.execute("SELECT id FROM link WHERE order_id=? AND agreed=1", (order_id,))]
            give = json.loads(order["give"]) if order else json.loads(conn.execute(
                "SELECT v FROM meta WHERE k=?", (f"give:{order_id}",)).fetchone()["v"])
            conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (f"give:{order_id}", json.dumps(give)))
        self.withdraw(order_id)                            # nobody new takes it while it goes back
        for tid in busy:
            trade = self.book.get(tid)
            if trade is not None and trade.state not in ("done", "aborted"):
                return ""                                  # a swap on it is under way: back once it settles
        txid = self._return_deposit(held, give)
        with self._db() as conn:
            since = float((conn.execute("SELECT v FROM meta WHERE k=?", (f"since:{order_id}",)).fetchone()
                           or {"v": time.time()})["v"])
            # Nothing there yet may be change still on its way: only an hour of nothing closes it.
            if txid or time.time() - since > 3600:
                conn.execute("UPDATE delegated SET state='returned' WHERE order_id=?", (order_id,))
        try:
            self.state.trade_orders.cancel_id(order_id)
        except Exception:                                  # noqa: BLE001
            pass
        return txid

    def _return_deposit(self, held: dict, give: dict) -> str:
        """Every coin at the deposit address to the seller -- carrying the token or NFT with it when that is the side."""
        import contextlib
        from . import encoding, fees, funding, inscriptions as I, payload as P, tokens as tokenlib, txbuild, utxos
        net = give["network"]
        chain = self.chains[net]
        deposit, home = held["deposit"][net], held["payout"].get(net, "")
        if not home:
            raise ValueError("this order has no return address on its giving chain")
        index = self.state.token_index(chain)
        with contextlib.closing(index.open()) as db:
            coins = [dict(c) for c in utxos.unspent(db, deposit)]
        if not coins:
            return ""                                      # nothing there (never deposited, or already back)
        outputs: list = []
        if give.get("kind") == "token":
            amount = index.balance(deposit, int(give["asset"]))
            if amount > 0:
                outputs.append((0, txbuild.op_return_script(encoding.encode_class_c(
                    tokenlib.send_payload(int(give["asset"]), amount)))))
        elif give.get("kind") == "nft":
            outputs.append((0, txbuild.op_return_script(encoding.encode_class_c(
                P.AnyData(data=I.Transfer(txid=bytes.fromhex(give["asset"])).encode()).encode()))))
        total = sum(int(c["value"]) for c in coins)
        fee = funding.price(len(coins), outputs + [(total, txbuild.p2pkh_script(home))], fees.MIN_FEE_PER_KB)
        if total - fee < fees.DUST_LIMIT:
            raise ValueError("the deposit is too small to send back")
        outputs.append((total - fee, txbuild.p2pkh_script(home)))   # last: the token's recipient too
        inputs = [{"txid": c["txid"], "vout": int(c["vout"]), "value": int(c["value"]), "address": deposit}
                  for c in coins]
        script = txbuild.p2pkh_script(deposit)
        key = held["held"]
        sighashes = [funding.sighash(inputs, outputs, n, script).hex() for n in range(len(inputs))]
        unsigned = funding.Unsigned(raw="", inputs=inputs, outputs=outputs, sighashes=sighashes, fee=fee,
                                    change=0, what=f"return order {held['order_id']}")
        raw = funding.assemble(unsigned, [key.sign(bytes.fromhex(h)).hex() for h in sighashes], key.pubkey)
        with chain.rpc() as rpc:
            return rpc.call("sendrawtransaction", raw)

    def delegated_of(self, owner: str) -> list[dict]:
        """An account's delegated orders and where each stands: open, filled, releasing, returned."""
        out = []
        with self._db() as conn:
            rows = conn.execute("SELECT order_id, state FROM delegated WHERE owner=?", (owner,)).fetchall()
            for r in rows:
                st = r["state"]
                if st == "open":
                    # A swap on it under way: "taking", so its seller's page shows no Cancel meanwhile (2026-10-17).
                    for link in conn.execute("SELECT id FROM link WHERE order_id=? AND agreed=1", (r["order_id"],)):
                        t = self.book.get(link["id"])
                        if t is not None and t.state not in ("done", "aborted"):
                            st = "taking"
                            break
                out.append({"id": r["order_id"], "state": st})
        return out

    #: How long an accepted swap waits for the taker's lock before it is called off and its order stands again.
    TAKER_WAIT = 30 * 60

    def _taking(self, order_id: str, taking: bool) -> None:
        try:
            self.state.trade_orders.set_taking(order_id, taking)
            self.state.bump_generation()                   # every page of this arcade redraws its book now
        except Exception:                                  # noqa: BLE001 -- a node without the order book
            pass

    def _being_taken(self, order_id: str) -> bool:
        """A swap on this order is under way: accepted, and neither done nor called off."""
        with self._db() as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM link WHERE order_id=? AND agreed=1", (order_id,))]
        for tid in ids:
            t = self.book.get(tid)
            if t is not None and t.state not in ("done", "aborted"):
                return True
        return False

    def _own_order_of(self, trade_id: str) -> str:
        """The node's own standing order a swap answers ("" for a delegated one, or none)."""
        with self._db() as conn:
            row = conn.execute("SELECT order_id FROM link WHERE id=?", (trade_id,)).fetchone()
        oid = row["order_id"] if row else ""
        return oid if oid and self.delegated(oid) is None else ""

    def _order_of(self, trade_id: str) -> dict | None:
        with self._db() as conn:
            row = conn.execute("SELECT order_id FROM link WHERE id=?", (trade_id,)).fetchone()
        return self.delegated(row["order_id"]) if row and row["order_id"] else None

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
        courier.mesh_inbox(self.state)                    # on the mesh before asking, to hear the answer
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
        """A leg this node gives: its trade key as giver, its own lock-fee terms on that chain (2026-10-21). A token or
        NFT goes from its home address (state.home_address), where the node's own tokens are kept."""
        return T.with_fee(T.Leg(network, kind, str(asset), int(amount), self.key.pubkey.hex(), taker_key.hex()),
                          self.fee_terms(network))

    # --- telling the other nodes ----------------------------------------------------------

    #: How long a posted order stands before this node posts it again (it is shown only
    #: while this node is online in any case; the expiry bounds a node that vanished).
    POST_EVERY = 12 * 3600
    ORDER_LIFE = 24 * 3600

    def standing(self, own: bool = False) -> list[dict]:
        """Every order this node answers; `own`: only the node's own, not an account's delegated to it (2026-10-19:
        the operator's exchange hid @apple's delegated order as the node's own)."""
        import json
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM standing_order ORDER BY id").fetchall()
            held = {r["order_id"] for r in conn.execute("SELECT order_id FROM delegated")} if own else set()
        return [{"id": r["id"], "give": json.loads(r["give"]), "get": json.loads(r["get"])} for r in rows
                if r["id"] not in held]

    def withdraw(self, order_id: str) -> None:
        with self._db() as conn:
            gone = conn.execute("DELETE FROM standing_order WHERE id=?", (order_id,)).rowcount
            if not gone:
                return                                     # already withdrawn: nothing to say again
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
        # On this node's book (and so every arcade's, book_relay.py) at once, not a block later (2026-10-19).
        home = self.state.derived_address or ""
        if home:
            try:
                self.state.trade_orders.file_batch(home, self.messaging.network, f"node-{node_id[:16]}", body)
            except Exception:                              # noqa: BLE001 -- the inscription files it anyway
                pass
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
        on = self.enabled()
        if not on and not self.has_delegated():
            return []
        done: list[tuple[str, str, str]] = []
        self._read_inbox()
        with self._db() as conn:
            waiting = [r["order_id"] for r in conn.execute("SELECT order_id FROM delegated WHERE state='releasing'")]
        for oid in waiting:
            try:
                self.release(oid)
            except Exception:                              # noqa: BLE001 -- tried again next tick
                pass
        for trade in self.book.open_trades():
            # The operator's switch covers the node's own trading; a seller's delegated order is the seller's.
            if not on and self._order_of(trade.id) is None:
                continue
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
            row = conn.execute("SELECT v FROM meta WHERE k='mesh_cursor'").fetchone()
        # Swap talk over the mesh (trade_courier.mesh_inbox); the chain read above stays for steps
        # sent before 2026-10-17, when talk still went on the chain.
        heard, mesh_cursor = courier.mesh_inbox(self.state, int(row["v"]) if row else 0)
        for msg in heard:
            said, peer = msg["step"], msg["sender"]
            with self._db() as conn:
                link = conn.execute("SELECT * FROM link WHERE id=?", (said["id"],)).fetchone()
            if said["step"] == "propose" and link is None:
                self._consider(said, peer)
            elif link is not None and link["peer"] == peer:
                self._note(said)
        with self._db() as conn:
            conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('mesh_cursor', ?)", (str(mesh_cursor),))

    def _consider(self, said: dict, peer: str) -> None:
        """A proposal against one of this node's orders: accept exactly-matching terms."""
        import json
        with self._db() as conn:
            order = conn.execute("SELECT * FROM standing_order WHERE id=?", (said["order"],)).fetchone()
        held = self.delegated(said["order"]) if order is not None else None
        if order is not None and held is None and not self.enabled():
            order = None                                   # the node's own orders wait for the operator's switch
        if held is not None and held["state"] != "open":
            order = None
        if order is None:
            courier.send(self.state, self.messaging, bytes.fromhex(peer),
                         M.build("cancel", said["id"], reason="no such order here"))
            return
        if self._being_taken(said["order"]):
            # First taker wins (2026-10-19: "no two users can fill the same order"): a lock moves the whole
            # order, so a second swap on it could only end in a refund.
            courier.send(self.state, self.messaging, bytes.fromhex(peer),
                         M.build("cancel", said["id"], reason="somebody else is taking this order"))
            return
        want_give, want_get = json.loads(order["give"]), json.loads(order["get"])
        they_give, they_get = M.leg_from(said["give"]), M.leg_from(said["get"])
        mine = (held["held"] if held else self.key).pubkey.hex()
        ok = (they_get.network == want_give["network"] and they_get.kind == want_give["kind"]
              and they_get.asset == want_give["asset"] and they_get.amount == want_give["amount"]
              and they_get.giver_key == mine
              and they_give.network == want_get["network"] and they_give.kind == want_get["kind"]
              and they_give.asset == want_get["asset"] and they_give.amount >= want_get["amount"]
              and they_give.taker_key == mine
              # The operator fee in this node's own lock: none, or exactly the terms the order was listed with.
              and fee_terms_fit(they_get, want_give))
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
            conn.execute("INSERT INTO link(id, peer, agreed, order_id) VALUES(?,?,1,?)",
                         (trade.id, peer, said["order"]))
            conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (f"accepted:{trade.id}", str(time.time())))
        self._taking(said["order"], True)                  # off every book while this swap runs (2026-10-19)
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
        if leg.kind == "token20":
            from .token20_swap import token20_seen
            index = self.state.ord_indexes().get(leg.network)
            if index is None:
                if seen is not None:
                    seen.confirmations = 0                # no ord index here: never counted
                return seen
            return token20_seen(seen, leg, redeem, rpc, index, chain.params)
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
        from . import funding
        guard = funding.guard_on(leg.network) if leg.network != "doge" else None
        return doginal_seen(seen, leg, redeem, rpc, guard or self.doginals)


    def _send_coins(self, rpc: Any, network: str, address: str, amount: int) -> str:
        """A coin leg into its lock with `sendtoaddress` -- which picks its own coins, and on
        Dogecoin could pick one carrying a Doginal (2026-10-15). So there, every coin an ordinary
        send may not use (doginals.spendable: inscribed, or small and not vouched for) is locked
        in the wallet for the one send and let go after; a coin already locked stays locked."""
        from . import funding
        # Litecoin too (2026-10-18): its inscriptions are Doginals-format and its guard is the app's
        # (funding.GUARD_BY_NET), and a sendtoaddress there spent an inscribed coin on regtest.
        if network == "doge":
            guard = self._doginals or funding.GUARD
        elif getattr(NETWORKS.get(network), "addon", False):
            # Litecoin, and any other add-on chain the app guards (2026-10-22: Bellscoin, Luckycoin).
            guard = funding.guard_on(network)
        else:
            guard = None
        if guard is None:
            # Elsewhere there is nothing to set aside; a node that checks no inscriptions sends as it always did
            # (the app sets the guard whenever it runs Dogecoin or Litecoin, so a live node always has one).
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

    def _lock_doginal(self, leg: T.Leg, redeem: bytes, rpc: Any, reserve: int = H.DOGINAL_RESERVE) -> str:
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
        outputs = H.doginal_lock_outputs(found["value"], redeem, reserve)
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

    def _lock_asset(self, leg: T.Leg, lock_address: str, chain: Any, rpc: Any, value: int = T.LOCK_RESERVE) -> str:
        """The token or NFT into the lock, from this node's home address, with the reserve
        that pays its way out as the lock output (the reference: last, so the lock is the
        recipient). `value`: the reserve and this node's operator fee (trade.fund_value)."""
        from . import inscriptions as I, payload as P, tokens as tokenlib
        if leg.kind == "token":
            payload = tokenlib.send_payload(int(leg.asset), leg.amount)
        else:
            payload = P.AnyData(data=I.Transfer(txid=bytes.fromhex(leg.asset)).encode()).encode()
        sender = tokenlib.TokenSender(rpc, chain.params)
        prepared = sender.prepare(self.state.home_address(chain), payload, lock_address,
                                  reference_value=int(value))
        return sender.broadcast(prepared)

    def _way_out(self, coin: dict, leg: T.Leg, redeem: bytes, chain: Any, to: str = "",
                 operator: tuple[int, str] | None = None, refund: bool = False):
        """The claim or refund's inputs and outputs, to this node (or `to`): the coin itself, or the
        token/NFT moved out by the lock's own reserve. A claim pays the locker's `operator` fee
        (trade.payee) as an output of its own; a refund gives everything back (2026-10-21)."""
        to = to or self._address(leg.network)
        if leg.kind == "coin":
            return H.pay_out(coin, to, fee=T.claim_fee(leg.network), params=chain.params, operator=operator)
        if leg.kind in ("doginal", "token20"):
            # A DRC-20 / PRC-20 lock is a Doginal lock's shape (token20_swap): its note at output 0, the reserve at 1.
            return H.doginal_out(coin, {"txid": coin["txid"], "vout": 1, "value": coin["reserve"]}, to,
                                 operator=operator, refund=refund)
        return H.move_out(coin, H.address_of(redeem, chain.params), T.move_data(leg), to,
                          operator=operator, params=chain.params)

    def _delegated_lock(self, held: dict, leg: T.Leg, lock_address: str, chain: Any, rpc: Any,
                        value: int | None = None) -> str:
        """The seller's side into its lock from the order's deposit address, signed with the order's key: a coin
        straight in; a token or NFT as one OP_RETURN plus the lock's reserve (last, so the lock is the recipient)."""
        import contextlib
        from . import encoding, fees, funding, inscriptions as I, payload as P, tokens as tokenlib, txbuild
        deposit = held["deposit"][leg.network]
        lock_script = txbuild.p2pkh_script(lock_address)
        # What the lock holds (trade.fund_value): the amount with the claim's network fee and the operator fee on
        # top, or the reserve and the operator fee beside a token or NFT (2026-10-21).
        value = T.fund_value(leg) if value is None else int(value)
        if leg.kind == "coin":
            outputs = [(value, lock_script)]
        elif leg.kind in ("token", "nft"):
            raw = (tokenlib.send_payload(int(leg.asset), leg.amount) if leg.kind == "token"
                   else P.AnyData(data=I.Transfer(txid=bytes.fromhex(leg.asset)).encode()).encode())
            outputs = [(0, txbuild.op_return_script(encoding.encode_class_c(raw))), (value, lock_script)]
        else:
            raise ValueError(f"a delegated order does not lock a {leg.kind} yet")
        with contextlib.closing(self.state.token_index(chain).open()) as db:
            unsigned = funding.build(db, chain.params, deposit, outputs, rate=fees.MIN_FEE_PER_KB,
                                     what=f"lock order {held['order_id']}")
        key = held["held"]
        signatures = [key.sign(bytes.fromhex(h)).hex() for h in unsigned.sighashes]
        return rpc.call("sendrawtransaction", funding.assemble(unsigned, signatures, key.pubkey))

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
        held = self._order_of(trade.id)
        own = self._own_order_of(trade.id)                 # the node's own order, taken the same way (2026-10-19)
        key = held["held"] if held else self.key
        if (held or own) and not mine and not theirs:
            with self._db() as conn:
                row = conn.execute("SELECT v FROM meta WHERE k=?", (f"accepted:{trade.id}",)).fetchone()
            if row and now - float(row["v"]) > self.TAKER_WAIT:
                trade.state, trade.note = "aborted", "the taker never locked their side, so nothing was locked"
                self.book.save(trade)
                self._taking(held["order_id"] if held else own, False)   # the order stands again
                return T.ABORT, trade.note
        home_give = held["deposit"][trade.give.network] if held else self._address(trade.give.network)
        home_get = held["payout"][trade.get.network] if held else self._address(trade.get.network)
        with give_chain.rpc() as grpc, get_chain.rpc() as trpc:
            facts = T.Facts(
                mine=self._asset(observe(grpc, mine, {home_give}, self._scanned),
                                 trade.give, trade.my_redeem(), give_chain, grpc),
                theirs=self._asset(observe(trpc, theirs, {home_get}, self._scanned),
                                   trade.get, trade.their_redeem(), get_chain, trpc))
            step, why = T.next_step(trade, facts, now)
            peer = bytes.fromhex(link["peer"])
            if step == T.LOCK:
                lock_address = H.address_of(trade.my_redeem(), give_chain.params)
                # The sender pays (2026-10-21): the claim's network fee and this arcade's operator fee go in with it.
                value = T.fund_value(trade.give, trade.get)
                if held:
                    txid = self._delegated_lock(held, trade.give, lock_address, give_chain, grpc, value)
                elif trade.give.kind == "coin":
                    txid = self._send_coins(grpc, trade.give.network, lock_address, value)
                elif trade.give.kind == "doginal":
                    txid = self._lock_doginal(trade.give, trade.my_redeem(), grpc, value)
                elif trade.give.kind == "token20":
                    # A DRC-20 / PRC-20 lock is four signed steps from an account's tab (/account/trade/lock20); this
                    # arcade's own wallet makes none yet (2026-10-22), so it never gives one.
                    raise ValueError("this arcade's own wallet does not lock DRC-20 / PRC-20 tokens")
                else:
                    txid = self._lock_asset(trade.give, lock_address, give_chain, grpc, value)
                tx = grpc.call("getrawtransaction", txid, 1)
                vout = next(o["n"] for o in tx["vout"]
                            if lock_address in o["scriptPubKey"].get("addresses", []))
                with self._db() as conn:
                    conn.execute("UPDATE link SET my_lock=? WHERE id=?", (f"{txid}:{vout}", trade.id))
                courier.send(self.state, self.messaging, peer, M.build(
                    "locked", trade.id, network=trade.give.network, txid=txid, vout=vout))
            elif step == T.CLAIM:
                coin = {**theirs, "value": facts.theirs.value, "reserve": facts.theirs.reserve}
                inputs, outputs = self._way_out(coin, trade.get, trade.their_redeem(), get_chain, to=home_get,
                                                operator=T.payee(trade.get, trade.give))
                txid = trpc.call("sendrawtransaction", H.claimed(
                    inputs, outputs, trade.their_redeem(), bytes.fromhex(trade.secret), key.sign))
                courier.send(self.state, self.messaging, peer, M.build(
                    "claimed", trade.id, network=trade.get.network, txid=txid))
            elif step == T.REFUND:
                coin = {**mine, "value": facts.mine.value, "reserve": facts.mine.reserve}
                inputs, outputs = self._way_out(coin, trade.give, trade.my_redeem(), give_chain, to=home_give,
                                                refund=True)
                txid = grpc.call("sendrawtransaction", H.refunded(
                    inputs, outputs, trade.my_redeem(), key.sign))
                courier.send(self.state, self.messaging, peer, M.build(
                    "refunded", trade.id, network=trade.give.network, txid=txid))
            elif step in (T.DONE, T.ABORT):
                trade.state = "done" if step == T.DONE else "aborted"
                if own and (step == T.DONE or mine):
                    # The node's own order, taken whole (or its side locked and refunded): it never stands again.
                    self.withdraw(own)
                    try:
                        self.state.trade_orders.cancel_id(own)
                        self.state.bump_generation()
                    except Exception:                      # noqa: BLE001 -- a node without the order book
                        pass
                elif own:
                    self._taking(own, False)               # nothing of the node's was locked: it stands again
                if held and step == T.ABORT and not mine:
                    self._taking(held["order_id"], False)       # nothing of the seller's left the deposit
                elif held and step == T.ABORT:
                    # The seller's side was locked, so the order was taken: it never stands again (2026-10-18,
                    # The operator: "It should've been removed as soon as it was taken" -- a PEP for LTC order went back on
                    # the book after its PEP had been claimed). Whatever comes back to the deposit (a refund) goes
                    # back to the seller, as after a fill.
                    with self._db() as conn:
                        conn.execute("UPDATE delegated SET state='releasing' WHERE order_id=?", (held["order_id"],))
                        conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES(?, ?)",
                                     (f"since:{held['order_id']}", str(time.time())))
                    self.withdraw(held["order_id"])
                    try:
                        self.state.trade_orders.cancel_id(held["order_id"])
                        self.state.bump_generation()
                    except Exception:                      # noqa: BLE001 -- a node without the order book
                        pass
                if held and step == T.DONE:
                    # Taken whole: the order is filled, off this node's book, and answers no one else.
                    with self._db() as conn:
                        conn.execute("UPDATE delegated SET state='filled' WHERE order_id=?", (held["order_id"],))
                        order = conn.execute("SELECT give FROM standing_order WHERE id=?",
                                             (held["order_id"],)).fetchone()
                        if order:
                            conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)",
                                         (f"give:{held['order_id']}", order["give"]))
                        # What its lock left at the deposit (the fee coins' change) goes back too, once indexed.
                        conn.execute("UPDATE delegated SET state='releasing' WHERE order_id=?", (held["order_id"],))
                        conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES(?, ?)",
                                     (f"since:{held['order_id']}", str(time.time())))
                    self.withdraw(held["order_id"])
                    try:
                        self.state.trade_orders.cancel_id(held["order_id"])
                    except Exception:                      # noqa: BLE001 -- a node without the order book
                        pass
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
