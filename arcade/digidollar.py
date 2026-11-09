"""DigiDollar in the arcade's own wallet (2026-11-05: "add the digi dollar to the main app ... it should be its own
wallet coin option", then: "It should be able to be added, just like any other coin or chain is added or borrowed ...
not dependent on the installation of a module").

DigiDollar is DigiByte's stablecoin (DigiByte Core 9.26): a dollar amount held by a zero-value Taproot output, the
amount written in the transaction's "DD" OP_RETURN record. It is not a chain of its own: wherever this arcade has
DigiByte (its own Core, or one borrowed over the mesh) and an account has a DigiByte address, the Wallet shows DigiDollar
as a coin beside DGB.

* **The coins** come from this arcade's own index of the chain, as DGB's do (`on_block`, beside utxos.on_block): each
  account that opens DigiDollar is watched from then on (`dd_watched`), and its DigiDollar coins are kept in `dd_coin`,
  journalled, so a reorg gives them back. Coins it got before then are read from the chain's own coin set once
  (`seed_from_core`), where this arcade runs its Core. A borrowed Core serves no `scantxoutset`, so there the arcade
  it borrows from reads its coin set instead (`seed_from_lender`, 2026-11-09). Until then the Wallet says the block
  its history starts at (`from_block`).
* **An address** is the account's key's Taproot output in DigiByte Core's encoding (base58.cpp CDigiDollarAddress):
  base58check of `52 85` ("DD...") on mainnet or `b1 29` ("TD...") on the test chain, and the 32-byte output key.
* **A send** is built by the DigiDollar module's own code (`ddlib.ddtx.send`, the module's lib/ddtx.py byte for
  byte: one implementation, two front doors, 2026-11-07), as DigiByte Core builds one (txbuilder.cpp
  BuildTransferTransaction, version 0x02000770): the account's DigiDollar coins by their Taproot key path, DGB coins
  for the fee, the payee's output, the DigiDollar change, the DGB change, and `OP_RETURN "DD" 2 <each output's cents>`.
  The browser reads it from its own bytes (coins.js verifyOffer) and signs the Taproot inputs with Schnorr, as it signs
  a module's (modhost.build_offer). Addresses and reading a transaction's "DD" record are that code's too. What is the
  Wallet's own is where the coins come from: this arcade's index, so a borrowed DigiByte works and no module is needed.

Mints and redeems (vaults) stay in the DigiDollar node module (examples/packages/digidollar).
"""
from __future__ import annotations

from typing import Any, Callable

from . import taproot
from .db import register_journalled_table
from .ddlib import ddtx
from .ddlib import taproot as ddtr

#: The chains it is on, and their address versions.
NETS = (ddtx.MAIN, ddtx.TEST)
VERSIONS = ddtx.DD_VERSIONS
SEND_VERSION = ddtx.SEND_VERSION
MAX_FEE = ddtx.MAX_FEE
OP_RETURN = ddtr.OP_RETURN
#: Script numbers and pushes, as the module writes them.
num, push, pushes, read_num = ddtr.num, ddtr.push, ddtr.pushes, ddtr.read_num

SCHEMA = """
-- An account's DigiDollar coins, unspent: one row per zero-value Taproot output a "DD" record gives dollars to.
CREATE TABLE IF NOT EXISTS dd_coin (
    txid    TEXT    NOT NULL,
    vout    INTEGER NOT NULL,
    address TEXT    NOT NULL,      -- its DigiDollar address, DD... or TD...
    cents   INTEGER NOT NULL,
    height  INTEGER NOT NULL,
    PRIMARY KEY (txid, vout)
);
CREATE INDEX IF NOT EXISTS dd_coin_address ON dd_coin(address);
-- The DigiDollar addresses followed, since when (the index's height when the account first opened DigiDollar).
CREATE TABLE IF NOT EXISTS dd_watched (
    address TEXT PRIMARY KEY,
    since   INTEGER NOT NULL,
    seeded  INTEGER NOT NULL DEFAULT 0     -- 1 once its coins from before `since` were read from the coin set
);
"""


def install(db) -> None:
    db.conn.executescript(SCHEMA)
    register_journalled_table("dd_coin", ("txid", "vout"))


# --- addresses and reading transactions: the module's (ddlib.ddtx) ---------------------------------------------

def output_key(pubkey: bytes) -> bytes:
    """The Taproot output key of a compressed public key (its key path only, no scripts)."""
    return ddtr.tweak(pubkey[1:])


address_of_key = ddtx.address_of_key
key_of = ddtx.dd_key
dollars_out = ddtx.dollars_out


def address(net: str, pubkey: bytes) -> str:
    """The DigiDollar address of a wallet key on `net`: the one a mint pays it, and a send to it pays."""
    return ddtx.dd_address(net, pubkey[1:])


def watching(db) -> set[str]:
    return {row[0] for row in db.conn.execute("SELECT address FROM dd_watched")}


def watch(db, dd_address: str, height: int) -> bool:
    """Follow a DigiDollar address from `height` on. True when it is new."""
    cur = db.conn.execute("INSERT INTO dd_watched (address, since) VALUES (?, ?) ON CONFLICT(address) DO NOTHING",
                          (dd_address, int(height)))
    return cur.rowcount > 0


def coins(db, dd_address: str) -> list[dict[str, Any]]:
    return [{"txid": r[0], "vout": int(r[1]), "cents": int(r[2]), "height": int(r[3])} for r in db.conn.execute(
        "SELECT txid, vout, cents, height FROM dd_coin WHERE address = ? ORDER BY cents DESC, txid", (dd_address,))]


def watched_row(db, dd_address: str) -> dict | None:
    row = db.conn.execute("SELECT since, seeded FROM dd_watched WHERE address = ?", (dd_address,)).fetchone()
    return {"since": int(row[0]), "seeded": bool(row[1])} if row else None


def from_block(row: dict) -> int:
    """The first block whose DigiDollar payments to the address this arcade has read: 0 once the coins it got before it
    was watched are read too (its history is complete), else the block after `since`, the index's height when it was
    first watched (on_block reads every block after it). (2026-11-09, security review: `whole: false` alone read like
    missing coins; the Wallet now says which blocks its history covers.)"""
    return 0 if row.get("seeded") else int(row.get("since") or 0) + 1


def on_block(state, height: int, block: dict[str, Any], net: str, watched: set[str]) -> dict[str, int]:
    """What this block pays the watched DigiDollar addresses, then what it spends of theirs (payments first, as
    utxos.on_block, for a coin made and spent in one block). Journalled through `state`."""
    if not watched or net not in VERSIONS:
        return {"added": 0, "spent": 0}
    added = spent = 0
    txs = block.get("tx") or []
    for tx in txs:
        for vout, cents, key in dollars_out(tx):
            where = address_of_key(net, key)
            if where in watched:
                state.insert("dd_coin", {"txid": tx["txid"], "vout": vout, "address": where, "cents": cents,
                                         "height": height})
                added += 1
    for tx in txs:
        for vin in tx.get("vin") or []:
            previous, index = vin.get("txid"), vin.get("vout")
            if previous is None or index is None:
                continue
            row = state.db.conn.execute("SELECT 1 FROM dd_coin WHERE txid = ? AND vout = ?",
                                        (previous, int(index))).fetchone()
            if row is not None:
                state.delete("dd_coin", {"txid": previous, "vout": int(index)})
                spent += 1
    return {"added": added, "spent": spent}


def seed_from_core(index, rpc, net: str, dd_address: str, upto: int) -> int | None:
    """The DigiDollar coins `dd_address` holds now, from the Core's own coin set, up to the index's height `upto`
    (the coins it got before it was watched; utxos.seed_from_core does the same for DGB). None when the Core has no
    `scantxoutset` (a borrowed one)."""
    from .rpc import RpcError
    script = taproot.p2tr(key_of(net, dd_address))
    try:
        said = rpc.call("scantxoutset", "start", [f"raw({script.hex()})"]) or {}
    except RpcError as exc:
        if exc.code == -32601:
            return None
        raise
    filed = 0
    with index.open() as db:
        for coin in said.get("unspents") or []:
            height = int(coin.get("height") or 0)
            if height <= 0 or height > int(upto):
                continue
            tx = rpc.call("getrawtransaction", coin["txid"], True)
            for vout, cents, key in dollars_out(tx):
                if vout == int(coin["vout"]) and address_of_key(net, key) == dd_address:
                    cur = db.conn.execute("INSERT OR IGNORE INTO dd_coin (txid, vout, address, cents, height) "
                                          "VALUES (?,?,?,?,?)", (coin["txid"], vout, dd_address, cents, height))
                    filed += cur.rowcount
        db.conn.execute("UPDATE dd_watched SET seeded = 1 WHERE address = ?", (dd_address,))
    return filed


def seed_from_lender(index, rpc, net: str, dd_address: str, upto: int) -> int | None:
    """seed_from_core on a borrowed DigiByte (2026-11-09, security review: its history stayed "not whole" for good):
    the coin set read by the arcade it is borrowed from (coinscan.Lender, `arcade_coinscan`), as the watcher reads a
    borrowed chain's DGB coins below its floor (coinscan.collect). `rpc` is the borrowed chain's client.

    The sharer answers at once with what it has read so far. The coins it names, at or below `upto`, are checked against
    their blocks (proved from their bytes as every borrowed block is): the transaction is there and its "DD" record
    gives that output dollars to this address. A coin spent since (`gettxout`, the mempool left out) is not filed. That
    none was left out is the sharer's word, as in coinscan.collect.

    Returns how many coins it filed, and marks the address read whole; None while the sharer has not read it yet (or
    asks this arcade to wait), to be asked again later. coinscan.NotLent when the sharer reads no coin set for it."""
    from . import coinscan
    from .rpc import RpcError
    try:
        said = rpc.batch([(coinscan.SCAN_METHOD, [{"addresses": [dd_address]}])])
    except RpcError as exc:
        if exc.code == coinscan.NOT_LENT and "too often" not in str(exc.message):
            raise coinscan.NotLent(str(exc.message)) from exc
        return None                                        # asked too often (the watcher's own ask): later
    answer = said[0] if said and isinstance(said[0], dict) else {}
    if dd_address in (answer.get("waiting") or []):
        return None
    if dd_address not in (answer.get("coins") or {}):
        # A sharer from before DigiDollar addresses were read skips one: neither found nor waiting.
        raise coinscan.NotLent("the sharer reads no DigiDollar coin set")
    if int((answer.get("height") or {}).get(dd_address) or 0) < int(upto):
        return None                                        # read before the address was watched: asked again later
    key = key_of(net, dd_address)
    blocks: dict[int, dict] = {}
    found: list[tuple[str, int, str, int, int]] = []
    for txid, vout, _sats, height in answer["coins"][dd_address] or []:
        height, vout = int(height), int(vout)
        if height <= 0 or height > int(upto):
            continue                                       # above `upto`: on_block files it
        if height not in blocks:
            blocks[height] = rpc.get_block(rpc.get_block_hash(height), 2)
        tx = next((t for t in blocks[height].get("tx") or [] if isinstance(t, dict) and t.get("txid") == txid), None)
        cents = next((c for n, c, k in (dollars_out(tx) if tx else []) if n == vout and k == key), None)
        if cents is None:
            continue                                       # not in its block, or no dollars of this address's
        found.append((str(txid), vout, dd_address, cents, height))
    unspent = rpc.batch([("gettxout", [t, v, False]) for t, v, _a, _c, _h in found]) if found else []
    filed = 0
    with index.open() as db:
        for coin, still in zip(found, unspent):
            if still:
                filed += db.conn.execute("INSERT OR IGNORE INTO dd_coin (txid, vout, address, cents, height) "
                                         "VALUES (?,?,?,?,?)", coin).rowcount
        db.conn.execute("UPDATE dd_watched SET seeded = 1 WHERE address = ?", (dd_address,))
    return filed


# --- a send --------------------------------------------------------------------------------------------------------

def prepare_send(net: str, pubkey: bytes, held: list[dict], dgb: list[dict], change_script: bytes, to: str,
                 cents: int) -> dict:
    """The send of `cents` DigiDollars to the DigiDollar address `to`, built by the module's own send (ddtx.send):
    `held` are the sender's DigiDollar coins ({txid, vout, cents}) from this arcade's index, `dgb` its DGB coins
    ({txid, vout, value}) for the fee."""
    return ddtx.send(pubkey[1:], held, dgb, change_script, key_of(net, to), cents, net, str(to).strip(),
                     many="send your DGB to yourself in one coin first, then try again")


def offer(prepared: dict, params, call: Callable, dgb_address: str, pubkey: bytes):
    """The unsigned offer for a prepared send: every input read from the chain and checked to be the sender's
    (modhost.build_offer), the fee at most MAX_FEE."""
    from . import modhost
    return modhost.build_offer("this send", prepared, params, call, dgb_address, pubkey, max_fee=MAX_FEE,
                               title="DigiDollar")
