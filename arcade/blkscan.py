"""Reading a node's block files straight from disk, for the shared walk (2026-10-25: "Fast full walks").

A walk over RPC is held to what one node serves (~7 MB/s measured on 2026-10-25: ~8 hours for all of Dogecoin); the
same blocks read from the node's own `blk*.dat` files go at the speed of the disk. The files hold blocks as the node
received them -- out of order, orphans included -- each as `magic(4) size(4) block(size)`, and the tail of the newest
file is zeros the node has set aside. So:

1. `BlockFiles.refresh()` reads every block's 80-byte header once (seeking past the rest), keeping
   hash -> (prev, file, offset, size) in a small sqlite file beside the arcade's other data; later refreshes read only
   what was appended.
2. `BlockFiles.chain(tip_hash)` follows `prev` back from the node's best block, so each height maps to the block on
   the main chain (orphans are never on that path), kept as a height table and extended from the last tip.
3. `scan(...)` reads a range of heights and returns the outputs paying any of a set of addresses: the same output
   types the coin index keeps (utxos.SPENDABLE: pay-to-address, pay-to-script-hash, segwit pay-to-key), so a walk and
   the index never disagree about what a coin is.

Nothing here is trusted further than it has to be: what a walk finds is checked against a node (gettxout: unspent,
paying that address) before it counts, and its blocks are read again by a second arcade (coinlist.segments).
"""
from __future__ import annotations

import glob
import hashlib
import os
import sqlite3
from typing import Any, Iterable

from . import rawchain
from .script import b58check_decode, segwit_decode

#: What a reader keeps per walk at most before it hands its matches back.
MATCHES_MAX = 2_000_000


def _sha256d(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


# --- addresses -> what an output script carries -----------------------------------------------------------------------
def targets(addresses: Iterable[str], params) -> dict[tuple[str, bytes], str]:
    """{(kind, 20-byte hash): address} for the addresses this chain can pay (kind "pkh", "sh" or "wpkh")."""
    out: dict[tuple[str, bytes], str] = {}
    hrp = getattr(params, "bech32_hrp", "") or ""
    for a in addresses:
        a = str(a or "").strip()
        if not a:
            continue
        if hrp and a.lower().startswith(hrp + "1"):
            ver, prog = segwit_decode(hrp, a.lower())
            if ver == 0 and prog is not None and len(prog) == 20:
                out[("wpkh", bytes(prog))] = a
            continue
        try:
            version, payload = b58check_decode(a)
        except Exception:                                    # noqa: BLE001 -- not an address of this chain
            continue
        if len(payload) != 20:
            continue
        if version == params.pubkeyhash_version:
            out[("pkh", payload)] = a
        elif version == params.scripthash_version:
            out[("sh", payload)] = a
    return out


def _kind(script: bytes) -> tuple[str, bytes] | None:
    n = len(script)
    if n == 25 and script[0] == 0x76 and script[1] == 0xA9 and script[2] == 20 and script[23] == 0x88 \
            and script[24] == 0xAC:
        return "pkh", script[3:23]
    if n == 23 and script[0] == 0xA9 and script[1] == 20 and script[22] == 0x87:
        return "sh", script[2:22]
    if n == 22 and script[0] == 0x00 and script[1] == 20:
        return "wpkh", script[2:22]
    return None


# --- one block's bytes -> the outputs paying the targets --------------------------------------------------------------
def _varint(b: bytes, i: int) -> tuple[int, int]:
    n = b[i]
    if n < 0xFD:
        return n, i + 1
    w = {0xFD: 2, 0xFE: 4, 0xFF: 8}[n]
    return int.from_bytes(b[i + 1:i + 1 + w], "little"), i + 1 + w


def block_matches(raw: bytes, want: dict[tuple[str, bytes], str], height: int, auxpow: bool = True
                  ) -> list[list]:
    """[[txid, vout, sats, height, address]] for the outputs in a raw block that pay a target. Reads every
    transaction's layout (the speed is here: no decoding of what is not an output script); a transaction's id is
    worked out only when one of its outputs matched."""
    r = rawchain._Reader(raw)
    rawchain._read_header(r, auxpow=auxpow)
    count, i = _varint(raw, r.at)
    out: list[list] = []
    for _ in range(count):
        start = i
        i += 4                                               # version
        segwit = raw[i] == 0 and raw[i + 1] != 0
        if segwit:
            i += 2
        nin, i = _varint(raw, i)
        for _ in range(nin):
            i += 36
            ln, i = _varint(raw, i)
            i += ln + 4
        nout, i = _varint(raw, i)
        hits = []
        for n in range(nout):
            sats = int.from_bytes(raw[i:i + 8], "little")
            ln, i = _varint(raw, i + 8)
            k = _kind(raw[i:i + ln]) if ln in (22, 23, 25) else None
            if k is not None and k in want and sats > 0:
                hits.append((n, sats, want[k]))
            i += ln
        if segwit:
            for _ in range(nin):
                items, i = _varint(raw, i)
                for _ in range(items):
                    ln, i = _varint(raw, i)
                    i += ln
        i += 4                                               # locktime
        if hits:
            if segwit:
                txid = rawchain.read_tx(raw[start:i]).txid
            else:
                txid = _sha256d(raw[start:i])[::-1].hex()
            out.extend([txid, n, sats, height, a] for n, sats, a in hits)
    return out


# --- the files ------------------------------------------------------------------------------------------------------
class BlockFiles:
    """A node's block files, as a height -> (file, offset, size) table for its main chain."""

    def __init__(self, blocks_dir: str, cache_path: str, auxpow: bool = True):
        self.dir, self.auxpow = blocks_dir, auxpow
        self.db = sqlite3.connect(cache_path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS block (hash BLOB PRIMARY KEY, prev BLOB, file INTEGER, offset INTEGER,
                                              size INTEGER) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS file (n INTEGER PRIMARY KEY, read_to INTEGER);
            CREATE TABLE IF NOT EXISTS height (h INTEGER PRIMARY KEY, hash BLOB, file INTEGER, offset INTEGER,
                                               size INTEGER);
        """)

    def files(self) -> list[tuple[int, str]]:
        out = []
        for p in glob.glob(os.path.join(self.dir, "blk*.dat")):
            name = os.path.basename(p)[3:-4]
            if name.isdigit():
                out.append((int(name), p))
        return sorted(out)

    def readable(self) -> bool:
        fs = self.files()
        return bool(fs) and os.access(fs[0][1], os.R_OK)

    def refresh(self) -> int:
        """Read the headers of every block not seen yet; how many were added."""
        added = 0
        done = dict(self.db.execute("SELECT n, read_to FROM file"))
        for n, path in self.files():
            at = int(done.get(n, 0))
            size = os.path.getsize(path)
            if at >= size:
                continue
            rows = []
            with open(path, "rb") as f:
                f.seek(at)
                while at + 8 <= size:
                    head = f.read(8)
                    if len(head) < 8 or head[:4] == b"\0\0\0\0":
                        break                                # the zeros the node set aside
                    length = int.from_bytes(head[4:], "little")
                    if at + 8 + length > size:
                        break                                # still being written
                    header = f.read(80)
                    rows.append((_sha256d(header)[::-1], header[4:36][::-1], n, at + 8, length))
                    at += 8 + length
                    f.seek(at)
            with self.db:
                self.db.executemany("INSERT OR IGNORE INTO block VALUES (?,?,?,?,?)", rows)
                self.db.execute("INSERT OR REPLACE INTO file VALUES (?,?)", (n, at))
            added += len(rows)
        return added

    def chain(self, tip_hash: str, tip_height: int) -> int:
        """Map heights to the main chain ending at `tip_hash` (height `tip_height`), from the last mapped tip; how
        many heights were (re)written. A reorg is followed back to where the old mapping agrees."""
        h, at = bytes.fromhex(tip_hash), int(tip_height)
        rows = []
        while at >= 0:
            have = self.db.execute("SELECT hash FROM height WHERE h = ?", (at,)).fetchone()
            if have is not None and have[0] == h:
                break
            row = self.db.execute("SELECT prev, file, offset, size FROM block WHERE hash = ?", (h,)).fetchone()
            if row is None:
                raise LookupError(f"block {h.hex()} at {at} is not in the block files (refresh first)")
            rows.append((at, h, row[1], row[2], row[3]))
            h, at = row[0], at - 1
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO height VALUES (?,?,?,?,?)", rows)
            self.db.execute("DELETE FROM height WHERE h > ?", (int(tip_height),))
        return len(rows)

    def mapped(self) -> tuple[int, int] | None:
        row = self.db.execute("SELECT MIN(h), MAX(h) FROM height").fetchone()
        return None if row is None or row[0] is None else (int(row[0]), int(row[1]))

    def raw(self, height: int) -> bytes:
        row = self.db.execute("SELECT file, offset, size, hash FROM height WHERE h = ?", (int(height),)).fetchone()
        if row is None:
            raise LookupError(f"height {height} is not mapped")
        with open(os.path.join(self.dir, "blk%05d.dat" % row[0]), "rb") as f:
            f.seek(row[1])
            data = f.read(row[2])
        if _sha256d(data[:80])[::-1] != row[3]:
            raise rawchain.RawError(f"the block file at height {height} does not hold the block mapped there")
        return data

    def scan(self, top: int, bottom: int, addresses: Iterable[str], params,
             progress: dict[str, Any] | None = None) -> list[list]:
        """The outputs paying `addresses` in heights `top` down to `bottom` (walks DOWN, as the shared walk does)."""
        want = targets(addresses, params)
        out: list[list] = []
        if not want:
            return out
        rows = self.db.execute("SELECT h, file, offset, size, hash FROM height WHERE h BETWEEN ? AND ? "
                               "ORDER BY h DESC", (int(bottom), int(top))).fetchall()
        if len(rows) != int(top) - int(bottom) + 1:
            raise LookupError(f"heights {bottom}..{top} are not all mapped")
        handles: dict[int, Any] = {}
        try:
            for h, fn, off, size, bh in rows:
                f = handles.get(fn)
                if f is None:
                    if len(handles) > 8:
                        for x in handles.values():
                            x.close()
                        handles.clear()
                    f = handles[fn] = open(os.path.join(self.dir, "blk%05d.dat" % fn), "rb")
                f.seek(off)
                raw = f.read(size)
                if _sha256d(raw[:80])[::-1] != bh:
                    raise rawchain.RawError(f"the block file at height {h} does not hold the block mapped there")
                out.extend(block_matches(raw, want, h, auxpow=self.auxpow))
                if progress is not None:
                    progress["height"] = h
                    progress["bytes"] = progress.get("bytes", 0) + size
                if len(out) > MATCHES_MAX:
                    raise OverflowError("too many matches for one walk")
        finally:
            for x in handles.values():
                x.close()
        return out


def _scan_part(args) -> list[list]:
    blocks_dir, cache_path, auxpow, top, bottom, addresses, params = args
    return BlockFiles(blocks_dir, cache_path, auxpow).scan(top, bottom, addresses, params)


def scan_parallel(files: BlockFiles, cache_path: str, top: int, bottom: int, addresses: Iterable[str], params,
                  workers: int | None = None) -> list[list]:
    """`BlockFiles.scan` over this machine's cores: the range cut into parts, one process each (the matching is
    plain Python, so threads would share one core)."""
    import concurrent.futures as cf
    top, bottom = int(top), int(bottom)
    workers = max(1, min(int(workers or (os.cpu_count() or 2) // 2), 16, top - bottom + 1))
    if workers == 1:
        return files.scan(top, bottom, addresses, params)
    addresses = list(addresses)
    step = (top - bottom + 1) / workers
    parts = [(top - round(i * step), top - round((i + 1) * step) + 1) for i in range(workers)]
    # Spawned, never forked (2026-11-04, app.dogecoinarcade.com down 16:21-16:32): a forked child inherits every file
    # the worker has open -- web.lock among them -- and two left waiting when the worker was reloaded mid-walk held the
    # run lock, so the next worker never started. doginal_index's pool and blockminer's are spawned already.
    import multiprocessing
    with cf.ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn")) as ex:
        got = ex.map(_scan_part, [(files.dir, cache_path, files.auxpow, t, b, addresses, params) for t, b in parts])
        return [m for part in got for m in part]
