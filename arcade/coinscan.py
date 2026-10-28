"""A sharer's coin-set read for an arcade that borrows the chain (2026-10-28).

A coin paid to an account below its chain's floor is found by asking the Core's coin set (`scantxoutset`,
utxos.seed_from_core): the index reads only from the floor up. An arcade that borrows the chain had no Core to ask, and
a sharer serves no `scantxoutset` (mesh/coreshare.py SERVED), so on a borrowed chain such a coin was found in no way at
all (101 DGB paid to an account at blocks 24,460,559 and 24,460,603, below DigiByte's floor, SadFrog 2026-10-28).

So a sharer answers SCAN_METHOD, its own call and not a Core's:

* The sharer (`Lender`): the addresses asked about wait in one queue for every borrower, and one `scantxoutset` reads
  them all, at most once every SCAN_GAP seconds per chain. The ask is answered at once, never held for the minutes a
  scan takes: what is known so far, and which addresses are still waiting. An answer is kept KEEP_SECONDS for the
  borrower to collect, then forgotten. The addresses are never logged. Off with the setting `lend_coinscan` false.
* The borrower (`collect`): asks once for each address it watches (`coinscan_owed:<net>`, until answered), not
  routinely. Every coin it is told of is checked against its block, read from the sharer as every borrowed block is
  (proved from its bytes): a sharer cannot make a coin up. That a coin is still unspent, and that none was left out, is
  the sharer's word, as every borrowed answer bytes cannot prove. Off with the setting `ask_coinscan` false.

What it costs a borrower's users: the sharer learns which addresses that arcade's accounts hold, where a borrower that
only reads whole blocks says nothing of them.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Iterable

from .script import b58check_decode

log = logging.getLogger("arcade.coinscan")

#: The call, answered by a sharer (mesh/service.py shared_cores) for a borrower (coreshare SERVED).
SCAN_METHOD = "arcade_coinscan"
#: Addresses in one ask; a borrower sends at most ASK_PARTS asks in one batch.
SCAN_ADDRESSES = 100
ASK_PARTS = 5
#: Addresses waiting for the next scan, from every borrower together: an ask past it is answered "waiting" for later.
QUEUE_MAX = 500
#: A chain's coin set is read for borrowers at most once every ten minutes (minutes of disk each: Litecoin's took about
#: four on a hard disk, 2026-10-18).
SCAN_GAP = 600.0
#: How long one scan may take (utxos.SCAN_SECONDS).
SCAN_SECONDS = 1200.0
#: An answer is kept this long for the borrower to collect, then forgotten.
KEEP_SECONDS = 3600.0
#: A borrower asks again for what is still waiting no sooner than this (the watcher's pass is every ten minutes).
ASK_AGAIN = 300.0
#: A sharer whose Core has no `scantxoutset` (Dogecoin and Pepecoin 1.14): asked again after six hours.
NONE_AGAIN = 6 * 3600.0
#: The error a sharer answers when it reads no coin set for borrowers (its Core has none, or the operator said no).
NOT_LENT = -32601


def _address(text: str, params) -> bool:
    try:
        version, payload = b58check_decode(text)
    except Exception:                                      # noqa: BLE001
        return False
    return len(payload) == 20 and version in (params.pubkeyhash_version, params.scripthash_version)


class NotLent(Exception):
    """This sharer reads no coin set for borrowers."""


class Lender:
    """One chain's coin-set reads for borrowers, on the sharer. `rpc` makes this machine's own Core's client."""

    def __init__(self, params, rpc: Callable[[], Any], *, clock: Callable[[], float] = time.time,
                 start: Callable[[Callable[[], None]], None] | None = None):
        self.params, self.rpc, self.clock = params, rpc, clock
        self.start = start or (lambda run: threading.Thread(target=run, daemon=True,
                                                            name=f"arcade-coinscan-{params.name}").start())
        self.lock = threading.Lock()
        self.waiting: set[str] = set()
        #: address -> (when found, the scan's height, [[txid, vout, sats, height], ...])
        self.found: dict[str, tuple[float, int, list]] = {}
        self.running = False
        self.last = float("-inf")
        self.none = ""                                     # why this Core reads no coin set, once known

    def ask(self, asked: Any) -> dict[str, Any]:
        """{"addresses": [...]} -> {"coins": {address: [[txid, vout, sats, height], ...]}, "height": {address: h},
        "waiting": [...], "next": seconds until the next scan may start}. NotLent when this Core has no coin set."""
        asked = asked if isinstance(asked, dict) else {}
        addresses = list(dict.fromkeys(a for a in (str(a) for a in (asked.get("addresses") or []))
                                       if _address(a, self.params)))[:SCAN_ADDRESSES]
        now = self.clock()
        with self.lock:
            if self.none:
                raise NotLent(self.none)
            for a in [a for a, (at, _h, _c) in self.found.items() if now - at > KEEP_SECONDS]:
                del self.found[a]
            ready = {a: self.found[a] for a in addresses if a in self.found}
            room = QUEUE_MAX - len(self.waiting)
            for a in addresses:
                if a not in ready and a not in self.waiting and room > 0:
                    self.waiting.add(a)
                    room -= 1
            waiting = [a for a in addresses if a not in ready]
            go = not self.running and bool(self.waiting) and now - self.last >= SCAN_GAP
            if go:
                self.running, self.last = True, now
            wait = max(0.0, self.last + SCAN_GAP - now)
        if go:                                             # outside the lock: the scan takes it to file its answer
            self.start(self._scan)
        return {"coins": {a: c for a, (_at, _h, c) in ready.items()},
                "height": {a: h for a, (_at, h, _c) in ready.items()},
                "waiting": waiting, "next": round(wait)}

    def _scan(self) -> None:
        from .rpc import RpcError
        from .script import parse_output
        from .utxos import SPENDABLE
        with self.lock:
            addresses = sorted(self.waiting)
        try:
            with self.rpc() as rpc:
                held = getattr(rpc, "_timeout", None)
                if held is not None:
                    rpc._timeout = max(held, SCAN_SECONDS)
                try:
                    said = rpc.call("scantxoutset", "start", [f"addr({a})" for a in addresses]) or {}
                finally:
                    if held is not None:
                        rpc._timeout = held
            height = int(said.get("height") or 0)
            coins: dict[str, list] = {a: [] for a in addresses}
            for coin in said.get("unspents") or []:
                parsed = parse_output(str(coin.get("scriptPubKey", "")), 0, self.params)
                if parsed.type not in SPENDABLE or parsed.address not in coins:
                    continue
                coins[parsed.address].append([str(coin["txid"]), int(coin["vout"]),
                                              int(round(float(coin.get("amount", 0)) * 100_000_000)),
                                              int(coin.get("height") or 0)])
            with self.lock:
                at = self.clock()
                for a in addresses:
                    self.found[a] = (at, height, coins[a])
                self.waiting -= set(addresses)
            log.info("%s: coin set read for borrowers, %d addresses", self.params.name, len(addresses))
        except RpcError as exc:
            if exc.code == -32601:                         # method not found: a Core with no coin set to read
                with self.lock:
                    self.none = f"{self.params.name}'s Core here reads no coin set"
                    self.waiting.clear()
            else:                                          # most often a scan of its own running: the next gap
                log.info("%s: coin set read for borrowers put off: %s", self.params.name, exc.message)
        except Exception:                                  # noqa: BLE001 -- the next gap tries again
            log.warning("%s: coin set read for borrowers failed", self.params.name, exc_info=True)
        finally:
            with self.lock:
                self.running = False


# --------------------------------------------------------------------------- the borrower's side

def owed_key(net: str) -> str:
    return f"coinscan_owed:{net}"


def owe(state, net: str, addresses: Iterable[str]) -> None:
    """Remember these addresses as still to be asked about (once each, until answered)."""
    addresses = set(addresses)
    if not addresses:
        return
    owed = set(_owed(state, net))
    if not addresses - owed:
        return
    state.set_setting(owed_key(net), json.dumps(sorted(owed | addresses)))


def _owed(state, net: str) -> list[str]:
    try:
        said = json.loads(state.setting(owed_key(net), "[]") or "[]")
    except (TypeError, ValueError):
        return []
    return [str(a) for a in said] if isinstance(said, list) else []


def collect(state, chain, index, rpc, *, upto: int, clock: Callable[[], float] = time.time,
            asked_at: dict | None = None) -> int:
    """Ask the sharer about the addresses still owed on this borrowed chain; file the coins it found at or below
    `upto` that their blocks bear out. Returns how many coins it filed. An address answered is no longer owed."""
    from .rpc import RpcError
    from .script import parse_output
    import contextlib
    net = chain.network
    owed = _owed(state, net)
    asked_at = asked_at if asked_at is not None else {}
    if not owed or not upto or clock() < asked_at.get(net, float("-inf")):
        return 0
    parts = [owed[i:i + SCAN_ADDRESSES] for i in range(0, len(owed), SCAN_ADDRESSES)][:ASK_PARTS]
    try:
        said = rpc.batch([(SCAN_METHOD, [{"addresses": part}]) for part in parts])
    except RpcError as exc:
        # An older sharer ("a shared Core does not answer"), one whose Core reads no coin set, or one asked too often.
        none = exc.code == NOT_LENT and "too often" not in str(exc.message)
        asked_at[net] = clock() + (NONE_AGAIN if none else ASK_AGAIN)
        log.info("%s: the sharer reads no coin set for this arcade (%s); asked again later", chain.params.label,
                 exc.message)
        return 0
    asked_at[net] = clock() + ASK_AGAIN
    answered: set[str] = set()
    filed = 0
    blocks: dict[int, dict] = {}
    found: list[tuple[str, int, str, int, int]] = []
    with contextlib.closing(index.open()) as db:
        for one in said:
            if not isinstance(one, dict):
                continue
            for address, coins in (one.get("coins") or {}).items():
                if address not in owed:
                    continue
                answered.add(address)
                for txid, vout, sats, height in coins or []:
                    height, vout, sats = int(height), int(vout), int(sats)
                    if height <= 0 or height > upto or sats <= 0:
                        continue
                    if height not in blocks:
                        blocks[height] = rpc.get_block(rpc.get_block_hash(height), 2)
                    tx = next((t for t in blocks[height].get("tx") or []
                               if isinstance(t, dict) and t.get("txid") == txid), None)
                    out = (tx or {}).get("vout") or []
                    if vout >= len(out):
                        log.warning("%s: a coin the sharer named is not in block %d", chain.params.label, height)
                        continue
                    script = (out[vout].get("scriptPubKey") or {}).get("hex", "")
                    value = int(round(float(out[vout].get("value", 0)) * 100_000_000))
                    if parse_output(script, 0, chain.params).address != address or value != sats:
                        log.warning("%s: a coin the sharer named does not match its block %d", chain.params.label,
                                    height)
                        continue
                    found.append((txid, vout, address, sats, height))
        # Unspent now (the mempool left out, as utxos.backfill does): an answer can be an hour old, and a coin spent
        # since in a block the index already read would otherwise stay in its table for good.
        unspent = rpc.batch([("gettxout", [t, v, False]) for t, v, _a, _s, _h in found]) if found else []
        for coin, still in zip(found, unspent):
            if still:
                filed += db.conn.execute(
                    "INSERT OR IGNORE INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                    coin).rowcount
        db.conn.commit()
    if answered:
        state.set_setting(owed_key(net), json.dumps(sorted(set(_owed(state, net)) - answered)))
    if filed:
        log.info("%s: %d coins below the index filed from the sharer's coin set", chain.params.label, filed)
    return filed
