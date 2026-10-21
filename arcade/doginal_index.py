"""Our own index of every Doginal on the Dogecoin chain, built and checked by the arcades themselves (the operator
2026-10-20: "index all 8 million doginals ... mindful of our 500 GB hard drive and limited compute"; "the mesh network
shared, indexing and verification"; "it should get the block scan the block get the next block scan the next block";
"if one goes off-line, that small chunk of blocks can be allocated to someone new"). Read from the chain only: no
outside index is asked, ever. Plan: ~/docs/doginals-index-plan.md on the operator's machine.

Two jobs, three modes (setting `doginal_index`):

* `walk` -- an arcade with its own Dogecoin Core reads every block from BASE in order, one at a time: each
  inscription's birth (the transaction whose input opens an ord envelope, as doginals.trace has it), and every move
  after (koinu first-in-first-out, ord's rule), so it knows where each one is now. Kept in doginal-index.sqlite: about
  100 bytes an inscription, pointers into the chain, never the content.
* `help` (the default) -- the shared work: the chain is cut into CHUNK-block chunks; an arcade leases one over the
  mesh, scans it a block at a time (through its own Core or a borrowed one, every block proved by rawchain), and says
  the chunk's hash. A walker says the same hash for every chunk it passes. Two arcades with the same hash for a chunk:
  `agreed`. A different hash is a lie or a bug, and is shown.
* `off`.

What a chunk's hash covers (`block_hash_of`): each block's hash, and each birth in it (txid, the input that carries
the envelope, the content type). Births can be found by anybody holding the block; where an inscription is NOW needs
every earlier block in order and the values of the coins spent, so only a walker answers that.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from . import ord_rules, rawchain

log = logging.getLogger("arcade.doginal_index")

#: Where each chain's ord index starts: the official first-inscription height of that chain's ord indexer (the operator
#: 2026-10-20: "The block height on each chain varies so find the official block height for each chains ord index"),
#: read from each indexer's src/chain.rs `first_inscription_height`. Every arcade cuts the same chunks from these,
#: so they never change without a new protocol word.
STARTS = {"doge": 4_600_000, "main": 186_920, "test": 34_237, "ltc": 2_424_429, "ltctest": 2_669_127}
SOURCES = {
    "doge": "ord-dogecoin (apezord) and wonky-ord: 4,600,000",
    "main": "ord-pepecoin (mvdnbrk) and pepecoin-ord-indexer: 186,920",
    "test": "pepecoin-ord-indexer (reallyshadydev): 34,237",
    "ltc": "ord-litecoin (ynohtna92): 2,424,429",
    "ltctest": "ord-litecoin (ynohtna92): 2,669,127",
}
#: Chains whose ords are mostly Taproot (ord's witness envelope) as well as the Doginals kind.
TAPROOT = frozenset({"ltc", "ltctest"})
BASE = STARTS["doge"]
CHUNK = 100
#: Blocks this far under the tip are scanned; the newest ones may still be replaced by a reorg.
SETTLE = 12
#: How many different arcades must say the same hash before a chunk is `agreed` (fewer than 5 arcades: every chunk
#: is scanned twice).
AGREE = 2
#: A lease lasts this long unless renewed by progress; a helper that goes quiet loses its chunk to the next one.
LEASE_SECONDS = 600
PROGRESS_EVERY = 10              # blocks between two progress words
#: Undo kept for this many blocks back, so a reorg rolls the walker back instead of stopping it.
UNDO_BLOCKS = 300
#: walk: the whole index here; help: chunks for the shared list; read: no index here, its questions asked of a linked
#: arcade that walks (2026-10-20: "read only. Where it can read the index, but it doesn't hold the index on
#: its own copy of arcade"); off.
MODES = ("walk", "help", "read", "off")
#: A machine with at least this many CPU cores helps on half of them by default and never rests; a smaller one (.25
#: has 4, and serves the live site) helps on one, resting as long as each pass took (half that core).
BIG_FROM_CORES = 8


def default_cores() -> int:
    """Cores the ord indexes help with when the operator has not said (Admin -> Ords)."""
    import os
    n = os.cpu_count() or 1
    return max(1, n // 2) if n >= BIG_FROM_CORES else 1


def _parse(job: tuple) -> tuple[str, str, int]:
    """One block's (hash, line, births) from its raw hex, in a worker process (a lane of a helper on many cores)."""
    raw_hex, block_hash, taproot, mweb = job
    block = rawchain.read_block(bytes.fromhex(raw_hex), block_hash, mweb=mweb)
    found = births(block, taproot)
    return block.hash, block_hash_of(block.hash, found), len(found)


# --------------------------------------------------------------------------- reading a block

def _pushes(raw: bytes) -> list:
    from .doginals import _pushes as pushes
    return pushes(raw)


def envelope_input(tx: rawchain.Tx) -> int | None:
    """The first input of `tx` that opens an ord envelope (a push of "ord" first), or None (doginals.envelope)."""
    for j, i in enumerate(tx.vin):
        if i["vout"] != -1 and i["script"][:4] == b"\x03ord":
            return j
    return None


def _parts_end(data: list) -> bool:
    """Do these pushes close the envelope: (count, data) pairs down to a count of 0 (doginals.content's reading)?"""
    i = 0
    while i + 1 < len(data) and data[i] is not None and len(data[i]) <= 3 and data[i + 1] is not None:
        if int.from_bytes(data[i], "little") == 0:
            return True
        i += 2
    return False


def birth_of(tx: rawchain.Tx) -> dict | None:
    """{"vin": 0, "ctype", "done"} when `tx` STARTS a Doginals-kind inscription, read alone as the official indexers
    read it (ord_rules.genesis: input 0 only, push-only, "ord", a piece count above 0). "done": complete in this
    transaction; else it continues in the one whose input 0 spends it."""
    if not tx.vin or tx.vin[0]["vout"] == -1:
        return None
    g = ord_rules.genesis(tx.vin[0]["script"])
    if g is None:
        return None
    return {"vin": 0, "ctype": g[1].hex(), "done": g[0] == "complete"}


def _ops(script: bytes):
    """(opcode, pushed bytes or None) for each operation of a script; stops quietly at a truncated push."""
    at = 0
    while at < len(script):
        op = script[at]
        at += 1
        if 1 <= op <= 75:
            n = op
        elif op == 0x4C and at < len(script):
            n, at = script[at], at + 1
        elif op == 0x4D:
            n, at = int.from_bytes(script[at:at + 2], "little"), at + 2
        elif op == 0x4E:
            n, at = int.from_bytes(script[at:at + 4], "little"), at + 4
        else:
            yield op, (b"" if op == 0 else bytes([op - 0x50]) if 0x51 <= op <= 0x60 else None)
            continue
        if at + n > len(script):
            return
        yield op, script[at:at + n]
        at += n


def taproot_envelopes(witness: list[bytes]) -> list[str]:
    """The content types (hex) of ord's witness envelopes in one input -- OP_FALSE OP_IF "ord" fields... OP_ENDIF in
    the tapscript of a script-path spend (ord's inscription format, as ord-litecoin reads it)."""
    w = [x for x in witness or []]
    if len(w) >= 2 and w[-1][:1] == b"\x50":                  # an annex
        w = w[:-1]
    if len(w) < 2:
        return []
    ops = list(_ops(w[-2]))
    found = []
    i = 0
    while i + 2 < len(ops):
        if ops[i][0] == 0x00 and ops[i + 1][0] == 0x63 and ops[i + 2][1] == b"ord":
            ctype, j = b"", i + 3
            while j < len(ops) and ops[j][0] != 0x68:
                tag = ops[j][1]
                if tag == b"":                                    # the body: pushes to OP_ENDIF
                    j += 1
                    while j < len(ops) and ops[j][0] != 0x68:
                        j += 1
                    break
                if tag == b"\x01" and j + 1 < len(ops) and isinstance(ops[j + 1][1], bytes):
                    ctype = ops[j + 1][1]
                j += 2
            found.append(ctype.hex())
            i = j + 1
        else:
            i += 1
    return found


def births_of(tx: rawchain.Tx, taproot: bool = False) -> list[dict]:
    """Every inscription `tx` gives birth to: {"n", "vin", "ctype", "done"}; the id is txid + "i" + n. The Doginals kind
    (an input script opening with "ord", i0) and, on a Taproot chain, ord's witness envelopes, numbered in input order."""
    out = []
    b = birth_of(tx)
    if b is not None:
        out.append({"n": 0, **b})
    if taproot:
        for j, i in enumerate(tx.vin):
            for ctype in taproot_envelopes(i.get("witness") or []):
                out.append({"n": len(out), "vin": j, "ctype": ctype, "done": True})
    return out


def births(block: rawchain.Block, taproot: bool = False) -> list[tuple[str, int, int, str]]:
    """(txid, n, envelope input, content type hex) for every birth in the block, in block order."""
    return [(tx.txid, b["n"], b["vin"], b["ctype"]) for tx in block.txs for b in births_of(tx, taproot)]


def block_hash_of(block_hash: str, found: list[tuple[str, int, int, str]]) -> str:
    """One block's line in a chunk's hash: the block, and what was born in it."""
    h = hashlib.sha256(block_hash.encode() + b"\n")
    for txid, n, vin, ctype in found:
        h.update(f"{txid}i{n}:{vin}:{ctype}\n".encode())
    return h.hexdigest()


def chunk_hash_of(lines: list[str]) -> str:
    """A chunk's hash from its CHUNK block lines, in height order."""
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def chunk_of(height: int, base: int | None = None) -> int:
    return (height - (BASE if base is None else base)) // CHUNK


def chunk_range(c: int, base: int | None = None) -> tuple[int, int]:
    lo = (BASE if base is None else base) + c * CHUNK
    return lo, lo + CHUNK


def ready_chunks(tip: int, base: int | None = None) -> int:
    """How many chunks are wholly settled under this tip."""
    return max(0, (tip - SETTLE + 1 - (BASE if base is None else base)) // CHUNK)


# --------------------------------------------------------------------------- the store

class Store:
    def __init__(self, path: Path, base: int | None = None):
        self.path = Path(path)
        self.base = BASE if base is None else base
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=60, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        # Kept under other rules (or none, before 2026-10-20's): cut again from the start.
        if self.meta("rules") != ord_rules.RULES:
            for (table,) in self._db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'meta'"
                                             ).fetchall():
                self._db.execute(f"DROP TABLE IF EXISTS {table}")
            self._db.execute("DELETE FROM meta")
            self.set_meta("rules", ord_rules.RULES)
        self._db.executescript("""
            -- every block a walker or a helper read: its hash, its line in the chunk hash, births seen in it
            CREATE TABLE IF NOT EXISTS block (height INTEGER PRIMARY KEY, hash TEXT NOT NULL, line TEXT NOT NULL,
                                              n INTEGER NOT NULL DEFAULT 0, moves TEXT);
            -- the inscriptions (walker), numbered as the official indexers number them: when complete
            -- ctype: the content type as text; proto: a JSON body's "p" (drc-20, dns, ...); size: the body's bytes --
            -- so a protocol indexed later finds its own inscriptions by type or "p" (2026-10-20)
            CREATE TABLE IF NOT EXISTS insc (rid INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, number INTEGER NOT NULL,
                                             height INTEGER NOT NULL, txpos INTEGER NOT NULL, ctype TEXT NOT NULL,
                                             proto TEXT, size INTEGER, pieces INTEGER NOT NULL DEFAULT 1,
                                             burned INTEGER);
            CREATE INDEX IF NOT EXISTS insc_number ON insc (number);
            CREATE INDEX IF NOT EXISTS insc_ctype ON insc (ctype, number);
            CREATE INDEX IF NOT EXISTS insc_proto ON insc (proto, number);
            -- where each one is now (walker): the output and the koinu inside it
            CREATE TABLE IF NOT EXISTS at (rid INTEGER PRIMARY KEY, txid TEXT NOT NULL, vout INTEGER NOT NULL,
                                           off INTEGER NOT NULL, height INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS at_out ON at (txid, vout);
            -- a body still arriving: keyed by the txid of its latest piece, input 0's scripts so far (hex)
            CREATE TABLE IF NOT EXISTS pending (txid TEXT PRIMARY KEY, scripts TEXT NOT NULL, height INTEGER NOT NULL,
                                                origin TEXT NOT NULL);
            -- DRC-20 / PRC-20 (ord_rules): tokens, balances (units, as text: they pass 2^63), transfer notes not sent yet
            CREATE TABLE IF NOT EXISTS tok (tick TEXT PRIMARY KEY, shown TEXT NOT NULL, id TEXT NOT NULL,
                                            number INTEGER NOT NULL, supply TEXT NOT NULL, lim TEXT NOT NULL,
                                            dec INTEGER NOT NULL, minted TEXT NOT NULL, deployer TEXT NOT NULL,
                                            height INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS bal (owner TEXT NOT NULL, tick TEXT NOT NULL, overall TEXT NOT NULL,
                                            transferable TEXT NOT NULL, PRIMARY KEY (owner, tick));
            CREATE TABLE IF NOT EXISTS xfer (rid INTEGER PRIMARY KEY, tick TEXT NOT NULL, amt TEXT NOT NULL,
                                             owner TEXT NOT NULL, reveal TEXT NOT NULL);
            -- name claims ("p":"dns"), each with the name it makes under every reading (NULL: not a claim there)
            CREATE TABLE IF NOT EXISTS names (rid INTEGER PRIMARY KEY, number INTEGER NOT NULL, height INTEGER NOT NULL,
                                              kind TEXT NOT NULL, raw TEXT NOT NULL, spec TEXT, json TEXT,
                                              doge2023 TEXT);
            CREATE INDEX IF NOT EXISTS names_spec ON names (spec, number);
            CREATE INDEX IF NOT EXISTS names_json ON names (json, number);
            CREATE INDEX IF NOT EXISTS names_doge2023 ON names (doge2023, number);
            CREATE TABLE IF NOT EXISTS undo (height INTEGER PRIMARY KEY, doc TEXT NOT NULL);
            -- every arcade's word about every chunk, this one's included
            CREATE TABLE IF NOT EXISTS seen (chunk INTEGER NOT NULL, node TEXT NOT NULL, hash TEXT NOT NULL,
                                             births INTEGER NOT NULL, at REAL NOT NULL, PRIMARY KEY (chunk, node));
        """)

    def q(self, sql: str, args: tuple = ()) -> list:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def x(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self._db.execute(sql, args)

    def many(self, sql: str, rows: list) -> None:
        with self._lock:
            self._db.executemany(sql, rows)

    def tx(self):
        return _Tx(self)

    def meta(self, k: str, default: Any = None) -> Any:
        row = self.q("SELECT v FROM meta WHERE k=?", (k,))
        return json.loads(row[0][0]) if row else default

    def set_meta(self, k: str, v: Any) -> None:
        self.x("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, json.dumps(v)))

    # --- chunks --------------------------------------------------------------------------------------------------

    def chunk_line(self, c: int) -> tuple[str, int] | None:
        """(this arcade's hash of chunk c, births in it) when every one of its blocks has been read here."""
        lo, hi = chunk_range(c, self.base)
        rows = self.q("SELECT height, line, n FROM block WHERE height>=? AND height<? ORDER BY height", (lo, hi))
        if len(rows) != CHUNK:
            return None
        return chunk_hash_of([r[1] for r in rows]), sum(r[2] for r in rows)

    def say(self, c: int, node: str, hash_: str, n: int) -> bool:
        """File an arcade's hash for a chunk (node "" is this arcade). True when it is new."""
        was = self.q("SELECT hash FROM seen WHERE chunk=? AND node=?", (c, node))
        if was and was[0][0] == hash_:
            return False
        self.x("INSERT OR REPLACE INTO seen VALUES (?,?,?,?,?)", (c, node, hash_, n, time.time()))
        return True

    def words(self, c: int) -> dict[str, str]:
        return {r[0]: r[1] for r in self.q("SELECT node, hash FROM seen WHERE chunk=?", (c,))}

    def all_words(self, below: int) -> dict[int, dict[str, str]]:
        out: dict[int, dict[str, str]] = {}
        for c, node, h in self.q("SELECT chunk, node, hash FROM seen WHERE chunk < ?", (below,)):
            out.setdefault(c, {})[node] = h
        return out


class _Tx:
    def __init__(self, store: Store):
        self.store = store

    def __enter__(self):
        self.store._lock.acquire()
        self.store._db.execute("BEGIN")
        return self.store._db

    def __exit__(self, kind, *exc):
        try:
            self.store._db.execute("ROLLBACK" if kind else "COMMIT")
        finally:
            self.store._lock.release()


# --------------------------------------------------------------------------- the walker

class Reorg(Exception):
    pass


def _outpoint(txid: str, vout: int) -> bytes:
    return bytes.fromhex(txid) + vout.to_bytes(4, "little")


#: The key of each table a block's walk writes, for its undo (every row's old value is kept before it changes).
KEYS = {"insc": ("rid",), "at": ("rid",), "pending": ("txid",), "tok": ("tick",), "bal": ("owner", "tick"),
        "xfer": ("rid",), "names": ("rid",)}


class _Block:
    """One block's writes, each row's old value kept first (rollback puts them back)."""

    def __init__(self, db):
        self.db = db
        self.old: list[list] = []
        self._seen: set = set()

    def get(self, table: str, key: tuple) -> dict | None:
        cols = KEYS[table]
        cur = self.db.execute(f"SELECT * FROM {table} WHERE " + " AND ".join(f"{c}=?" for c in cols), key)
        row = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], row)) if row else None

    def put(self, table: str, key: tuple, row: dict | None) -> None:
        if (table, key) not in self._seen:
            self._seen.add((table, key))
            self.old.append([table, list(key), self.get(table, key)])
        cols = KEYS[table]
        self.db.execute(f"DELETE FROM {table} WHERE " + " AND ".join(f"{c}=?" for c in cols), key)
        if row is not None:
            self.db.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                            tuple(row.values()))


class Walker:
    """Every block from the chain's start in order, by the official indexers' rules (ord_rules): inscriptions numbered
    when complete, every move, DRC-20 / PRC-20 balances, name claims. Needs this arcade's OWN Core (the values of spent
    coins are asked of it)."""

    def __init__(self, store: Store, taproot: bool = False, mweb: bool = False, params: Any = None,
                 token_protocol: str = "drc-20"):
        self.store = store
        self.base, self.taproot, self.mweb = store.base, taproot, mweb
        self.params = params
        self.token_protocol = token_protocol
        self.tracked: dict[bytes, list[tuple[int, int]]] | None = None     # outpoint -> [(rid, koinu offset)]
        self.values: OrderedDict[bytes, int] = OrderedDict()               # recent outputs' koinu
        self.rate = 0.0
        self.error = ""

    def next_height(self) -> int:
        return int(self.store.meta("walk_next", self.base))

    def _load(self) -> None:
        tracked: dict[bytes, list[tuple[int, int]]] = {}
        for rid, txid, vout, off in self.store.q("SELECT rid, txid, vout, off FROM at"):
            tracked.setdefault(_outpoint(txid, vout), []).append((rid, off))
        self.tracked = tracked

    def _value(self, rpc, txid: str, vout: int) -> int:
        key = _outpoint(txid, vout)
        got = self.values.get(key)
        if got is not None:
            return got
        tx = rawchain.read_tx(bytes.fromhex(rpc.call("getrawtransaction", txid, 0)), txid)
        for n, (v, _s) in enumerate(tx.vout):
            self._keep(_outpoint(txid, n), v)
        return tx.vout[vout][0]

    def _keep(self, key: bytes, value: int) -> None:
        self.values[key] = value
        if len(self.values) > 2_000_000:
            self.values.popitem(last=False)

    def owner_of(self, script: bytes) -> str:
        """Who holds an output: its address, or a script's hash when it has none (drc20 ScriptKey)."""
        from .script import b58check_encode, hash160
        p = self.params
        if p is not None:
            if len(script) == 25 and script[:3] == b"\x76\xa9\x14" and script[23:] == b"\x88\xac":
                return b58check_encode(p.pubkeyhash_version, script[3:23])
            if len(script) == 23 and script[:2] == b"\xa9\x14" and script[22:] == b"\x87":
                return b58check_encode(p.scripthash_version, script[2:22])
        return "script:" + hash160(script).hex()

    def step(self, rpc, tip: int, budget: float = 5.0) -> int:
        """Walk on for about `budget` seconds, never past tip - SETTLE. Returns the blocks walked."""
        if self.tracked is None:
            self._load()
        start, done = time.monotonic(), 0
        while True:
            h = self.next_height()
            if h > tip - SETTLE or (done and time.monotonic() - start >= budget):
                break
            block_hash = rpc.call("getblockhash", h)
            block = rawchain.read_block(bytes.fromhex(rpc.call("getblock", block_hash, 0)), block_hash, mweb=self.mweb)
            if h > self.base:
                before = self.store.q("SELECT hash FROM block WHERE height=?", (h - 1,))
                if before and before[0][0] != block.prev:
                    self.rollback(h - 1)
                    continue
            self.walk_block(rpc, h, block)
            done += 1
        took = time.monotonic() - start
        if done:
            self.rate = done / max(took, 1e-6)
        return done

    def walk_block(self, rpc, h: int, block: rawchain.Block) -> list[str]:
        """One block, filed with its undo. Returns the moves lines (for tests)."""
        tracked = self.tracked
        assert tracked is not None
        moves: list[str] = []
        born_here = births(block, self.taproot)
        next_number = int(self.store.meta("next_number", 0))
        number_before = next_number
        next_rid = (self.store.q("SELECT COALESCE(MAX(rid), 0) FROM insc")[0][0]) + 1
        with self.store.tx() as db:
            w = _Block(db)
            for pos, tx in enumerate(block.txs):
                for n, (v, _s) in enumerate(tx.vout):
                    if v <= 100_000_000:
                        self._keep(_outpoint(tx.txid, n), v)
                if pos == 0:
                    continue
                # What this transaction carries in: inscriptions on the coins it spends, at their koinu among the inputs.
                needs = [j for j, i in enumerate(tx.vin) if i["vout"] != -1 and _outpoint(i["txid"], i["vout"]) in tracked]
                sizes_upto = max(needs, default=-1)
                taproot = [b for b in births_of(tx, True) if b["n"] or b["vin"]] if self.taproot else []
                sizes_upto = max([sizes_upto] + [b["vin"] for b in taproot])
                sizes = [self._value(rpc, i["txid"], i["vout"]) for i in tx.vin[:sizes_upto]] if sizes_upto > 0 else []
                # (rid, koinu among the inputs, new here, the txid it sat in before this one)
                carried: list[tuple[int, int, bool, str]] = []
                for j in needs:
                    base = sum(sizes[:j])
                    for rid, off in tracked.pop(_outpoint(tx.vin[j]["txid"], tx.vin[j]["vout"])):
                        carried.append((rid, base + off, False, tx.vin[j]["txid"]))
                new: list[tuple[int, str, bytes, bytes, int]] = []  # (rid, id, ctype, body, pieces)
                # A new inscription only when nothing inscribed sits on the first koinu (inscription_updater.rs).
                if all(c[1] != 0 for c in carried) and tx.vin and tx.vin[0]["vout"] != -1:
                    prev = tx.vin[0]["txid"]
                    chain = w.get("pending", (prev,))
                    scripts = ([bytes.fromhex(x) for x in json.loads(chain["scripts"])] if chain else []) + \
                        [tx.vin[0]["script"]]
                    state, ctype, body = ord_rules.parse(scripts)
                    if chain is not None and state != "partial":
                        w.put("pending", (prev,), None)
                    if state == "partial":
                        if chain is not None:
                            w.put("pending", (prev,), None)
                        w.put("pending", (tx.txid,), {"txid": tx.txid, "scripts": json.dumps([x.hex() for x in scripts]),
                                                     "height": h, "origin": chain["origin"] if chain else tx.txid})
                    elif state == "complete":
                        first = json.loads(chain["scripts"]) if chain else []
                        origin = chain["origin"] if chain else tx.txid
                        iid = f"{origin}i0"
                        if not w.db.execute("SELECT 1 FROM insc WHERE id=?", (iid,)).fetchone():
                            rid, next_rid = next_rid, next_rid + 1
                            w.put("insc", (rid,), {"rid": rid, "id": iid, "number": next_number, "height": h,
                                                    "txpos": pos, "ctype": ctype.decode("utf-8", "replace"),
                                                    "proto": ord_rules.protocol_of(body), "size": len(body),
                                                    "pieces": len(first) + 1, "burned": None})
                            next_number += 1
                            new.append((rid, iid, ctype, body, len(scripts)))
                            carried.append((rid, 0, True, ""))
                for b in taproot:                                    # Litecoin's witness envelopes
                    iid = f"{tx.txid}i{b['n']}"
                    if w.db.execute("SELECT 1 FROM insc WHERE id=?", (iid,)).fetchone():
                        continue
                    rid, next_rid = next_rid, next_rid + 1
                    w.put("insc", (rid,), {"rid": rid, "id": iid, "number": next_number, "height": h, "txpos": pos,
                                            "ctype": bytes.fromhex(b["ctype"]).decode("utf-8", "replace"),
                                            "proto": None, "size": None, "pieces": 1, "burned": None})
                    next_number += 1
                    carried.append((rid, sum(sizes[:b["vin"]]), True, ""))
                if not carried:
                    continue
                # Where each carried koinu lands: the outputs in order; past the last one, it went to the fee.
                ends, total = [], 0
                for v, _s in tx.vout:
                    total += v
                    ends.append(total)
                landed: dict[int, tuple[int | None, str | None]] = {}     # rid -> (vout or None, owner or None)
                for rid, p, is_new, _was in sorted(carried, key=lambda c: c[1]):
                    iid = w.db.execute("SELECT id FROM insc WHERE rid=?", (rid,)).fetchone()[0]
                    k = next((n for n, e in enumerate(ends) if p < e), None)
                    if k is None:
                        w.put("at", (rid,), None)
                        row = w.get("insc", (rid,))
                        w.put("insc", (rid,), {**row, "burned": h})
                        moves.append(f"X {iid}\n")
                        landed[rid] = (None, None)
                        continue
                    off = p - (ends[k - 1] if k else 0)
                    w.put("at", (rid,), {"rid": rid, "txid": tx.txid, "vout": k, "off": off, "height": h})
                    tracked.setdefault(_outpoint(tx.txid, k), []).append((rid, off))
                    moves.append(f"{'B' if is_new else 'M'} {iid} {tx.txid}:{k}:{off}\n")
                    landed[rid] = (k, self.owner_of(tx.vout[k][1]))
                self._tokens(w, h, tx, new, carried, landed)
                self._names(w, h, new)
            line = block_hash_of(block.hash, born_here)
            moved = hashlib.sha256("".join(moves).encode()).hexdigest()
            db.execute("INSERT OR REPLACE INTO block VALUES (?,?,?,?,?)", (h, block.hash, line, len(born_here), moved))
            db.execute("INSERT OR REPLACE INTO undo VALUES (?,?)",
                       (h, json.dumps({"rows": w.old, "next_number": number_before})))
            db.execute("DELETE FROM undo WHERE height<?", (h - UNDO_BLOCKS,))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('walk_next', ?)", (json.dumps(h + 1),))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('next_number', ?)", (json.dumps(next_number),))
            if (h + 1 - self.base) % CHUNK == 0:
                c = chunk_of(h, self.base)
                rows = db.execute("SELECT line, n FROM block WHERE height>=? AND height<=? ORDER BY height",
                                  (chunk_range(c, self.base)[0], h)).fetchall()
                if len(rows) == CHUNK:
                    db.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?,?,?)",
                               (c, "", chunk_hash_of([r[0] for r in rows]), sum(r[1] for r in rows), time.time()))
        return moves

    # --- DRC-20 / PRC-20 -----------------------------------------------------------------------------------------

    def _tokens(self, w: _Block, h: int, tx, new: list, carried: list, landed: dict) -> None:
        """deploy / mint / inscribe-transfer for a new single-transaction inscription that landed in an output, then
        the first move of each transfer note (wonky-ord drc20_updater.rs, context.rs)."""
        for rid, iid, ctype, body, pieces in new:
            if pieces != 1 or landed.get(rid, (None,))[0] is None:
                continue                                      # only single-transaction inscriptions, never to the fee
            op = ord_rules.token_op(self.token_protocol, ctype, body)
            if op is None:
                continue
            number = w.get("insc", (rid,))["number"]
            try:
                self._token_op(w, h, rid, iid, number, op, landed[rid][1], tx.txid)
            except ord_rules.TokenError:
                pass
        for rid, _p, is_new, was in carried:
            if is_new:
                continue
            note = w.get("xfer", (rid,))
            if note is None or note["reveal"] != was:
                continue                                      # no note, or not its first move: ignored
            to = landed.get(rid, (None, None))[1] or note["owner"]   # to the fee: back to the sender
            if self._send(w, note, to):
                w.put("xfer", (rid,), None)

    @staticmethod
    def _bal(w: _Block, owner: str, tick: str) -> dict:
        return w.get("bal", (owner, tick)) or {"owner": owner, "tick": tick, "overall": "0", "transferable": "0"}

    def _token_op(self, w: _Block, h: int, rid: int, iid: str, number: int, op: dict, owner: str, txid: str) -> None:
        tick = op["tick"].lower()
        info = w.get("tok", (tick,))
        if op["op"] == "deploy":
            if info is not None:
                raise ord_rules.TokenError("duplicate tick")
            d = ord_rules._num(op["dec"] if op.get("dec") is not None else str(ord_rules.MAX_DECIMALS))
            if d != d.to_integral_value():
                raise ord_rules.TokenError("decimals not whole")
            dec = int(d)
            if dec > ord_rules.MAX_DECIMALS:
                raise ord_rules.TokenError("too many decimals")
            supply = ord_rules._num(op["max"])
            limit = ord_rules._num(op["lim"] if op.get("lim") is not None else op["max"])
            for d in (supply, limit):
                if d <= 0 or d > ord_rules.MAX_SUPPLY:
                    raise ord_rules.TokenError("out of range")
            w.put("tok", (tick,), {"tick": tick, "shown": op["tick"], "id": iid, "number": number,
                                   "supply": str(ord_rules._units(supply, dec)), "lim": str(ord_rules._units(limit, dec)),
                                   "dec": dec, "minted": "0", "deployer": owner, "height": h})
            return
        if info is None:
            raise ord_rules.TokenError("no such tick")
        dec = int(info["dec"])
        amt = ord_rules._num(op["amt"])
        if ord_rules._scale(amt) > dec:
            raise ord_rules.TokenError("too many decimals")
        units = ord_rules._units(amt, dec)
        if op["op"] == "mint":
            if units <= 0 or units > int(info["lim"]):
                raise ord_rules.TokenError("amount out of range")
            minted, supply = int(info["minted"]), int(info["supply"])
            if minted >= supply:
                raise ord_rules.TokenError("minted out")
            units = min(units, supply - minted)                # cut to what is left
            bal = self._bal(w, owner, tick)
            w.put("bal", (owner, tick), {**bal, "overall": str(int(bal["overall"]) + units)})
            w.put("tok", (tick,), {**info, "minted": str(minted + units)})
            return
        # inscribe-transfer: set an amount aside on this note
        if units <= 0 or units > int(info["supply"]):
            raise ord_rules.TokenError("amount out of range")
        bal = self._bal(w, owner, tick)
        available = int(bal["overall"]) - int(bal["transferable"])
        if available < units:
            raise ord_rules.TokenError("insufficient balance")
        w.put("bal", (owner, tick), {**bal, "transferable": str(int(bal["transferable"]) + units)})
        w.put("xfer", (rid,), {"rid": rid, "tick": tick, "amt": str(units), "owner": owner, "reveal": txid})

    def _send(self, w: _Block, note: dict, to: str) -> bool:
        """A transfer note's first move: its amount leaves the sender and reaches `to`. False (nothing done, the note
        kept) when the sender no longer has it, as wonky-ord's checked_sub refuses."""
        tick, units, frm = note["tick"], int(note["amt"]), note["owner"]
        src = self._bal(w, frm, tick)
        overall, transferable = int(src["overall"]) - units, int(src["transferable"]) - units
        if overall < 0 or transferable < 0:
            return False
        w.put("bal", (frm, tick), {**src, "overall": str(overall), "transferable": str(transferable)})
        dst = self._bal(w, to, tick)
        w.put("bal", (to, tick), {**dst, "overall": str(int(dst["overall"]) + units)})
        return True

    # --- names -------------------------------------------------------------------------------------------------

    def _names(self, w: _Block, h: int, new: list) -> None:
        for rid, _iid, ctype, body, _pieces in new:
            claim = ord_rules.name_claim(ctype, body)
            if claim is None:
                continue
            kind, raw = claim
            row = {"rid": rid, "number": w.get("insc", (rid,))["number"], "height": h, "kind": kind, "raw": raw[:300]}
            for rule in ord_rules.NAME_RULES:
                row[rule] = ord_rules.normalise(rule, kind, raw)
            if any(row[r] for r in ord_rules.NAME_RULES):
                w.put("names", (rid,), row)

    def rollback(self, h: int) -> None:
        """Undo block h (a reorg replaced it), then reload what is tracked."""
        row = self.store.q("SELECT doc FROM undo WHERE height=?", (h,))
        if not row:
            self.error = f"a reorg deeper than {UNDO_BLOCKS} blocks at {h}: the walk stopped there"
            raise Reorg(self.error)
        undo = json.loads(row[0][0])
        with self.store.tx() as db:
            for table, key, old in reversed(undo["rows"]):
                cols = KEYS[table]
                db.execute(f"DELETE FROM {table} WHERE " + " AND ".join(f"{c}=?" for c in cols), tuple(key))
                if old is not None:
                    db.execute(f"INSERT INTO {table} ({','.join(old)}) VALUES ({','.join('?' * len(old))})",
                               tuple(old.values()))
            db.execute("DELETE FROM block WHERE height=?", (h,))
            db.execute("DELETE FROM undo WHERE height=?", (h,))
            db.execute("DELETE FROM seen WHERE chunk=? AND node=''", (chunk_of(h, self.base),))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('walk_next', ?)", (json.dumps(h),))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('next_number', ?)", (json.dumps(undo["next_number"]),))
        log.warning("ord index: block %d was replaced (reorg); walked back", h)
        self.tracked = None
        self._load()


# --------------------------------------------------------------------------- the shared chunks

class Chunks:
    """The chunk leases this arcade knows of (its own and its linked arcades'), and choosing the next chunk to scan."""

    def __init__(self, store: Store):
        self.store = store
        self.leases: dict[int, dict[str, tuple[float, int]]] = {}       # chunk -> node -> (until, at height)
        self.peers: dict[str, dict] = {}                                 # node -> its last "hi"
        # This arcade's own leases, one per lane (a core it helps with): [{"c", "at"}]. One lease was kept before.
        self.lanes: list[dict] = list(store.meta("lanes") or ([store.meta("lease")] if store.meta("lease") else []))

    @property
    def mine(self) -> dict | None:
        return self.lanes[0] if self.lanes else None

    def lease_from(self, node: str, c: int, at: int, until: float) -> None:
        self.leases.setdefault(int(c), {})[node] = (float(until), int(at))

    def drop_from(self, node: str, c: int) -> None:
        self.leases.get(int(c), {}).pop(node, None)

    def gone(self, node: str) -> None:
        for held in self.leases.values():
            held.pop(node, None)

    def wanted(self, c: int, now: float, words: dict[str, str] | None = None) -> int:
        """How many more scans chunk c wants, counting the leases running on it."""
        words = self.store.words(c) if words is None else words
        if "" in words:
            return 0
        tally: dict[str, int] = {}
        for h in words.values():
            tally[h] = tally.get(h, 0) + 1
        best = max(tally.values(), default=0)
        busy = sum(1 for until, _at in self.leases.get(c, {}).values() if until > now)
        return max(0, AGREE - best - busy)

    def choose(self, ready: int, now: float, exclude: set | frozenset = frozenset()) -> int | None:
        """The next chunk to take: one that some arcade has already said (so it becomes agreed) first, then the lowest
        nobody has; a random one of the first few either way, so two helpers rarely take the same."""
        said = self.store.all_words(ready)
        first = [c for c in sorted(said) if c not in exclude and self.wanted(c, now, said[c]) > 0][:16]
        if not first:
            c = 0
            while c < ready and len(first) < 16:
                if c not in said and c not in exclude and self.wanted(c, now, {}) > 0:
                    first.append(c)
                c += 1
        return random.choice(first) if first else None


def scan_block(rpc, h: int, taproot: bool = False, mweb: bool = False) -> tuple[str, str, int]:
    """(block hash, its line, births in it) for one block, read raw and proved (rawchain)."""
    block_hash = rpc.call("getblockhash", h)
    block = rawchain.read_block(bytes.fromhex(rpc.call("getblock", block_hash, 0)), block_hash, mweb=mweb)
    found = births(block, taproot)
    return block.hash, block_hash_of(block.hash, found), len(found)


# --------------------------------------------------------------------------- the worker

class DoginalIndex:
    """The thread's work: walk or help, a little at a time, and what the mesh hears and says (DxShare)."""

    def __init__(self, path: Path, rpc_factory: Callable[[], Any] | None, mode: Callable[[], str],
                 own_core: Callable[[], bool] = lambda: True, net: str = "doge", mweb: bool = False,
                 cores: Callable[[], int] | None = None):
        self.net = net
        self.cores = cores or default_cores
        self._pool: Any = None
        self._pool_size = 0
        self.base = STARTS.get(net, BASE) if net != "doge" else BASE
        self.taproot, self.mweb = net in TAPROOT, mweb
        self.store = Store(path, self.base)
        self.rpc_factory = rpc_factory
        self.mode = mode
        self.own_core = own_core
        from .config import NETWORKS
        self.walker = Walker(self.store, self.taproot, mweb, params=NETWORKS.get(net),
                             token_protocol="prc-20" if net in ("main", "test") else "drc-20")
        self.chunks = Chunks(self.store)
        self.share: Any = None                       # DxShare, when the mesh runs
        self.tip = 0
        self.helped = int(self.store.meta("helped", 0))
        self.help_rate = 0.0
        self.error = ""
        self._said_upto = int(self.store.meta("said_upto", -1))

    # --- one pass --------------------------------------------------------------------------------------------------

    def step(self, budget: float = 5.0) -> float:
        """Do about `budget` seconds of work; returns how long to rest (as long as the work took: half the time)."""
        mode = self.mode()
        if mode not in ("walk", "help") or self.rpc_factory is None:
            return 10.0
        started = time.monotonic()
        try:
            with self.rpc_factory() as rpc:
                self.tip = int(rpc.call("getblockcount"))
                if mode == "walk" and self.own_core():
                    self._let_go()
                    self.walker.step(rpc, self.tip, budget)
                else:
                    self._help(rpc, budget)
            self.error = ""
        except Reorg as exc:
            self.error = str(exc)
            return 60.0
        except Exception as exc:                         # noqa: BLE001 -- said in the admin panel, tried again
            self.error = str(exc)[:200]
            log.debug("doginal index pass failed", exc_info=True)
            return 30.0
        self._tell_new_chunks()
        took = time.monotonic() - started
        # One core on a small machine: rest as long as the work took. More, or a big machine: straight on (the operator
        # 2026-10-20: a test machine "is a much faster machine. It shouldn't be pacing with the other one").
        import os
        if self._lanes_wanted() > 1 or (os.cpu_count() or 1) >= BIG_FROM_CORES:
            return 0.05
        return max(0.5, took)

    def _lanes_wanted(self) -> int:
        try:
            return max(1, int(self.cores()))
        except (TypeError, ValueError):
            return 1

    def _help(self, rpc, budget: float) -> None:
        """Help with as many chunks at once as this arcade gives cores (a lane each): their next blocks are fetched
        side by side and read in worker processes, a block a lane at a time (2026-10-20: dedicate cores)."""
        now = time.time()
        ready = ready_chunks(self.tip, self.base)
        lanes = self.chunks.lanes
        want = self._lanes_wanted()
        while len(lanes) > want:                                  # fewer cores now: the newest lanes let go
            self._drop_lane(lanes[-1])
        held = {lane["c"] for lane in lanes}
        while len(lanes) < want:
            c = self.chunks.choose(ready, now, exclude=held)
            if c is None:
                break
            lane = {"c": c, "at": chunk_range(c, self.base)[0]}
            lanes.append(lane)
            held.add(c)
            self._say_lease(lane)
        self._keep_lanes()
        if not lanes:
            return
        start, done = time.monotonic(), 0
        while not (done and time.monotonic() - start >= budget):
            todo = [lane for lane in lanes if lane["at"] < chunk_range(lane["c"], self.base)[1]]
            if not todo:
                break
            heights = [lane["at"] for lane in todo]
            stored = {h for h in heights if self.store.q("SELECT 1 FROM block WHERE height=?", (h,))}
            for h, (block_hash, line, n) in self._scan_many(rpc, [h for h in heights if h not in stored]).items():
                self.store.x("INSERT OR REPLACE INTO block (height, hash, line, n) VALUES (?,?,?,?)",
                             (h, block_hash, line, n))
            for lane in todo:
                lo = chunk_range(lane["c"], self.base)[0]
                lane["at"] += 1
                done += 1
                if (lane["at"] - lo) % PROGRESS_EVERY == 0:
                    self._say_lease(lane)
            self._keep_lanes()
        if done:
            self.help_rate = done / max(time.monotonic() - start, 1e-6)
        for lane in list(lanes):
            c = lane["c"]
            if lane["at"] >= chunk_range(c, self.base)[1]:
                got = self.store.chunk_line(c)
                if got is not None:
                    self.store.say(c, "", got[0], got[1])
                    self.helped += 1
                    self.store.set_meta("helped", self.helped)
                self._drop_lane(lane)
        self._keep_lanes()

    def _scan_many(self, rpc, heights: list[int]) -> dict[int, tuple[str, str, int]]:
        """(hash, line, births) for each height: one at a time on one lane; on several, fetched side by side (threads,
        each its own connection) and read in worker processes (Python reads blocks on one core per process)."""
        if not heights:
            return {}
        if len(heights) == 1 or self._lanes_wanted() == 1:
            return {h: scan_block(rpc, h, self.taproot, self.mweb) for h in heights}
        from concurrent.futures import ThreadPoolExecutor

        def fetch(h: int) -> tuple[str, str]:
            with self.rpc_factory() as own:
                block_hash = own.call("getblockhash", h)
                return block_hash, own.call("getblock", block_hash, 0)
        with ThreadPoolExecutor(max_workers=len(heights)) as fetchers:
            raws = list(fetchers.map(fetch, heights))
        pool = self._workers()
        jobs = [(raw, block_hash, self.taproot, self.mweb) for block_hash, raw in raws]
        return dict(zip(heights, pool.map(_parse, jobs)))

    def _workers(self):
        want = self._lanes_wanted()
        if self._pool is None or self._pool_size != want:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = ProcessPoolExecutor(max_workers=want, mp_context=multiprocessing.get_context("spawn"))
            self._pool_size = want
        return self._pool

    def _keep_lanes(self) -> None:
        self.store.set_meta("lanes", self.chunks.lanes)
        self.store.set_meta("lease", None)

    def _drop_lane(self, lane: dict) -> None:
        if lane in self.chunks.lanes:
            self.chunks.lanes.remove(lane)
        if self.share is not None:
            self.share.post({"op": "ld", "c": lane["c"]})

    def _let_go(self) -> None:
        """A helper's chunk let go at once (now walking: the walk reads it anyway), so nobody waits for its lease."""
        for lane in list(self.chunks.lanes):
            self._drop_lane(lane)
        self._keep_lanes()

    def _say_lease(self, lease: dict) -> None:
        if self.share is not None:
            self.share.post({"op": "ls", "c": lease["c"], "at": lease["at"], "until": time.time() + LEASE_SECONDS})

    def _tell_new_chunks(self) -> None:
        """This arcade's hashes it has not said yet, to every linked arcade."""
        if self.share is None:
            return
        rows = self.store.q("SELECT rowid, chunk, hash, births FROM seen WHERE node='' AND rowid > ? "
                            "ORDER BY rowid LIMIT 500", (self._said_upto,))
        if not rows:
            return
        self.share.post({"op": "dn", "rows": [[c, h, n] for _r, c, h, n in rows]})
        self._said_upto = int(rows[-1][0])
        self.store.set_meta("said_upto", self._said_upto)

    def all_mine(self) -> list[list]:
        return [[c, h, n] for c, h, n in self.store.q("SELECT chunk, hash, births FROM seen WHERE node=''")]

    # --- what the mesh heard ---------------------------------------------------------------------------------------

    def heard(self, node: str, header: dict) -> None:
        op = header.get("op")
        if op == "hi":
            self.chunks.peers[node] = {k: header.get(k) for k in ("mode", "walk", "helped", "v", "rules")} | \
                {"at": time.time()}
            return
        if (self.chunks.peers.get(node) or {}).get("rules") != ord_rules.RULES:
            return                         # an arcade on other rules (or not heard from yet): its chunks are not ours
        if op == "ls":
            self.chunks.lease_from(node, int(header["c"]), int(header.get("at") or 0),
                                   min(float(header.get("until") or 0), time.time() + LEASE_SECONDS))
        elif op == "ld":
            self.chunks.drop_from(node, int(header["c"]))
        elif op == "dn":
            for row in (header.get("rows") or [])[:1000]:
                try:
                    c, h, n = int(row[0]), str(row[1]), int(row[2])
                except (TypeError, ValueError, IndexError):
                    continue
                if 0 <= c and len(h) == 64:
                    self.store.say(c, node, h, n)
                    self.chunks.drop_from(node, c)

    def hello(self) -> dict:
        return {"op": "hi", "net": self.net, "base": self.base, "mode": self.mode(),
                "walk": self.walker.next_height() if self.mode() == "walk" else None, "helped": self.helped, "v": 2,
                "rules": ord_rules.RULES}

    # --- for people ------------------------------------------------------------------------------------------------

    def status(self) -> dict:
        now = time.time()
        ready = ready_chunks(self.tip, self.base) if self.tip else 0
        per = self.store.all_words(max(ready, 1))
        agreed = differ = one = 0
        differs = []
        for c, words in per.items():
            tally: dict[str, int] = {}
            for h in words.values():
                tally[h] = tally.get(h, 0) + 1
            if max(tally.values()) >= AGREE:
                agreed += 1
            elif len(words) == 1:
                one += 1
            if len(tally) > 1:
                differ += 1
                if len(differs) < 10:
                    differs.append({"chunk": c, "blocks": list(chunk_range(c, self.base)),
                                    "says": {(n[:12] or "this arcade"): h[:16] for n, h in words.items()}})
        leases = []
        for c, held in self.chunks.leases.items():
            for node, (until, at) in held.items():
                if until > now:
                    lo, hi = chunk_range(c, self.base)
                    leases.append({"node": node[:12], "chunk": c, "blocks": [lo, hi], "at": at,
                                   "done": max(0, min(CHUNK, at - lo))})
        mine = self.chunks.mine
        walk = None
        if self.mode() == "walk":
            nxt = self.walker.next_height()
            counts = self.store.q("SELECT COUNT(*), COUNT(burned) FROM insc")[0]
            walk = {"next": nxt, "tip": self.tip, "left": max(0, self.tip - SETTLE - nxt + 1),
                    "inscriptions": counts[0], "burned": counts[1],
                    "tokens": self.store.q("SELECT COUNT(*) FROM tok")[0][0],
                    "holders": self.store.q("SELECT COUNT(DISTINCT owner) FROM bal WHERE overall != '0'")[0][0],
                    "names": {rule: self.store.q(f"SELECT COUNT(DISTINCT {rule}) FROM names")[0][0]
                              for rule in ord_rules.NAME_RULES},
                    "rate": round(self.walker.rate, 2), "error": self.walker.error}
        found = self.store.q("SELECT COALESCE(SUM(n), 0) FROM block")[0][0]
        return {"net": self.net, "found": found, "mode": self.mode(), "base": self.base, "source": SOURCES.get(self.net, ""), "chunk": CHUNK, "tip": self.tip, "ready": ready,
                "agreed": agreed, "one": one, "none": max(0, ready - len(per)), "differ": differ, "differs": differs,
                "lease": ({"chunk": mine["c"], "blocks": list(chunk_range(mine["c"], self.base)), "at": mine["at"],
                           "done": mine["at"] - chunk_range(mine["c"], self.base)[0]} if mine else None),
                "lanes": [{"chunk": lane["c"], "at": lane["at"], "done": lane["at"] - chunk_range(lane["c"], self.base)[0]}
                          for lane in self.chunks.lanes],
                "cores": self._lanes_wanted(),
                "walkers": [{"node": n[:12], "walked_to": h} for n, h in self.walkers()],
                "helped": self.helped, "help_rate": round(self.help_rate, 2), "leases": sorted(leases, key=lambda l: l["chunk"]),
                "peers": {n[:12]: p for n, p in self.chunks.peers.items()}, "walk": walk, "error": self.error}

    # --- lookups ---------------------------------------------------------------------------------------------------

    def answer(self, question: dict) -> dict:
        """A linked arcade's question, answered from this arcade's own walk: {"what": "where", "id"} or
        {"what": "on", "txid", "vout"}. Only a walking arcade answers: a helper knows births, not where things are."""
        if self.mode() != "walk":
            raise ValueError("this arcade does not walk this chain")
        what = question.get("what")
        if what == "where":
            return {"where": self.where(str(question.get("id") or ""))}
        if what == "token":
            return {"token": self.token(str(question.get("tick") or ""))}
        if what == "kinds":
            return {"kinds": self.kinds(str(question.get("ctype") or ""), str(question.get("proto") or ""))}
        if what == "balances":
            return {"balances": self.balances(str(question.get("owner") or ""))}
        if what == "name":
            return {"name": self.name(str(question.get("name") or ""))}
        if what == "on":
            return {"on": self.on(str(question.get("txid") or ""), int(question.get("vout") or 0))}
        raise ValueError("no such question")

    def lookup(self, question: dict) -> dict:
        """The answer, and whose it is: this arcade's own walk, else (read only, or not walking) a linked arcade that
        walks this chain, asked over the mesh."""
        if self.mode() == "walk":
            return {**self.answer(question), "from": "this arcade", "walked_to": self.walker.next_height()}
        if self.share is None or not hasattr(self.share, "ask"):
            raise ValueError("no linked arcade to ask: the mesh is off here")
        return self.share.ask(question)

    def walkers(self) -> list[tuple[str, int]]:
        """Linked arcades that walk this chain, furthest first: (node, the block they have walked to)."""
        rows = [(node, int(p.get("walk") or 0)) for node, p in self.chunks.peers.items()
                if p.get("mode") == "walk" and time.time() - float(p.get("at") or 0) < 300]
        return sorted(rows, key=lambda r: -r[1])

    def where(self, inscription: str) -> dict | None:
        row = self.store.q("SELECT i.id, i.number, i.height, i.ctype, i.proto, i.size, i.burned, a.txid, a.vout, a.off "
                           "FROM insc i LEFT JOIN at a ON a.rid = i.rid WHERE i.id=?", (inscription,))
        if not row:
            return None
        iid, number, height, ctype, proto, size, burned, txid, vout, off = row[0]
        return {"id": iid, "number": number, "height": height, "content_type": ctype, "protocol": proto, "size": size,
                "burned": burned, "output": f"{txid}:{vout}" if txid else None, "offset": off}

    def kinds(self, ctype: str = "", proto: str = "", first: int = 20) -> dict:
        """How many inscriptions of a content type (a prefix: "image/" for every image) or a JSON "p", and the first
        ones by number; with neither, the commonest types and protocols. For indexing a protocol later."""
        if ctype or proto:
            col, val = ("ctype", ctype + "%") if ctype else ("proto", proto)
            op = "LIKE" if ctype else "="
            n = self.store.q(f"SELECT COUNT(*) FROM insc WHERE {col} {op} ?", (val,))[0][0]
            ids = self.store.q(f"SELECT number, id FROM insc WHERE {col} {op} ? ORDER BY number LIMIT ?", (val, first))
            return {"count": n, "first": [{"number": a, "id": b} for a, b in ids]}
        return {"types": self.store.q("SELECT ctype, COUNT(*) c FROM insc GROUP BY ctype ORDER BY c DESC LIMIT 25"),
                "protocols": self.store.q("SELECT proto, COUNT(*) c FROM insc WHERE proto IS NOT NULL GROUP BY proto "
                                          "ORDER BY c DESC LIMIT 25")}

    def token(self, tick: str, top: int = 20) -> dict | None:
        """A DRC-20 / PRC-20 token: its deploy, how much is minted, and its biggest holders (units shown with its
        decimals)."""
        row = self.store.q("SELECT shown, id, number, supply, lim, dec, minted, deployer, height FROM tok WHERE tick=?",
                           (tick.lower(),))
        if not row:
            return None
        shown, iid, number, supply, lim, dec, minted, deployer, height = row[0]
        unit = 10 ** dec
        holders = sorted(self.store.q("SELECT owner, overall, transferable FROM bal WHERE tick=? AND overall != '0'",
                                      (tick.lower(),)), key=lambda b: -int(b[1]))
        fmt = (lambda v: str(int(v) // unit) + (("." + str(int(v) % unit).rjust(dec, "0").rstrip("0"))
                                                if dec and int(v) % unit else ""))
        return {"tick": shown, "deploy": iid, "number": number, "height": height, "deployer": deployer,
                "supply": fmt(supply), "limit": fmt(lim), "decimals": dec, "minted": fmt(minted),
                "holders": len(holders),
                "top": [{"owner": o, "balance": fmt(b), "transferable": fmt(t)} for o, b, t in holders[:top]]}

    def balances(self, owner: str) -> list[dict]:
        out = []
        for tick, overall, transferable in self.store.q("SELECT tick, overall, transferable FROM bal WHERE owner=? "
                                                        "AND overall != '0'", (owner,)):
            info = self.token(tick, top=0) or {}
            dec = int(info.get("decimals") or 0)
            out.append({"tick": info.get("tick", tick), "units": overall, "transferable_units": transferable,
                        "decimals": dec})
        return out

    def name(self, name: str) -> dict:
        """A name under each reading (ord_rules.NAME_RULES): who claimed it first there, and every later claim."""
        want = name.strip().lower()
        out: dict = {"name": want, "readings": {}}
        for rule in ord_rules.NAME_RULES:
            claims = self.store.q(f"SELECT n.number, n.kind, n.raw, i.id, a.txid, a.vout FROM names n "
                                  f"JOIN insc i ON i.rid = n.rid LEFT JOIN at a ON a.rid = n.rid "
                                  f"WHERE n.{rule}=? ORDER BY n.number", (want,))
            out["readings"][rule] = {
                "winner": ({"inscription": claims[0][3], "number": claims[0][0], "kind": claims[0][1],
                            "output": f"{claims[0][4]}:{claims[0][5]}" if claims[0][4] else None} if claims else None),
                "later_claims": len(claims) - 1 if claims else 0}
        return out

    def name_disagreements(self, limit: int = 50) -> dict:
        """Names whose first claim is a different inscription under two readings, or that one reading has and another
        does not: what choosing a reading would change."""
        firsts: dict[str, dict[str, int]] = {}
        for rule in ord_rules.NAME_RULES:
            for nm, number in self.store.q(f"SELECT {rule}, MIN(number) FROM names WHERE {rule} IS NOT NULL "
                                           f"GROUP BY {rule}"):
                firsts.setdefault(nm, {})[rule] = number
        rules = list(ord_rules.NAME_RULES)
        differ = [{"name": nm, **{r: got.get(r) for r in rules}} for nm, got in firsts.items()
                  if len({got.get(r) for r in rules if got.get(r) is not None}) > 1]
        return {"names": len(firsts), "per_reading": {r: sum(1 for g in firsts.values() if r in g) for r in rules},
                "different_first_claim": len(differ), "examples": differ[:limit]}

    def on(self, txid: str, vout: int) -> list[dict]:
        return [{"id": r[0], "offset": r[1]} for r in self.store.q(
            "SELECT i.id, a.off FROM at a JOIN insc i ON i.rid = a.rid WHERE a.txid=? AND a.vout=? ORDER BY a.off",
            (txid, vout))]


class Runner:
    """The daemon thread: each chain's DoginalIndex.step in turn, resting as long as the passes took (half the machine
    at most). `indexes`: one index, or a callable giving {net: index} (chains switched on while it runs)."""

    def __init__(self, indexes: Any):
        self.indexes = indexes if callable(indexes) and not hasattr(indexes, "step") else (lambda: {"": indexes})
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="arcade-doginal-index", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        self._stop.wait(20)                              # let the node start first
        while not self._stop.is_set():
            rests = []
            try:
                for index in list(self.indexes().values()):
                    if index.mode() in ("walk", "help"):
                        rests.append(index.step())
            except Exception:                            # noqa: BLE001 -- never dies
                log.debug("ord index step", exc_info=True)
                rests.append(30.0)
            self._stop.wait(min(rests) if rests else 10.0)
