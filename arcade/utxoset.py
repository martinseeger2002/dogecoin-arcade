"""A chain's whole unspent set, by address, and the blocks that carry data (tbuuol 2026-10-25). KOTIA ONLY for now.

The arcade's own index (utxos.py) keeps the coins of the addresses it watches, from the floor on, and reads back what
an address held before through the Core (`scantxoutset`, `importmulti`). Kotia's Core has neither, so coins from
below the floor never showed. For a small chain it is cheaper to keep every unspent output there is, built once
from block 0: then any address's coins are one read, at any height, with no rescan.

Kotia only (Params.full_utxo_set): built and checked against Kotia 1.0.0 (1.53 million blocks, 79,022 coins, 10 minutes
through its Core, a 5 MB file). Another chain needs looking at before it is switched on: its size (Dogecoin's set
would be gigabytes and days), its header naming (`scrypt_headers_below`), its block files' magic (MAGIC), and what
its transactions carry (SegWit and MWEB parts are read by rawchain, but no chain with them has been built here).

Tables, an array per key where it can be, which keeps them small:
- `coins`: an address -> its unspent outputs, packed 49-byte records (txid, vout, value, height, kind). A script with
  no address (bare multisig, non-standard) is filed under "script:<hash160 of the script>". `kind` 0 is what the
  arcade's wallet can spend (pay-to-pubkey-hash, script hash: utxos.SPENDABLE); 1 a pay-to-pubkey output (Kotia's
  staking rewards: the key's address, but not signed the way the wallet signs); 2 anything else.
- `data_blocks`: a height -> the txids of its transactions that carry an OP_RETURN, packed 32-byte ids. Only where
  the data is: the bytes stay on the chain, and a reader of the data (MoonBrix, the arcade's assets) works through
  these blocks itself.
- `history`: what each transaction did to the addresses asked for (`history_for`: the arcade's accounts), the same
  rows as utxos.activity -- received, spent, paid out, the first other address. Every other address keeps no history.

What is spent is gone. An OP_RETURN output is never a coin. Block 0's outputs are not coins either (Bitcoin's code
never lets them be spent, and the Core does not count them). The last UNDO_DEPTH blocks keep what they changed, so a
reorg is rolled back; a deeper one means building again.

Building reads blocks through the Core (`rpc_blocks`) or straight from its blk*.dat files (`file_blocks`, which needs
to run as the Core's user). Afterwards `connect` follows the chain block by block and asks the Core for the coin each
input spends (a node with txindex=1).
"""
from __future__ import annotations

import json
import sqlite3
import struct
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from . import rawchain
from .script import OutputType, classify_script, hash160

#: One unspent output inside an address's array: txid (display order), vout, value in sats, height, kind.
RECORD = struct.Struct("<32sIqIB")
TXID = 32
#: What a coin is, for whoever reads it: the wallet spends only WALLET coins (utxos.SPENDABLE's legacy kinds).
WALLET, PUBKEY, OTHER = 0, 1, 2
_KIND = {OutputType.PUBKEYHASH: WALLET, OutputType.SCRIPTHASH: WALLET, OutputType.PUBKEY: PUBKEY}
#: Blocks whose changes are kept, so a reorg up to this deep is rolled back rather than built again.
UNDO_DEPTH = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (address TEXT PRIMARY KEY, utxos BLOB NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS data_blocks (height INTEGER PRIMARY KEY, txids BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS history (
    txid TEXT NOT NULL, address TEXT NOT NULL, height INTEGER NOT NULL, received INTEGER NOT NULL,
    spent INTEGER NOT NULL, paid_out INTEGER NOT NULL, other TEXT NOT NULL, PRIMARY KEY (txid, address)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS history_address ON history(address, height);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS undo (height INTEGER PRIMARY KEY, hash TEXT NOT NULL, changes TEXT NOT NULL);
"""


class UtxoSetError(RuntimeError):
    """The set and the chain disagree (a block that does not follow, a coin that is not there)."""


def classify(script: bytes, params) -> tuple[str, int]:
    """(the key an output is filed under, its kind): its address, or "script:<hash160>" for a script without one."""
    kind, address, *_ = classify_script(script, params)
    return address or "script:" + hash160(script).hex(), _KIND.get(kind, OTHER)


def owner(script: bytes, params) -> str:
    return classify(script, params)[0]


def _is_data(script: bytes) -> bool:
    return script[:1] == b"\x6a"


def _coin(script: bytes) -> bool:
    """Can this output ever be spent: not empty (a coinstake's marker, a coinbase's nothing), not OP_RETURN."""
    return bool(script) and not _is_data(script)


class UtxoSet:
    """The unspent set of one chain in one SQLite file."""

    def __init__(self, path: str | Path, params) -> None:
        self.path = Path(path)
        self.params = params
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- reading ---------------------------------------------------------------------------------------------

    def _meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    @property
    def height(self) -> int | None:
        got = self._meta("height")
        return None if got is None else int(got)

    @property
    def best(self) -> str | None:
        return self._meta("best")

    @property
    def history_for(self) -> set[str]:
        """The addresses whose history is kept (the arcade's accounts when it was built or last widened)."""
        return set(json.loads(self._meta("history_for") or "[]"))

    def unspent(self, address: str, wallet_only: bool = False) -> list[dict[str, Any]]:
        row = self.conn.execute("SELECT utxos FROM coins WHERE address=?", (address,)).fetchone()
        if not row:
            return []
        return [{"txid": txid.hex(), "vout": vout, "value": value, "height": height, "kind": kind}
                for txid, vout, value, height, kind in RECORD.iter_unpack(row[0])
                if not wallet_only or kind == WALLET]

    def balance(self, address: str) -> int:
        return sum(c["value"] for c in self.unspent(address))

    def data_blocks(self, since: int = 0) -> list[tuple[int, list[str]]]:
        """(height, [txid, ...]) for every block from `since` that carries an OP_RETURN, in height order."""
        return [(h, [blob[i:i + TXID].hex() for i in range(0, len(blob), TXID)])
                for h, blob in self.conn.execute("SELECT height, txids FROM data_blocks WHERE height>=? ORDER BY height",
                                                 (since,))]

    def history(self, addresses: Iterable[str]) -> list[dict[str, Any]]:
        """utxos.activity's rows for these addresses, oldest first."""
        addresses = list(addresses)
        if not addresses:
            return []
        marks = ",".join("?" for _ in addresses)
        cols = ("txid", "address", "height", "received", "spent", "paid_out", "other")
        return [dict(zip(cols, row)) for row in self.conn.execute(
            f"SELECT {', '.join(cols)} FROM history WHERE address IN ({marks}) ORDER BY height, txid", addresses)]

    def stats(self) -> dict[str, Any]:
        coins = addresses = value = 0
        for (blob,) in self.conn.execute("SELECT utxos FROM coins"):
            addresses += 1
            for _t, _v, amount, _h, _k in RECORD.iter_unpack(blob):
                coins += 1
                value += amount
        data = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(txids)), 0) FROM data_blocks").fetchone()
        return {"height": self.height, "best": self.best, "coins": coins, "addresses": addresses, "value": value,
                "data_blocks": data[0], "data_txs": data[1] // TXID,
                "history_rows": self.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]}

    def reset(self) -> None:
        """Forget everything (Admin -> Chains -> Rebuild): the next pass builds the set again from block 0."""
        with self.conn:
            for table in ("coins", "data_blocks", "history", "meta", "undo"):
                self.conn.execute(f"DELETE FROM {table}")

    # --- building from block 0 -------------------------------------------------------------------------------

    def build(self, blocks: Iterable[tuple[int, str, rawchain.Block]], tip: int,
              history_for: Iterable[str] = (), progress: Callable[[int, int], None] | None = None,
              coins: bool = True) -> dict[str, Any]:
        """Read `blocks` (height, hash, block) from 0 to `tip` in order and write the set they leave, and the history
        of `history_for`. Everything is held in memory meanwhile -- the unspent set of a small chain, not its
        history -- and written once. `coins=False` writes only the history of `history_for` (more accounts on a
        set already built: their past needs the whole chain read again, their coins do not)."""
        wanted = set(history_for)
        live: dict[tuple[bytes, int], tuple[str, int, int, int]] = {}  # (txid, vout) -> (owner, value, height, kind)
        data: dict[int, list[bytes]] = {}
        undo: list[tuple[int, str, dict]] = []
        rows: list[tuple] = []
        expected, prev = 0, None
        for height, block_hash, block in blocks:
            if height != expected or (prev is not None and block.prev != prev):
                raise UtxoSetError(f"block {height} ({block_hash}) does not follow block {expected - 1}")
            spent, made, carried, moved = self._changes(height, block, lambda key: live.pop(key, None), wanted)
            for key, coin in made:
                live[key] = coin
            if carried:
                data[height] = carried
            rows += moved
            if tip - height < UNDO_DEPTH:
                undo.append((height, block_hash, _undo_record(spent, made, block.prev)))
            prev, expected = block_hash, height + 1
            if progress is not None and height % 10_000 == 0:
                progress(height, len(live))
        if expected != tip + 1:
            raise UtxoSetError(f"the blocks stopped at {expected - 1}, not at {tip}")
        with self.conn:
            if coins:
                by_owner: dict[str, list[bytes]] = defaultdict(list)
                for (txid, vout), (who, value, height, kind) in live.items():
                    by_owner[who].append(RECORD.pack(txid, vout, value, height, kind))
                self.conn.execute("DELETE FROM coins")
                self.conn.execute("DELETE FROM data_blocks")
                self.conn.execute("DELETE FROM undo")
                self.conn.execute("DELETE FROM history")
                self.conn.executemany("INSERT INTO coins VALUES (?,?)",
                                      ((who, b"".join(sorted(recs))) for who, recs in by_owner.items()))
                self.conn.executemany("INSERT INTO data_blocks VALUES (?,?)",
                                      ((h, b"".join(ids)) for h, ids in data.items()))
                self.conn.executemany("INSERT INTO undo VALUES (?,?,?)",
                                      ((h, bh, json.dumps(ch)) for h, bh, ch in undo))
                self._set_tip(tip, prev)
                known = wanted
            else:
                if self.height != tip:
                    raise UtxoSetError(f"the history was read to {tip}, but the set is at {self.height}")
                known = self.history_for | wanted
            self.conn.executemany("INSERT OR REPLACE INTO history VALUES (?,?,?,?,?,?,?)", rows)
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('history_for', ?)", (json.dumps(sorted(known)),))
        return self.stats()

    # --- following the chain ---------------------------------------------------------------------------------

    def connect(self, height: int, block_hash: str, block: rawchain.Block,
                spent_output: Callable[[str, int], tuple[bytes, int]]) -> None:
        """Add the next block. `spent_output(txid, vout)` -> (script, value) of a coin made before this block (the
        Core's getrawtransaction): an input names the coin it spends, not whose it was."""
        if self.height is None or height != self.height + 1 or block.prev != self.best:
            raise UtxoSetError(f"block {height} does not follow the set's tip {self.height} ({self.best})")
        made_here: dict[tuple[bytes, int], tuple[str, int, int, int]] = {}

        def take(key):
            script, _value = spent_output(key[0].hex(), key[1])
            who, _kind = classify(script, self.params)
            gone = self._remove(who, key)
            if gone is None:
                raise UtxoSetError(f"block {height} spends {key[0].hex()}:{key[1]}, which is not in the set")
            return (who,) + gone

        spent, made, carried, moved = self._changes(height, block, take, self.history_for, made_here)
        with self.conn:
            for key, (who, value, h, kind) in made_here.items():
                self._add(who, key, value, h, kind)
            if carried:
                self.conn.execute("INSERT OR REPLACE INTO data_blocks VALUES (?,?)", (height, b"".join(carried)))
            self.conn.executemany("INSERT OR REPLACE INTO history VALUES (?,?,?,?,?,?,?)", moved)
            self.conn.execute("INSERT OR REPLACE INTO undo VALUES (?,?,?)",
                              (height, block_hash, json.dumps(_undo_record(spent, made, block.prev))))
            self.conn.execute("DELETE FROM undo WHERE height<=?", (height - UNDO_DEPTH,))
            self._set_tip(height, block_hash)

    def disconnect(self) -> None:
        """Take the tip block back off (a reorg), from what it changed."""
        height = self.height
        row = self.conn.execute("SELECT changes FROM undo WHERE height=?", (height,)).fetchone()
        if row is None:
            raise UtxoSetError(f"block {height} has no undo kept; build the set again")
        changes = json.loads(row[0])
        with self.conn:
            for who, txid, vout, _value, _h, _kind in changes["made"]:
                self._remove(who, (bytes.fromhex(txid), vout))
            for who, txid, vout, value, h, kind in changes["spent"]:
                self._add(who, (bytes.fromhex(txid), vout), value, h, kind)
            self.conn.execute("DELETE FROM data_blocks WHERE height=?", (height,))
            self.conn.execute("DELETE FROM history WHERE height=?", (height,))
            self.conn.execute("DELETE FROM undo WHERE height=?", (height,))
            self._set_tip(height - 1, changes["prev"])

    # --- internals -------------------------------------------------------------------------------------------

    def _changes(self, height, block, take, wanted, made_here=None):
        """What a block does: the coins it spends (via `take`), the coins it makes, its data-carrying txids, and the
        history rows of the `wanted` addresses. Transactions are taken in block order, so a coin made earlier in the
        block is found in `made_here`: it never reaches the set and leaves nothing to undo, but its spend is still
        history. History counts what the wallet can spend (WALLET coins), as utxos.on_block does."""
        made_here = {} if made_here is None else made_here
        spent, carried, moved = [], [], []
        for tx in block.txs:
            txid = bytes.fromhex(tx.txid)
            got: dict[str, list[int]] = {}
            for i in tx.vin:
                if i["vout"] == -1:
                    continue
                key = (bytes.fromhex(i["txid"]), i["vout"])
                if key in made_here:
                    coin = made_here.pop(key)
                else:
                    coin = take(key)
                    if coin is None:
                        continue
                    spent.append((key, coin))
                who, value, _h, kind = coin
                if kind == WALLET and who in wanted:
                    got.setdefault(who, [0, 0])[1] += value
            outs, data = [], False
            for n, (value, script) in enumerate(tx.vout):
                if _is_data(script):
                    data = True
                    outs.append(("", value))
                    continue
                if not _coin(script):
                    outs.append(("", value))
                    continue
                who, kind = classify(script, self.params)
                outs.append((who if kind == WALLET else "", value))
                if height > 0:
                    made_here[(txid, n)] = (who, value, height, kind)
                    if kind == WALLET and who in wanted and value > 0:
                        got.setdefault(who, [0, 0])[0] += value
            if data:
                carried.append(txid)
            for who, (received, gave) in got.items():
                others = [(a, v) for a, v in outs if a != who]
                moved.append((tx.txid, who, height, received, gave, sum(v for _, v in others) if gave else 0,
                              next((a for a, v in others if a and v > 0), "") if gave else ""))
        return spent, list(made_here.items()), carried, moved

    def _add(self, who: str, key: tuple[bytes, int], value: int, height: int, kind: int) -> None:
        row = self.conn.execute("SELECT utxos FROM coins WHERE address=?", (who,)).fetchone()
        blob = (row[0] if row else b"") + RECORD.pack(key[0], key[1], value, height, kind)
        self.conn.execute("INSERT OR REPLACE INTO coins VALUES (?,?)", (who, blob))

    def _remove(self, who: str, key: tuple[bytes, int]) -> tuple[int, int, int] | None:
        """Take a coin out of `who`'s array: (value, height, kind), or None when it is not there."""
        row = self.conn.execute("SELECT utxos FROM coins WHERE address=?", (who,)).fetchone()
        if not row:
            return None
        records = list(RECORD.iter_unpack(row[0]))
        gone = next((r for r in records if (r[0], r[1]) == key), None)
        if gone is None:
            return None
        kept = [r for r in records if r is not gone]
        if kept:
            self.conn.execute("UPDATE coins SET utxos=? WHERE address=?",
                              (b"".join(RECORD.pack(*r) for r in kept), who))
        else:
            self.conn.execute("DELETE FROM coins WHERE address=?", (who,))
        return gone[2], gone[3], gone[4]

    def _set_tip(self, height: int, block_hash: str | None) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('height', ?)", (str(height),))
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('best', ?)", (block_hash or "",))


def _undo_record(spent, made, prev: str) -> dict:
    return {"prev": prev,
            "spent": [[who, key[0].hex(), key[1], value, h, kind] for key, (who, value, h, kind) in spent],
            "made": [[who, key[0].hex(), key[1], value, h, kind] for key, (who, value, h, kind) in made]}


# --- reading blocks ---------------------------------------------------------------------------------------------

def _read(raw: bytes, block_hash: str | None, params) -> rawchain.Block:
    return rawchain.read_block(raw, block_hash, mweb=params.mweb, timed=params.tx_time, signed=params.block_sig,
                               scrypt_below=params.scrypt_headers_below)


def rpc_blocks(rpc, params, start: int, end: int, step: int = 500,
               rest: float = 0.0) -> Iterator[tuple[int, str, rawchain.Block]]:
    """Blocks `start`..`end` through the Core, raw (`getblock h false`) and proved by rawchain, in batches. `rest`:
    time slept after each batch, as a share of the time the batch took (0: as fast as the Core answers)."""
    for first in range(start, end + 1, step):
        took = time.perf_counter()
        heights = list(range(first, min(first + step, end + 1)))
        hashes = rpc.batch([("getblockhash", [h]) for h in heights])
        raws = rpc.batch([("getblock", [h, False]) for h in hashes])
        for height, block_hash, raw in zip(heights, hashes, raws):
            yield height, block_hash, _read(bytes.fromhex(raw), block_hash, params)
        if rest:
            time.sleep(min(5.0, (time.perf_counter() - took) * rest))


def file_blocks(blocks_dir: str | Path, params, magic: bytes, tip_hash: str,
                genesis: str | None = None) -> Iterator[tuple[int, str, rawchain.Block]]:
    """Blocks 0..`tip_hash` straight from a Core's blk*.dat files: each record is the network magic, a 4-byte
    length and the raw block. A Core writes blocks in the order they arrive, forks and orphans included, so every
    header is read first, and the chain is the one walked back from `tip_hash` to block 0. Needs read access to
    the files (the Core's user); appending while this reads is fine, a half-written last record is skipped."""
    files = sorted(Path(blocks_dir).glob("blk*.dat"))
    where: dict[bytes, tuple[int, int, int, bytes]] = {}               # hash -> (file, offset, size, prev)
    for number, path in enumerate(files):
        with open(path, "rb") as f:
            blob = f.read()
        at = 0
        while at + 8 <= len(blob):
            if blob[at:at + 4] != magic:
                at = blob.find(magic, at + 1)
                if at < 0:
                    break
                continue
            size = int.from_bytes(blob[at + 4:at + 8], "little")
            if at + 8 + size > len(blob) or size < 80:
                break
            header = blob[at + 8:at + 88]
            version = int.from_bytes(header[:4], "little", signed=True)
            named = rawchain.scrypt_hash(header) if version < params.scrypt_headers_below else rawchain.sha256d(header)
            where[named] = (number, at + 8, size, header[4:36])
            at += 8 + size
    chain, at = [], bytes.fromhex(tip_hash)[::-1]
    zero = b"\0" * 32
    while at != zero:
        if at not in where:
            raise UtxoSetError(f"block {at[::-1].hex()} is not in the block files")
        chain.append(at)
        at = where[at][3]
    chain.reverse()
    if genesis and chain[0][::-1].hex() != genesis:
        raise UtxoSetError(f"the files' first block is {chain[0][::-1].hex()}, not this chain's genesis")
    opened: dict[int, Any] = {}
    try:
        for height, named in enumerate(chain):
            number, offset, size, _prev = where[named]
            f = opened.get(number) or opened.setdefault(number, open(files[number], "rb"))
            f.seek(offset)
            block_hash = named[::-1].hex()
            yield height, block_hash, _read(f.read(size), block_hash, params)
    finally:
        for f in opened.values():
            f.close()


def spent_output_by_rpc(rpc) -> Callable[[str, int], tuple[bytes, int]]:
    """`connect`'s `spent_output`: the coin's script and value from the Core (getrawtransaction, txindex=1)."""
    def look(txid: str, vout: int) -> tuple[bytes, int]:
        out = rpc.call("getrawtransaction", txid, 1)["vout"][vout]
        return bytes.fromhex(out["scriptPubKey"]["hex"]), int(round(float(out["value"]) * 100_000_000))
    return look


def follow(uset: UtxoSet, rpc, params, upto: int | None = None) -> int:
    """Bring the set to the Core's tip (or `upto`), block by block, rolling back first when its tip left the chain
    (a reorg). Returns how many blocks were added."""
    added = 0
    tip = int(rpc.call("getblockcount")) if upto is None else int(upto)
    look = spent_output_by_rpc(rpc)
    while uset.height is not None:
        height = uset.height
        if str(rpc.call("getblockhash", height)) != uset.best:
            uset.disconnect()
            continue
        if height >= tip:
            break
        block_hash = str(rpc.call("getblockhash", height + 1))
        uset.connect(height + 1, block_hash, _read(bytes.fromhex(rpc.call("getblock", block_hash, False)), block_hash,
                                                   params), look)
        added += 1
    return added


#: The network magic in front of every block in a Core's blk*.dat (chainparams.cpp pchMessageStart), by chain.
MAGIC = {"kot": bytes.fromhex("3db58000")}


def main(argv: list[str] | None = None) -> int:
    """python -m arcade.utxoset build|follow|status|balance|data --net kot --db FILE [--conf F | --datadir D]
    [--blocks DIR]  (build from the blk*.dat files in DIR instead of through the Core)."""
    import argparse
    import time

    from .config import NETWORKS, load_rpc_credentials
    from .rpc import RpcClient

    ap = argparse.ArgumentParser(prog="python -m arcade.utxoset")
    ap.add_argument("action", choices=("build", "follow", "status", "balance", "data"))
    ap.add_argument("address", nargs="?")
    ap.add_argument("--net", default="kot")
    ap.add_argument("--db", required=True)
    ap.add_argument("--conf", type=Path)
    ap.add_argument("--datadir", type=Path)
    ap.add_argument("--blocks", type=Path)
    args = ap.parse_args(argv)
    params = NETWORKS[args.net]
    if not params.full_utxo_set:
        raise SystemExit(f"{params.label}: the full unspent set is a Kotia-only feature for now (Params.full_utxo_set)")
    uset = UtxoSet(args.db, params)
    try:
        if args.action in ("build", "follow"):
            rpc = RpcClient(load_rpc_credentials(params, conf_path=args.conf, datadir=args.datadir))
            if args.action == "build":
                tip = int(rpc.call("getblockcount"))
                began = time.time()
                if args.blocks:
                    blocks = file_blocks(args.blocks, params, MAGIC[args.net], str(rpc.call("getblockhash", tip)),
                                         params.genesis or None)
                else:
                    blocks = rpc_blocks(rpc, params, 0, tip)
                said = uset.build(blocks, tip, progress=lambda h, n: print(
                    f"{h:>9}  {100 * h / max(tip, 1):5.1f} %  unspent {n:>8}  {time.time() - began:6.0f} s", flush=True))
                said["seconds"] = round(time.time() - began)
                print(json.dumps(said, indent=1))
            else:
                print("blocks added:", follow(uset, rpc, params))
        elif args.action == "status":
            print(json.dumps(uset.stats(), indent=1))
        elif args.action == "balance":
            coins = uset.unspent(args.address or "")
            print(json.dumps({"address": args.address, "balance": sum(c["value"] for c in coins) / 1e8,
                              "coins": coins}, indent=1))
        else:
            for height, txids in uset.data_blocks():
                print(height, " ".join(txids))
    finally:
        uset.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
