"""Coins, by address, for addresses this node does not own.

`fundrawtransaction` is a wallet call: it can only choose inputs from keys
the node holds. Every account in this design keeps its key in a browser, so
the node holds nothing of theirs and cannot ask the wallet to pay for
anything they do. Without something to replace that call, an account can
hold coins and never spend one (docs/multi-user.md §3, §4).

This is that something: which outputs are unspent, for the addresses worth
watching.

**Watched only, and deliberately.** A full UTXO set for a chain this age is
millions of rows and would be the largest table in the index by an order of
magnitude, against the standing instruction to keep it lean. What is
watched is what somebody asked about: the accounts on this node, and
anybody whose tag a page has looked up. Watching is cheap to start and
never retroactive -- an address added today is watched from today, and the
rows before that are gone. A node that needs them re-reads from the floor,
which is what the bootstrap exists to avoid doing twice.

**Journalled, like everything else that follows the chain.** A reorg has to
un-spend what a disconnected block spent, and a table that is written
outside `StateDB` survives a reorg and lies afterwards. So every row goes
through the journal, and a rollback puts the coins back.

What this buys beyond funding: anybody's balance -- the profile wallet page
currently has to say it cannot see one -- and knowing before a trade
whether a counterparty holds what they are offering, rather than finding
out when the build fails.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .db import register_journalled_table
from .rpc import RpcError, RpcTransportError
from .script import OutputType, parse_output

SCHEMA = """
-- One row per unspent output belonging to a watched address. Spent ones
-- are deleted rather than flagged: the question asked of this table is
-- always "what can be spent", and a flag makes every reader remember to
-- filter by it.
CREATE TABLE IF NOT EXISTS utxo (
    txid    TEXT    NOT NULL,
    vout    INTEGER NOT NULL,
    address TEXT    NOT NULL,
    value   INTEGER NOT NULL,      -- satoshis
    height  INTEGER NOT NULL,
    PRIMARY KEY (txid, vout)
);
CREATE INDEX IF NOT EXISTS utxo_address ON utxo(address, value DESC);

-- What each transaction did to a watched address: the Wallet tab's history
-- (2026-10-08: "it should have a history"). `utxo` forgets a coin the moment
-- it is spent, which is right for "what can be spent" and leaves nothing to
-- answer "what happened", so this keeps one row per (transaction, address):
-- what the transaction paid the address, what it took from it, and, for a
-- spend, what went to everybody else and the first of them. The net is
-- received - spent; a send's fee is spent - received - paid_out.
-- Like `watched`, it starts when watching did and is not retroactive.
CREATE TABLE IF NOT EXISTS activity (
    txid     TEXT    NOT NULL,
    address  TEXT    NOT NULL,
    height   INTEGER NOT NULL,
    received INTEGER NOT NULL DEFAULT 0,
    spent    INTEGER NOT NULL DEFAULT 0,
    paid_out INTEGER NOT NULL DEFAULT 0,
    other    TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (txid, address)
);
CREATE INDEX IF NOT EXISTS activity_address ON activity(address, height DESC);

-- The addresses worth the rows. `since` is the height watching began, so a
-- balance can say whether it is the whole story or only what has happened
-- since somebody asked.
CREATE TABLE IF NOT EXISTS watched (
    address TEXT PRIMARY KEY,
    since   INTEGER NOT NULL,
    why     TEXT NOT NULL DEFAULT ''
);
"""


def install(db) -> None:
    """Add the tables and register them for the undo journal."""
    db.conn.executescript(SCHEMA)
    register_journalled_table("utxo", ("txid", "vout"))
    register_journalled_table("activity", ("txid", "address"))


def watch(db, address: str, height: int, why: str = "") -> None:
    """Start watching an address. Never retroactive, and says so.

    Deliberately NOT a rescan. Going back for an address somebody just
    typed means re-reading the chain on a page draw, and the honest
    alternative is to say what is known from here rather than to pretend.
    """
    db.conn.execute(
        "INSERT INTO watched (address, since, why) VALUES (?,?,?) "
        "ON CONFLICT(address) DO NOTHING", (address, int(height), why))


SPENDABLE = (OutputType.PUBKEYHASH, OutputType.SCRIPTHASH,
             OutputType.WITNESS_V0_KEYHASH)


def backfill(index, rpc, params, address: str, start: int, end: int, progress: dict | None = None) -> int:
    """The coins an address already held when it started being watched: the
    one exception to "never retroactive", for an account brought here from
    another node (2026-09-30: "he has zero balance ... has @tag"), whose coins
    all arrived while this node was not looking.

    Reads blocks `start`..`end` for payments to `address`, then keeps only what
    the chain says is still unspent (gettxout), so a spend this pass did not
    see -- in a later block, or read by the indexer meanwhile -- cannot leave a
    coin counted that is gone. Returns how many coins it filed.
    """
    return backfill_many(index, rpc, params, {address}, start, end, progress=progress)


#: Seconds one block of a read-back takes here, pacing included, by chain (Params.name): measured by each pass, for
#: saying how long the next one will take (plan item 18). A guess until a pass has run.
READ_RATE: dict[str, float] = {}
#: The guess: a few blocks a second with the pacing above.
READ_RATE_GUESS = 0.25


def read_rate(params) -> float:
    """Seconds a block of a read-back takes on this chain: the last pass's measure, else a guess."""
    return READ_RATE.get(getattr(params, "name", ""), READ_RATE_GUESS)


#: One read-back at a time, in the whole process, and at a pace (2026-10-19: an account brought to pepecoinarcade.com
#: started five at once -- 55,000 P test blocks, 49,000 P main, 7,000 Dogecoin over the mesh -- and the arcade's pages
#: stopped answering for everybody until they were done).
_BACKFILL_ONE = threading.Lock()
#: Time slept per block, as a share of the time the block took: 1.5 keeps a read-back to about 40% of one core.
BACKFILL_REST = 1.5


def backfill_many(index, rpc, params, addresses: set[str], start: int, end: int,
                  progress: dict | None = None) -> int:
    """`backfill` for several addresses in ONE pass over the blocks (2026-10-17: every account's Dogecoin address at
    once, which read block by block per address would be thousands of reads each). One pass at a time, paced, so
    the arcade's own pages keep answering while it runs.

    `progress`, when given, is kept up to date for whoever is waiting (plan item 18): "height" (the block being
    read, absent until this pass has the lock) and "per_block" (seconds a block takes, pacing included)."""
    addresses = set(addresses)
    if not addresses:
        return 0
    with _BACKFILL_ONE:
        return _backfill_many(index, rpc, params, addresses, start, end, progress)


def _backfill_many(index, rpc, params, addresses: set[str], start: int, end: int,
                   progress: dict | None = None) -> int:
    found = []
    began, done = time.monotonic(), 0
    for height in range(int(start), int(end) + 1):
        if progress is not None:
            progress["height"] = height
        took = time.perf_counter()
        block = rpc.get_block(rpc.get_block_hash(height), 2)
        for tx in block.get("tx") or []:
            if not isinstance(tx, dict):
                continue
            for out in tx.get("vout", []):
                script = (out.get("scriptPubKey") or {}).get("hex", "")
                if not script:
                    continue
                parsed = parse_output(script, 0, params)
                if parsed.type in SPENDABLE and parsed.address in addresses:
                    found.append((tx["txid"], int(out.get("n", 0)), parsed.address,
                                  int(round(float(out.get("value", 0)) * 100_000_000)), height))
        if BACKFILL_REST:
            time.sleep(min(1.0, (time.perf_counter() - took) * BACKFILL_REST))
        done += 1
        if done >= 20:
            per = (time.monotonic() - began) / done
            READ_RATE[getattr(params, "name", "")] = per
            if progress is not None:
                progress["per_block"] = per
    filed = 0
    with index.open() as db:
        for txid, vout, address, value, height in found:
            if value <= 0 or not rpc.call("gettxout", txid, vout, False):
                continue
            db.conn.execute(
                "INSERT OR IGNORE INTO utxo (txid, vout, address, value, height) "
                "VALUES (?,?,?,?,?)", (txid, vout, address, value, height))
            filed += 1
    return filed


#: How long one `scantxoutset` may take: it reads the node's whole coin set from disk, about four minutes
#: for Litecoin mainnet's 54 million coins on this node's hard disk (2026-10-18).
SCAN_SECONDS = 1200.0


#: How long a Core may take to rescan its whole chain for a watch-only import (Dogecoin 1.14 on a spinning disk).
CORE_RESCAN_SECONDS = 24 * 3600


class CoreBusy(Exception):
    """The Core did not take the call: its queue is full or it is still at a rescan sent earlier (one an arcade
    restart cut the connection to, which the Core finishes anyway). Not a failure of the import: asked again later,
    and the reads stay borrowed meanwhile."""


def _up_since(rpc) -> float | None:
    """A time the Core has been running since, at the latest: its oldest peer connection (a Core keeps no uptime
    in 1.14). None when it has no peers."""
    try:
        times = [int(p.get("conntime") or 0) for p in (rpc.call("getpeerinfo") or [])]
    except (RpcError, RpcTransportError):
        return None
    times = [t for t in times if t > 0]
    return float(min(times)) if times else None


def _rescan_already_ran(rpc, addresses, sent) -> bool:
    """Whether an `importmulti` rescan sent earlier (`sent`: {"at", "addresses"}) already covered these addresses
    and ran to its end: every one sent then and watched now, and the Core not restarted since (a peer of its older
    than the send). Then the coins are listed with no second rescan of the whole chain -- hours on Dogecoin."""
    try:
        if not all((rpc.call("validateaddress", a) or {}).get("iswatchonly") for a in addresses):
            return False
    except (RpcError, RpcTransportError):
        return False
    # The wallet already holds transactions for every one of them: a rescan found them, and ran to its end (the operator
    # 2026-10-22, "looks like rescan started over": the peer test below said "restarted" after a 22-hour rescan, which
    # drops and makes its peers anew, and the whole chain was sent again). An address with no history at all is told
    # apart only by the peer test, and rescanned when in doubt.
    if _wallet_has_history(rpc, addresses):
        return True
    if not isinstance(sent, dict) or not set(addresses) <= set(sent.get("addresses") or ()):
        return False
    up = _up_since(rpc)
    return up is not None and up < float(sent.get("at") or 0)


def _wallet_has_history(rpc, addresses) -> bool:
    """Whether the Core's wallet knows at least one transaction for EVERY address (watch-only included):
    listreceivedbyaddress(minconf 0, include_empty, include_watchonly)."""
    try:
        rows = rpc.call("listreceivedbyaddress", 0, True, True) or []
    except (RpcError, RpcTransportError):
        return False
    seen = {str(r.get("address")): bool(r.get("txids")) for r in rows if isinstance(r, dict)}
    return bool(addresses) and all(seen.get(a) for a in addresses)


def seed_from_core_wallet(index, rpc, params, addresses, upto: int, *, sent=None,
                          remember=None) -> dict[str, int] | None:
    """The coins an address holds now, from the Core's own WALLET: imported watch-only (never a key) with a rescan of
    the whole chain, then listed (2026-10-21: an imported wallet's coins from before the index's floor were
    never found on Dogecoin and Pepecoin, whose 1.14 Cores have no scantxoutset). `importmulti` with timestamp 0
    rescans from the first block and holds the Core's lock until done -- hours on a big chain -- so the caller borrows
    the chain's reads meanwhile (core_catchup.hold). Returns how many coins it filed, or None when this Core has no
    wallet (then the caller reads blocks instead). Filed like seed_from_core's rows, at or below `upto` only.
    Every address in one call, so one rescan serves every import waiting for it (2026-10-21: "multiple people
    can import their wallets during a rescan, and during the next rescan, all of their wallet addresses will be
    watched"). Returns {address: coins filed}.

    `sent` is what `remember` was handed by an earlier call ({"at", "addresses"}), saved by the caller before the
    `importmulti` goes: when that rescan already ran to its end for these addresses, it is not sent again. CoreBusy
    when the Core does not take a call (queue full, still rescanning, its connection cut)."""
    addresses = sorted(set(addresses))
    try:
        rpc.call("getwalletinfo")
    except RpcTransportError as exc:
        # Busy since at least now: most often at a rescan an earlier call sent (an arcade restart cut its
        # connection, so nothing here remembers sending it). Remembered as sent now, so that once the Core answers,
        # addresses it watches already and a Core not restarted since are listed, not rescanned again.
        if remember is not None and not (isinstance(sent, dict)
                                         and set(addresses) <= set(sent.get("addresses") or ())):
            remember({"at": time.time(), "addresses": addresses})
        raise CoreBusy(str(exc)) from None
    except RpcError as exc:
        if exc.code == -28:                          # warming up
            raise CoreBusy(str(exc)) from None
        return None                                  # -disablewallet
    if not _rescan_already_ran(rpc, addresses, sent):
        if remember is not None:
            remember({"at": time.time(), "addresses": addresses})
        held = getattr(rpc, "_timeout", None)
        if held is not None:
            rpc._timeout = max(held, CORE_RESCAN_SECONDS)
        try:
            said = rpc.call("importmulti", [{"scriptPubKey": {"address": a}, "timestamp": 0, "watchonly": True,
                                             "label": "arcade-import"} for a in addresses], {"rescan": True}) or []
        except RpcTransportError as exc:
            # Never taken (queue full), or the connection went while the Core rescans on: the next try finds it.
            raise CoreBusy(str(exc)) from None
        finally:
            if held is not None:
                rpc._timeout = held
        refused = [(a, r) for a, r in zip(addresses, said) if not (r or {}).get("success")]
        if len(said) != len(addresses) or refused:
            raise RuntimeError(f"the Core would not watch {refused[0][0] if refused else 'every address'}: "
                               f"{(refused[0][1] or {}).get('error') if refused else 'it answered for fewer'}")
    tip = int(rpc.call("getblockcount"))
    filed = {a: 0 for a in addresses}
    with index.open() as db:
        for coin in rpc.call("listunspent", 1, 99_999_999, addresses) or []:
            address = coin.get("address")
            height = tip - int(coin.get("confirmations") or 0) + 1
            value = int(round(float(coin.get("amount", 0)) * 100_000_000))
            if address not in filed or height <= 0 or height > int(upto) or value <= 0:
                continue
            parsed = parse_output(str(coin.get("scriptPubKey", "")), 0, params)
            if parsed.type not in SPENDABLE or parsed.address != address:
                continue
            cur = db.conn.execute(
                "INSERT OR IGNORE INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                (str(coin["txid"]), int(coin["vout"]), address, value, height))
            filed[address] += cur.rowcount
    return filed


def seed_from_core(index, rpc, params, addresses: set[str], upto: int) -> int | None:
    """The coins these addresses hold now, from the Core's own coin set (`scantxoutset`), at any height
    up to `upto` (2026-10-18). An index reads only from its floor up, so coins an address got below it
    were never filed: 0.1 LTC sent to an account in block 3,197,387 against an L main floor of
    3,198,000. This asks the Core what the address holds instead of reading blocks.

    `upto` is the index's height now, with the addresses already watched. A coin above it is filed by
    the index when it reads that block. A coin at or below it that the index already holds is left as
    it is; one spent since is not in the Core's set, so it is not filed; one spent later is taken out
    when the index reads the spend. Filed like `backfill_many`'s rows, with no history rows.

    Returns how many coins it filed, or None when this Core has no `scantxoutset` (Pepecoin and
    Dogecoin Core 1.14 do not; Litecoin Core 0.21 does). Then the caller reads blocks instead.
    """
    addresses = set(addresses)
    if not addresses:
        return 0
    held = getattr(rpc, "_timeout", None)
    if held is not None:
        rpc._timeout = max(held, SCAN_SECONDS)
    try:
        said = rpc.call("scantxoutset", "start", [f"addr({a})" for a in sorted(addresses)]) or {}
    except RpcError as exc:
        if exc.code == -32601:                       # method not found
            return None
        raise
    finally:
        if held is not None:
            rpc._timeout = held
    filed = 0
    with index.open() as db:
        for coin in said.get("unspents") or []:
            height = int(coin.get("height") or 0)
            value = int(round(float(coin.get("amount", 0)) * 100_000_000))
            if height <= 0 or height > int(upto) or value <= 0:
                continue
            parsed = parse_output(str(coin.get("scriptPubKey", "")), 0, params)
            if parsed.type not in SPENDABLE or parsed.address not in addresses:
                continue
            cur = db.conn.execute(
                "INSERT OR IGNORE INTO utxo (txid, vout, address, value, height) VALUES (?,?,?,?,?)",
                (str(coin["txid"]), int(coin["vout"]), parsed.address, value, height))
            filed += cur.rowcount
    return filed


def watching(db) -> set[str]:
    return {row[0] for row in db.conn.execute("SELECT address FROM watched")}


def unspent(db, address: str) -> list[dict[str, Any]]:
    """What an address can spend, largest first."""
    return [dict(row) for row in db.conn.execute(
        "SELECT txid, vout, address, value, height FROM utxo "
        "WHERE address = ? ORDER BY value DESC, txid", (address,))]


def balance(db, address: str) -> int:
    row = db.conn.execute(
        "SELECT COALESCE(SUM(value), 0) FROM utxo WHERE address = ?",
        (address,)).fetchone()
    return int(row[0] or 0)


def since(db, address: str) -> int | None:
    row = db.conn.execute("SELECT since FROM watched WHERE address = ?",
                          (address,)).fetchone()
    return int(row[0]) if row else None


def on_block(state, height: int, block: dict[str, Any], params,
             addresses: set[str]) -> dict[str, int]:
    """Add what this block pays, then take what it spends, for watched addresses.

    Payments FIRST, then spends. A coin this block both made and ate has to be
    in the table before the pass that removes it can look for it -- and that is
    the ordinary case here, not a corner of it: the node mines a block to
    confirm a faucet gift, and whatever the account spends that gift on can
    land in the same block. Applying spends first left those rows in the index
    for good, since no later block ever names that outpoint again, so every
    balance and every coin selection built from this table kept counting coins
    the chain had already eaten.

    A transaction's own change cannot be caught by this, which is what the old
    order was guarding: a spend names the *previous* transaction's id, and no
    two transactions share one.

    Everything goes through `state`, which journals it, so a disconnected
    block gives the coins back.
    """
    if not addresses:
        return {"added": 0, "spent": 0}
    added = spent = 0
    txs = block.get("tx") or []
    # (txid, address) -> [received, spent], and every transaction's outputs as
    # (address or "", value), for the history rows written at the end.
    moved: dict[tuple[str, str], list[int]] = {}
    outputs: dict[str, list[tuple[str, int]]] = {}

    for tx in txs:
        for out in tx.get("vout", []):
            script = (out.get("scriptPubKey") or {}).get("hex", "")
            if not script:
                continue
            parsed = parse_output(script, 0, params)
            # Only what a key or a script hash can spend. A bare multisig
            # output is a payload (Class B) and is never spendable in practice
            # -- including them would fill this table with dust nobody can
            # move, which is the exact way the index stopped being lean last
            # time. Script hashes since 2026-09-30: a refereed prize pool lives
            # at one, and its claims, its "once per wallet" and its close all
            # read what the pool holds from here.
            value = int(round(float(out.get("value", 0)) * 100_000_000))
            outputs.setdefault(tx["txid"], []).append(
                (parsed.address if parsed.type in SPENDABLE else "", value))
            if parsed.type not in SPENDABLE:
                continue
            if parsed.address not in addresses:
                continue
            if value <= 0:
                continue
            moved.setdefault((tx["txid"], parsed.address), [0, 0])[0] += value
            state.insert("utxo", {
                "txid": tx["txid"], "vout": int(out.get("n", 0)),
                "address": parsed.address, "value": value, "height": height})
            added += 1

    for tx in txs:
        for vin in tx.get("vin", []):
            previous, index = vin.get("txid"), vin.get("vout")
            if previous is None or index is None:
                continue                      # a coinbase spends nothing
            row = state.db.conn.execute(
                "SELECT address, value FROM utxo WHERE txid = ? AND vout = ?",
                (previous, int(index))).fetchone()
            if row is not None:
                moved.setdefault((tx["txid"], row[0]), [0, 0])[1] += int(row[1])
                state.delete("utxo", {"txid": previous, "vout": int(index)})
                spent += 1

    for (txid, address), (got, gave) in moved.items():
        others = [(a, v) for a, v in outputs.get(txid, []) if a != address]
        state.insert("activity", {
            "txid": txid, "address": address, "height": height,
            "received": got, "spent": gave,
            "paid_out": sum(v for _, v in others) if gave else 0,
            "other": next((a for a, v in others if a and v > 0), "") if gave else ""})
    return {"added": added, "spent": spent}


def activity(db, addresses: list[str], before: int | None = None,
             limit: int = 50) -> list[dict[str, Any]]:
    """What happened to these addresses, newest first, `limit` rows at a time.

    One row per transaction: a transaction that touched two of the addresses
    (testnet and mainnet cannot, but an account's tag address and coin address
    on one chain can) is summed into one, because a person sent one thing.
    `before` pages by height.
    """
    if not addresses:
        return []
    marks = ",".join("?" for _ in addresses)
    where = f"address IN ({marks})" + (" AND height < ?" if before is not None else "")
    args: list[Any] = list(addresses) + ([int(before)] if before is not None else [])
    rows = db.conn.execute(
        f"SELECT a.txid, a.height, a.received, a.spent, a.paid_out, a.other, "
        f"COALESCE(b.time, 0) FROM ("
        f"SELECT txid, MAX(height) AS height, SUM(received) AS received, "
        f"SUM(spent) AS spent, MAX(paid_out) AS paid_out, MAX(other) AS other "
        f"FROM activity WHERE {where} GROUP BY txid) a "
        f"LEFT JOIN block b ON b.height = a.height "
        f"ORDER BY a.height DESC, a.txid LIMIT ?", args + [int(limit)]).fetchall()
    out = []
    for txid, height, got, gave, paid, other, when in rows:
        out.append({"txid": txid, "height": int(height), "time": int(when or 0),
                    "received": int(got),
                     "spent": int(gave), "net": int(got) - int(gave),
                     "fee": max(0, int(gave) - int(got) - int(paid)) if gave else 0,
                     "other": other or ""})
    return out
