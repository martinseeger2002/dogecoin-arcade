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
import zlib
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from . import mesh_web, ord_rules, ord_values, rawchain

log = logging.getLogger("arcade.doginal_index")

#: Where each chain's ord index starts: the official first-inscription height of that chain's ord indexer (the operator
#: 2026-10-20: "The block height on each chain varies so find the official block height for each chains ord index"),
#: read from each indexer's src/chain.rs `first_inscription_height`. Every arcade cuts the same chunks from these,
#: so they never change without a new protocol word.
STARTS = {"doge": 4_600_000, "main": 186_920, "test": 34_237, "ltc": 2_424_429, "ltctest": 2_669_127,
          "bel": 22_470, "lky": 88_554, "wjk": 1}
SOURCES = {
    "doge": "ord-dogecoin (apezord) and wonky-ord: 4,600,000",
    "main": "ord-pepecoin (mvdnbrk) and pepecoin-ord-indexer: 186,920",
    "test": "pepecoin-ord-indexer (reallyshadydev): 34,237",
    "ltc": "ord-litecoin (ynohtna92): 2,424,429",
    "ltctest": "ord-litecoin (ynohtna92): 2,669,127",
    "bel": "Nintondo/ord (Bellscoin): 22,470",
    "lky": "the first Doginals-style envelope on Luckycoin (no official indexer height found; scanned): 88,554",
    "wjk": "ord-wojakcoin chain.rs first_inscription_height: 1 (Doginals envelopes; SegWit is not active)",
}
#: Chains whose ords are mostly Taproot (ord's witness envelope) as well as the Doginals kind.
TAPROOT = frozenset({"ltc", "ltctest", "bel"})     # Bellscoin: Nintondo/ord reads Taproot envelopes too
BASE = STARTS["doge"]
CHUNK = 100
#: Blocks this far under the tip are scanned in chunks; the newest ones may still be replaced by a reorg. A chunk is
#: scanned by many arcades and its word compared, so it covers only blocks a reorg will not move.
SETTLE = 12
#: How close to the tip the walk goes (2026-10-22: "Why can't strawberry keep up with the confirmed blocks?" --
#: it sat 12 blocks under). The last blocks past the chunks are read from the Core; a reorg is walked back (rollback).
WALK_SETTLE = 2
#: How many different arcades must say the same hash before a chunk is `agreed` (fewer than 5 arcades: every chunk
#: is scanned twice).
AGREE = 2
#: The audit raises a chunk's confidence to this many arcades agreeing, and no further (2026-10-20: "two nodes
#: in agreement ... legitimate; if three or four or more nodes are in agreement then the confidence score can go up").
AUDIT_MAX = 5
#: A lease lasts this long unless renewed by progress; a helper that goes quiet loses its chunk to the next one.
LEASE_SECONDS = 600
PROGRESS_EVERY = 10              # blocks between two progress words
#: Undo kept for this many blocks back, so a reorg rolls the walker back instead of stopping it.
UNDO_BLOCKS = 300
#: How the index's tables are laid out on disk. 2 (2026-10-20, the operator: "slim down the index ... without losing
#: functionality"): ids and txids as bytes, content types by number, a body still arriving kept as its envelope's
#: state (not its pieces), undo packed. An index of layout 1 is converted in place by the index thread (Store.upgrade).
#: Nothing peers compare depends on it -- chunk hashes and moves are the same either way.
LAYOUT = 3
# 3 (2026-10-21, the operator: "whole copies, slimmer"): the id and coin lookups keyed by the txid's first 8 bytes (the
# whole txid is still compared); no content-type, protocol or number lookups (the Admin type: / p: searches scan,
# and rows are kept in number order: rid = number + 1).
#: How many chunks past the walk an arcade holding the index asks its peers for at once, each from another peer
#: (2026-10-20: served "torrent style").
PREFETCH = 8
#: Spend notes kept here when no arcade holds the index (and so nobody stitches them): beyond this the furthest are
#: let go -- they can be scanned again. Notes everybody holding the index has stitched past are let go at once.
NOTES_MAX_BYTES = 512 * 1024 ** 2
#: Lookups at once on this arcade's own Core while the walk gathers a block's earlier transactions (_fetch_many).
LANES_OWN_FETCH = 4
#: The lanes bring in the coin values of the blocks walked before the values file existed (ord_values) only while the
#: walk is at least this far behind the tip: one at the tip asks so little it is not worth reading the chain again.
VALUES_BEHIND = 10_000
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


def _parse(job: tuple) -> tuple[str, str, int, bytes]:
    """One block's (hash, line, births, notes) from its raw hex, in a worker process (a lane of a helper on many
    cores)."""
    from . import ord_notes
    raw_hex, block_hash, taproot, mweb, dialect, every = job
    block = rawchain.read_block(bytes.fromhex(raw_hex), block_hash, mweb=mweb)
    found = births(block, taproot, dialect, every)
    return block.hash, block_hash_of(block.hash, found), len(found), ord_notes.one_block(block, taproot, dialect, every)


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


def birth_of(tx: rawchain.Tx, dialect: str = "dogecoin") -> dict | None:
    """{"vin": 0, "ctype", "done"} when `tx` STARTS a Doginals-kind inscription, read alone as the chain's official
    indexer reads it (ord_rules.genesis: input 0 only, push-only, "ord", a piece count -- above 0 on Dogecoin, any on
    Pepecoin). "done": complete in this transaction; else it continues in the one whose input 0 spends it."""
    if not tx.vin or tx.vin[0]["vout"] == -1:
        return None
    g = ord_rules.genesis(tx.vin[0]["script"], dialect)
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
    return [ctype for ctype, _delegate in taproot_read(witness)]


def taproot_read(witness: list[bytes]) -> list[tuple[str, str | None]]:
    """(content type hex, delegate id or None) of each of ord's witness envelopes in one input. The delegate as
    ord-litecoin reads it (src/inscriptions/envelope.rs `Tag::Delegate.take(&mut fields)`: the first value of tag 11,
    tag.rs `Delegate = 11`; inscription.rs `inscription_id_field`, as wonky-ord's)."""
    return [(e["ctype"].hex(), ord_rules.id_from_tag(e["delegate"], "dogecoin")) for e in _taproot_scan(witness)]


#: The most body one witness envelope is read for (doginals.content): a tapscript fits in one block's witness, so
#: Litecoin's and Bellscoin's 4,000,000 weight units (witness bytes weigh 1) bound it; past this it is not served.
TAPROOT_BODY_MAX = 4_000_000


def taproot_bodies(witness: list[bytes]) -> list[dict]:
    """Each of ord's witness envelopes in one input, in order, as taproot_read finds them, with its body:
    {"ctype": bytes, "delegate": raw tag bytes or None, "body": bytes, or None when the envelope has no body tag,
    "big": over TAPROOT_BODY_MAX (its body then None too)}. The body is every push after the OP_0 that ends the
    fields, joined, to OP_ENDIF (ord's `Inscription.body`); other tags (pointer 2, parent 3, metadata 5, ...) are
    passed over."""
    return _taproot_scan(witness, bodies=True)


def _taproot_scan(witness: list[bytes], bodies: bool = False) -> list[dict]:
    """ord's witness envelopes in the tapscript of a script-path spend: the second-to-last witness item, or the
    third-to-last when the last is an annex (0x50 first). OP_FALSE OP_IF "ord", (tag, value) pairs, OP_0, the body's
    pushes, OP_ENDIF. The body is gathered only when `bodies` (the block scan needs only the fields)."""
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
            ctype, delegate, body, big, j = b"", None, None, False, i + 3
            while j < len(ops) and ops[j][0] != 0x68:
                tag = ops[j][1]
                if tag == b"":                                    # the body: pushes to OP_ENDIF
                    j += 1
                    parts, size = [], 0
                    while j < len(ops) and ops[j][0] != 0x68:
                        piece = ops[j][1]
                        if bodies and not big and isinstance(piece, bytes):
                            size += len(piece)
                            big = size > TAPROOT_BODY_MAX
                            parts.append(piece)
                        j += 1
                    body = None if big or not bodies else b"".join(parts)
                    break
                if tag == b"\x01" and j + 1 < len(ops) and isinstance(ops[j + 1][1], bytes):
                    ctype = ops[j + 1][1]
                if tag == b"\x0b" and delegate is None and j + 1 < len(ops) and isinstance(ops[j + 1][1], bytes):
                    delegate = ops[j + 1][1]
                j += 2
            found.append({"ctype": ctype, "delegate": delegate, "body": body, "big": big})
            i = j + 1
        else:
            i += 1
    return found


def births_of(tx: rawchain.Tx, taproot: bool = False, dialect: str = "dogecoin",
              every_input: bool = False) -> list[dict]:
    """Every inscription `tx` gives birth to: {"n", "vin", "ctype", "done", "kind"}; the id is txid + "i" + n. The
    Doginals kind ("p2sh": an input script opening with "ord", read in the chain's dialect) in input 0 -- and, where the
    chain's indexer reads every input (`every_input`, ord_rules.EVERY_INPUT: Bellscoin), in any later input whose
    envelope is complete there -- and, on a Taproot chain, ord's witness envelopes ("taproot", each with "at", its
    place among its input's envelopes, and its "delegate", which no chunk hash covers); numbered in input order."""
    out = []
    for j, i in enumerate(tx.vin):
        if j == 0:
            b = birth_of(tx, dialect)
            if b is not None:
                out.append({"n": 0, **b, "kind": "p2sh"})
        elif every_input and i["vout"] != -1 and i["script"][:4] == b"\x03ord":
            g = ord_rules.genesis(i["script"], dialect)
            if g is not None and g[0] == "complete":
                out.append({"n": len(out), "vin": j, "ctype": g[1].hex(), "done": True, "kind": "p2sh"})
        if taproot:
            for at, (ctype, delegate) in enumerate(taproot_read(i.get("witness") or [])):
                out.append({"n": len(out), "vin": j, "ctype": ctype, "done": True, "delegate": delegate,
                            "kind": "taproot", "at": at})
    return out


def births(block: rawchain.Block, taproot: bool = False, dialect: str = "dogecoin",
           every_input: bool = False) -> list[tuple[str, int, int, str]]:
    """(txid, n, envelope input, content type hex) for every birth in the block, in block order."""
    return [(tx.txid, b["n"], b["vin"], b["ctype"]) for tx in block.txs
            for b in births_of(tx, taproot, dialect, every_input)]


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


#: The index's tables (layout LAYOUT). meta is made first, on its own; the lookups (INDEXES) after.
TABLES = """
            -- every block a walker or a helper read: its hash, its line in the chunk hash, births seen in it
            CREATE TABLE IF NOT EXISTS block (height INTEGER PRIMARY KEY, hash TEXT NOT NULL, line TEXT NOT NULL,
                                              n INTEGER NOT NULL DEFAULT 0, moves TEXT);
            -- the inscriptions (walker), numbered as the official indexers number them: when complete
            -- ctype: the content type as text; proto: a JSON body's "p" (drc-20, dns, ...); size: the body's bytes --
            -- so a protocol indexed later finds its own inscriptions by type or "p" (2026-10-20)
            -- its id is tx (the reveal's txid, 32 bytes as written in hex) and n ("<txid hex>i<n>"); ct: its content
            -- type's number in ctypes; delegate (every chain) and parent (Pepecoin): the inscription its tags name,
            -- as id_blob (the txid's 32 bytes as written, then n in 4 bytes little-endian), NULL for none (rules 4);
            -- reinscribed: 1 when it was made on a koinu that already carried an inscription (2026-10-22), NULL when
            -- not, or when it was filed before the mark existed
            CREATE TABLE IF NOT EXISTS insc (rid INTEGER PRIMARY KEY, tx BLOB NOT NULL, n INTEGER NOT NULL,
                                             number INTEGER NOT NULL, height INTEGER NOT NULL, txpos INTEGER NOT NULL,
                                             ct INTEGER NOT NULL, proto TEXT, size INTEGER,
                                             pieces INTEGER NOT NULL DEFAULT 1, burned INTEGER,
                                             delegate BLOB, parent BLOB, reinscribed INTEGER);
            -- every content type seen, by number (a few hundred, for millions of inscriptions)
            CREATE TABLE IF NOT EXISTS ctypes (ct INTEGER PRIMARY KEY, ctype TEXT NOT NULL UNIQUE);
            -- where each one is now (walker): the output and the koinu inside it
            CREATE TABLE IF NOT EXISTS at (rid INTEGER PRIMARY KEY, tx BLOB NOT NULL, vout INTEGER NOT NULL,
                                           off INTEGER NOT NULL, height INTEGER NOT NULL);
            -- a body still arriving, keyed by the txid of its latest piece: its envelope's state (content type, pieces
            -- still to come), its size so far and pieces so far, and the body itself only while a rule could still
            -- read it (ord_rules.body_matters; NULL once let go of -- the content is read from the chain when wanted)
            CREATE TABLE IF NOT EXISTS pending (txid TEXT PRIMARY KEY, ctype BLOB NOT NULL, left INTEGER NOT NULL,
                                                body BLOB, size INTEGER NOT NULL, pieces INTEGER NOT NULL,
                                                height INTEGER NOT NULL, origin TEXT NOT NULL);
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
            -- each block's undo: every row's old value, packed (_pack)
            CREATE TABLE IF NOT EXISTS undo (height INTEGER PRIMARY KEY, doc BLOB NOT NULL);
            -- every arcade's word about every chunk, this one's included
            CREATE TABLE IF NOT EXISTS seen (chunk INTEGER NOT NULL, node TEXT NOT NULL, hash TEXT NOT NULL,
                                             births INTEGER NOT NULL, at REAL NOT NULL, PRIMARY KEY (chunk, node));
            -- spend notes (ord_notes): each block's as it is read, until its chunk is whole; then the chunk's, packed,
            -- whose they are ("" this arcade's own scan), until every arcade holding the index has stitched past them
            CREATE TABLE IF NOT EXISTS pnote (height INTEGER PRIMARY KEY, raw BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS notes (chunk INTEGER PRIMARY KEY, blob BLOB NOT NULL, nd TEXT NOT NULL,
                                              src TEXT NOT NULL, at REAL NOT NULL);
            -- chunks the walk stitched from another arcade's notes, and what that arcade said of them: should the
            -- audit outvote it, the index is built again
            CREATE TABLE IF NOT EXISTS stitched (chunk INTEGER PRIMARY KEY, node TEXT NOT NULL, hash TEXT NOT NULL,
                                                 nd TEXT NOT NULL);
        """
#: The lookups. An inscription's id and a coin are found by the first 8 bytes of their txid, then the whole txid
#: compared (a query must say substr(tx, 1, 8) = ? to use them: see _tx8).
INDEXES = """
            CREATE INDEX IF NOT EXISTS insc_id8 ON insc (substr(tx, 1, 8), n);
            CREATE INDEX IF NOT EXISTS at_out8 ON at (substr(tx, 1, 8), vout);
            CREATE INDEX IF NOT EXISTS names_spec ON names (spec, number);
            CREATE INDEX IF NOT EXISTS names_json ON names (json, number);
            CREATE INDEX IF NOT EXISTS names_doge2023 ON names (doge2023, number);
            CREATE INDEX IF NOT EXISTS bal_rich ON bal (tick, length(overall), overall);
            CREATE INDEX IF NOT EXISTS insc_mesh ON insc (proto) WHERE proto = 'mesh';
"""
#: insc_mesh (2026-10-26, the .mesh web, arcade/mesh_web.py): the "p":"mesh" pages, newest first, without reading
#: the millions of other inscriptions; a partial index holds only those rows.
#: bal_rich (2026-10-21, Admin -> Ords -> Tokens: "a list rich list of the token holders paginated to 20 holders"): a
#: token's holders biggest first, read straight off the index, a page at a time. A balance is a whole number of units
#: as text with no leading zeros, so its length and then its text sort it as a number (RICH_ORDER). Made in place, as
#: every lookup here is, when an index of this layout opens (IF NOT EXISTS): about 37 bytes a balance -- measured on a
#: copy of a test machine's Dogecoin balances, 532,353 of them: 19.9 MB beside a 9.3 GB index (0.2%), built in 0.24 s; a page
#: 2,000 holders down the biggest token (45,677 holders) reads in under a millisecond.
RICH_ORDER = "ORDER BY length(overall) DESC, overall DESC"
#: Inscriptions this index numbers by the chain and its indexer's published rules that other explorers do not show,
#: found comparing numbers by hand (2026-10-21: "Let's keep it. it follows the rules and it renders as an
#: image ... Can we flag it ... in a lightweight way?"). Shown beside the inscription in Admin -> Ords; numbering is
#: never touched by it. Outside indexes are not named (2026-10-20): the note says what is so, not who differs.
SEEN_ELSEWHERE: dict[str, dict[str, str]] = {
    "main": {
        "83d1cbea653d4c8e8f729dc1af8fea4802b27abdafde436524ff8882768d0a31i0":
            "Other explorers do not show this one. It is a whole inscription by ord-pepecoin's own rules (an image, "
            "block 292,159), so it is numbered here, and every number after it is one higher than theirs.",
    },
}
#: Holders on one page of the rich list, and ticks offered at once by the ticker search.
RICH_PAGE = 20
TICKS_AT_ONCE = 12
SCHEMA = TABLES + INDEXES
#: Layout 2's lookups, dropped by the conversion to 3.
OLD_INDEXES = ("insc_id", "insc_number", "insc_ct", "insc_proto", "at_out")



def _pack(doc: Any) -> bytes:
    """An undo record: JSON (bytes as {"$b": hex}), compressed."""
    return zlib.compress(json.dumps(doc, separators=(",", ":"),
                                    default=lambda v: {"$b": bytes(v).hex()}).encode(), 6)


def _unpack(blob: bytes) -> Any:
    return json.loads(zlib.decompress(blob), object_hook=lambda d: bytes.fromhex(d["$b"]) if d.keys() == {"$b"} else d)


def iid_of(tx: bytes, n: int) -> str:
    return f"{bytes(tx).hex()}i{n}"


def iid_parts(iid: str) -> tuple[bytes, int] | None:
    """(tx, n) of "<txid hex>i<n>", or None when it is not one."""
    txid, _, n = iid.strip().lower().partition("i")
    try:
        tx = bytes.fromhex(txid)
        return (tx, int(n)) if len(tx) == 32 and n.isdigit() else None
    except ValueError:
        return None


def id_blob(iid: str | None) -> bytes | None:
    """An inscription id as the insc table's delegate / parent keep it (2026-10-21): the txid's 32 bytes as written
    (the same bytes as insc.tx, so the row it names is found by its tx lookup) and n as 4 bytes little-endian -- 36
    bytes, a third of the id's text; NULL (None) for none. Rare enough (only inscriptions that name one) that nothing
    else is worth it."""
    key = iid_parts(iid) if iid else None
    return key[0] + key[1].to_bytes(4, "little") if key else None


def iid_of_blob(blob: bytes | None) -> str | None:
    return iid_of(bytes(blob)[:32], int.from_bytes(bytes(blob)[32:36], "little")) if blob else None


def _shown(units: Any, dec: int) -> str:
    """Whole units as the token shows them: with its decimals, trailing zeros dropped."""
    v, unit = int(units), 10 ** int(dec)
    return str(v // unit) + (("." + str(v % unit).rjust(dec, "0").rstrip("0")) if dec and v % unit else "")


def _pct(part: Any, whole: Any) -> float | None:
    """`part` as a percentage of `whole` (both whole units, as text past 2^63), to four places."""
    try:
        whole = int(whole)
        return round(int(part) * 100 / whole, 4) if whole > 0 else None
    except (TypeError, ValueError):
        return None


#: Questions the explorer asks (2026-10-21) that a walking arcade from before does not know.
NEWER_QUESTIONS = ("number", "ticks", "holders", "note", "ticklist", "mesh", "mesh_directory", "typed")


def _first_page_of(token: dict | None) -> dict | None:
    """An older walker's answer to "token" as the first page of the rich list: its biggest holders, ranked, without
    their share of the supply (its balances come already shown with decimals)."""
    if not token:
        return None
    top = token.get("top") or []
    return {**{k: v for k, v in token.items() if k != "top"}, "minted_pct": None, "page": 0, "per": RICH_PAGE,
            "pages": max(1, -(-int(token.get("holders") or 0) // RICH_PAGE)),
            "rows": [{"rank": k + 1, "owner": h.get("owner"), "balance": h.get("balance"),
                      "transferable": h.get("transferable"), "pct": None} for k, h in enumerate(top[:RICH_PAGE])]}


# --------------------------------------------------------------------------- the store

class Store:
    def __init__(self, path: Path, base: int | None = None, dialect: str = "dogecoin", net: str = ""):
        self.path = Path(path)
        self.base = BASE if base is None else base
        self.dialect = dialect                         # how the chain's envelopes read (ord_rules.dialect_of)
        self.net = net                                 # its chain, for its rules version (ord_rules.rules_for)
        self.rules = ord_rules.rules_for(net)
        self._lock = threading.RLock()                 # the one writer connection
        self._readers = threading.local()              # a read connection per thread (q)
        self._gen = 0                                  # bumped when the file is swapped (a snapshot installed)
        self._open()

    def _open(self) -> None:
        self._db = sqlite3.connect(self.path, timeout=60, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        # The write log is cut back to this after each checkpoint (2026-10-20: PEP Main's had grown to 2.8 GB).
        self._db.execute("PRAGMA journal_size_limit=67108864")
        self._db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        # Kept under other rules (or none, before 2026-10-20's): cut again from the start.
        # All or nothing (2026-10-21, rules 3 on .25: PEP's index was left with 5 of its tables and the new rules,
        # taken for layout 2, so the rest were never made): the drop, the meta and the new marks in one transaction.
        if self.meta("rules") != self.rules:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for (table,) in self._db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'meta'"
                                                 ).fetchall():
                    self._db.execute(f"DROP TABLE IF EXISTS {table}")
                self._db.execute("DELETE FROM meta")
                self._db.execute("INSERT OR REPLACE INTO meta VALUES ('layout', ?)", (json.dumps(LAYOUT),))
                self._db.execute("INSERT OR REPLACE INTO meta VALUES ('rules', ?)", (json.dumps(self.rules),))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        # An index of layout 1 (before 2026-10-20's slimming) stays as it is until the index thread converts it;
        # until then nothing reads its walk tables (old_layout). One with no inscription in it has nothing to convert:
        # it is of this layout, and whatever of its tables is missing is made below.
        cols = [r[1] for r in self._db.execute("PRAGMA table_info(insc)").fetchall()]
        empty = not cols or not self._db.execute("SELECT 1 FROM insc LIMIT 1").fetchone()
        self.layout = LAYOUT if empty else 1 if "id" in cols else int(self.meta("layout") or 2)
        self.old_layout = self.layout != LAYOUT
        self.upgrading = ""
        if not self.old_layout:
            self._db.executescript(SCHEMA)
            if self.meta("layout") is None:
                self.set_meta("layout", LAYOUT)
            self._names_without_maps(self._db)
        self._columns_added(self._db)

    @staticmethod
    def _names_without_maps(db) -> None:
        """Block-map claims out of the names (ord_rules.is_block_map, 2026-10-26): an index walked before the rule kept
        "<block>.pepemap" and its kin as names under the spec reading -- 1.64 million of them on Pepecoin. Once per
        index (and again for a snapshot fetched from an arcade that had not done it yet), while the index opens, before
        the walk writes; readers go on meanwhile (WAL)."""
        if db.execute("SELECT 1 FROM meta WHERE k='names_maps'").fetchone():
            return
        if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='names'").fetchone():
            maps = "(spec LIKE '%map' OR spec LIKE '%maps' OR spec LIKE '%mep')"
            db.execute(f"DELETE FROM names WHERE {maps} AND json IS NULL AND doge2023 IS NULL")
            db.execute(f"UPDATE names SET spec=NULL WHERE {maps}")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('names_maps', '1')")

    @staticmethod
    def _columns_added(db) -> None:
        # A chunk's word carries its notes' digest too (2026-10-20); an index from before gains the column.
        if "nd" not in [r[1] for r in db.execute("PRAGMA main.table_info(seen)").fetchall()]:
            db.execute("ALTER TABLE main.seen ADD COLUMN nd TEXT")
        # Each inscription's delegate and parent (rules 4, 2026-10-21). An index of rules 4 is always made with them;
        # an index of another layout, kept until it is converted, gains them (NULL: none), so its rows read alike.
        cols = [r[1] for r in db.execute("PRAGMA main.table_info(insc)").fetchall()]
        for col in ("delegate", "parent"):
            if cols and col not in cols:
                db.execute(f"ALTER TABLE main.insc ADD COLUMN {col} BLOB")
        # A reinscription's mark (2026-10-22, SadFrog: "highlight them as reinscriptions ... so we can later put a toggle
        # in the admin panel"): an index from before gains the column, NULL on the rows it already had.
        if cols and "reinscribed" not in cols:
            db.execute("ALTER TABLE main.insc ADD COLUMN reinscribed INTEGER")

    def q(self, sql: str, args: tuple = ()) -> list:
        """A read, on this thread's own connection: never waits for a write in progress (WAL lets readers through) nor
        for another reader (2026-10-20: page threads queued here behind the mesh's chunk words, and the site's pages
        waited with them)."""
        db = getattr(self._readers, "db", None)
        if db is None or getattr(self._readers, "gen", -1) != self._gen:
            if db is not None:
                db.close()
            db = self._readers.db = sqlite3.connect(self.path, timeout=60, isolation_level=None)
            self._readers.gen = self._gen
        return db.execute(sql, args).fetchall()

    def close(self) -> None:
        with self._lock:
            self._db.close()
        db = getattr(self._readers, "db", None)
        if db is not None:
            db.close()
            self._readers.db = None

    def forget_seen(self) -> None:
        """The walk's coin filter kept beside this file (Walker.save_seen) no longer matches it: let go of, so the next
        start makes it from this file's own rows."""
        for tail in (".seen", ".seen.tmp"):
            try:
                Path(str(self.path) + tail).unlink()
            except FileNotFoundError:
                pass

    def swap(self, other: Path) -> None:
        """Put another index file in this one's place (a snapshot fetched from peers), and read from it from now on:
        the writer reopens at once, every thread's reader on its next read."""
        import os
        self.forget_seen()
        with self._lock:
            self._db.close()
            for tail in ("-wal", "-shm"):
                try:
                    os.remove(str(self.path) + tail)
                except FileNotFoundError:
                    pass
            os.replace(other, self.path)
            self._gen += 1
            self._open()

    #: Tables copied as they are when an index is converted to the new layout (the rest are converted).
    SAME = ("block", "tok", "bal", "xfer", "names")
    #: ... and the ones the mesh may write meanwhile, copied last, under the lock, just before the swap.
    LIVE = ("meta", "seen", "notes", "pnote", "stitched")

    def upgrade(self) -> None:
        """Convert an index of layout 1 to LAYOUT, in place of it (2026-10-20: "slim down the index ... without
        losing functionality"). A new file is built beside it from it -- inscriptions and where they are with ids as
        bytes and content types by number; bodies still arriving as their envelope's state; every undo packed and
        converted the same way -- then swapped in as a fetched snapshot is. The walk waits meanwhile (the index thread
        does this, never a page); nothing else changes, and nothing is walked again."""
        self.forget_seen()
        import os
        new = self.path.with_name(self.path.name + ".upgrade")
        for tail in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(str(new) + tail)
            except FileNotFoundError:
                pass
        db = sqlite3.connect(new, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("PRAGMA synchronous=OFF")
            db.execute("ATTACH DATABASE ? AS old", (str(self.path),))
            db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
            db.executescript(TABLES)                   # the lookups are made once the rows are in: much faster
            self._columns_added(db)
            source = self.layout

            def copy(table: str) -> None:
                cols = ",".join(d[1] for d in db.execute(f"PRAGMA old.table_info({table})").fetchall())
                db.execute(f"INSERT INTO main.{table} ({cols}) SELECT {cols} FROM old.{table}")
            db.execute("BEGIN")
            if source >= 2:                            # layout 2: the same rows, only the lookups change
                for table in ("ctypes", "insc", "at", "pending", "undo") + self.SAME:
                    self.upgrading = table
                    copy(table)
                self._upgrade_finish(db, new)
                return
            self.upgrading = "content types"
            db.execute("INSERT INTO ctypes (ctype) SELECT DISTINCT ctype FROM old.insc ORDER BY ctype")
            cts = dict(db.execute("SELECT ctype, ct FROM ctypes").fetchall())
            self.upgrading = "inscriptions"
            db.execute("INSERT INTO insc (rid, tx, n, number, height, txpos, ct, proto, size, pieces, burned) "
                       "SELECT i.rid, unhex(substr(i.id, 1, 64)), CAST(substr(i.id, 66) AS INTEGER), "
                       "i.number, i.height, i.txpos, c.ct, i.proto, i.size, i.pieces, i.burned "
                       "FROM old.insc i JOIN ctypes c ON c.ctype = i.ctype")
            self.upgrading = "where each inscription is"
            db.execute("INSERT INTO at SELECT rid, unhex(txid), vout, off, height FROM old.at")
            for table in self.SAME:
                self.upgrading = table
                copy(table)

            def ct_of(ctype: str) -> int:
                if ctype not in cts:
                    db.execute("INSERT INTO ctypes (ctype) VALUES (?)", (ctype,))
                    cts[ctype] = db.execute("SELECT ct FROM ctypes WHERE ctype=?", (ctype,)).fetchone()[0]
                return cts[ctype]

            def pending_of(old: dict) -> dict:
                row = None
                for script in json.loads(old["scripts"]):
                    got = carry(row, bytes.fromhex(script), self.dialect)
                    row = {**got, "ctype": got["ctype"]}
                return {"txid": old["txid"], "ctype": row["ctype"], "left": row["left"], "body": row["body"],
                        "size": row["size"], "pieces": row["pieces"], "height": old["height"], "origin": old["origin"]}

            def row_of(table: str, old: dict | None) -> dict | None:
                if old is None:
                    return None
                if table == "insc":
                    tx, n = iid_parts(old["id"])
                    return {"rid": old["rid"], "tx": tx, "n": n, "number": old["number"], "height": old["height"],
                            "txpos": old["txpos"], "ct": ct_of(old["ctype"]), "proto": old["proto"],
                            "size": old["size"], "pieces": old["pieces"], "burned": old["burned"]}
                if table == "at":
                    return {"rid": old["rid"], "tx": bytes.fromhex(old["txid"]), "vout": old["vout"],
                            "off": old["off"], "height": old["height"]}
                if table == "pending":
                    return pending_of(old)
                return old

            self.upgrading = "bodies still arriving"
            cols = [d[1] for d in db.execute("PRAGMA old.table_info(pending)").fetchall()]
            for vals in db.execute("SELECT * FROM old.pending").fetchall():
                r = pending_of(dict(zip(cols, vals)))
                db.execute(f"INSERT INTO pending ({','.join(r)}) VALUES ({','.join('?' * len(r))})", tuple(r.values()))
            heights = [h for (h,) in db.execute("SELECT height FROM old.undo ORDER BY height").fetchall()]
            for k, h in enumerate(heights):
                self.upgrading = f"undo records ({k} of {len(heights)})"
                doc = json.loads(db.execute("SELECT doc FROM old.undo WHERE height=?", (h,)).fetchone()[0])
                doc["rows"] = [[t, key, row_of(t, old)] for t, key, old in doc["rows"]]
                db.execute("INSERT INTO undo VALUES (?,?)", (h, _pack(doc)))
            self._upgrade_finish(db, new)
        except BaseException:
            try:
                db.close()
            except Exception:                            # noqa: BLE001
                pass
            raise
        finally:
            self.upgrading = ""

    def _upgrade_finish(self, db, new: Path) -> None:
        """The rows are in (an open transaction on `db`, the new file): the lookups, then what the mesh may have written
        meanwhile, copied under the lock, and the new file swapped in."""
        import os
        db.execute("COMMIT")
        self.upgrading = "the lookups"
        db.executescript(INDEXES)
        self.upgrading = "the last of it"
        with self._lock:
            db.execute("BEGIN")
            for table in self.LIVE:
                db.execute(f"DELETE FROM main.{table}")
                cols = ",".join(d[1] for d in db.execute(f"PRAGMA old.table_info({table})").fetchall())
                db.execute(f"INSERT INTO main.{table} ({cols}) SELECT {cols} FROM old.{table}")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('layout', ?)", (json.dumps(LAYOUT),))
            db.execute("COMMIT")
            db.execute("DETACH DATABASE old")
            db.close()
            fd = os.open(new, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            was = os.path.getsize(self.path)
            self.swap(new)
            self.old_layout = False
        log.info("ord index %s: converted to layout %d, %.1f GB -> %.1f GB", self.path.name, LAYOUT, was / 1e9,
                 os.path.getsize(self.path) / 1e9)

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

    def say(self, c: int, node: str, hash_: str, n: int, nd: str | None = None) -> bool:
        """File an arcade's hash for a chunk (node "" is this arcade), and its notes' digest when it has one. True when
        it is new."""
        was = self.q("SELECT hash, nd FROM seen WHERE chunk=? AND node=?", (c, node))
        if was and was[0][0] == hash_ and (nd is None or was[0][1] == nd):
            return False
        self.x("INSERT OR REPLACE INTO seen (chunk, node, hash, births, at, nd) VALUES (?,?,?,?,?,?)",
               (c, node, hash_, n, time.time(), nd if nd is not None else (was[0][1] if was else None)))
        return True

    # --- spend notes -----------------------------------------------------------------------------------------------

    def note_block(self, h: int, raw: bytes) -> None:
        self.x("INSERT OR REPLACE INTO pnote VALUES (?,?)", (h, raw))

    def finish_notes(self, c: int, rpc=None) -> tuple[str, int] | None:
        """Chunk c's notes from its blocks' (when all of them are here): kept as this arcade's own, the block notes
        let go. (digest, packed size), or None. With `rpc`, an envelope begun before the chunk is followed back to its
        first piece (ord_notes.finish); should that fail, its pieces are kept whole."""
        from . import ord_notes
        lo, hi = chunk_range(c, self.base)
        rows = self.q("SELECT raw FROM pnote WHERE height>=? AND height<? ORDER BY height", (lo, hi))
        if len(rows) != CHUNK:
            return None
        lookup = (lambda txid: bytes.fromhex(rpc.call("getrawtransaction", txid, 0))) if rpc is not None else None
        try:
            notes = ord_notes.finish([r[0] for r in rows], lookup, self.dialect, taproot=self.net in TAPROOT)
        except Exception as exc:                         # noqa: BLE001 -- a Core that cannot say: kept whole
            log.warning("ord index: chunk %d's envelopes could not be followed back (%s): kept whole", c, exc)
            notes = ord_notes.finish([r[0] for r in rows], dialect=self.dialect, taproot=self.net in TAPROOT)
        nd, blob = ord_notes.digest(notes), ord_notes.pack(notes)
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO notes VALUES (?,?,?,?,?)", (c, blob, nd, "", time.time()))
            db.execute("DELETE FROM pnote WHERE height>=? AND height<?", (lo, hi))
        return nd, len(blob)

    #: What a reset clears: everything the walk built. The words (`seen`) stay -- what each arcade said is still so.
    BUILT = ("insc", "at", "pending", "tok", "bal", "xfer", "names", "undo", "block", "notes", "pnote", "stitched")

    def reset(self) -> None:
        """Start the index again from the chain's first block (its words kept)."""
        self.forget_seen()
        with self.tx() as db:
            for table in self.BUILT:
                db.execute(f"DELETE FROM {table}")
            db.execute("DELETE FROM seen WHERE node=''")
            db.execute("DELETE FROM meta WHERE k IN ('walk_next', 'next_number', 'lanes', 'lease', 'said_upto')")

    def notes_of(self, c: int) -> tuple[bytes, str, str] | None:
        """(packed notes, digest, whose) for chunk c, if they are kept here."""
        row = self.q("SELECT blob, nd, src FROM notes WHERE chunk=?", (c,))
        return (bytes(row[0][0]), row[0][1], row[0][2]) if row else None

    def words(self, c: int) -> dict[str, str]:
        return {r[0]: r[1] for r in self.q("SELECT node, hash FROM seen WHERE chunk=?", (c,))}

    def all_notes_words(self, below: int) -> dict[int, dict[str, tuple[str, str | None, int]]]:
        """Every arcade's word on every chunk under `below`: chunk -> node -> (hash, notes digest, births)."""
        out: dict[int, dict[str, tuple[str, str | None, int]]] = {}
        for c, node, h, nd, n in self.q("SELECT chunk, node, hash, nd, births FROM seen WHERE chunk < ?", (below,)):
            out.setdefault(c, {})[node] = (h, nd, n)
        return out

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


def carry(chain: dict | None, script: bytes, dialect: str = "dogecoin") -> dict:
    """A body arriving in pieces, one piece on: the pending row it continues (None: a first piece) and the next
    transaction's input 0 script, to {state, ctype, left, body (None once let go of), size, pieces, delegate,
    parents} -- delegate (an id or None) and parents (ids claimed, Pepecoin only) once complete, from the trailer of
    the script it completes in (ord_rules.read says why nothing later)."""
    if chain is None:
        env, size, kept = ord_rules.envelope_start(script, dialect), None, True
    else:
        had = bytes(chain["body"]) if chain["body"] is not None else b""
        env = ord_rules.envelope_more(("partial", bytes(chain["ctype"]), chain["left"], had), script, dialect)
        size, kept = chain["size"] + len(env[3]) - len(had), chain["body"] is not None
    state, ctype, left, body, tags = env
    size = len(body) if size is None else size
    if state == "none":
        return {"state": "none", "ctype": b"", "left": 0, "body": None, "size": 0, "pieces": 0, "delegate": None,
                "parents": []}
    pieces = (chain["pieces"] if chain else 0) + 1
    if not kept or (state == "partial" and not ord_rules.body_matters(ctype, body, size)):
        body = None
    return {"state": state, "ctype": ctype, "left": left, "body": body, "size": size, "pieces": pieces,
            "delegate": ord_rules.delegate_of(tags, dialect), "parents": ord_rules.parents_of(tags, dialect)}


#: The key of each table a block's walk writes, for its undo (every row's old value is kept before it changes).
KEYS = {"insc": ("rid",), "at": ("rid",), "pending": ("txid",), "tok": ("tick",), "bal": ("owner", "tick"),
        "xfer": ("rid",), "names": ("rid",)}


class _Seen:
    """Which coins may have an inscription on them (2026-10-21: the walker held every inscription's place in
    Python memory -- about 17 GB on a test machine for 22 million -- and the out-of-memory killer took the arcade).

    A Bloom filter of coins (outpoints), about 1.5 bytes each: False means no inscription sits on that coin, for
    certain; True means maybe, and the index file says (`at`, by its at_out8 lookup). Nothing is ever taken out (a
    spent coin stays "maybe", and is looked up and found empty); built again from `at` once it has taken in more
    than it was made for."""

    K = 7

    def __init__(self, expect: int):
        self.capacity = max(1 << 16, int(expect))
        self.m = 1 << max(20, (self.capacity * 12 - 1).bit_length())        # >= 12 bits a coin: ~0.3% false
        self.bits = bytearray(self.m >> 3)
        self.added = 0

    def _spots(self, key: bytes):
        h = hashlib.blake2b(key, digest_size=16).digest()
        a, b = int.from_bytes(h[:8], "little"), int.from_bytes(h[8:], "little") | 1
        mask = self.m - 1
        return [(a + i * b) & mask for i in range(self.K)]

    def add(self, key: bytes) -> None:
        bits = self.bits
        for i in self._spots(key):
            bits[i >> 3] |= 1 << (i & 7)
        self.added += 1

    def __contains__(self, key: bytes) -> bool:
        bits = self.bits
        return all(bits[i >> 3] & (1 << (i & 7)) for i in self._spots(key))

    def full(self) -> bool:
        return self.added > self.capacity


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
                 token_protocol: str = "drc-20", dialect: str | None = None):
        self.store = store
        self.base, self.taproot, self.mweb = store.base, taproot, mweb
        self.every_input = ord_rules.every_input(getattr(store, "net", ""))   # Bellscoin: every input's envelope
        #: "pepecoin" | "dogecoin" (ord_rules.dialect_of): how envelopes read, and when a new one is looked for.
        self.dialect = dialect or store.dialect
        self.params = params
        self.token_protocol = token_protocol
        self.tracked: _Seen | None = None                                  # coins an inscription may sit on
        self.values: OrderedDict[bytes, int] = OrderedDict()               # recent outputs' koinu
        #: Every coin's value seen, on disk (ord_values); None: memory only (asked of a Core again once let go of).
        self.disk: ord_values.Values | None = None
        self.rate = 0.0
        # How a block's earlier transactions are fetched, many at once: fetch_many(txids) -> {txid: raw bytes}; the
        # index gives one that spreads them over every lane's Core (DoginalIndex._fetch_many). None: batched on `rpc`.
        self.fetch_many: Callable[[list[str]], dict] | None = None
        self.error = ""

    def next_height(self) -> int:
        return int(self.store.meta("walk_next", self.base))

    def _ct(self, db, ctype: str) -> int:
        """A content type's number in ctypes, adding it the first time it is seen (on the walk's own connection)."""
        kept = getattr(self, "_cts", None)
        if kept is None or kept[0] != self.store._gen:
            kept = self._cts = (self.store._gen, {})
        ct = kept[1].get(ctype)
        if ct is None:
            db.execute("INSERT OR IGNORE INTO ctypes (ctype) VALUES (?)", (ctype,))
            ct = kept[1][ctype] = db.execute("SELECT ct FROM ctypes WHERE ctype=?", (ctype,)).fetchone()[0]
        return ct

    #: The filter kept on disk beside the index (2026-10-25: rebuilt from `at` at every start -- 172 million rows on
    #: Dogecoin, read a row at a time in Python, about an hour of one core -- and the walk stood still meanwhile; every
    #: restart of an update began it again). Read back in a moment; only the rows placed since it was kept are added.
    SEEN_MAGIC = b"ARCSEEN1"

    def _seen_path(self) -> Path:
        return Path(str(self.store.path) + ".seen")

    def _read_seen(self, n: int) -> tuple[_Seen, int] | None:
        """(the filter as kept, the height to add rows from), or None when there is none usable."""
        import struct
        try:
            with open(self._seen_path(), "rb") as f:
                head = f.read(8 + 32)
                if len(head) != 40 or head[:8] != self.SEEN_MAGIC:
                    return None
                m, capacity, added, since = struct.unpack("<QQQQ", head[8:])
                if capacity < n or m % 8 or m < (1 << 20):
                    return None                          # made for fewer coins than there are now: made again
                seen = _Seen.__new__(_Seen)
                seen.capacity, seen.m, seen.added = capacity, m, added
                seen.bits = bytearray(m >> 3)
                if f.readinto(seen.bits) != m >> 3:
                    return None
            return seen, since
        except OSError:
            return None

    def save_seen(self) -> bool:
        """Keep the filter on disk (written beside, then put in place): everything the walk has placed is in it, so
        the rows placed from the block before the walk's next one on are what a later start adds."""
        import os
        import struct
        seen = self.tracked
        if seen is None:
            return False
        since = max(0, self.next_height() - 2)
        path, tmp = self._seen_path(), Path(str(self._seen_path()) + ".tmp")
        try:
            with open(tmp, "wb") as f:
                f.write(self.SEEN_MAGIC + struct.pack("<QQQQ", seen.m, seen.capacity, seen.added, since))
                f.write(seen.bits)
            os.replace(tmp, path)
            return True
        except OSError:
            log.warning("ord index: the coin filter could not be kept on disk", exc_info=True)
            return False

    def _load(self) -> None:
        """The filter of coins inscriptions sit on, from `at` (made for twice as many, so it rarely needs making
        again): the one kept on disk and the rows placed since, or -- none kept, or too small now -- made from all
        of `at`, then kept."""
        n = int(self.store.q("SELECT COUNT(*) FROM at")[0][0])
        kept = self._read_seen(n)
        if kept is not None and not kept[0].full():
            seen, since = kept
            db = sqlite3.connect(self.store.path, timeout=60)
            try:
                for tx, vout in db.execute("SELECT tx, vout FROM at WHERE height >= ?", (since,)):
                    seen.add(bytes(tx) + vout.to_bytes(4, "little"))
            finally:
                db.close()
            self.tracked = seen
            return
        seen = _Seen(2 * n + 100_000)
        # Read a row at a time on a connection of its own: a list of 22 million rows was itself gigabytes.
        db = sqlite3.connect(self.store.path, timeout=60)
        try:
            for tx, vout in db.execute("SELECT tx, vout FROM at"):
                seen.add(bytes(tx) + vout.to_bytes(4, "little"))
        finally:
            db.close()
        self.tracked = seen
        self.save_seen()

    @staticmethod
    def _on(db, txid: str, vout: int) -> list[tuple[int, int]]:
        """The inscriptions on a coin, from the index file (on the walk's own connection: this block's moves in it)."""
        tx = bytes.fromhex(txid)
        return [(rid, off) for rid, off in db.execute(
            "SELECT rid, off FROM at WHERE substr(tx, 1, 8)=? AND tx=? AND vout=?", (tx[:8], tx, vout))]

    def _value(self, rpc, txid: str, vout: int) -> int:
        key = _outpoint(txid, vout)
        got = self.values.get(key)
        if got is not None:
            return got
        if self.disk is not None:
            got = self.disk.get(ord_values.key(txid, vout))
            if got is not None:
                self._keep(key, got)
                return got
        tx = rawchain.read_tx(bytes.fromhex(rpc.call("getrawtransaction", txid, 0)), txid)
        for n, (v, _s) in enumerate(tx.vout):
            self._keep(_outpoint(txid, n), v)
        if self.disk is not None:
            self.disk.put([(ord_values.key(txid, n), v) for n, (v, _s) in enumerate(tx.vout)])
        return tx.vout[vout][0]

    #: Transactions asked of a Core in one round trip while a block's input values are gathered.
    PREFETCH_BATCH = 200

    def _prefetch(self, rpc, block, tracked) -> int:
        """Every earlier transaction this block will ask the value of (the inputs before a coin an inscription may sit
        on), fetched before the walk goes through it -- in batches, spread over the lanes' Cores -- instead of one round
        trip each (2026-10-25: Strawberry, reading through lenders, crawled through the 2023 blocks a lookup at a time,
        thousands a block). What is not fetched here is still asked one at a time (_value). Returns how many."""
        inblock = {tx.txid for tx in block.txs}
        want: dict[tuple[str, int], None] = {}
        for tx in block.txs[1:]:
            maybe = [j for j, i in enumerate(tx.vin) if i["vout"] != -1 and _outpoint(i["txid"], i["vout"]) in tracked]
            if not maybe:
                continue
            for i in tx.vin[:max(maybe)]:
                if i["txid"] not in inblock and _outpoint(i["txid"], i["vout"]) not in self.values:
                    want[(i["txid"], i["vout"])] = None
        if not want:
            return 0
        if self.disk is not None:                        # walked here before: on disk (ord_values), no Core asked
            kept = self.disk.get_many([ord_values.key(t, n) for t, n in want])
            for t, n in list(want):
                got_v = kept.get(ord_values.key(t, n))
                if got_v is not None:
                    self._keep(_outpoint(t, n), got_v)
                    del want[(t, n)]
        txids = list(dict.fromkeys(t for t, _n in want))
        if not txids:
            return 0
        got = self.fetch_many(txids) if self.fetch_many is not None else fetch_raw_many(rpc, txids, self.PREFETCH_BATCH)
        fetched: list[tuple[bytes, int]] = []
        for txid, raw in got.items():
            try:
                tx = rawchain.read_tx(raw, txid)
            except Exception:                            # noqa: BLE001 -- asked again one at a time
                continue
            for n, (v, _s) in enumerate(tx.vout):
                self._keep(_outpoint(txid, n), v)
                fetched.append((ord_values.key(txid, n), v))
        if self.disk is not None and fetched:
            self.disk.put(fetched)
        return len(got)

    def _keep(self, key: bytes, value: int) -> None:
        self.values[key] = value
        if len(self.values) > 500_000:
            self.values.popitem(last=False)

    def owner_of(self, script: bytes) -> str:
        """Who holds an output: its address, or a script's hash when it has none (drc20 ScriptKey)."""
        from . import ord_notes
        from .script import b58check_encode, hash160
        from .ord_notes import Hashed
        if isinstance(script, Hashed):                   # from spend notes: the script's hash, kept instead of it
            return "script:" + script.hex()
        p = self.params
        if p is not None:
            if len(script) == 25 and script[:3] == b"\x76\xa9\x14" and script[23:] == b"\x88\xac":
                return b58check_encode(p.pubkeyhash_version, script[3:23])
            if len(script) == 23 and script[:2] == b"\xa9\x14" and script[22:] == b"\x87":
                return b58check_encode(p.scripthash_version, script[2:22])
            # SegWit and Taproot (2026-10-25): bel1q / bel1p, ltc1q / ltc1p, as Core and the chain's explorers name
            # them. The same programs the notes carry (ord_notes.witness_program), so a walk from notes names alike.
            program = ord_notes.witness_program(script) if getattr(p, "bech32_hrp", "") else None
            if program is not None:
                from .script import segwit_encode
                return segwit_encode(p.bech32_hrp, *program)
        return "script:" + hash160(script).hex()

    def step(self, rpc, tip: int, budget: float = 5.0, notes: Callable[[int], Any] | None = None) -> int:
        """Walk on for about `budget` seconds, never past tip - WALK_SETTLE. Returns the blocks walked.

        `notes(chunk)` says where each chunk's blocks come from (2026-10-20: scanned in line by whoever is free,
        stitched here in order): ([blocks], this arcade's own scan?) from its spend notes; False to wait for them;
        None (or no `notes`) to read each block from `rpc`."""
        if self.tracked is None or self.tracked.full():
            self._load()
        start, done = time.monotonic(), 0
        while True:
            h = self.next_height()
            if h > tip - WALK_SETTLE or (done and time.monotonic() - start >= budget):
                break
            c = chunk_of(h, self.base)
            got = notes(c) if notes is not None else None
            if got is False:
                break                                    # the notes are coming: wait for them
            if got is not None:
                block, own = got[0][h - chunk_range(c, self.base)[0]], got[1]
            else:
                block_hash = rpc.call("getblockhash", h)
                block = rawchain.read_block(bytes.fromhex(rpc.call("getblock", block_hash, 0)), block_hash,
                                            mweb=self.mweb)
                own = True
            if h > self.base:
                before = self.store.q("SELECT hash FROM block WHERE height=?", (h - 1,))
                if before and before[0][0] != block.prev:
                    self.rollback(h - 1)
                    continue
            self.walk_block(rpc, h, block, own=own)
            done += 1
        took = time.monotonic() - start
        if done:
            self.rate = done / max(took, 1e-6)
        return done

    def walk_block(self, rpc, h: int, block, own: bool = True) -> list[str]:
        """One block, filed with its undo. Returns the moves lines (for tests). `own`: read here (from this arcade's
        Core, or notes of its own scan) -- only then is the chunk's word this arcade's, and its notes written; a block
        stitched from another arcade's notes is that arcade's word, not a second one."""
        tracked = self.tracked
        assert tracked is not None
        moves: list[str] = []
        born_here = births(block, self.taproot, self.dialect, self.every_input)
        next_number = int(self.store.meta("next_number", 0))
        number_before = next_number
        next_rid = (self.store.q("SELECT COALESCE(MAX(rid), 0) FROM insc")[0][0]) + 1
        try:
            self._prefetch(rpc, block, tracked)
        except Exception:                                # noqa: BLE001 -- the values are asked one at a time instead
            log.debug("ord index: prefetch for block %d failed", h, exc_info=True)
        with self.store.tx() as db:
            w = _Block(db)
            for pos, tx in enumerate(block.txs):
                for n, (v, _s) in enumerate(tx.vout):
                    if v <= 100_000_000:
                        self._keep(_outpoint(tx.txid, n), v)
                if pos == 0:
                    continue
                # What this transaction carries in: inscriptions on the coins it spends, at their koinu among the inputs.
                # The filter says "maybe" for every coin an inscription sits on (and, now and then, one it does not):
                # the index file says which.
                on = {j: self._on(w.db, i["txid"], i["vout"]) for j, i in enumerate(tx.vin)
                      if i["vout"] != -1 and _outpoint(i["txid"], i["vout"]) in tracked}
                needs = [j for j, got in on.items() if got]
                sizes_upto = max(needs, default=-1)
                # Births besides input 0's Doginals kind (2026-10-25): ord's witness envelopes on a Taproot chain --
                # in input 0 too, which a filter on the birth's place used to drop (every Litecoin and Bellscoin one
                # sampled sits there) -- and, where the chain's indexer reads every input (Bellscoin), a complete
                # envelope in a later input.
                more = [b for b in births_of(tx, self.taproot, self.dialect, self.every_input)
                        if b["kind"] == "taproot" or b["vin"] > 0] if self.taproot or self.every_input else []
                sizes_upto = max([sizes_upto] + [b["vin"] for b in more])
                sizes = [self._value(rpc, i["txid"], i["vout"]) for i in tx.vin[:sizes_upto]] if sizes_upto > 0 else []
                # (rid, koinu among the inputs, new here, the txid it sat in before this one)
                carried: list[tuple[int, int, bool, str]] = []
                for j in needs:
                    base = sum(sizes[:j])
                    for rid, off in on[j]:
                        carried.append((rid, base + off, False, tx.vin[j]["txid"]))
                new: list[tuple[int, str, bytes, bytes, int]] = []  # (rid, id, ctype, body, pieces)
                returned: list[int] = []            # Pepecoin: parents spent beside their child, sent to output 1
                # Dogecoin: a new inscription only when nothing inscribed sits on the first koinu (wonky-ord
                # inscription_updater.rs `if inscriptions.iter().all(|flotsam| flotsam.offset != 0)`). Pepecoin: in
                # every transaction (ord-pepecoin's: "In Pepecoin, we always check for new inscriptions to support
                # parent/child collection provenance" -- `if true`), rules 4.
                # Dogecoin numbers a reinscription too since rules 5 (2026-10-22: "Number them like nintondo"), and
                # Bellscoin with it (ord_rules.NUMBERS_REINSCRIPTIONS), so only Litecoin's and Luckycoin's Doginals kind
                # still waits for an uninscribed first koinu.
                if (self.dialect == "pepecoin" or self.store.net in ord_rules.NUMBERS_REINSCRIPTIONS
                        or all(c[1] != 0 for c in carried)) and tx.vin \
                        and tx.vin[0]["vout"] != -1:
                    prev = tx.vin[0]["txid"]
                    chain = w.get("pending", (prev,))
                    got = carry(chain, tx.vin[0]["script"], self.dialect)
                    state, ctype = got["state"], got["ctype"]
                    # A transaction that spends a piece but is no next piece (e.g. its input 0 spends the piece's change
                    # output) leaves the chain where it was: the official indexers keep it ("ParsedInscription::None"
                    # removes nothing, ord-pepecoin/ord-dogecoin inscription_updater.rs), and the real next piece, later
                    # in the block or after, still continues it. Dropping it here lost such inscriptions (2026-10-21: 862
                    # of PEP's first 2.07 million, against pepeblocks.com -- 790fb113...i0 in block 293,201 the first).
                    if chain is not None and state == "complete":
                        w.put("pending", (prev,), None)
                    if state == "partial":
                        if chain is not None:
                            w.put("pending", (prev,), None)
                        w.put("pending", (tx.txid,), {"txid": tx.txid, "ctype": ctype, "left": got["left"],
                                                     "body": got["body"], "size": got["size"], "pieces": got["pieces"],
                                                     "height": h, "origin": chain["origin"] if chain else tx.txid})
                    elif state == "complete":
                        origin = chain["origin"] if chain else tx.txid
                        iid, otx = f"{origin}i0", bytes.fromhex(origin)
                        # A body let go of on the way could not matter to any rule: read as empty, it matters to none.
                        body = got["body"] if got["body"] is not None else b""
                        if not w.db.execute("SELECT 1 FROM insc WHERE substr(tx, 1, 8)=? AND tx=? AND n=0",
                                            (otx[:8], otx)).fetchone():
                            parent = self._parent(w, got["parents"], carried) if chain is None else None
                            if parent is not None:
                                returned = parent[1]
                            rid, next_rid = next_rid, next_rid + 1
                            # Made on input 0's first koinu: a reinscription when an inscription already sits there.
                            again = 1 if any(c[1] == 0 and not c[2] for c in carried) else None
                            w.put("insc", (rid,), {"rid": rid, "tx": otx, "n": 0, "number": next_number, "height": h,
                                                    "txpos": pos, "ct": self._ct(w.db, ctype.decode("utf-8", "replace")),
                                                    "proto": ord_rules.protocol_of(body) if got["body"] is not None
                                                    else None, "size": got["size"],
                                                    "pieces": got["pieces"], "burned": None,
                                                    "delegate": id_blob(got["delegate"]),
                                                    "parent": id_blob(parent[0]) if parent else None,
                                                    "reinscribed": again})
                            next_number += 1
                            new.append((rid, iid, ctype, body, got["pieces"]))
                            carried.append((rid, 0, True, ""))
                for b in more:                       # witness envelopes, and a later input's (Bellscoin)
                    btx = bytes.fromhex(tx.txid)
                    if w.db.execute("SELECT 1 FROM insc WHERE substr(tx, 1, 8)=? AND tx=? AND n=?",
                                    (btx[:8], btx, b["n"])).fetchone():
                        continue
                    rid, next_rid = next_rid, next_rid + 1
                    at_koinu = sum(sizes[:b["vin"]])
                    again = 1 if any(c[1] == at_koinu and not c[2] for c in carried) else None
                    ctype = bytes.fromhex(b["ctype"])
                    # Its body, so its size and "p" are kept and a token operation in it is read (bel-20 mints in
                    # witness envelopes, 2026-10-25); None past TAPROOT_BODY_MAX, or a body let go of in the notes.
                    if b["kind"] == "taproot":
                        found = taproot_bodies(tx.vin[b["vin"]].get("witness") or [])
                        body = found[b["at"]]["body"] if b["at"] < len(found) else None
                    else:
                        body = ord_rules.parse([tx.vin[b["vin"]]["script"]], self.dialect)[2]
                    w.put("insc", (rid,), {"rid": rid, "tx": btx, "n": b["n"], "number": next_number, "height": h,
                                            "txpos": pos, "ct": self._ct(w.db, ctype.decode("utf-8", "replace")),
                                            "proto": ord_rules.protocol_of(body) if body is not None else None,
                                            "size": len(body) if body is not None else None, "pieces": 1,
                                            "burned": None, "delegate": id_blob(b.get("delegate")), "parent": None,
                                            "reinscribed": again})
                    next_number += 1
                    new.append((rid, f"{tx.txid}i{b['n']}", ctype, body if body is not None else b"", 1))
                    carried.append((rid, at_koinu, True, ""))
                if not carried:
                    continue
                # Where each carried koinu lands: the outputs in order; past the last one, it went to the fee.
                ends, total = [], 0
                for v, _s in tx.vout:
                    total += v
                    ends.append(total)
                landed: dict[int, tuple[int | None, str | None]] = {}     # rid -> (vout or None, owner or None)
                # A parent spent beside its new child goes to output 1, at its first koinu, whatever its place among
                # the inputs (ord-pepecoin inscription_updater.rs: "In our P2SH model, the commit must be input[0] ...
                # so parents are input[1+] ... where it gets consumed by fees. Fix: explicitly assign the parent to its
                # return output" -- `if let Some(return_output) = tx.output.get(1)`), before the others land.
                if returned and len(tx.vout) > 1:
                    for rid in returned:
                        iid = iid_of(*w.db.execute("SELECT tx, n FROM insc WHERE rid=?", (rid,)).fetchone())
                        w.put("at", (rid,), {"rid": rid, "tx": bytes.fromhex(tx.txid), "vout": 1, "off": 0,
                                             "height": h})
                        tracked.add(_outpoint(tx.txid, 1))
                        moves.append(f"M {iid} {tx.txid}:1:0\n")
                        landed[rid] = (1, self.owner_of(tx.vout[1][1]))
                else:
                    returned = []
                for rid, p, is_new, _was in sorted((c for c in carried if c[0] not in returned), key=lambda c: c[1]):
                    iid = iid_of(*w.db.execute("SELECT tx, n FROM insc WHERE rid=?", (rid,)).fetchone())
                    k = next((n for n, e in enumerate(ends) if p < e), None)
                    if k is None:
                        w.put("at", (rid,), None)
                        row = w.get("insc", (rid,))
                        w.put("insc", (rid,), {**row, "burned": h})
                        moves.append(f"X {iid}\n")
                        landed[rid] = (None, None)
                        continue
                    off = p - (ends[k - 1] if k else 0)
                    w.put("at", (rid,), {"rid": rid, "tx": bytes.fromhex(tx.txid), "vout": k, "off": off, "height": h})
                    tracked.add(_outpoint(tx.txid, k))
                    moves.append(f"{'B' if is_new else 'M'} {iid} {tx.txid}:{k}:{off}\n")
                    landed[rid] = (k, self.owner_of(tx.vout[k][1]))
                self._tokens(w, h, tx, new, carried, landed)
                self._names(w, h, new)
            line = block_hash_of(block.hash, born_here)
            moved = hashlib.sha256("".join(moves).encode()).hexdigest()
            db.execute("INSERT OR REPLACE INTO block VALUES (?,?,?,?,?)", (h, block.hash, line, len(born_here), moved))
            db.execute("INSERT OR REPLACE INTO undo VALUES (?,?)",
                       (h, _pack({"rows": w.old, "next_number": number_before})))
            db.execute("DELETE FROM undo WHERE height<?", (h - UNDO_BLOCKS,))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('walk_next', ?)", (json.dumps(h + 1),))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('next_number', ?)", (json.dumps(next_number),))
            if own and isinstance(block, rawchain.Block):
                from . import ord_notes
                db.execute("INSERT OR REPLACE INTO pnote VALUES (?,?)",
                           (h, ord_notes.one_block(block, self.taproot, self.dialect, self.every_input)))
        if own and (h + 1 - self.base) % CHUNK == 0:
            c = chunk_of(h, self.base)
            rows = self.store.q("SELECT line, n FROM block WHERE height>=? AND height<=? ORDER BY height",
                                (chunk_range(c, self.base)[0], h))
            if len(rows) == CHUNK:
                kept = self.store.notes_of(c)
                nd = kept[1] if kept and kept[2] == "" else (self.store.finish_notes(c, rpc) or (None,))[0]
                self.store.say(c, "", chunk_hash_of([r[0] for r in rows]), sum(r[1] for r in rows), nd)
        if self.disk is not None:
            self.disk.block(block.txs, h)
        return moves

    def _parent(self, w: _Block, claimed: list[str], carried: list) -> tuple[str, list[int]] | None:
        """Pepecoin: the parents a new inscription's tags claim that stand -- (the first one's id, every one's rid) --
        or None. ord-pepecoin inscription_updater.rs keeps a claim only when the parent is indexed and this
        transaction spends the coin it sits on (`if let Some(parent_entry) = self.id_to_entry.get(..)` ... `if
        txs[0].input.iter().any(|input| input.previous_output == parent_satpoint.outpoint)`). txs[0] is the
        envelope's FIRST transaction; called only when that is this one (no pieces kept): an envelope begun earlier
        spent its parent's coin back then, the parent moved with it, and its coin now is never one txs[0] spent."""
        spent = {c[0] for c in carried if not c[2]}
        first, rids = None, []
        for pid in claimed:
            key = iid_parts(pid)
            row = w.db.execute("SELECT rid FROM insc WHERE substr(tx, 1, 8)=? AND tx=? AND n=?",
                               (key[0][:8],) + key).fetchone() if key else None
            if row is not None and row[0] in spent:
                first = first or pid
                if row[0] not in rids:
                    rids.append(row[0])
        return (first, rids) if first else None

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
        undo = _unpack(row[0][0])
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
            db.execute("DELETE FROM pnote WHERE height>=?", (h,))
            db.execute("DELETE FROM notes WHERE chunk>=? AND src=''", (chunk_of(h, self.base),))
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
        """How many more scans chunk c wants now, counting the leases running on it: one while it has none (the
        cover); once every chunk has one, up to AUDIT_MAX agreeing (the audit). None from this arcade twice."""
        words = self.store.words(c) if words is None else words
        if "" in words:
            return 0
        busy = sum(1 for until, _at in self.leases.get(c, {}).values() if until > now)
        if not words:
            return max(0, 1 - busy)
        tally: dict[str, int] = {}
        for h in words.values():
            tally[h] = tally.get(h, 0) + 1
        return max(0, AUDIT_MAX - max(tally.values()) - busy)

    def choose(self, ready: int, now: float, exclude: set | frozenset = frozenset(),
               first: list[int] | tuple = ()) -> int | None:
        """The next chunk to take (2026-10-20: "none of them should be assigned the same blocks until ... the
        entire index has been created; at that point ... the nodes can audit each other's work").

        Cover first: the lowest chunk in line that nobody has said and nobody holds, so a fast arcade simply takes the
        next and the next. Only when every chunk under the tip has been said once, the audit: a chunk this arcade has
        not said, the fewest agreeing first (a random one of the first few, so auditors spread)."""
        said = self.store.all_words(ready)
        held = {c for c, by in self.leases.items() for until, _at in by.values() if until > now}
        for c in first:                                  # the walk here needs these, and nobody can send their notes
            if c < ready and c not in exclude:
                return c
        for c in range(ready):
            if c not in said and c not in held and c not in exclude:
                return c
        if any(c not in said for c in range(ready)):
            return None                                  # the rest of the cover is in hand: wait for it
        audit = []
        for c, words in said.items():
            if c in exclude or c in held or "" in words:
                continue
            tally: dict[str, int] = {}
            for h in words.values():
                tally[h] = tally.get(h, 0) + 1
            best = max(tally.values())
            if best < AUDIT_MAX:
                audit.append((best, c))
        if not audit:
            return None
        audit.sort()
        least = audit[0][0]
        return random.choice([c for best, c in audit[:16] if best == least])


def fetch_raw_many(rpc, txids: list[str], batch: int = 200) -> dict[str, bytes]:
    """{txid: raw} for as many of `txids` as `rpc` gives, `batch` to a round trip; a batch that fails is left out (each
    of its transactions is asked one at a time later)."""
    out: dict[str, bytes] = {}
    for at in range(0, len(txids), batch):
        part = txids[at:at + batch]
        try:
            raws = rpc.batch([("getrawtransaction", [t, 0]) for t in part])
        except Exception:                                # noqa: BLE001
            continue
        for t, raw in zip(part, raws):
            if isinstance(raw, str):
                out[t] = bytes.fromhex(raw)
    return out


def scan_block(rpc, h: int, taproot: bool = False, mweb: bool = False,
               dialect: str = "dogecoin", every_input: bool = False) -> tuple[str, str, int, bytes]:
    """(block hash, its line, births in it, its spend notes) for one block, read raw and proved (rawchain)."""
    from . import ord_notes
    block_hash = rpc.call("getblockhash", h)
    block = rawchain.read_block(bytes.fromhex(rpc.call("getblock", block_hash, 0)), block_hash, mweb=mweb)
    found = births(block, taproot, dialect, every_input)
    return (block.hash, block_hash_of(block.hash, found), len(found),
            ord_notes.one_block(block, taproot, dialect, every_input))


# --------------------------------------------------------------------------- the worker

class DoginalIndex:
    """The thread's work: walk or help, a little at a time, and what the mesh hears and says (DxShare)."""

    def __init__(self, path: Path, rpc_factory: Callable[[], Any] | None, mode: Callable[[], str],
                 own_core: Callable[[], bool] = lambda: True, net: str = "doge", mweb: bool = False,
                 cores: Callable[[], int] | None = None, walk_share: Callable[[], int] | None = None,
                 lenders: Callable[[], list] | None = None, fetch_allowed: Callable[[], bool] | None = None):
        self.net = net
        # The linked arcades lending this chain's Core, [(node, client factory)]: a lane each (2026-10-24).
        self.lenders = lenders or (lambda: [])
        self.cores = cores or default_cores
        # A machine below BIG_FROM_CORES fetches this chain's whole index only when its operator said so (ord_snapshot
        # may_fetch; Admin API {"net", "fetch": true}).
        self.fetch_allowed = fetch_allowed or (lambda: False)
        # Without a setting: all of a big machine, half of a small one (as it paced before 2026-10-21).
        self.walk_share = walk_share or (lambda: 100 if self._big() else 50)
        self._pool: Any = None
        self._pool_size = 0
        self.base = STARTS.get(net, BASE) if net != "doge" else BASE
        self.taproot, self.mweb = net in TAPROOT, mweb
        self.every_input = ord_rules.every_input(net)    # Bellscoin: an envelope in every input (rules 7)
        # Each chain read as its own official indexer reads it (rules 4, 2026-10-21): births, completions, notes.
        self.dialect = ord_rules.dialect_of(net)
        self.store = Store(path, self.base, self.dialect, net)
        self.rules = self.store.rules
        self.rpc_factory = rpc_factory
        self.mode = mode
        self.own_core = own_core
        from .config import NETWORKS
        self.walker = Walker(self.store, self.taproot, mweb, params=NETWORKS.get(net),
                             token_protocol=ord_rules.token_protocol(net), dialect=self.dialect)
        self.chunks = Chunks(self.store)
        self.walker.fetch_many = self._fetch_many
        self.share: Any = None                       # DxShare, when the mesh runs
        self.me: str | None = None                   # this arcade's node id on the mesh (DxShare sets it)
        self.tip = 0
        self.helped = int(self.store.meta("helped", 0))
        self.help_rate = 0.0
        self._paced: list[tuple[float, int]] = []    # (when, next height) after each pass: the walk's real pace
        self.error = ""
        self._said_upto = int(self.store.meta("said_upto", -1))

    # --- one pass --------------------------------------------------------------------------------------------------

    #: How often the write log is squeezed back down (a checkpoint that truncates it).
    WAL_EVERY = 600

    def step(self, budget: float = 5.0) -> float:
        """Do about `budget` seconds of work; returns how long to rest (as long as the work took: half the time)."""
        if self.store.old_layout:
            self.upgrade()
            return 0.05
        mode = self.mode()
        if time.monotonic() - getattr(self, "_wal_at", 0.0) > self.WAL_EVERY:
            self._wal_at = time.monotonic()
            try:
                self.store.x("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:                        # busy readers: next time
                pass
            self.snapshots.tidy()
        if mode not in ("walk", "help") or self.rpc_factory is None:
            return 10.0
        started = time.monotonic()
        try:
            with self.rpc_factory() as rpc:
                self.tip = int(rpc.call("getblockcount"))
                if mode == "walk":
                    self._walk(rpc, budget)
                else:
                    self._help(rpc, budget)
                self._prune_notes()
            self.error = ""
        except Reorg as exc:
            self.error = str(exc)
            return 60.0
        except Exception as exc:                         # noqa: BLE001 -- said in the admin panel, tried again
            self.error = str(exc)[:200]
            log.debug("doginal index pass failed", exc_info=True)
            return 30.0
        self._tell_new_chunks()
        if mode == "walk":
            self._note_pace()
            if time.monotonic() - getattr(self, "_seen_saved", 0.0) > self.SEEN_EVERY and self.walker.tracked is not None:
                self._seen_saved = time.monotonic()
                self.walker.save_seen()
        took = time.monotonic() - started
        # Paced by the share of the machine the operator gives the walk (2026-10-21): work `took`, rest so that
        # the work is that share of the time. All of it: straight on (2026-10-20: a test machine "shouldn't be pacing").
        try:
            share = max(10, min(int(self.walk_share()), 100))
        except (TypeError, ValueError):
            share = 100
        if share >= 100:
            return 0.05
        return max(0.5, took * (100 / share - 1))

    @staticmethod
    def _big() -> bool:
        import os
        return (os.cpu_count() or 1) >= BIG_FROM_CORES

    #: How often the walk's coin filter is kept on disk (Walker.save_seen): a start adds only what came after.
    SEEN_EVERY = 900.0

    #: The window the walk's pace is measured over (seconds of wall time, its rests and waits for notes included).
    PACE_WINDOW = 120.0

    def _note_pace(self) -> None:
        now = time.monotonic()
        self._paced = [p for p in self._paced if now - p[0] <= self.PACE_WINDOW] + [(now, self.walker.next_height())]

    def walk_pace(self) -> float:
        """Blocks a second the walk really moves on, over the last PACE_WINDOW (2026-10-21: the panel said
        12,000 a second on PEP main while it moved about 15): `walker.rate` is only the speed inside a pass, where
        blocks already scanned stitch in thousands a second; the pace also counts the rests and the waits for the
        scans ahead."""
        if len(self._paced) < 2 or self._paced[-1][0] - self._paced[0][0] < 10:
            return 0.0
        (t0, h0), (t1, h1) = self._paced[0], self._paced[-1]
        return max(0.0, (h1 - h0) / (t1 - t0))

    def pass_budget(self) -> float:
        """How long one pass may hold the web process: short bursts on a small machine, so a page waits a second at
        most; a big machine runs its passes long."""
        import os
        return 5.0 if (os.cpu_count() or 1) >= BIG_FROM_CORES else 1.0

    # --- holding the index: scan ahead on the spare cores, fetch what others scanned, stitch in order ------------

    def _walk(self, rpc, budget: float) -> None:
        """One pass of an arcade that holds the whole index (2026-10-20). Its spare cores scan the chunks in
        line ahead of the walk (a lane each); chunks other arcades scanned are fetched from them; the walk stitches
        whatever notes are here, in order, and reads a block from its own Core only when nobody has or is about to
        have that chunk's notes."""
        snaps = self.snapshots
        if getattr(self, "_snap_asked", False) and snaps.want_made():
            self._snap_asked = False
            snaps.make()
        snaps.step(self.share)
        # The whole index on its way: the walk goes on from this arcade's own Core meanwhile, and whichever gets there
        # first wins (2026-10-22: "Both"; Snapshots.step lets the copy go once the walk passes its block).
        if snaps.fetch is None:
            self._ask_for_snapshot()
        if self.walker.disk is None:
            self.walker.disk = ord_values.Values(Path(str(self.store.path) + ".values"))
        if self._lane_sources(walking=True):
            self._help(rpc, budget if self._values_lanes() else budget / 2, walking=True)
        else:
            self._let_go()
        self._fetch_ahead()
        if self._values_lanes():
            # Bringing in the coin values (ord_values): the walk waits. Without them each of its blocks waits on a
            # lender for nearly every coin -- tens of seconds a block, on the same thread the lanes need (2026-10-25,
            # Strawberry: 3 chunks brought in in 5 minutes beside a walk of 70 blocks).
            return
        self.walker.step(rpc, self.tip, budget, notes=self._notes_for)

    @property
    def snapshots(self):
        if getattr(self, "_snapshots", None) is None:
            from .ord_snapshot import Snapshots
            self._snapshots = Snapshots(self)
        return self._snapshots

    def _ask_for_snapshot(self) -> None:
        """Far behind the furthest holder, and nobody offers a snapshot: ask that holder to make one."""
        from .ord_snapshot import WORTH_BLOCKS
        walkers = [(n, h) for n, h in self.walkers() if int((self.chunks.peers.get(n) or {}).get("v") or 0) >= 3]
        if not walkers or self.share is None or not hasattr(self.share, "post_to"):
            return
        node, h = walkers[0]
        if h - self.walker.next_height() < WORTH_BLOCKS:
            return
        why = self.snapshots.may_fetch()
        if why:
            self.snapshots.said = why                    # far behind, and not a machine to fetch it: walks on
            return
        if self.snapshots.best_offer() is not None:
            return
        if time.time() - getattr(self, "_snap_wished", 0.0) < 600:      # asked again every 10 minutes until offered
            return
        self._snap_wished = time.time()
        self.share.post_to(node, {"op": "sq"})

    def upgrade(self) -> None:
        """The one-time conversion to the slim layout (Store.upgrade), then everything kept in memory read again. A
        snapshot of the old layout is no use to anyone now: let go of."""
        try:
            self.store.upgrade()
        except Exception as exc:                         # noqa: BLE001 -- said in the Ords tab, tried again
            self.error = f"slimming the index failed: {str(exc)[:160]}"
            log.warning("ord index %s: upgrade failed", self.net, exc_info=True)
            time.sleep(60)
            return
        self.error = ""
        self.walker.tracked = None
        self.walker._cts = None
        self._status_kept = None
        self._counts_kept = None
        self.snapshots.forget()

    def install(self, path: Path) -> None:
        """A whole index fetched from peers and checked: in this one's place, the walk on from its block."""
        self.store.swap(path)
        for tail in ("-wal", "-shm"):
            try:
                Path(str(path) + tail).unlink()
            except FileNotFoundError:
                pass
        self.walker.tracked = None
        self._decoded = None
        self.chunks.lanes = []
        self._forget_values()
        self._said_upto = -1
        self.store.set_meta("said_upto", -1)
        self._status_kept = None
        self._counts_kept = None

    def _notes_for(self, c: int):
        """For the walk: ([blocks], own scan?) from chunk c's notes; False to wait for them (a lane here or a peer is
        on it, or they are on their way); None to read the chunk from this arcade's Core."""
        cached = getattr(self, "_decoded", None)
        if cached is not None and cached[0] == c:
            return cached[1]
        got = self.store.notes_of(c)
        if got is not None:
            from . import ord_notes
            try:
                said = (ord_notes.decode(ord_notes.unpack(got[0])), got[2] == "")
            except ord_notes.NotesError as exc:
                log.warning("ord index: chunk %d's notes do not read (%s); let go", c, exc)
                self.store.x("DELETE FROM notes WHERE chunk=?", (c,))
                return False
            self._decoded = (c, said)
            if got[2] != "":
                word = self.store.q("SELECT hash FROM seen WHERE chunk=? AND node=?", (c, got[2]))
                self.store.x("INSERT OR REPLACE INTO stitched VALUES (?,?,?,?)",
                             (c, got[2], word[0][0] if word else "", got[1]))
            return said
        if c >= ready_chunks(self.tip, self.base):
            return None                                  # not a whole chunk yet: nobody scans it; its few blocks read
        if any(lane["c"] == c for lane in self.chunks.lanes) or c in self._fetching():
            return False
        if self._lane_sources(walking=True):
            return False                                 # the lanes here take every chunk in line, this one too
        lo = chunk_range(c, self.base)[0]
        now = time.time()
        for node, (until, at) in self.chunks.leases.get(c, {}).items():
            if until > now and at - lo >= CHUNK // 2:
                return False                             # another arcade is past half way: its notes come soon
        if not self.own_core():
            return False
        if getattr(self, "_reading", None) != c:          # reading it here: say so, so no helper takes it too
            self._reading = c
            self._say_lease({"c": c, "at": self.walker.next_height()})
        return None

    def _needed(self) -> list[int]:
        """Chunks just ahead of the walk with no notes here, none on their way, and nobody to send them -- said only by
        an arcade that keeps no notes (an older one), or by ones that let them go: the lanes here scan them first."""
        first = chunk_of(self.walker.next_height(), self.base)
        ready = ready_chunks(self.tip, self.base)
        words = self.store.all_notes_words(min(ready, first + PREFETCH))
        failed = self.__dict__.get("_notes_failed", {})
        out = []
        for c in range(first, min(ready, first + PREFETCH)):
            if c in self._fetching() or self.store.notes_of(c) is not None:
                continue
            by = words.get(c) or {}
            if by and not any(n and nd and n not in failed.get(c, set()) for n, (_h, nd, _b) in by.items()):
                out.append(c)
        return out

    def _fetching(self) -> dict:
        return self.__dict__.setdefault("_asked_notes", {})

    def _fetch_ahead(self) -> None:
        """Ask peers for the notes of the chunks in line past the walk, each chunk of another peer where it can."""
        share = self.share
        if share is None or not hasattr(share, "fetch_notes"):
            return
        now = time.time()
        asked = self._fetching()
        for c, at in list(asked.items()):
            if now - at[0] > 120:
                asked.pop(c, None)                       # never answered: ask again, elsewhere
        failed = self.__dict__.setdefault("_notes_failed", {})
        first = chunk_of(self.walker.next_height(), self.base)
        ready = ready_chunks(self.tip, self.base)
        words = self.store.all_notes_words(min(ready, first + PREFETCH))
        for c in range(first, min(ready, first + PREFETCH)):
            if c in asked or self.store.notes_of(c) is not None or any(l["c"] == c for l in self.chunks.lanes):
                continue
            offers = sorted(n for n, (_h, nd, _b) in (words.get(c) or {}).items()
                            if n and nd and n not in failed.get(c, set()))
            if not offers:
                continue
            node = offers[c % len(offers)]
            h, nd, _b = words[c][node]
            asked[c] = (now, node)
            share.fetch_notes(c, node, nd, h)

    def got_notes(self, node: str, c: int, blob: bytes, nd: str, hash_: str) -> bool:
        """Notes a peer sent for chunk c: kept when they are what it said of the chunk -- their digest, and the births
        hash the blocks in them make (each block proved by its merkle root)."""
        from . import ord_notes
        self._fetching().pop(c, None)
        try:
            notes = ord_notes.unpack(blob)
            if ord_notes.digest(notes) != nd:
                raise ord_notes.NotesError("not the digest it said")
            blocks = ord_notes.decode(notes)
            if len(blocks) != CHUNK:
                raise ord_notes.NotesError(f"{len(blocks)} blocks, not {CHUNK}")
            if chunk_hash_of([block_hash_of(b.hash, births(b, self.taproot, self.dialect, self.every_input))
                              for b in blocks]) != hash_:
                raise ord_notes.NotesError("its blocks do not make the hash it said")
        except ord_notes.NotesError as exc:
            log.warning("ord index: arcade %s sent chunk %d's notes that are wrong: %s", node[:12], c, exc)
            self.fetch_failed(node, c)
            return False
        self.store.x("INSERT OR REPLACE INTO notes VALUES (?,?,?,?,?)", (c, blob, nd, node, time.time()))
        return True

    def fetch_failed(self, node: str, c: int) -> None:
        self._fetching().pop(c, None)
        self.__dict__.setdefault("_notes_failed", {}).setdefault(c, set()).add(node)

    def notes_blob(self, c: int) -> tuple[bytes, str] | None:
        """Chunk c's notes for a peer that asked: (packed, digest), when kept here."""
        got = self.store.notes_of(c)
        return (got[0], got[1]) if got else None

    def holders(self) -> list[tuple[str, int]]:
        """Arcades holding this chain's whole index, with the block each has walked to ("" this one)."""
        out = [(node, h) for node, h in self.walkers()]
        if self.mode() == "walk":
            out.append(("", self.walker.next_height()))
        return out

    def _check_stitched(self) -> None:
        """A chunk stitched from another arcade's notes that the audit has since outvoted -- more arcades, two at least,
        say a different births hash for it -- means the index past it may be wrong. Said in the Ords panel for the
        operator to decide; never wiped by itself (2026-10-20: a test machine's PEP index, half walked, "I don't want
        you to lose any of that"). Only the births hash counts: notes in another format differ without being wrong."""
        rows = self.store.q("SELECT s.chunk, s.hash FROM stitched s")
        disputed = []
        if rows:
            words = self.store.all_notes_words(max(c for c, _h in rows) + 1)
            for c, h in rows:
                tally: dict[str, int] = {}
                for wh, _wnd, _b in (words.get(c) or {}).values():
                    tally[wh] = tally.get(wh, 0) + 1
                ours = tally.get(h, 0)
                against = max((n for wh, n in tally.items() if wh != h), default=0)
                if against >= AGREE and against > ours:
                    disputed.append(c)
        self.disputed = disputed
        if disputed:
            c = disputed[0]
            log.warning("ord index %s: %d chunk(s) stitched from notes other arcades have outvoted, first %d",
                        self.net, len(disputed), c)

    def _prune_notes(self) -> None:
        """Let go of the notes every arcade holding the index has stitched past (2026-10-20: "Delete after
        stitch"); with nobody holding it, keep at most NOTES_MAX_BYTES, the nearest chunks first."""
        now = time.monotonic()
        if now - getattr(self, "_pruned", 0.0) < 60:
            return
        self._pruned = now
        if self.mode() == "walk":
            self._check_stitched()
        # Only holders that stitch notes wait for them: an arcade from before the notes (v2) never will.
        held = [(n, h) for n, h in self.holders() if not n or int((self.chunks.peers.get(n) or {}).get("v") or 0) >= 3]
        # ... and only holders within WORTH_BLOCKS of the furthest: one further behind fetches the whole index (or
        # walks from its Core) rather than wait on notes (2026-10-21: a test machine kept 588 MB of Dogecoin notes for a
        # holder whose walk had not left the chain's first block).
        from .ord_snapshot import WORTH_BLOCKS
        if held:
            front = max(h for _n, h in held)
            held = [(n, h) for n, h in held if front - h < WORTH_BLOCKS]
        if held:
            behind = min(h for _n, h in held)
            self.store.x("DELETE FROM notes WHERE chunk < ?", (chunk_of(behind, self.base),))
        # Block notes of a chunk this arcade is no longer reading (a lane let go, somebody else said it): not needed.
        busy = {lane["c"] for lane in self.chunks.lanes}
        if self.mode() == "walk":
            busy.add(chunk_of(self.walker.next_height(), self.base))
        for (c,) in self.store.q("SELECT DISTINCT (height - ?) / ? FROM pnote", (self.base, CHUNK)):
            if c not in busy and (self.store.q("SELECT 1 FROM seen WHERE chunk=? LIMIT 1", (c,))
                                  or self.store.notes_of(c) is not None):
                lo, hi = chunk_range(c, self.base)
                self.store.x("DELETE FROM pnote WHERE height>=? AND height<?", (lo, hi))
        total = 0
        for c, size in self.store.q("SELECT chunk, LENGTH(blob) FROM notes ORDER BY chunk"):
            total += size
            if total > NOTES_MAX_BYTES:
                self.store.x("DELETE FROM notes WHERE chunk >= ?", (c,))
                break

    def _lanes_wanted(self) -> int:
        try:
            return max(1, int(self.cores()))
        except (TypeError, ValueError):
            return 1

    def _lenders_now(self) -> list[tuple[str, Any, int]]:
        """(node, client factory, lanes it lends) of the arcades lending this chain now, the resting ones left out."""
        try:
            got = list(self.lenders() or [])
        except Exception:                                # noqa: BLE001 -- the mesh not up yet: none this pass
            got = []
        rest = self.__dict__.setdefault("_lender_rest", {})
        now = time.time()
        return [(row[0], row[1], int(row[2]) if len(row) > 2 else 1) for row in got if rest.get(row[0], 0) < now]

    def _lent(self) -> dict:
        """{node: client factory} of the arcades lending this chain now."""
        return {n: f for n, f, _lanes in self._lenders_now()}

    #: Transactions in one ask of the walk's parallel lookups (_fetch_many): small, so a lender's lanes each take one.
    FETCH_BATCH = 50

    def _fetch_many(self, txids: list[str]) -> dict[str, bytes]:
        """A block's earlier transactions for the walk, split over every Core a lane reads from -- this arcade's own and
        each lender's -- in small batches, as many at once on each as the lanes it lends (2026-10-25: "parallel
        asks": one batch at a time left a lender's disk serving a lookup at a time; several let it read side by side)."""
        sources: list[tuple[Any, int]] = []
        if self.own_core() and self.rpc_factory is not None:
            sources.append((self.rpc_factory, LANES_OWN_FETCH))
        sources += [(f, lanes) for _n, f, lanes in self._lenders_now()]
        if not sources and self.rpc_factory is not None:
            sources = [(self.rpc_factory, 1)]
        if not sources:
            return {}
        # Dealt round the sources in proportion to their lanes: a lender lending 4 takes four batches to another's one;
        # a few transactions still spread over every slot.
        slots = [f for f, lanes in sources for _l in range(max(1, lanes))]
        size = max(1, min(self.FETCH_BATCH, -(-len(txids) // len(slots))))
        batches = [txids[at:at + size] for at in range(0, len(txids), size)]
        jobs = [(slots[i % len(slots)], part) for i, part in enumerate(batches)]

        def one(job) -> dict[str, bytes]:
            factory, part = job
            try:
                with factory() as rpc:
                    return fetch_raw_many(rpc, part, len(part))
            except Exception:                            # noqa: BLE001 -- its share is asked one at a time
                return {}
        if len(jobs) == 1:
            return one(jobs[0])
        out: dict[str, bytes] = {}
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(len(jobs), len(slots))) as pool:
            for got in pool.map(one, jobs):
                out.update(got)
        return out

    def _lent_lanes(self) -> list[str]:
        """A lane per lane each lender lends (2026-10-25: "several lanes on each lender"), dealt round the
        lenders -- each one's first lane, then each one's second -- so the cores this arcade gives cut the slowest last;
        as many as this arcade gives cores."""
        rows = self._lenders_now()
        out, round_ = [], 0
        while len(out) < self._lanes_wanted() and any(lanes > round_ for _n, _f, lanes in rows):
            out += [n for n, _f, lanes in rows if lanes > round_]
            round_ += 1
        return out[:self._lanes_wanted()]

    def _lane_sources(self, walking: bool = False) -> list[str]:
        """Where each lane reads from: "" for this arcade's own Core (all its cores helping; walking, one fewer -- the
        walk is one), then one lane on every arcade lending the chain (2026-10-24: "borrow as many nodes as are
        available and run them each on their own lane"; "Anytime an arcade is indexing. It should be able to borrow as
        many nodes as it wants"). An arcade with no Core of its own reads only through its lenders."""
        own = (self._lanes_wanted() - (1 if walking else 0)) if self.own_core() else 0
        return [""] * max(0, own) + self._lent_lanes()

    def _help(self, rpc, budget: float, walking: bool = False) -> None:
        """Help with a chunk on each lane at once -- one per core given to it on this arcade's own Core, and one on every
        arcade lending the chain: their next blocks are fetched side by side, each lane from its own Core, and read in
        worker processes, a block a lane at a time (2026-10-20: dedicate cores; 2026-10-24: borrowed lanes)."""
        now = time.time()
        ready = ready_chunks(self.tip, self.base)
        sources = self._lane_sources(walking) if (walking or not self.own_core()) else (
            [""] * self._lanes_wanted() + self._lent_lanes())
        lanes = self.chunks.lanes
        # A lane whose lender is gone, or more of this arcade's own than it gives cores now: let go, newest first.
        free = list(sources)
        for lane in list(lanes):
            via = lane.get("via", "")
            if via in free:
                free.remove(via)
            else:
                self._drop_lane(lane)
        held = {lane["c"] for lane in lanes}
        while free:
            c = self.chunks.choose(ready, now, exclude=held, first=self._needed() if self.mode() == "walk" else ())
            if c is None:
                break
            lane = {"c": c, "at": chunk_range(c, self.base)[0], "via": free.pop(0)}
            lanes.append(lane)
            held.add(c)
            self._say_lease(lane)
        # Lanes with nothing to scan for the shared list bring in the coin values of blocks walked before the values
        # file existed (ord_values): never said, never leased -- for this arcade's walk alone.
        vlanes = self._values_lanes()
        for lane in list(vlanes):
            if lane.get("via", "") in free:
                free.remove(lane.get("via", ""))
            else:
                vlanes.remove(lane)                      # its lender gone, or the shared list needs the lane
        if free:
            for c in self._values_wanted({lane["c"] for lane in vlanes}):
                if not free:
                    break
                vlanes.append({"c": c, "at": chunk_range(c, self.base)[0], "via": free.pop(0)})
        self._keep_lanes()
        if not lanes and not vlanes:
            return
        start, done = time.monotonic(), 0
        while not (done and time.monotonic() - start >= budget):
            todo = [lane for lane in lanes + vlanes if lane["at"] < chunk_range(lane["c"], self.base)[1]]
            if not todo:
                break
            stored = {lane["at"] for lane in todo if lane in lanes and
                      self.store.q("SELECT 1 FROM pnote WHERE height=?", (lane["at"],))}
            asked = [(lane["at"], lane.get("via", "")) for lane in todo if lane["at"] not in stored]
            scanned = self._scan_many(rpc, asked)
            for_values = {lane["at"] for lane in todo if lane in vlanes}
            for h, (block_hash, line, n, notes) in scanned.items():
                if h in for_values:
                    self._absorb_values(h, notes)
                    continue
                with self.store.tx() as db:
                    if not db.execute("SELECT 1 FROM block WHERE height=?", (h,)).fetchone():
                        db.execute("INSERT INTO block (height, hash, line, n) VALUES (?,?,?,?)",
                                   (h, block_hash, line, n))
                    db.execute("INSERT OR REPLACE INTO pnote VALUES (?,?)", (h, notes))
            for lane in todo:
                if lane["at"] not in stored and lane["at"] not in scanned:
                    # Its lender did not answer: the lane waits; three misses in a row and that arcade rests five
                    # minutes, its chunk let go for another lane to take.
                    lane["miss"] = lane.get("miss", 0) + 1
                    if lane["miss"] >= 3 and lane.get("via"):
                        self.__dict__.setdefault("_lender_rest", {})[lane["via"]] = time.time() + 300
                        log.info("ord index %s: arcade %s did not answer three times; rests 5 minutes",
                                 self.net, lane["via"][:12])
                        if lane in vlanes:
                            vlanes.remove(lane)
                        else:
                            self._drop_lane(lane)
                    continue
                lane.pop("miss", None)
                lo = chunk_range(lane["c"], self.base)[0]
                lane["at"] += 1
                done += 1
                if (lane["at"] - lo) % PROGRESS_EVERY == 0 and lane in lanes:
                    self._say_lease(lane)
            self._keep_lanes()
            if not scanned and not stored:
                break
        if done:
            self.help_rate = done / max(time.monotonic() - start, 1e-6)
        for lane in list(lanes):
            c = lane["c"]
            if lane["at"] >= chunk_range(c, self.base)[1]:
                got = self.store.chunk_line(c)
                if got is not None:
                    kept = self.store.notes_of(c)
                    nd = kept[1] if kept and kept[2] == "" else (self.store.finish_notes(c, rpc) or (None,))[0]
                    self.store.say(c, "", got[0], got[1], nd)
                    self.helped += 1
                    self.store.set_meta("helped", self.helped)
                self._drop_lane(lane)
        for lane in list(vlanes):
            if lane["at"] >= chunk_range(lane["c"], self.base)[1]:
                self.walker.disk.mark_done(lane["c"])
                vlanes.remove(lane)
        self._keep_lanes()

    def _forget_values(self) -> None:
        """The walk jumped (a whole index installed): the values file would miss the blocks jumped over without
        knowing it, so it starts again from the walk's next block, and the lanes bring in everything before."""
        disk, self.walker.disk = self.walker.disk, None
        if disk is not None:
            disk.close()
        for tail in ("", "-wal", "-shm"):
            try:
                Path(str(self.store.path) + ".values" + tail).unlink()
            except FileNotFoundError:
                pass
        self._vlanes = []
        self.store.set_meta("vlanes", [])

    def _values_status(self) -> dict | None:
        """For the Ords panel: the coin values kept on disk (ord_values), and how many chunks walked before the file
        existed are still to bring in."""
        disk = self.walker.disk
        if disk is None:
            return None
        start = disk.start()
        before = chunk_of(start - 1, self.base) + 1 if start is not None and start > self.base else 0
        return {"from": start, "bytes": disk.size(), "behind_chunks": before,
                "brought_in": len([c for c in disk.done() if c < before]),
                "lanes": [{"chunk": l["c"], "at": l["at"]} for l in self._values_lanes()]}

    def _values_lanes(self) -> list[dict]:
        if getattr(self, "_vlanes", None) is None:
            self._vlanes = list(self.store.meta("vlanes") or [])
        return self._vlanes

    def _values_wanted(self, exclude: set) -> list[int]:
        """The next chunks (lowest first, a few) whose coin values the walk has never seen: walked before the values
        file existed. Only on a big machine, walking, far enough behind the tip to be worth it."""
        disk = self.walker.disk
        if disk is None or self.mode() != "walk" or not self._big():
            return []
        if self.tip - self.walker.next_height() < VALUES_BEHIND:
            return []
        start = disk.start()
        if start is None or start <= self.base:
            return []
        done, out = disk.done(), []
        for c in range(0, chunk_of(start - 1, self.base) + 1):
            if c not in done and c not in exclude:
                out.append(c)
                if len(out) >= 64:
                    break
        return out

    def _absorb_values(self, h: int, notes: bytes) -> None:
        """A block a values lane read (its notes, proved by the merkle root): its coins on disk. A block the walk has
        filed itself (from the values file's first block on) is left alone -- its spends are in already."""
        from . import ord_notes
        disk = self.walker.disk
        start = disk.start() if disk is not None else None
        if start is None or h >= start:
            return
        disk.block(ord_notes.decode(bytes([ord_notes.VERSION]) + notes)[0].txs, h)

    def _scan_many(self, rpc, asked: list) -> dict[int, tuple[str, str, int]]:
        """(hash, line, births) for each (height, via) a lane asks: one at a time on one lane of this arcade's own Core;
        on several, fetched side by side (threads, each its own connection: this Core's, or the lending arcade's the
        lane reads from) and read in worker processes (Python reads blocks on one core per process)."""
        if not asked:
            return {}
        import os
        small = (os.cpu_count() or 1) < BIG_FROM_CORES
        heights = [h for h, _via in asked]
        if (len(asked) == 1 or self._lanes_wanted() == 1) and not small and not any(via for _h, via in asked):
            return {h: scan_block(rpc, h, self.taproot, self.mweb, self.dialect, self.every_input) for h in heights}
        # A small machine reads every block in a worker process, even on one lane: parsed here, it held the web
        # process's threads (2026-10-20, .25 helping: pages waited a minute).
        from concurrent.futures import ThreadPoolExecutor
        lent = self._lent()

        def fetch(job: tuple) -> tuple[str, str] | None:
            h, via = job
            factory = lent.get(via) if via else self.rpc_factory
            if factory is None:
                return None                              # no longer lends this chain: the lane lets go
            try:
                with factory() as own:
                    block_hash = own.call("getblockhash", h)
                    return block_hash, own.call("getblock", block_hash, 0)
            except Exception as exc:                     # noqa: BLE001 -- one lane's lender: the others go on
                if not via:
                    raise
                log.debug("ord index %s: block %d from arcade %s failed: %s", self.net, h, via[:12], exc)
                return None
        with ThreadPoolExecutor(max_workers=len(asked)) as fetchers:
            got = list(fetchers.map(fetch, asked))
        heights = [h for (h, _via), raw in zip(asked, got) if raw is not None]
        raws = [raw for raw in got if raw is not None]
        if not raws:
            return {}
        pool = self._workers()
        jobs = [(raw, block_hash, self.taproot, self.mweb, self.dialect, self.every_input) for block_hash, raw in raws]
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
        if getattr(self, "_vlanes", None) is not None:
            self.store.set_meta("vlanes", self._vlanes)
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
        rows = self.store.q("SELECT rowid, chunk, hash, births, nd FROM seen WHERE node='' AND rowid > ? "
                            "ORDER BY rowid LIMIT 500", (self._said_upto,))
        if not rows:
            return
        self.share.post({"op": "dn", "rows": [[c, h, n] + ([nd] if nd else []) for _r, c, h, n, nd in rows]})
        self._said_upto = int(rows[-1][0])
        self.store.set_meta("said_upto", self._said_upto)

    def all_mine(self) -> list[list]:
        return [[c, h, n] + ([nd] if nd else [])
                for c, h, n, nd in self.store.q("SELECT chunk, hash, births, nd FROM seen WHERE node=''")]

    # --- what the mesh heard ---------------------------------------------------------------------------------------

    def heard(self, node: str, header: dict) -> None:
        op = header.get("op")
        if op == "hi":
            self.chunks.peers[node] = {k: header.get(k) for k in ("mode", "walk", "helped", "v", "rules")} | \
                {"at": time.time()}
            if header.get("rules") == self.rules:
                # "sn<layout>": a snapshot of this layout, said under its own key so an arcade of another layout
                # never fetches one it cannot read ("sn" was layout 1, "sn2" layout 2).
                self.snapshots.heard_offer(node, header.get(f"sn{LAYOUT}"))
            return
        if op == "sq":                                   # a linked arcade far behind asks for a snapshot
            self._snap_asked = True
            return
        if (self.chunks.peers.get(node) or {}).get("rules") != self.rules:
            return                         # an arcade on other rules (or not heard from yet): its chunks are not ours
        if op == "ls":
            c, at = int(header["c"]), int(header.get("at") or 0)
            self.chunks.lease_from(node, c, at, min(float(header.get("until") or 0), time.time() + LEASE_SECONDS))
            # Two arcades took the same chunk at the same moment: the one further behind lets go (the same node id
            # decides a tie, so exactly one of them does), and its lane takes the next in line.
            for lane in list(self.chunks.lanes):
                if lane["c"] == c and (lane["at"] < at or (lane["at"] == at and node < (self.me or ""))):
                    self._drop_lane(lane)
                    self._keep_lanes()
        elif op == "ld":
            self.chunks.drop_from(node, int(header["c"]))
        elif op == "dn":
            for row in (header.get("rows") or [])[:1000]:
                try:
                    c, h, n = int(row[0]), str(row[1]), int(row[2])
                    nd = str(row[3]) if len(row) > 3 and row[3] else None
                except (TypeError, ValueError, IndexError):
                    continue
                if 0 <= c and len(h) == 64 and (nd is None or len(nd) == 64):
                    self.store.say(c, node, h, n, nd)
                    self.chunks.drop_from(node, c)

    def hello(self) -> dict:
        said = {"op": "hi", "net": self.net, "base": self.base, "mode": self.mode(),
                "walk": self.walker.next_height() if self.mode() == "walk" else None, "helped": self.helped, "v": 3,
                "rules": self.rules}
        offer = self.snapshots.offer()
        if offer is not None:
            said[f"sn{LAYOUT}"] = offer
        return said

    # --- for people ------------------------------------------------------------------------------------------------

    #: How long an answer to status() is kept, and how long its heavy counts (whole-table counts over millions of rows)
    #: are (2026-10-20: the Ords tab asked every 5 s, each answer took 3-17 s on .25, and the site's pages waited).
    STATUS_SECONDS = 20.0
    COUNTS_SECONDS = 120.0

    def status(self) -> dict:
        """Kept STATUS_SECONDS; while it is being worked out again, everybody else gets the last one at once instead of
        working it out too (2026-10-20: Admin tabs piled up on it and held the threads that serve pages)."""
        kept = getattr(self, "_status_kept", None)
        if kept and time.monotonic() - kept[0] < self.STATUS_SECONDS:
            return kept[1]
        lock = self.__dict__.setdefault("_status_lock", threading.Lock())
        if not lock.acquire(blocking=kept is None):
            return kept[1]
        try:
            said = self._status()
            self._status_kept = (time.monotonic(), said)
            return said
        finally:
            lock.release()

    def _counts(self) -> dict:
        """The index's totals. A walking index's (inscriptions, tokens, holders, names) are never waited for: worked out
        on a thread of their own, and until they are, the last ones (or none, said as counting). 2026-10-22:
        @strawberry's Dogecoin index had 52.8 million inscriptions and the count took 42 s -- every Ords tab waited
        behind it, and the panel stopped loading."""
        kept = getattr(self, "_counts_kept", None)
        if kept and time.monotonic() - kept[0] < self.COUNTS_SECONDS:
            return kept[1]
        # Every mode off the asking thread (2026-10-26: on .25's busy disk an index it does not walk summed its block
        # table in 62 s (Dogecoin) and 235 s (Pepecoin), and every Admin -> Ords request queued behind it).
        walking = self.mode() == "walk" and not self.store.old_layout
        lock = self.__dict__.setdefault("_counts_lock", threading.Lock())
        if lock.acquire(blocking=False):
            def count() -> None:
                try:
                    self._count(walking=walking)
                except Exception as exc:                       # noqa: BLE001 -- tried again next time
                    log.warning("doginal index counts: %s", exc)
                finally:
                    lock.release()
            threading.Thread(target=count, name="ord-counts", daemon=True).start()
        if kept:
            return kept[1]
        return {"found": self.store.q("SELECT COALESCE(SUM(n), 0) FROM block")[0][0], "counting": True}

    def _count(self, walking: bool) -> dict:
        counts = {"found": self.store.q("SELECT COALESCE(SUM(n), 0) FROM block")[0][0]}
        if walking:
            ins = self.store.q("SELECT COUNT(*), COUNT(burned), COUNT(reinscribed) FROM insc")[0]
            counts.update(inscriptions=ins[0], burned=ins[1], reinscriptions=ins[2],
                          tokens=self.store.q("SELECT COUNT(*) FROM tok")[0][0],
                          holders=self.store.q("SELECT COUNT(DISTINCT owner) FROM bal WHERE overall != '0'")[0][0],
                          names={rule: self.store.q(f"SELECT COUNT(DISTINCT {rule}) FROM names")[0][0]
                                 for rule in ord_rules.NAME_RULES})
        self._counts_kept = (time.monotonic(), counts)
        return counts

    def _status(self) -> dict:
        now = time.time()
        ready = ready_chunks(self.tip, self.base) if self.tip else 0
        per: dict[int, dict[str, str]] = {}
        born: dict[tuple[int, str], int] = {}
        for c, node, h, n in self.store.q("SELECT chunk, node, hash, births FROM seen WHERE chunk < ?",
                                          (max(ready, 1),)):
            per.setdefault(c, {})[node] = h
            born[(c, h)] = n
        agreed = differ = one = agreed_births = 0
        differs = []
        # Confidence (2026-10-20): how many arcades said the same of a chunk -- 1 found, 2 legitimate, more
        # raises it. Chunks and the inscriptions in them, at each level (AUDIT_MAX and over together).
        levels = {k: {"chunks": 0, "inscriptions": 0} for k in range(1, AUDIT_MAX + 1)}
        agreeing: dict[int, int] = {}                     # chunk -> arcades saying its most-said hash
        for c, words in per.items():
            tally: dict[str, int] = {}
            for h in words.values():
                tally[h] = tally.get(h, 0) + 1
            best = max(tally, key=tally.get)
            agreeing[c] = tally[best]
            level = levels[min(tally[best], AUDIT_MAX)]
            level["chunks"] += 1
            level["inscriptions"] += born.get((c, best), 0)
            if tally[best] >= AGREE:
                agreed += 1
                # The births two arcades said with the same hash (2026-10-20: the inscriptions agreed on, in a
                # column of their own on the Ords tab).
                agreed_births += born.get((c, best), 0)
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
            counts = self._counts()
            walk = {"next": nxt, "tip": self.tip, "left": max(0, self.tip - WALK_SETTLE - nxt + 1),
                    "inscriptions": counts.get("inscriptions", 0), "burned": counts.get("burned", 0),
                    "tokens": counts.get("tokens", 0), "holders": counts.get("holders", 0),
                    "names": counts.get("names", {}), "counting": bool(counts.get("counting")),
                    "rate": round(self.walk_pace(), 2), "burst": round(self.walker.rate, 2),
                    "error": self.walker.error}
        found = self._counts().get("found", 0)
        kept = self.store.q("SELECT COUNT(*), COALESCE(SUM(LENGTH(blob)), 0), "
                            "COALESCE(SUM(src = ''), 0) FROM notes")[0]
        covered = sum(1 for c in range(ready) if c in per)
        snaps = self.snapshots
        f = snaps.fetch
        snapshot = {"said": snaps.said, "single": snaps.single,
                    "made": snaps.made["manifest"]["height"] if snaps.made else None,
                    "fetching": ({"from": f["from"][:12], "have": len(f["have"]),
                                  "pieces": len(f["manifest"]["pieces"]) if f.get("manifest") else None}
                                 if f else None)}
        # Where along the chain how many arcades agree (2026-10-21: the Ords ring's inner circle "red when there
        # is a single node that has the index ... yellow if there are two nodes with agreeing index, and ... green when
        # there are three"): runs of chunks [first, last + 1, agreeing], agreeing 0 (nobody said it yet) to 3 (3 or more).
        # 2026-10-22 (the operator: "The inner circle is not displaying correctly yet"): counted by the arcades HOLDING each
        # chunk -- every walker whose walk has passed it -- not only those that scanned it and said its word (an arcade
        # stitching another's notes never says one, so stretches two arcades hold showed red or grey). A chunk whose
        # words disagree counts only as many as agree on its most-said word.
        reached = sorted(int(h or 0) for _n, h in self.holders())
        runs: list[list[int]] = []
        for c in range(ready):
            end = chunk_range(c, self.base)[1]
            k = sum(1 for h in reached if h >= end)
            words = per.get(c) or {}
            if len(set(words.values())) > 1:
                k = min(k, agreeing.get(c, 0))
            k = min(k, 3)
            if runs and runs[-1][2] == k and runs[-1][1] == c:
                runs[-1][1] = c + 1
            else:
                runs.append([c, c + 1, k])
        return {"net": self.net, "levels": levels, "covered": covered, "snapshot": snapshot, "agree_runs": runs,
                "disputed": list(getattr(self, "disputed", []))[:20],
                "rebuilt": self.store.meta("rebuilt"),
                "phase": "cover" if covered < ready else "audit",
                "notes": {"chunks": kept[0], "bytes": kept[1], "own": kept[2]},
                "holders": [{"node": n[:12] or "this arcade", "walked_to": h} for n, h in self.holders()], "found": found, "mode": self.mode(), "base": self.base, "source": SOURCES.get(self.net, ""), "chunk": CHUNK, "tip": self.tip, "ready": ready,
                "agreed": agreed, "agreed_births": agreed_births, "one": one, "none": max(0, ready - len(per)), "differ": differ, "differs": differs,
                "lease": ({"chunk": mine["c"], "blocks": list(chunk_range(mine["c"], self.base)), "at": mine["at"],
                           "done": mine["at"] - chunk_range(mine["c"], self.base)[0]} if mine else None),
                "lanes": [{"chunk": lane["c"], "at": lane["at"], "done": lane["at"] - chunk_range(lane["c"], self.base)[0]}
                          for lane in self.chunks.lanes],
                "cores": self._lanes_wanted(),
                "values": self._values_status(),
                "walkers": [{"node": n[:12], "walked_to": h} for n, h in self.walkers()],
                "helped": self.helped, "help_rate": round(self.help_rate, 2), "leases": sorted(leases, key=lambda l: l["chunk"]),
                # With the copy of the whole index each one is fetching, as its "hi" says it (2026-10-22: two arcades
                # sat at one block for hours, and nothing here said whether they were fetching a copy or stuck).
                "peers": {n[:12]: {**p, **_copy_said(self.snapshots.offers.get(n))}
                          for n, p in self.chunks.peers.items()}, "walk": walk, "error": self.error,
                "upgrading": (f"slimming the index (one time): {self.store.upgrading or 'starting'}"
                              if self.store.old_layout else "")}

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
        # Admin -> Ords -> the explorer (2026-10-21): an inscription by its number (or id) with who holds it, ticks by
        # how they start, and a token's rich list a page at a time. An arcade from before answers "no such question".
        if what == "number":
            return {"number": self.inscription(question.get("number"), str(question.get("id") or ""))}
        if what == "ticks":
            return {"ticks": self.ticks(str(question.get("prefix") or ""))}
        if what == "holders":
            return {"holders": self.rich_list(str(question.get("tick") or ""), question.get("page") or 0)}
        if what == "kinds":
            return {"kinds": self.kinds(str(question.get("ctype") or ""), str(question.get("proto") or ""))}
        if what == "typed":
            return {"typed": self.typed(str(question.get("ctype") or ""), question.get("after"))}
        if what == "balances":
            return {"balances": self.balances(str(question.get("owner") or ""))}
        if what == "name":
            return {"name": self.name(str(question.get("name") or ""))}
        if what == "on":
            return {"on": self.on(str(question.get("txid") or ""), int(question.get("vout") or 0))}
        if what == "note":
            return {"note": self.note(str(question.get("id") or ""))}
        if what == "ticklist":
            return {"ticklist": self.ticklist()}
        if what == "mesh":                               # the .mesh web (arcade/mesh_web.py, 2026-10-26)
            return {"mesh": self.mesh(str(question.get("name") or ""))}
        if what == "mesh_directory":                     # "all": every name's site, not only .mesh (the PWA's Browser)
            return {"mesh_directory": self.mesh_directory(mesh_only=not question.get("all"))}
        if what == "moves":                              # a snapshot being checked: this walk's moves, chunk by chunk
            from .ord_snapshot import MOVES_SAMPLE, moves_digest
            chunks = [int(c) for c in (question.get("chunks") or [])[:MOVES_SAMPLE]]
            return {"moves": {str(c): moves_digest(self.store, c, self.base, CHUNK) for c in chunks}}
        raise ValueError("no such question")

    def lookup(self, question: dict) -> dict:
        """The answer, and whose it is ("from", "walked_to"; "node" when a linked arcade's), from the MOST COMPLETE
        index of this chain (2026-10-21: "All arcades should be able to access all other arcades indexs and
        they should always use the most complete index in the ord Explorer"): this arcade's own walk and every linked
        walker on these rules heard in the last five minutes (`walkers`), furthest walked first -- this arcade first
        when it is furthest or tied. Should the one asked fail (gone, too slow, an error, a version that cannot answer
        this), the next furthest is asked, down to this arcade's own walk when it walks (which always answers). A
        walker on other rules is never asked (`walkers`)."""
        own = self.walker.next_height() if self.mode() == "walk" else None
        peers = self.walkers() if self.share is not None and hasattr(self.share, "ask") else []
        order: list[tuple[str | None, int]] = peers + ([(None, own)] if own is not None else [])
        order.sort(key=lambda o: (-o[1], o[0] is not None))
        if not order:
            if self.share is None or not hasattr(self.share, "ask"):
                raise ValueError("no linked arcade to ask: the mesh is off here")
            raise ValueError("no linked arcade walks this chain yet")
        failed: list[str] = []
        older = ""
        for node, height in order:
            if node is None:
                return {**self.answer(question), "from": "this arcade", "walked_to": own}
            try:
                return {"from": f"arcade {node[:12]}", "walked_to": height, **self.share.ask(question, node=node),
                        "node": node}
            except Exception as exc:                     # noqa: BLE001 -- that arcade could not say: the next one
                said = str(exc)
                if "no such question" in said and question.get("what") in NEWER_QUESTIONS:
                    older = older or said.split(":")[0] or f"arcade {node[:12]}"
                    # The walking arcade runs a version from before the explorer (2026-10-21). A rich list's first
                    # page is its token's biggest holders, which it does answer.
                    if question.get("what") == "holders" and not int(question.get("page") or 0):
                        try:
                            got = self.share.ask({"what": "token", "tick": str(question.get("tick") or "")}, node=node)
                        except Exception as again:          # noqa: BLE001
                            failed.append(str(again)[:120])
                            continue
                        return {"from": f"arcade {node[:12]}", "walked_to": height, **got, "node": node,
                                "holders": _first_page_of(got.get("token")),
                                "older": f"{older} runs an older version: only the first page of holders, and no "
                                         f"percentages"}
                failed.append(said[:120])
        if older:
            raise ValueError(f"{older} walks this chain but runs an older version that cannot answer this yet; it can "
                             f"once it updates")
        raise ValueError("no linked arcade that walks this chain could answer: " + "; ".join(failed))

    def walkers(self) -> list[tuple[str, int]]:
        """Linked arcades that walk this chain, furthest first: (node, the block they have walked to)."""
        # Only arcades on these rules (2026-10-21): an index built under other rules numbers otherwise.
        rows = [(node, int(p.get("walk") or 0)) for node, p in self.chunks.peers.items()
                if p.get("mode") == "walk" and p.get("rules") == self.rules
                and time.time() - float(p.get("at") or 0) < 300]
        return sorted(rows, key=lambda r: -r[1])

    def where(self, inscription: str) -> dict | None:
        key = iid_parts(inscription)
        if key is None or self.store.old_layout:
            return None
        row = self.store.q("SELECT i.tx, i.n, i.number, i.height, c.ctype, i.proto, i.size, i.burned, a.tx, a.vout, "
                           "a.off, i.delegate, i.parent, i.reinscribed FROM insc i JOIN ctypes c ON c.ct = i.ct "
                           "LEFT JOIN at a ON a.rid = i.rid WHERE substr(i.tx, 1, 8)=? AND i.tx=? AND i.n=?",
                           (key[0][:8],) + key)
        if not row:
            return None
        tx, n, number, height, ctype, proto, size, burned, at, vout, off, delegate, parent, again = row[0]
        return {"id": iid_of(tx, n), "number": number, "height": height, "content_type": ctype, "protocol": proto,
                "size": size, "burned": burned, "output": f"{bytes(at).hex()}:{vout}" if at else None, "offset": off,
                "delegate": iid_of_blob(delegate), "parent": iid_of_blob(parent), "reinscription": bool(again)}

    def delegate(self, inscription: str) -> dict | None:
        """What /content serves for an inscription that names a delegate (2026-10-21): None when this walk does
        not hold `inscription` or it names none (its own content is served, as before); else {"delegate": id,
        "known": the delegate is an inscription this walk holds, "delegates": that delegate names one of its own}.
        One hop only: ord-pepecoin's and wonky-ord's /content swap in the delegate once (`if let Some(delegate) =
        inscription.delegate_id() { inscription = index.get_inscription_by_id(delegate)?.ok_or_not_found(..)? }`)."""
        key = iid_parts(inscription)
        if key is None or self.store.old_layout:
            return None
        row = self.store.q("SELECT delegate FROM insc WHERE substr(tx, 1, 8)=? AND tx=? AND n=?", (key[0][:8],) + key)
        if not row or not row[0][0]:
            return None
        blob = bytes(row[0][0])
        tx, n = blob[:32], int.from_bytes(blob[32:36], "little")
        of = self.store.q("SELECT delegate FROM insc WHERE substr(tx, 1, 8)=? AND tx=? AND n=?", (tx[:8], tx, n))
        return {"delegate": iid_of_blob(blob), "known": bool(of), "delegates": bool(of and of[0][0])}

    def kinds(self, ctype: str = "", proto: str = "", first: int = 20) -> dict:
        """How many inscriptions of a content type (a prefix: "image/" for every image) or a JSON "p", and the first
        ones by number; with neither, the commonest types and protocols. For indexing a protocol later."""
        if self.store.old_layout:
            return {"count": 0, "first": []} if ctype or proto else {"types": [], "protocols": []}
        if ctype or proto:
            where, val = (("ct IN (SELECT ct FROM ctypes WHERE ctype LIKE ?)", ctype + "%") if ctype
                          else ("proto = ?", proto))
            n = self.store.q(f"SELECT COUNT(*) FROM insc WHERE {where}", (val,))[0][0]
            # Rows are kept in number order (rid = number + 1): no sort, and the scan stops at the first ones.
            ids = self.store.q(f"SELECT number, tx, n FROM insc WHERE {where} ORDER BY rid LIMIT ?", (val, first))
            return {"count": n, "first": [{"number": a, "id": iid_of(t, k)} for a, t, k in ids]}
        return {"types": self.store.q("SELECT c.ctype, COUNT(*) n FROM insc i JOIN ctypes c ON c.ct = i.ct "
                                      "GROUP BY i.ct ORDER BY n DESC LIMIT 25"),
                "protocols": self.store.q("SELECT proto, COUNT(*) c FROM insc WHERE proto IS NOT NULL GROUP BY proto "
                                          "ORDER BY c DESC LIMIT 25")}

    #: The Explore tab's pages of one content type (2026-10-25): how many a page, and how many rows one ask may read
    #: past `after` looking for them (a rare type stops there and says where to go on from).
    TYPED_PAGE = 24
    TYPED_SCAN = 2_000_000
    TYPED_WINDOW = 200_000

    def typed(self, ctype: str, after=None, limit: int | None = None) -> dict:
        """The next inscriptions of a content type (a prefix: "image/" for every image) after number `after`, by
        number: {"rows": [{number, id, content_type, size}], "next": the number to go on after, or None at the end}.
        Unlike `kinds`, nothing is counted, so it stays quick on a chain of a hundred million (the Explore tab)."""
        limit = max(1, min(int(limit or self.TYPED_PAGE), 60))
        try:
            after = int(after) if after is not None and str(after) != "" else -1
        except (TypeError, ValueError):
            after = -1
        ctype = ctype.replace("\\", "").replace("%", "")
        if self.store.old_layout or not ctype:
            return {"rows": [], "next": None}
        wanted = ctype.replace("_", "\\_") + "%"
        cts = {ct: name for ct, name in self.store.q("SELECT ct, ctype FROM ctypes WHERE ctype LIKE ? ESCAPE '\\'",
                                                     (wanted,))}
        if not cts:
            return {"rows": [], "next": None}
        top = self.store.q("SELECT MAX(rid) FROM insc")[0][0] or 0
        marks = ",".join("?" * len(cts))
        rows: list[dict] = []
        lo = max(0, after + 1)                           # rid = number + 1: the rows after `after` start at rid after+2
        while len(rows) < limit and lo < top and lo - (after + 1) < self.TYPED_SCAN:
            hi = lo + self.TYPED_WINDOW
            for number, tx, n, ct, size in self.store.q(
                    f"SELECT number, tx, n, ct, size FROM insc WHERE rid > ? AND rid <= ? AND ct IN ({marks}) "
                    f"ORDER BY rid LIMIT ?", (lo, hi, *cts, limit - len(rows))):
                rows.append({"number": number, "id": iid_of(tx, n), "content_type": cts[ct], "size": size})
            lo = hi
        if len(rows) >= limit:
            nxt = rows[-1]["number"]
        else:
            nxt = (lo - 1) if lo < top else None
        return {"rows": rows, "next": nxt}

    def token(self, tick: str, top: int = 20) -> dict | None:
        """A DRC-20 / PRC-20 token: its deploy, how much is minted, and its biggest holders (units shown with its
        decimals). The holders come sorted from bal_rich (2026-10-21; before, every holder was read and sorted here)."""
        row = self.store.q("SELECT shown, id, number, supply, lim, dec, minted, deployer, height FROM tok WHERE tick=?",
                           (tick.lower(),))
        if not row:
            return None
        shown, iid, number, supply, lim, dec, minted, deployer, height = row[0]
        holders = self.store.q("SELECT COUNT(*) FROM bal WHERE tick=? AND overall != '0'", (tick.lower(),))[0][0]
        top_rows = self.store.q(f"SELECT owner, overall, transferable FROM bal WHERE tick=? AND overall != '0' "
                                f"{RICH_ORDER} LIMIT ?", (tick.lower(), top)) if top else []
        return {"tick": shown, "deploy": iid, "number": number, "height": height, "deployer": deployer,
                "supply": _shown(supply, dec), "limit": _shown(lim, dec), "decimals": dec,
                "minted": _shown(minted, dec), "holders": holders,
                "top": [{"owner": o, "balance": _shown(b, dec), "transferable": _shown(t, dec)} for o, b, t in top_rows]}

    def balances(self, owner: str) -> list[dict]:
        out = []
        for tick, overall, transferable, shown, dec in self.store.q(
                "SELECT b.tick, b.overall, b.transferable, t.shown, t.dec FROM bal b LEFT JOIN tok t ON t.tick = b.tick "
                "WHERE b.owner=? AND b.overall != '0'", (owner,)):
            out.append({"tick": shown or tick, "units": overall, "transferable_units": transferable,
                        "decimals": int(dec or 0)})
        return out

    # --- the explorer (Admin -> Ords, 2026-10-21) -----------------------------------------------------------

    def last_number(self) -> int:
        """The highest inscription number this walk knows (-1: none yet). Rows are numbered rid = number + 1."""
        got = self.store.q("SELECT MAX(rid) FROM insc")[0][0]
        return int(got) - 1 if got else -1

    def inscription(self, number: Any = None, iid: str = "") -> dict:
        """2026-10-21: "display ord 0 of the chain that is selected with arrows to navigate by ord number". One
        inscription by its number (or its id), with where it sits now and who holds that coin, and the last number
        known, so the viewer knows where Next stops. {"inscription": None} when there is no such number (yet)."""
        out: dict = {"inscription": None, "last": -1}
        if self.store.old_layout:
            return out
        out["last"] = self.last_number()
        if iid:
            key = iid_parts(iid)
            rows = self.store.q("SELECT rid FROM insc WHERE substr(tx, 1, 8)=? AND tx=? AND n=?",
                                (key[0][:8],) + key) if key else []
            if not rows:
                return out
            rid = rows[0][0]
        else:
            try:
                rid = int(number) + 1
            except (TypeError, ValueError):
                raise ValueError("an inscription number") from None
            if rid < 1:
                return out
        row = self.store.q("SELECT i.tx, i.n, i.number, i.height, c.ctype, i.proto, i.size, i.burned, i.pieces, a.tx, "
                           "a.vout, a.off, a.height, i.delegate, i.parent, i.reinscribed FROM insc i "
                           "JOIN ctypes c ON c.ct = i.ct LEFT JOIN at a ON a.rid = i.rid WHERE i.rid=?", (rid,))
        if not row:
            return out
        tx, n, num, height, ctype, proto, size, burned, pieces, at, vout, off, moved, delegate, parent, again = row[0]
        txid = bytes(at).hex() if at else None
        out["inscription"] = {"id": iid_of(tx, n), "number": num, "height": height, "content_type": ctype,
                              "protocol": proto, "size": size, "pieces": pieces, "burned": bool(burned),
                              "output": f"{txid}:{vout}" if txid else None, "offset": off, "moved_at": moved,
                              "owner": self._owner_at(txid, vout) if txid and not burned else None,
                              # rules 4: the inscription its tags name (Admin -> Ords shows and opens them), and the
                              # delegate's content type -- what /content serves for it
                              "delegate": iid_of_blob(delegate), "parent": iid_of_blob(parent),
                              "delegate_content_type": self._ctype_of(bytes(delegate)) if delegate else None,
                              # made on a koinu that already carried an inscription (None: filed before the mark)
                              "reinscription": bool(again),
                              "note": SEEN_ELSEWHERE.get(self.net, {}).get(iid_of(tx, n))}
        return out

    def _ctype_of(self, blob: bytes) -> str | None:
        """The content type of the inscription an id_blob names, when this walk holds it."""
        tx, n = blob[:32], int.from_bytes(blob[32:36], "little")
        row = self.store.q("SELECT c.ctype FROM insc i JOIN ctypes c ON c.ct = i.ct WHERE substr(i.tx, 1, 8)=? AND "
                           "i.tx=? AND i.n=?", (tx[:8], tx, n))
        return row[0][0] if row else None

    def _owner_at(self, txid: str, vout: int) -> str | None:
        """Who holds a coin: its address (Walker.owner_of), read from this arcade's Core; None without one (a walk
        stitched from other arcades' notes has none to ask) or when it cannot say."""
        if self.rpc_factory is None or not self.own_core():
            return None
        cache = self.__dict__.setdefault("_owners", {})
        if (txid, vout) in cache:
            return cache[(txid, vout)]
        try:
            with self.rpc_factory() as rpc:
                tx = rawchain.read_tx(bytes.fromhex(rpc.call("getrawtransaction", txid, 0)), txid)
            owner = self.walker.owner_of(tx.vout[vout][1])
        except Exception:                                # noqa: BLE001 -- said as unknown
            return None
        if len(cache) > 2000:
            cache.clear()
        cache[(txid, vout)] = owner
        return owner

    def ticks(self, prefix: str = "", first: int = TICKS_AT_ONCE) -> dict:
        """2026-10-21: "the ability to search by token ticker": the tokens whose tick starts so, in tick order,
        a few at a time (the primary key on tick, read as a range), and how many in all."""
        p = prefix.strip().lower()[:8]
        if self.store.old_layout:
            return {"count": 0, "first": []}
        where, args = ("tick >= ? AND tick < ?", (p, p + "\U0010ffff")) if p else ("1", ())
        count = self.store.q(f"SELECT COUNT(*) FROM tok WHERE {where}", args)[0][0]
        rows = self.store.q(f"SELECT tick, shown, number, supply, minted, dec FROM tok WHERE {where} ORDER BY tick "
                            f"LIMIT ?", args + (max(1, min(int(first), 50)),))
        return {"count": count, "first": [
            {"tick": shown, "number": number, "supply": _shown(supply, dec), "minted": _shown(minted, dec),
             "minted_pct": _pct(minted, supply)} for _t, shown, number, supply, minted, dec in rows]}

    def ticklist(self, most: int = 5000) -> list[str]:
        """Every DRC-20 / PRC-20 tick as deployed (its `shown` spelling), in tick order, for the exchange's search
        (2026-10-22: "The tokens should be added to the search of tokens with their ticker, followed by the
        protocol"): one compact list, the primary key read in order."""
        if self.store.old_layout:
            return []
        return [r[0] for r in self.store.q("SELECT shown FROM tok ORDER BY tick LIMIT ?", (max(1, int(most)),))]

    def rich_list(self, tick: str, page: Any = 0) -> dict | None:
        """2026-10-21: "When a ticker is selected it should show information about token and a list rich list
        of the token holders paginated to 20 holders." The token, and one page of its holders biggest first, each with
        their share of the supply; sorted by SQL on bal_rich, never by reading every holder."""
        try:
            page = max(0, int(page))
        except (TypeError, ValueError):
            raise ValueError("a page number") from None
        if self.store.old_layout:
            return None
        row = self.store.q("SELECT supply, minted, dec FROM tok WHERE tick=?", (tick.lower(),))
        info = self.token(tick, top=0)
        if not row or info is None:
            return None
        supply, minted, dec = row[0]
        rows = self.store.q(f"SELECT owner, overall, transferable FROM bal WHERE tick=? AND overall != '0' "
                            f"{RICH_ORDER} LIMIT ? OFFSET ?", (tick.lower(), RICH_PAGE, page * RICH_PAGE))
        return {**{k: v for k, v in info.items() if k != "top"}, "minted_pct": _pct(minted, supply), "page": page, "per": RICH_PAGE,
                "pages": max(1, -(-info["holders"] // RICH_PAGE)),
                "rows": [{"rank": page * RICH_PAGE + k + 1, "owner": o, "balance": _shown(b, dec),
                          "transferable": _shown(t, dec), "pct": _pct(b, supply)}
                         for k, (o, b, t) in enumerate(rows)]}

    def name(self, name: str) -> dict:
        """A name under each reading (ord_rules.NAME_RULES): who claimed it first there, and every later claim."""
        want = name.strip().lower()
        out: dict = {"name": want, "readings": {}}
        if self.store.old_layout:
            return out
        for rule in ord_rules.NAME_RULES:
            claims = self.store.q(f"SELECT n.number, n.kind, n.raw, i.tx, i.n, a.tx, a.vout FROM names n "
                                  f"JOIN insc i ON i.rid = n.rid LEFT JOIN at a ON a.rid = n.rid "
                                  f"WHERE n.{rule}=? ORDER BY n.number", (want,))
            out["readings"][rule] = {
                "winner": ({"inscription": iid_of(claims[0][3], claims[0][4]), "number": claims[0][0],
                            "kind": claims[0][1],
                            "output": f"{bytes(claims[0][5]).hex()}:{claims[0][6]}" if claims[0][5] else None}
                           if claims else None),
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

    # --- the .mesh web (arcade/mesh_web.py) --------------------------------------------------------------------

    #: Reads an inscription's body: (content type, bytes). None: this chain's guard (funding.GUARD_BY_NET), the same
    #: reader /content uses, which keeps every body on disk for good.
    mesh_reader = None

    def _mesh_body(self, iid: str) -> bytes:
        cache = self.__dict__.setdefault("_mesh_bodies", {})
        if iid in cache:
            return cache[iid]
        reader = self.mesh_reader
        if reader is None:
            from . import funding
            guard = (getattr(funding, "GUARD_BY_NET", None) or {}).get(self.net)
            reader = guard.content if guard is not None else None
        if reader is None:
            raise ValueError("this arcade reads no inscriptions of this chain")
        body = bytes(reader(iid)[1])
        if len(cache) > 1000:
            cache.clear()
        cache[iid] = body
        return body

    def _mesh_pages(self) -> list:
        """The "p":"mesh" inscriptions, newest first (insc_mesh)."""
        return self.store.q("SELECT tx, n, number, height, rid FROM insc WHERE proto = 'mesh' ORDER BY rid DESC LIMIT ?",
                            (mesh_web.SCAN,))

    def _mesh_holder(self, name: str) -> tuple[str | None, str | None]:
        """(the name's first claim, the address holding that claim now) under the spec reading; a name that is not
        .mesh, the first reading that has a claim of it (the JSON reading's names the spec leaves out)."""
        readings = self.name(name)["readings"]
        rules = ["spec"] if mesh_web.is_mesh(name) else list(ord_rules.NAME_RULES)
        win = next(((readings.get(r) or {}).get("winner") for r in rules if (readings.get(r) or {}).get("winner")), None)
        if not win or not win.get("output"):
            return (win or {}).get("inscription"), None
        txid, vout = win["output"].split(":")
        return win["inscription"], self._owner_at(txid, int(vout))

    def _mesh_page(self, row: tuple, name: str | None, holder: str | None) -> dict | None:
        """One page row, when it is a page of `name` (any name: None) held by `holder` where it sits now. Where it
        sits, not its reveal's first output (2026-10-26: a record of three transactions -- a chunked inscription --
        lands on its LAST reveal; output 0 of the first only passes it on to the next piece, so the newest
        me-seeks.mesh site never counted). For a page that never moved the two are the same coin."""
        tx, n, number, height, rid = row
        iid = iid_of(tx, n)
        try:
            body = self._mesh_body(iid)
        except Exception:                                # noqa: BLE001 -- not readable now: the next one
            return None
        page = mesh_web.parse(body)
        if page is None or (name is not None and page["name"] != name):
            return None
        at = self.store.q("SELECT tx, vout FROM at WHERE rid = ?", (rid,))
        where = (bytes(at[0][0]).hex(), int(at[0][1])) if at else (bytes(tx).hex(), 0)
        if holder is None or self._owner_at(*where) != holder:
            return None
        return {"id": iid, "number": number, "height": height, "name": page["name"], "pages": page["pages"],
                "size": len(body), "sha256": mesh_web.digest(body)}

    def mesh(self, name: str) -> dict:
        """A name's site (.mesh, or any p:dns name since 2026-10-26): who holds the name, and the newest page inscribed
        to that holder (the operator chose "Latest by the holder"). {"page": None} when the holder has inscribed none,
        {"holder": None} when nobody holds it."""
        made = mesh_web.site_name(name)
        out: dict = {"name": made, "claim": None, "holder": None, "page": None}
        if not made or self.store.old_layout:
            return out
        out["claim"], out["holder"] = self._mesh_holder(made)
        if out["holder"]:
            for row in self._mesh_pages():
                page = self._mesh_page(row, made, out["holder"])
                if page:
                    out["page"] = page
                    break
        return out

    def mesh_directory(self, mesh_only: bool = True) -> list[dict]:
        """Every .mesh name with a page that counts, newest page first: the web's front door, as in 1994. `mesh_only`
        False: every name's site."""
        if self.store.old_layout:
            return []
        holders: dict[str, str | None] = {}
        out, done = [], set()
        for row in self._mesh_pages():
            try:
                page = mesh_web.parse(self._mesh_body(iid_of(row[0], row[1])))
            except Exception:                            # noqa: BLE001
                continue
            if page is None or page["name"] in done or (mesh_only and not mesh_web.is_mesh(page["name"])):
                continue
            if page["name"] not in holders:
                holders[page["name"]] = self._mesh_holder(page["name"])[1]
            found = self._mesh_page(row, page["name"], holders[page["name"]])
            if found:
                done.add(page["name"])
                out.append({"name": found["name"], "page": found["id"], "number": found["number"],
                            "height": found["height"], "title": mesh_web.title_of(found["pages"].get("/", ""))})
        return out

    def note(self, iid: str) -> dict | None:
        """A DRC-20 / PRC-20 transfer note not moved yet (2026-10-22, a swap's lock holds one: token20_swap): its tick,
        amount in units and the owner it was inscribed to; None when the inscription is no such note -- never one,
        refused by the rules (more than its owner had), or already moved."""
        key = iid_parts(iid)
        if not key or self.store.old_layout:
            return None
        rows = self.store.q("SELECT x.tick, x.amt, x.owner FROM xfer x JOIN insc i ON i.rid = x.rid "
                            "WHERE substr(i.tx, 1, 8)=? AND i.tx=? AND i.n=?", (key[0][:8],) + key)
        return {"tick": rows[0][0], "amt": rows[0][1], "owner": rows[0][2]} if rows else None

    def on(self, txid: str, vout: int) -> list[dict]:
        if self.store.old_layout:
            return []
        # With its content type (2026-10-22: the Wallet's NFTs leave text, JSON and scripts out).
        return [{"id": iid_of(r[0], r[1]), "offset": r[2], "ctype": r[3]} for r in self.store.q(
            "SELECT i.tx, i.n, a.off, c.ctype FROM at a JOIN insc i ON i.rid = a.rid "
            "LEFT JOIN ctypes c ON c.ct = i.ct "
            "WHERE substr(a.tx, 1, 8)=? AND a.tx=? AND a.vout=? ORDER BY a.off",
            (bytes.fromhex(txid)[:8], bytes.fromhex(txid), vout))]


def _copy_said(offer) -> dict:
    """{"copy": {"h", "have", "of"}} for a peer's snapshot word: "have" is "all" for a copy it made, else how many of
    its "of" pieces it has fetched so far; {} when it says none."""
    if not isinstance(offer, dict):
        return {}
    have = offer.get("have")
    return {"copy": {"h": int(offer.get("h") or 0), "of": int(offer.get("n") or 0),
                     "have": "all" if have == "all" else len(have) if isinstance(have, list) else 0}}


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
                    if index.mode() in ("walk", "help") or index.store.old_layout:
                        budget = index.pass_budget() if hasattr(index, "pass_budget") else 5.0
                        rests.append(index.step(budget))
            except Exception:                            # noqa: BLE001 -- never dies
                log.debug("ord index step", exc_info=True)
                rests.append(30.0)
            self._stop.wait(min(rests) if rests else 10.0)
