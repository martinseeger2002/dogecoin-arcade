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

from typing import Any

from .db import register_journalled_table
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


def watch(db, address: str, height: int, why: str = "") -> None:
    """Start watching an address. Never retroactive, and says so.

    Deliberately NOT a rescan. Going back for an address somebody just
    typed means re-reading the chain on a page draw, and the honest
    alternative is to say what is known from here rather than to pretend.
    """
    db.conn.execute(
        "INSERT INTO watched (address, since, why) VALUES (?,?,?) "
        "ON CONFLICT(address) DO NOTHING", (address, int(height), why))


SPENDABLE = (OutputType.PUBKEYHASH, OutputType.SCRIPTHASH)


def backfill(index, rpc, params, address: str, start: int, end: int) -> int:
    """The coins an address already held when it started being watched: the
    one exception to "never retroactive", for an account brought here from
    another node (2026-09-30: "he has zero balance ... has @tag"), whose coins
    all arrived while this node was not looking.

    Reads blocks `start`..`end` for payments to `address`, then keeps only what
    the chain says is still unspent (gettxout), so a spend this pass did not
    see -- in a later block, or read by the indexer meanwhile -- cannot leave a
    coin counted that is gone. Returns how many coins it filed.
    """
    found = []
    for height in range(int(start), int(end) + 1):
        block = rpc.get_block(rpc.get_block_hash(height), 2)
        for tx in block.get("tx") or []:
            if not isinstance(tx, dict):
                continue
            for out in tx.get("vout", []):
                script = (out.get("scriptPubKey") or {}).get("hex", "")
                if not script:
                    continue
                parsed = parse_output(script, 0, params)
                if parsed.type in SPENDABLE and parsed.address == address:
                    found.append((tx["txid"], int(out.get("n", 0)),
                                  int(round(float(out.get("value", 0)) * 100_000_000)), height))
    filed = 0
    with index.open() as db:
        for txid, vout, value, height in found:
            if value <= 0 or not rpc.call("gettxout", txid, vout, False):
                continue
            db.conn.execute(
                "INSERT OR IGNORE INTO utxo (txid, vout, address, value, height) "
                "VALUES (?,?,?,?,?)", (txid, vout, address, value, height))
            filed += 1
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
            if parsed.type not in SPENDABLE:
                continue
            if parsed.address not in addresses:
                continue
            value = int(round(float(out.get("value", 0)) * 100_000_000))
            if value <= 0:
                continue
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
                "SELECT 1 FROM utxo WHERE txid = ? AND vout = ?",
                (previous, int(index))).fetchone()
            if row is not None:
                state.delete("utxo", {"txid": previous, "vout": int(index)})
                spent += 1
    return {"added": added, "spent": spent}
